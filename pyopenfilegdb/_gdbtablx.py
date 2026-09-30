"""``.gdbtablx`` 行索引文件的读写。

作用
----
``.gdbtable`` 本身是扁平的记录流,要随机访问第 N 条要素就必须知道它在
``.gdbtable`` 里的 **绝对文件偏移**。``.gdbtablx`` 就是这张"行号 -> 偏移"
表。它同时也是"记录是否存在"的唯一依据:

* 偏移 == 0  → 该槽位为空(从未写过,或已被删除)
* 偏移 != 0  → 记录体从该偏移开始

⚠️ 注意:.gdbtable 中并没有"块链"。这里出现的 "block/page" 指的是
**每 1024 条记录一页** 的索引分页单位,与压缩无关。

磁盘布局 (version 3)
---------------------
::

    +0   uint32  version                 必须等于 .gdbtable 的版本(3)
    +4   uint32  n1024BlocksPresent      已分配的索引页数
    +8   int32   nTotalRecordCount       记录槽位总数(含空洞)
    +12  uint32  nTablxOffsetSize        每个偏移占用的字节数,取值 4/5/6
    +16          偏移表, n1024BlocksPresent*1024 项, 每项 nTablxOffsetSize 字节
    +16+...      16 字节 trailer:
                     uint32 nBitmapInt32Words  位图占多少个 32bit 字(0 = 无位图)
                     uint32 nBitsForBlockMap   位图覆盖多少页
                     uint32 n1024BlocksBis     冗余的页数(必须等于 +4 的值)
                     uint32 nLeadingNonZero32BitWords
                 若 nBitmapInt32Words != 0, 后面跟位图本体

位图(base block map)
--------------------
当要素被删除后,GDAL 可以选择把偏移表"压实",并用位图记录哪些页仍然非空,
使得剩下的偏移是连续存放的。读取时:

    iBlock      = row // 1024
    若位图[iBlock] == 0 -> 该页整体为空, 偏移为 0
    iCorrected  = 已置位页数(在 iBlock 之前) * 1024 + row % 1024
    偏移位置     = 16 + nTablxOffsetSize * iCorrected

参考 GDAL:
- ``filegdbtable.cpp::FileGDBTable::ReadTableXHeaderV3``  读头 + trailer
- ``filegdbtable.cpp::FileGDBTable::GetOffsetInTableForRow``  偏移查找
- ``filegdbtable_write.cpp::FileGDBTable::WriteHeaderX``   写头
- ``filegdbtable_write.cpp::FileGDBTable::Sync``          写 trailer
"""
from __future__ import annotations

import os
from typing import List, Optional

from . import _constants as C
from ._datatypes import GdbFormatError, GdbWriteError
from ._util import get_int32, get_uint32

# 与 GDAL filegdbtable_write.cpp 中的 TABLX_HEADER_SIZE /
# TABLX_FEATURES_PER_PAGE 对应
TABLX_HEADER_SIZE = C.TABLX_HEADER_SIZE
FEATURES_PER_PAGE = C.TABLX_RECORDS_PER_BLOCK


class GdbTablx:
    """一个要素类的 ``.gdbtablx``。

    读路径由 :meth:`open` 构造;写路径由 :meth:`create` 构造,或用
    :meth:`set_offset` 维护后 :meth:`flush` 落盘。
    """

    def __init__(self, path: str) -> None:
        self.path = path
        # -- 头部字段 -------------------------------------------------------
        self.version: int = C.FGDB_VERSION_10
        self.n_blocks_present: int = 0     # n1024BlocksPresent
        self.total_record_count: int = 0   # nTotalRecordCount
        self.offset_size: int = 5          # 4 / 5 / 6
        self.trailer_offset: int = 0

        # -- 内容 -----------------------------------------------------------
        # 偏移表: 行号 -> .gdbtable 内偏移(0 表示空槽)
        self._offsets: List[int] = []
        # base block map;为空表示"无位图"的简单布局
        self._block_map: Optional[bytearray] = None
        self._block_map_bits: int = 0

        self._dirty = False

    # ======================================================================
    # 读
    # ======================================================================
    @classmethod
    def open(cls, path: str) -> 'GdbTablx':
        """解析一个已存在的 ``.gdbtablx``。"""
        with open(path, 'rb') as f:
            data = f.read()

        self = cls(path)
        if len(data) < TABLX_HEADER_SIZE:
            raise GdbFormatError(f'{os.path.basename(path)}: 文件过短,不是 .gdbtablx')

        self.version = get_uint32(data, 0)
        if self.version not in C.SUPPORTED_VERSIONS:
            from ._datatypes import GdbVersionError
            raise GdbVersionError(
                f'{os.path.basename(path)}: 不支持的 .gdbtablx 版本 {self.version}'
            )

        self.n_blocks_present = get_uint32(data, 4)
        self.total_record_count = get_int32(data, 8)
        self.offset_size = get_uint32(data, 12)

        if not (4 <= self.offset_size <= 6):
            raise GdbFormatError(
                f'{os.path.basename(path)}: 非法的 offset size {self.offset_size}'
            )
        if self.total_record_count < 0:
            raise GdbFormatError(
                f'{os.path.basename(path)}: 负的记录数 {self.total_record_count}'
            )

        self.trailer_offset = (
            TABLX_HEADER_SIZE
            + self.offset_size * FEATURES_PER_PAGE * self.n_blocks_present
        )

        # ---- trailer(存在时才读)------------------------------------------
        if self.n_blocks_present != 0:
            self._read_trailer(data)
        else:
            if self.total_record_count != 0:
                raise GdbFormatError(
                    f'{os.path.basename(path)}: 页数为 0 但记录数不为 0'
                )

        self._read_offset_table(data)
        return self

    def _read_trailer(self, data: bytes) -> None:
        """解析尾部 16 字节 trailer,并按需加载 base block map。"""
        off = self.trailer_offset
        if off + 16 > len(data):
            raise GdbFormatError(
                f'{os.path.basename(self.path)}: trailer 越界 (off={off}, size={len(data)})'
            )

        n_bitmap_words = get_uint32(data, off)          # nBitmapInt32Words
        n_bits_for_map = get_uint32(data, off + 4)      # nBitsForBlockMap
        n_blocks_bis = get_uint32(data, off + 8)        # n1024BlocksBis
        # off+12: nLeadingNonZero32BitWords,读的时候用不到

        if n_blocks_bis != self.n_blocks_present:
            raise GdbFormatError(
                f'{os.path.basename(self.path)}: trailer 页数 {n_blocks_bis} '
                f'与头部 {self.n_blocks_present} 不一致'
            )

        if n_bitmap_words == 0:
            # 没有位图:偏移表按行号直接索引(ArcGIS 最常见的情形)
            if n_bits_for_map != self.n_blocks_present:
                raise GdbFormatError(
                    f'{os.path.basename(self.path)}: 无位图时 nBitsForBlockMap '
                    f'({n_bits_for_map}) 应等于页数 ({self.n_blocks_present})'
                )
            self._block_map = None
            return

        # 有位图:按 GDAL 的语义加载
        n_bytes = (n_bitmap_words * 4 + 7) // 8
        body = off + 16
        if body + n_bytes > len(data):
            raise GdbFormatError(
                f'{os.path.basename(self.path)}: block map 越界'
            )
        self._block_map = bytearray(data[body:body + n_bytes])
        self._block_map_bits = n_bits_for_map

    def _read_offset_table(self, data: bytes) -> None:
        """把偏移表读进内存(每条记录一个整数)。"""
        n = self.total_record_count
        table_start = TABLX_HEADER_SIZE
        need = table_start + self.offset_size * n
        if n and need > len(data):
            # 文件被截断,只读到能读的部分,其余算空槽(容错)
            n = max(0, (len(data) - table_start) // self.offset_size)

        self._offsets = [0] * self.total_record_count
        if self._block_map is None:
            # 简单布局:第 row 项的偏移在 16 + offset_size*row
            for row in range(n):
                self._offsets[row] = self._read_offset_at(data, table_start +
                                                         self.offset_size * row)
        else:
            # 位图布局:只有置位的页才有条目
            phys = 0
            for i_block in range((self.total_record_count + FEATURES_PER_PAGE - 1)
                                 // FEATURES_PER_PAGE):
                if not self._test_block_bit(i_block):
                    continue
                base = i_block * FEATURES_PER_PAGE
                for k in range(FEATURES_PER_PAGE):
                    row = base + k
                    if row >= self.total_record_count:
                        break
                    off = table_start + self.offset_size * phys
                    if off + self.offset_size > len(data):
                        break
                    self._offsets[row] = self._read_offset_at(data, off)
                    phys += 1

    def _read_offset_at(self, data: bytes, pos: int) -> int:
        """读一个 offset_size 字节的小端整数(GDAL 是零扩展)。"""
        return int.from_bytes(data[pos:pos + self.offset_size], 'little')

    def _test_block_bit(self, i_block: int) -> bool:
        assert self._block_map is not None
        if i_block >= self._block_map_bits:
            return False
        return (self._block_map[i_block >> 3] & (1 << (i_block & 7))) != 0

    # ======================================================================
    # 读接口
    # ======================================================================
    @property
    def record_count(self) -> int:
        """槽位总数(含已删除空洞)。"""
        return self.total_record_count

    def offset_for_row(self, row: int) -> int:
        """第 ``row`` 条(0 基)在 ``.gdbtable`` 中的偏移;0 表示空槽。

        等价于 GDAL ``FileGDBTable::GetOffsetInTableForRow``。
        """
        if row < 0 or row >= self.total_record_count:
            return 0
        return self._offsets[row]

    # ======================================================================
    # 写接口
    # ======================================================================
    @classmethod
    def create(cls, path: str, offset_size: int = 5) -> 'GdbTablx':
        """新建一个空的 ``.gdbtablx``。

        :param offset_size: 每个偏移的字节数。5 字节可表示 1TB,足够用;
            GDAL 在创建时也是按需选择 4/5/6。
        """
        if not (4 <= offset_size <= 6):
            raise GdbWriteError(f'非法的 offset_size: {offset_size}')
        self = cls(path)
        self.offset_size = offset_size
        self.n_blocks_present = 0
        self.total_record_count = 0
        self._offsets = []
        self._block_map = None
        self._dirty = True
        return self

    def set_offset(self, row: int, offset: int) -> None:
        """把第 ``row`` 条的偏移设为 ``offset``(0 表示删除该槽位)。

        行号必须是"下一条待写"或其之前已存在的槽位,不允许跳跃式留洞 ——
        与 GDAL 的 append-only 语义一致。
        """
        if row < 0:
            raise GdbWriteError(f'行号不能为负: {row}')
        if row > self.total_record_count:
            raise GdbWriteError(
                f'不能跳过行号: 当前有 {self.total_record_count} 条, 却要写第 {row} 条'
            )
        if offset >= (1 << (8 * self.offset_size)):
            raise GdbWriteError(
                f'偏移 {offset} 超出 {self.offset_size} 字节表示范围'
            )

        if row == self.total_record_count:
            self._offsets.append(offset)
            self.total_record_count += 1
        else:
            self._offsets[row] = offset

        # 每写满一页就补一页索引空间
        pages_needed = (self.total_record_count + FEATURES_PER_PAGE - 1) // FEATURES_PER_PAGE
        if pages_needed > self.n_blocks_present:
            self.n_blocks_present = pages_needed
        self._dirty = True

    def flush(self) -> None:
        """把当前状态写回磁盘。

        采用 ArcGIS 最常见的"无位图"简单布局:偏移表按行号直接索引。
        好处是不需要维护 block map 的压实逻辑,代价是删除记录后不会回收
        索引空间 —— 与本项目 Tier2 的目标一致。
        """
        if not self._dirty and os.path.exists(self.path):
            return

        n = self.total_record_count
        pages = (n + FEATURES_PER_PAGE - 1) // FEATURES_PER_PAGE if n else 0
        # 至少保留 1 页,与 ArcGIS 生成的空表一致
        self.n_blocks_present = max(pages, 1) if n or pages else 0

        table_bytes = self.offset_size * FEATURES_PER_PAGE * self.n_blocks_present
        buf = bytearray()
        # --- 头部 ---
        buf += self.version.to_bytes(4, 'little')
        buf += self.n_blocks_present.to_bytes(4, 'little')
        buf += (n & 0xFFFFFFFF).to_bytes(4, 'little')
        buf += self.offset_size.to_bytes(4, 'little')
        # --- 偏移表 ---
        table = bytearray(table_bytes)
        for row in range(n):
            table[self.offset_size * row:
                  self.offset_size * (row + 1)] = \
                self._offsets[row].to_bytes(self.offset_size, 'little')
        buf += table
        # --- trailer:无位图 ---
        buf += (0).to_bytes(4, 'little')                    # nBitmapInt32Words = 0
        buf += self.n_blocks_present.to_bytes(4, 'little')   # nBitsForBlockMap
        buf += self.n_blocks_present.to_bytes(4, 'little')   # n1024BlocksBis
        buf += (0).to_bytes(4, 'little')                    # nLeadingNonZero32BitWords

        tmp = self.path + '.tmp'
        with open(tmp, 'wb') as f:
            f.write(bytes(buf))
        os.replace(tmp, self.path)
        self._dirty = False

    # ======================================================================
    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (f'<GdbTablx {os.path.basename(self.path)} '
                f'records={self.total_record_count} '
                f'pages={self.n_blocks_present} offset_size={self.offset_size}>')
