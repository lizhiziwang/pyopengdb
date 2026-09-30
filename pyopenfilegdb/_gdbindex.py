"""``.gdbindexes`` / ``.atx`` / ``.spx`` / ``.freelist`` —— 索引侧文件。

======  ====================================================================
文件     作用
======  ====================================================================
``.gdbindexes``  一张表的 **索引描述符列表**(索引名 + 表达式 + 若干魔数)。
                 本身不存数据,只说明"有哪些索引、建在哪个字段上"。
``.atx``         属性索引的实际数据:B 树,文件名 ``<表>.<索引名>.atx``。
``.spx``         空间索引的实际数据,文件名 ``<表>.spx``。
``.freelist``    已释放记录的 **空洞列表**,让写入方知道哪些槽位可以复用。
======  ====================================================================

参考 GDAL 源码位置
------------------
* ``ogr/ogrsf_frmts/openfilegdb/filegdbtable.cpp``
  - ``FileGDBTable::GetIndexCount()``  —— 读 ``.gdbindexes``(约 2497-2683 行)
  - ``FileGDBTable::HasSpatialIndex()`` —— 探测 ``.spx``(约 2685-2702 行)
* ``ogr/ogrsf_frmts/openfilegdb/filegdbindex.cpp``
  —— ``FileGDBIndexIteratorBase`` 等:``.atx`` B 树的游标实现
* ``ogr/ogrsf_frmts/openfilegdb/filegdbindex_write.cpp``
  —— 索引的 **写** 实现(建索引 / 更新索引)

.. note::
   **本模块的 Tier 定位**

   * ``.gdbindexes`` 的 **解析**:完整实现(Tier1)。它是纯描述性文件,
     结构简单,读完就能回答"这张表有哪些索引"。
   * ``.atx`` / ``.spx`` 的 **查询**:Tier3 桩。见 :class:`GdbBTreeIndex`
     的说明 —— 本库不借助索引做查询,扫描记录即可得到同样的结果。
   * ``.freelist``:Tier3 桩。见 :func:`read_freelist` 与
     :class:`GdbFreelist`。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from ._datatypes import GdbFormatError
from ._util import get_uint16, get_uint32, read_utf16_string

__all__ = [
    'GdbIndex',
    'read_gdbindexes',
    'has_spatial_index',
    'atx_filename',
    'spx_filename',
    'read_freelist',
    'GdbFreelist',
    'GdbBTreeIndex',
    'GdbSpatialIndex',
]

#: ``.gdbindexes`` 首字段的"这不是 v10 描述符"魔数。
#:
#: GDAL 在 GetIndexCount 里显式判它:FileGDB **v9 的索引结构完全不同**,
#: 且 GDAL 自己也没实现(只对栅格的 ``blk_key_index`` 做了硬编码特判)。
#: 我们同样不实现,只把文件识别出来。
GDBINDEXES_V9_MAGIC = 0x03859813

#: 一个索引描述符里那 4 个"魔数"字段的合法组合(GDAL 约 2590 行的判断)。
#:
#: ``(magic2, magic3)`` 三元组:``(2, 0)`` 普通属性索引、``(4, 0)`` 另一种
#: 普通索引、``(16, 65535)`` OBJECTID 索引(不落地成 ``.atx``)。
_MAGIC_OK = frozenset({(2, 0), (4, 0), (16, 65535)})

#: ``nIdxNameCharCount`` / ``nColNameCharCount`` 的合理性上限
#: (GDAL 硬编码 1024,超过即认为文件损坏)。
_MAX_NAME_CHARS = 1024


# ============================================================================
# 单个索引的描述符
# ============================================================================
@dataclass
class GdbIndex:
    """``.gdbindexes`` 里的一条索引描述符。

    :param name: 索引名,例如 ``'CatItemsByType'``。``.atx`` 文件名就是
        ``<表名>.<name>.atx``。
    :param expression: 索引表达式。通常是字段名(``'Type'``);
        也可能是 ``LOWER(Name)`` 这种函数包裹的形式 —— ArcGIS 建的
        大小写无关索引就是这么表示的。
    :param magic2: 描述符里第 2 个魔数(见 :data:`_MAGIC_OK`)。
    :param magic3: 描述符里第 3 个魔数。
    :param has_atx: 同名 ``.atx`` 文件是否真的存在于磁盘上。
    :param is_oid_index: 该索引是否建在 OBJECTID 上。
        这类索引 ArcGIS **不** 落成 ``.atx``(行号即 OID,不需要 B 树)。
    """

    name: str = ''
    expression: str = ''
    magic2: int = 0
    magic3: int = 0
    has_atx: bool = False
    is_oid_index: bool = False

    @property
    def field_name(self) -> str:
        """剥掉 ``LOWER(...)`` 包裹后的字段名。

        对应 GDAL ``FileGDBIndex::GetFieldNameFromExpression``
        (filegdbindex.cpp:47)。
        """
        expr = self.expression
        if len(expr) > 7 and expr[:6].upper() == 'LOWER(' and expr.endswith(')'):
            return expr[6:-1]
        return expr

    @property
    def is_case_insensitive(self) -> bool:
        """索引表达式是否带 ``LOWER(...)``。"""
        return len(self.expression) > 7 and \
            self.expression[:6].upper() == 'LOWER(' and \
            self.expression.endswith(')')

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (f'<GdbIndex {self.name!r} on {self.expression!r}'
                f'{" [atx]" if self.has_atx else ""}>')


# ============================================================================
# .gdbindexes 解析
# ============================================================================
def read_gdbindexes(gdbtable_path: str,
                    oid_field_name: str = '') -> List[GdbIndex]:
    """解析与 ``gdbtable_path`` 同名的 ``.gdbindexes``。

    对应 GDAL ``FileGDBTable::GetIndexCount()``
    (filegdbtable.cpp:2497-2683)。文件布局(GDAL 逆向所得,无公开文档)::

        u32   nIndexCount
        重复 nIndexCount 次:
            u32    索引名的 UTF-16 码元个数
            u16[]  索引名(Little-Endian UTF-16)
            u16    magic1(恒为 0)
            u32    magic2     ┐ 这三个共同决定描述符类型,
            u16    magic3     ┘ 见 _MAGIC_OK
            u32    magic4(恒为 1)
            u32    表达式(列名)的 UTF-16 码元个数
            u16[]  表达式
            u16    magic5(恒为 0)

    .. warning::
        真实文件里会出现 **被删除但没清干净的描述符** —— 此时
        ``magic2`` 被当成"名字长度"复用,正文是垃圾。GDAL 专门为此写了容错
        分支(约 2596-2625 行,参照 gdal issue #11295 里的一批
        ``a00000029.gdbindexes`` 之类文件)。这里的处理与 GDAL 一致:识别出
        这种描述符后跳到它声明的长度之后,继续读下一条。

    :param gdbtable_path: ``.gdbtable`` 路径(扩展名任意,会换成
        ``.gdbindexes``)。
    :param oid_field_name: OID 字段的逻辑名。用于标记
        :attr:`GdbIndex.is_oid_index`;留空则按 ``'FDO_'`` 前缀 /
        ``'OBJECTID'`` 猜测。
    :returns: :class:`GdbIndex` 列表;文件不存在时返回空列表
        (与 GDAL 的容忍行为一致 —— 没有索引不是错误)。
    """
    path = _sibling(gdbtable_path, 'gdbindexes')
    if not os.path.isfile(path):
        return []

    with open(path, 'rb') as f:
        data = f.read()

    # GDAL 对超过 1MB 的 .gdbindexes 直接判脏(约 2521 行)。
    if len(data) > 1024 * 1024:
        raise GdbFormatError(f'{os.path.basename(path)}: 索引描述文件异常大')
    if len(data) < 4:
        raise GdbFormatError(f'{os.path.basename(path)}: 文件不足 4 字节')

    count = get_uint32(data, 0)
    if count == GDBINDEXES_V9_MAGIC:
        # FileGDB v9 的索引结构,GDAL 也没实现,本库同样不支持。
        raise GdbFormatError(
            f'{os.path.basename(path)}: 这是 FileGDB v9 的索引结构'
            f'(首字段 0x{count:08X}),本库只支持 ArcGIS 10.x(v10)索引'
        )

    # GDAL 的合理性校验:索引数不应达到 (字段数+1)*10 的量级(约 2566 行)。
    # 这里没有字段数,用一个等价宽松的上限代替,防止损坏文件把我们带进死循环。
    if count > 100000:
        raise GdbFormatError(
            f'{os.path.basename(path)}: 索引个数 {count} 不合理'
        )

    base = os.path.splitext(os.path.basename(gdbtable_path))[0]
    out: List[GdbIndex] = []
    pos = 4
    end = len(data)

    for _ in range(count):
        index, pos = _read_one_index(data, pos, end, path, base,
                                     oid_field_name)
        if index is not None:
            out.append(index)

    return out


# ----------------------------------------------------------------------------
def _read_one_index(data: bytes, pos: int, end: int, path: str, base: str,
                    oid_field_name: str) -> Tuple[Optional[GdbIndex], int]:
    """读一条描述符,返回 ``(GdbIndex | None, 新位置)``。

    返回 ``None`` 表示这是一条"已损坏/已删除"的描述符,按 GDAL 的做法跳过
    (filegdbtable.cpp:2596-2625)。
    """
    # --- 索引名 ---------------------------------------------------------
    if end - pos < 4:
        raise GdbFormatError(f'{os.path.basename(path)}: 描述符在索引名长度处截断')
    name_len = get_uint32(data, pos)
    pos += 4
    if name_len > _MAX_NAME_CHARS:
        raise GdbFormatError(
            f'{os.path.basename(path)}: 索引名长度 {name_len} 不合理'
        )
    if end - pos < 2 * name_len:
        raise GdbFormatError(f'{os.path.basename(path)}: 描述符在索引名处截断')
    name, pos = read_utf16_string(data, pos, name_len)

    # --- 4 个魔数(u16 + u32 + u16 + u32 = 12 字节)-----------------------
    if end - pos < 12:
        raise GdbFormatError(f'{os.path.basename(path)}: 描述符在魔数处截断')
    magic2 = get_uint32(data, pos + 2)
    magic3 = get_uint16(data, pos + 6)

    if (magic2, magic3) not in _MAGIC_OK:
        # 损坏/已删除的描述符:magic2 的位置实际上是"名字长度"。
        # 与 GDAL 一样,顺着它声明的长度跳过去,再吃 2 字节魔数,继续下一条。
        pos += 2                       # magic1
        col_count = magic2             # 被复用的名字长度
        pos += 4
        if col_count > _MAX_NAME_CHARS:
            raise GdbFormatError(
                f'{os.path.basename(path)}: 损坏描述符的长度 {col_count} 不合理'
            )
        pos += 2 * col_count
        pos += 2                       # magic5
        return None, pos

    pos += 12

    # --- 表达式(列名)----------------------------------------------------
    if end - pos < 4:
        raise GdbFormatError(f'{os.path.basename(path)}: 描述符在表达式长度处截断')
    col_len = get_uint32(data, pos)
    pos += 4
    if col_len > _MAX_NAME_CHARS:
        raise GdbFormatError(
            f'{os.path.basename(path)}: 表达式长度 {col_len} 不合理'
        )
    if end - pos < 2 * col_len:
        raise GdbFormatError(f'{os.path.basename(path)}: 描述符在表达式处截断')
    expression, pos = read_utf16_string(data, pos, col_len)

    pos += 2                           # magic5

    index = GdbIndex(name=name, expression=expression,
                     magic2=magic2, magic3=magic3)
    index.is_oid_index = _looks_like_oid(expression, oid_field_name)
    # 索引数据文件:GDAL 用 CPLResetExtension 把 .gdbtable 换成 .atx 再拼索引名
    index.has_atx = os.path.isfile(
        os.path.join(os.path.dirname(os.path.abspath(path)),
                     f'{base}.{name}.atx')
    )
    return index, pos


def _looks_like_oid(expression: str, oid_field_name: str) -> bool:
    """判断索引是否建在 OBJECTID 上。

    GDAL 用 ``osExpression == m_apoFields[m_iObjectIdField]->GetName()``
    严格比较(filegdbtable.cpp:2655)。这里优先用调用方给的 OID 字段名;
    没给时退回命名惯例:真实文件里 OID 索引的表达式恒为 ``FDO_<OID 名>``
    (实测 ``FDO_ID`` / ``FDO_ObjectID``)。
    """
    if oid_field_name:
        return expression == oid_field_name
    return expression.upper().startswith('FDO_')


# ----------------------------------------------------------------------------
def _sibling(gdbtable_path: str, new_ext: str) -> str:
    """把 ``xxx.gdbtable`` 换成 ``xxx.<new_ext>``。"""
    base = os.path.splitext(os.fspath(gdbtable_path))[0]
    return base + '.' + new_ext


def atx_filename(gdbtable_path: str, index_name: str) -> str:
    """``<表>.<索引名>.atx`` 的完整路径(不检查是否存在)。"""
    base = os.path.splitext(os.path.basename(os.fspath(gdbtable_path)))[0]
    return os.path.join(os.path.dirname(os.path.abspath(gdbtable_path)),
                        f'{base}.{index_name}.atx')


def spx_filename(gdbtable_path: str) -> str:
    """``<表>.spx`` 的完整路径(不检查是否存在)。"""
    return _sibling(gdbtable_path, 'spx')


def has_spatial_index(gdbtable_path: str) -> bool:
    """``.spx`` 是否存在。对应 GDAL ``FileGDBTable::HasSpatialIndex()``。

    .. note::
        有 ``.spx`` 只说明 **曾经** 建过空间索引。ArcGIS 编辑数据后可能
        没来得及同步它,所以本库一律不用 ``.spx`` 加速查询,只把它当作
        "这个要素类有空间索引"的元信息暴露出去。
    """
    return os.path.isfile(spx_filename(gdbtable_path))


# ============================================================================
# .freelist —— Tier3 桩
# ============================================================================
class GdbFreelist:
    """``.freelist``(空洞列表)的只读视图 —— **Tier3 桩**。

    真实文件里,删除记录后 ArcGIS 会把"哪个 1024 记录页的第几个槽空出来了"
    记进 ``.freelist``,下次写入时复用这些槽位,避免文件无限增长。
    GDAL 侧对应 ``filegdbtable_freelist.cpp`` 的 ``FileGDBTable::ReadFreeList``
    / ``AddFreeListEntry`` 等。

    .. warning::
        本库 **不消费** 也不维护 ``.freelist``。原因与后果:

        1. 它纯粹是"省空间"的优化 —— 不读它不影响任何一次读取的正确性
           (记录的可见性由 ``.gdbtablx`` 的偏移与 ``.gdbtable`` 里的负长度
           共同表达,空洞本来就是"不可见"的)。
        2. 不维护它,只会让 **本库写入后** 的文件略显臃肿:被删除/被搬移的
           旧槽位不会被回收重用。
        3. 危险的是"读了却不正确维护" —— 那会让后续 ArcGIS 写入覆盖到仍在
           使用的记录上。因此这里选择 **完全不碰**:既不改它,也不依赖它,
           让 ArcGIS 自己按自己的规则处理(它会把 freelist 视为与索引同级的
           "可重建缓存")。

    本类只提供文件存在性与大小,方便调用方诊断。
    """

    def __init__(self, gdbtable_path: str) -> None:
        self.path = _sibling(gdbtable_path, 'freelist')
        self.exists = os.path.isfile(self.path)
        self.size = os.path.getsize(self.path) if self.exists else 0

    @property
    def is_empty(self) -> bool:
        """文件不存在或为 0 字节,都表示"没有空洞"。"""
        return self.size == 0

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (f'<GdbFreelist {os.path.basename(self.path)} '
                f'{"absent" if not self.exists else str(self.size) + "B"}>')


def read_freelist(gdbtable_path: str) -> GdbFreelist:
    """打开 ``.freelist`` 的元信息视图(不解码内容,见 :class:`GdbFreelist`)。"""
    return GdbFreelist(gdbtable_path)


# ============================================================================
# .atx / .spx 查询 —— Tier3 桩
# ============================================================================
class GdbBTreeIndex:
    """``.atx`` 属性索引的查询接口 —— **Tier3 桩,未实现**。

    ``.atx`` 是 FileGDB 私有的 B 树,GDAL 在 ``filegdbindex.cpp`` 里用
    ``FileGDBIndexIteratorBase`` + 一堆 ``FileGDB*Iterator``(``FileGDBNotIterator``
    / ``FileGDBAndIterator`` / ``FileGDBOrIterator`` …)实现了完整游标,
    还要处理多层页、页号编码、变长键压缩等细节。

    .. note::
        **为什么不实现它也不影响正确性**:索引只是加速器。没有它,
        ``GdbLayer.read_features()`` 退化成顺序扫描,结果集 **完全相同**,
        只是大表上的等值查询从 O(log n) 变成 O(n)。任务书把这一项列为
        Tier3,本库据此只保留接口形状与文件级的元信息。

    调用任何查询方法都会抛 :class:`NotImplementedError`,并提示改用
    ``read_features()`` 做扫描。
    """

    def __init__(self, index: GdbIndex, gdbtable_path: str = '') -> None:
        self.index = index
        self.table_path = gdbtable_path
        self.path = atx_filename(gdbtable_path, index.name) \
            if gdbtable_path else ''

    @property
    def field_name(self) -> str:
        return self.index.field_name

    def seek(self, *args: Any, **kwargs: Any) -> Any:
        """按值查询(OID 迭代器)。**未实现。**

        GDAL 对应 ``FileGDBIndexIterator`` 的 ``SetConstraint`` +
        ``GetNextRowSortedByValue``;见 filegdbindex.cpp。
        """
        raise NotImplementedError(
            f'属性索引 {self.index.name!r} 的 B 树查询是 Tier3 桩未实现;'
            f'请用 GdbLayer.read_features() 顺序扫描(lower({self.field_name}) '
            f'这类表达式可在 Python 侧过滤)'
        )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f'<GdbBTreeIndex {self.index.name!r} (未实现)>'


class GdbSpatialIndex:
    """``.spx`` 空间索引的查询接口 —— **Tier3 桩,未实现**。

    GDAL 在 ``filegdbindex.cpp`` 的 ``FileGDBSpatialIndexIteratorImpl`` 里
    实现了空间索引游标(与 ``.spx`` 的 R 树布局紧密耦合)。

    .. note::
        本库用 **几何包围盒直接判断** 代替:``GdbLayer.read_features(bbox=...)``
        会逐条解码几何再算包围盒。对于筛选少量要素完全够用;不实现 ``.spx``
        的唯一代价是大表上的窗口查询要全表扫一遍。
    """

    def __init__(self, gdbtable_path: str) -> None:
        self.table_path = gdbtable_path
        self.path = spx_filename(gdbtable_path)
        self.exists = os.path.isfile(self.path)

    def intersect(self, *args: Any, **kwargs: Any) -> Any:
        """包围盒查询。**未实现** —— 见类文档。"""
        raise NotImplementedError(
            '.spx 空间索引查询是 Tier3 桩未实现;'
            '请用 GdbLayer.read_features(bbox=(xmin, ymin, xmax, ymax))'
        )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f'<GdbSpatialIndex {"present" if self.exists else "absent"} (未实现)>'


def table_index_report(gdbtable_path: str,
                       oid_field_name: str = '') -> Dict[str, Any]:
    """把一张表的索引侧信息汇总成字典,供诊断/调试使用。

    :returns: ``{'indexes': [GdbIndex, ...], 'spatial_index': bool,
        'freelist_size': int}``。
    """
    try:
        indexes = read_gdbindexes(gdbtable_path, oid_field_name)
    except GdbFormatError:
        indexes = []
    return {
        'indexes': indexes,
        'spatial_index': has_spatial_index(gdbtable_path),
        'freelist_size': read_freelist(gdbtable_path).size,
    }
