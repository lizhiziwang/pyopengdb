"""``.gdbtable`` 文件解析与记录序列化。

本模块是整个读取链路的核心,严格对照 GDAL
``ogr/ogrsf_frmts/openfilegdb/filegdbtable.cpp`` 实现:

============================  ==========================================
本模块方法                     GDAL 对应
============================  ==========================================
:meth:`GdbTable.from_file`     ``FileGDBTable::Open``
:meth:`GdbTable._read_fields`  ``FileGDBTable::Open``(字段描述区部分)
:meth:`GdbTable.read_row`      ``FileGDBTable::SelectRow`` + ``GetAllFieldValues``
:meth:`GdbTable.iter_rows`     ``GetAndSelectNextNonEmptyRow`` 循环
:meth:`GdbTable.write_record`  ``FileGDBTable::CreateFeature``(Tier2)
============================  ==========================================

文件布局(version 3)
-------------------
::

  ---------- 主头 (40 字节) ----------
  +0   int32   version                 3 (ArcGIS 10.x) 或 4
  +4   int32   nValidRecordCount       有效记录数
  +8   int32   nHeaderBufferMaxSize    记录体最大字节数(写头部时回填)
  +12  ...     保留
  +24  uint64  nFileSize               整个 .gdbtable 的字节数(写头部时回填)
  +32  uint64  nOffsetFieldDesc        字段描述区的起始偏移,通常是 40

  ---------- 字段描述区 (从 nOffsetFieldDesc 开始) ----------
  +0   uint32  nFieldDescLength        本区总长度(含这 10 字节前缀)
  +4   uint32  nSecondaryHeaderVersion 10.x 为 4
  +8   byte    tableGeomType           FGTGT_* 表级几何类型
  +9   byte    flags                   bit0 = 字符串是 UTF-8
  +10  byte    (未用)
  +11  byte    geomTypeFlags           bit6 = 有 M, bit7 = 有 Z
  +12  uint16  nFields                 字段个数
  +14  ...     字段定义,共 nFieldDescLength-10 字节

  ---------- 记录区 (紧跟字段描述区,扁平无块) ----------
  反复出现:  [uint32 记录体长度][记录体]
  记录体 = [空值位图][字段值...]

空值位图
--------
每个 ``nullable`` 的字段占 1 bit,按字段顺序(含几何字段)排列,LSB-first。
位图为 1 表示该字段为空,此时该字段在记录体里 **不占任何字节**。
位图长度 = ``(nullable 字段数 + 7) // 8``。

⚠️ 字段类型/常量均以 ``_constants.py`` 为准,那里有逐条 GDAL 出处。
"""
from __future__ import annotations

import os
import struct
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import _constants as C
from ._datatypes import (
    GdbFeature,
    GdbField,
    GdbFormatError,
    GdbGeomField,
    GdbVersionError,
    GdbWriteError,
    _LazyGeometry,
    gdb_days_to_datetime,
)
from .geometry import Geometry
from ._gdbtablx import GdbTablx
from ._util import (ByteReader, ByteWriter, get_int32, get_uint32, get_uint64,
                    read_varuint)

# GDAL 在解析字段描述区时给缓冲区尾部预留的零字节,避免越界读。
# 见 filegdbtable.cpp 里的 ZEROES_AFTER_END_OF_BUFFER。
_ZEROES_AFTER_END_OF_BUFFER = 4

#: :meth:`GdbTable._read_record_lazy` 试探几何位置时**起步**读多少字节。
#:
#: 探针只要够装下"记录头到几何字段 varuint 长度前缀"之间的内容就行 ——
#: ArcGIS 常见的表里几何紧跟 OBJECTID,十几字节足矣,所以起步给得很小。
#: 探不到就按 :data:`_RECORD_GEOM_PROBE_GROWTH` 逐级放大,所以**给少了不会
#: 算错,只是多试几轮**;给大了才会为每条记录白读一截。
RECORD_GEOM_PROBE = 32

#: 探针放大倍数。
_RECORD_GEOM_PROBE_GROWTH = 4

#: 迭代 ``.gdbtable`` 时的读缓冲区大小。
#:
#: ⚠️ 这不是个可以随便调大的参数。``open()`` 默认给 8 KB 的
#: ``BufferedReader``,而记录在 ``.gdbtable`` 里**不连续**、每条都要
#: ``seek`` —— ``seek`` 会把缓冲区作废,于是每次 ``read`` 都实打实地拖
#: 一整个缓冲区。跳读几何之后我们每次只要几十~几百字节,8 KB 预读就全白读:
#: 实测 21217 条记录、默认缓冲区,OS 层要读 ~348 MB,**比不做跳读的
#: 262 MB 还多**;换成 512 B 之后降到 ~22 MB。
#:
#: 实测耗时(村行政区划,21217 条,热身缓存):0→0.807s / 128→0.724s /
#: 256→0.643s / **512→0.634s** / 2048→0.697s / 8192→0.656s。取 512。
_TABLE_READ_BUFFER = 512


# ---------------------------------------------------------------------------
# 记录解码计划
#
# 每条记录十几个字段,朴素写法是"逐字段查 field.nullable / field.is_oid /
# field.field_type,再调一堆小方法"。实测这条路上光 is_oid 属性就被求值
# 30 万次、need() 24 万次、remaining 26 万次 —— 全是纯 Python 的簿记开销,
# 跟"读数据"本身没关系。
#
# 所以这里把字段定义**预先编译**成一张扁平的表,主循环只做整数比较和
# struct 直接解包。见 GdbTable._build_decode_plan()。
# ---------------------------------------------------------------------------
_K_OID = 0        # 不占字节,值 = 行号 + 1
_K_I16 = 1
_K_I32 = 2
_K_F32 = 3
_K_F64 = 4
_K_I64 = 5
_K_STRING = 6     # varuint 长度 + 字节(STRING 与 XML 同构)
_K_DATETIME = 7   # double 天数 -> datetime(DATE / TIME / DATETIME 同构)
_K_GEOM = 8
_K_OTHER = 9      # BINARY / RASTER / GUID / DATETIME_WITH_OFFSET 等,走通用实现

_S_I16 = struct.Struct('<h')
_S_I32 = struct.Struct('<i')
_S_F32 = struct.Struct('<f')
_S_F64 = struct.Struct('<d')
_S_I64 = struct.Struct('<q')

#: FGFT -> 计划里的种类
_FIELD_KIND = {
    C.FGFT_OBJECTID: _K_OID,
    C.FGFT_INT16: _K_I16,
    C.FGFT_INT32: _K_I32,
    C.FGFT_FLOAT32: _K_F32,
    C.FGFT_FLOAT64: _K_F64,
    C.FGFT_INT64: _K_I64,
    C.FGFT_STRING: _K_STRING,
    C.FGFT_XML: _K_STRING,
    C.FGFT_DATE: _K_DATETIME,
    C.FGFT_TIME: _K_DATETIME,
    C.FGFT_DATETIME: _K_DATETIME,
    C.FGFT_GEOMETRY: _K_GEOM,
}

#: 种类 -> 固定字节数(变长/特殊的返回 0)
_KIND_SIZE = {
    _K_I16: 2, _K_I32: 4, _K_F32: 4, _K_F64: 8, _K_I64: 8, _K_DATETIME: 8,
}

#: 种类 -> struct 解包器
_KIND_UNPACK = {
    _K_I16: _S_I16, _K_I32: _S_I32, _K_F32: _S_F32,
    _K_F64: _S_F64, _K_I64: _S_I64, _K_DATETIME: _S_F64,
}

#: GUID 的格式化模板。FileGDB 存的是**小端混合序**:前 4 / 2 / 2 字节各自
#: 反向,后 8 字节正序 —— 见 GDAL ``filegdbtable.cpp`` 里 ``snprintf(
#: m_achGUIDBuffer, ...)`` 那一段的索引序列 ``{3,2,1,0,5,4,7,6,8,...}``。
_GUID_FMT = ('{%02X%02X%02X%02X-%02X%02X-%02X%02X-%02X%02X-%02X%02X%02X%02X%02X%02X}')


def _format_guid(b: bytes) -> str:
    """16 字节 -> ``{XXXXXXXX-....}``(见 :data:`_GUID_FMT`)。"""
    return _GUID_FMT % (b[3], b[2], b[1], b[0], b[5], b[4], b[7], b[6],
                        b[8], b[9], b[10], b[11], b[12], b[13], b[14], b[15])


class GdbTable:
    """一个 ``.gdbtable``(可能配套一个 ``.gdbtablx``)。

    通常不直接构造,而是通过 :meth:`from_file`。
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self.dir = os.path.dirname(os.path.abspath(path))
        # .gdbtable 的 8 位十六进制前缀,例如 'a00000001'
        self.basename = os.path.splitext(os.path.basename(path))[0]

        # ---- 头部 ----
        self.version: int = C.FGDB_VERSION_10
        self.valid_record_count: int = 0
        self.header_buffer_max_size: int = 0
        self.file_size: int = 0
        self.offset_field_desc: int = C.GDBTABLE_MAIN_HEADER_SIZE
        self.field_desc_length: int = 0
        self.secondary_header_version: int = C.SECONDARY_HEADER_VERSION_V10

        # ---- 表级几何信息 ----
        self.table_geom_type: int = C.FGTGT_NONE
        self.strings_are_utf8: bool = False
        self.has_z: bool = False
        self.has_m: bool = False

        # ---- 字段 ----
        self.fields: List[GdbField] = []
        self.geom_field: Optional[GdbGeomField] = None
        self.geom_field_index: int = -1
        self.oid_field_index: int = -1

        # ---- 索引 ----
        self.tablx: Optional[GdbTablx] = None

        self._nullable_field_count = 0
        self._nullable_bitmap_size = 0
        # 预编译的解码计划,见 _build_decode_plan()
        self._plan: List[Tuple[str, int, int, int, Any, GdbField]] = []
        self._oid_name = ''
        self._geom_name = ''

        # 记录区的起始偏移 = 字段描述区末尾,用于 Tier2 追加记录
        self.records_start: int = 0

        # ---- 写路径状态 ----
        # 以 'r+b' 打开的文件句柄;None 表示当前是只读的
        self._fp = None
        # 几何字段的全表包围盒被改过,需要在 sync() 时回填字段描述区
        self._dirty_geom_bbox = False
        # 全表包围盒 [xmin, ymin, xmax, ymax];None = 还没有任何几何
        self._bbox: Optional[List[float]] = None

    # ======================================================================
    # 打开
    # ======================================================================
    @classmethod
    def from_file(cls, path: str, read_tablx: bool = True) -> 'GdbTable':
        """打开 ``.gdbtable`` 并解析头部与字段定义。

        :param read_tablx: 是否同时加载同名的 ``.gdbtablx``。没有它就
            无法随机访问记录(GDAL 此时会退化成"扫描猜测记录位置")。
        """
        self = cls(path)
        if not os.path.exists(path):
            raise GdbFormatError(f'文件不存在: {path}')

        with open(path, 'rb') as f:
            header = f.read(C.GDBTABLE_MAIN_HEADER_SIZE)
            if len(header) < C.GDBTABLE_MAIN_HEADER_SIZE:
                raise GdbFormatError(
                    f'{os.path.basename(path)}: 文件不足 {C.GDBTABLE_MAIN_HEADER_SIZE} 字节'
                )

            self.version = get_int32(header, 0)
            if self.version not in C.SUPPORTED_VERSIONS:
                raise GdbVersionError(
                    f'{os.path.basename(path)}: 不支持的 FileGDB 版本 {self.version} '
                    f'(仅支持 {C.SUPPORTED_VERSIONS})'
                )

            if self.version == C.FGDB_VERSION_10:
                self.valid_record_count = get_int32(header, 4)
            else:
                # v4 把有效记录数挪到 +16 的 int64
                self.valid_record_count = get_int32(header, 16)

            self.header_buffer_max_size = get_int32(header, 8)
            self.file_size = get_uint64(header, 24)
            self.offset_field_desc = get_uint64(header, 32)

            if self.offset_field_desc != C.GDBTABLE_MAIN_HEADER_SIZE:
                # GDAL 支持"被删除的字段描述区"这种历史遗留布局,本实现不支持,
                # 但仍然按 GDAL 的方式尝试读取。
                pass

            f.seek(self.offset_field_desc)
            prefix = f.read(C.FIELD_DESC_PREFIX_SIZE)
            if len(prefix) < C.FIELD_DESC_PREFIX_SIZE:
                raise GdbFormatError(
                    f'{os.path.basename(path)}: 字段描述区前缀不完整'
                )

            self.field_desc_length = get_uint32(prefix, 0)
            if not (10 <= self.field_desc_length <= 10 * 1024 * 1024):
                raise GdbFormatError(
                    f'{os.path.basename(path)}: 非法的字段描述区长度 '
                    f'{self.field_desc_length}'
                )

            self.secondary_header_version = get_uint32(prefix, 4)
            geom_type = prefix[8]
            if C.is_valid_layer_geom_type(geom_type):
                self.table_geom_type = geom_type
            self.strings_are_utf8 = (prefix[9] & 0x1) != 0
            geom_flags = prefix[11]
            self.has_m = (geom_flags & (1 << 6)) != 0
            self.has_z = (geom_flags & (1 << 7)) != 0
            n_fields = int.from_bytes(prefix[12:14], 'little')

            body = f.read(self.field_desc_length - 10)
            self._read_fields(body, n_fields)

        # 记录区的起始偏移。
        #
        # ⚠️ 字段描述区第 0 个 uint32 存的是 **区长度减 4**(GDAL 的
        # m_nFieldDescLength,见 filegdbtable_write_fields.cpp:
        # ``nFieldSectionSize = abyBuffer.size() - sizeof(uint32_t)``),
        # 所以区末 = offsetFieldDesc + 4 + 该值,记录紧跟其后,
        # 中间 **没有** 填充字节(见 filegdbtable.cpp:1596 的断言
        # ``nOffset == nOffsetHeaderEnd + 4``)。
        #
        # 两种布局(GDAL 的 GuessFeatureLocations 用同样规则):
        #   1) 字段描述区就在主头之后(offsetFieldDesc == 40):
        #      记录从 40 + 4 + fieldDescLength 开始。
        #   2) 字段描述区被挪到了文件尾部(增删字段后 ArcGIS 会这么干):
        #      记录从 44 + creator 长度开始,描述区在文件末尾。
        self.records_start = self.offset_field_desc + 4 + self.field_desc_length
        if self.offset_field_desc == C.GDBTABLE_MAIN_HEADER_SIZE:
            self.records_start = self.offset_field_desc + 4 + self.field_desc_length

        if read_tablx:
            tablx_path = os.path.join(self.dir, self.basename + '.gdbtablx')
            if os.path.exists(tablx_path):
                self.tablx = GdbTablx.open(tablx_path)

        # 没有 .gdbtablx 时 GDAL 会去扫描猜测;本实现不做,直接报清楚
        if self.tablx is None and self.valid_record_count > 0:
            raise GdbFormatError(
                f'{os.path.basename(path)}: 缺少 .gdbtablx,无法定位记录'
            )
        return self

    # ----------------------------------------------------------------------
    def _read_fields(self, body: bytes, n_fields: int) -> None:
        """解析字段描述区主体。对应 ``FileGDBTable::Open`` 中的字段循环。"""
        # 尾部补零,对应 GDAL 的 ZEROES_AFTER_END_OF_BUFFER
        buf = body + b'\x00' * _ZEROES_AFTER_END_OF_BUFFER
        r = ByteReader(buf, 0, len(body))

        for i in range(n_fields):
            name = r.utf16_prefixed()
            alias = r.utf16_prefixed()
            ftype = r.u8()

            if ftype in (C.FGFT_GEOMETRY, C.FGFT_RASTER):
                field = self._read_geom_or_raster_field(r, name, alias, ftype)
            else:
                field = self._read_simple_field(r, name, alias, ftype)

            self.fields.append(field)
            if field.is_geometry:
                if self.geom_field_index >= 0:
                    raise GdbFormatError('表里有多个几何字段,不支持')
                self.geom_field_index = len(self.fields) - 1
                self.geom_field = field  # type: ignore[assignment]
            elif field.is_oid:
                if self.oid_field_index >= 0:
                    raise GdbFormatError('表里有多个 OBJECTID 字段,不支持')
                self.oid_field_index = len(self.fields) - 1

            if field.nullable:
                self._nullable_field_count += 1

        self._nullable_bitmap_size = C.bit_array_size_in_bytes(
            self._nullable_field_count
        )
        self._build_decode_plan()

    # ----------------------------------------------------------------------
    def _build_decode_plan(self) -> None:
        """把字段定义编译成一张扁平的解码计划。

        每个字段编译成 ``(字段名, 种类, 位图字节下标, 位图掩码, 解包器, 字段)``:

        * ``种类`` 见 ``_K_*``。主循环拿它做整数比较,不再去访问
          ``field.nullable`` / ``field.is_oid`` / ``field.field_type``
          —— 那三个属性在 2 万条记录上被求值了上百万次(``is_oid`` 一项
          实测 30.9 万次调用)。
        * ``位图字节下标`` 是空值位图里对应的**字节**下标,非 nullable 的
          字段是 ``-1``;``位图掩码`` 是对应的那一位。这样空值判断从
          "求值 nullable -> 累加位号 -> 移位取位"缩成一次下标 + 一次与。
        * ``解包器`` 是 :class:`struct.Struct`(定长类型)或 ``None``。
          直接放在计划里,免得每个字段每条记录再查一次字典;字节数也不用
          另查表,``Struct.size`` 就是。

        计划在 :meth:`_read_fields` / :meth:`create` 之后建一次,之后所有
        记录解码共用。:meth:`_decode_row_blob` 是唯一的消费者。
        """
        plan: List[Tuple[str, int, int, int, Any, GdbField]] = []
        bit = 0
        for f in self.fields:
            if f.nullable:
                bbyte, bmask = bit >> 3, 1 << (bit & 7)
                bit += 1
            else:
                bbyte, bmask = -1, 0
            kind = _FIELD_KIND.get(f.field_type, _K_OTHER)
            plan.append((f.name, kind, bbyte, bmask,
                         _KIND_UNPACK.get(kind), f))
        self._plan = plan
        # 供 _values_to_feature 把 OID / 几何从属性字典里摘出去,免得它
        # 每条记录扫一遍全部字段的类型。空串 = 没有这种字段(字段名不会是
        # 空串,所以拿空串当哨兵是安全的)。
        self._oid_name = self.fields[self.oid_field_index].name \
            if self.oid_field_index >= 0 else ''
        self._geom_name = self.geom_field.name if self.geom_field else ''

    # ----------------------------------------------------------------------
    def _read_simple_field(self, r: ByteReader, name: str, alias: str,
                           ftype: int) -> GdbField:
        """解析非几何/栅格字段。

        三种子布局(GDAL 里是一个 switch 的三个分支)::

            STRING   : int32 maxWidth | byte flags | varuint defaultLen
            OID/BIN/ : byte(未用) | byte flags
            GUID/XML
            其它      : byte(未用) | byte flags | byte defaultLen
        """
        flags = 0
        max_width = 0
        default_len = 0

        if ftype == C.FGFT_STRING:
            max_width = r.i32()
            if max_width < 0:
                raise GdbFormatError(f'字段 {name}: 负的字符串长度 {max_width}')
            flags = r.u8()
            default_len = r.varuint()
        elif ftype in (C.FGFT_OBJECTID, C.FGFT_BINARY, C.FGFT_GUID,
                       C.FGFT_GLOBALID, C.FGFT_XML):
            r.skip(1)
            flags = r.u8()
        else:
            r.skip(1)
            flags = r.u8()
            default_len = r.u8()

        # 只有 editable 的字段才真的存了默认值(GDAL 的 pabyIter 前进语句
        # 写在 if (flags & MASK_EDITABLE) 内部)
        default_value = None
        if flags & C.MASK_EDITABLE:
            if default_len:
                default_value = self._decode_default(r, ftype, default_len)
            else:
                default_value = None

        field = GdbField(
            name=name,
            field_type=ftype,
            length=max_width,
            nullable=bool(flags & C.MASK_NULLABLE),
            required=bool(flags & C.MASK_REQUIRED),
            editable=bool(flags & C.MASK_EDITABLE),
            alias=alias,
            default_value=default_value,
        )

        if ftype == C.FGFT_OBJECTID and flags != C.MASK_REQUIRED:
            # GDAL 断言 OBJECTID 的 flags 必须恰好是 MASK_REQUIRED;这里放宽为
            # 只在明显异常时报错,避免个别文件直接读不了。
            if flags & C.MASK_NULLABLE:
                raise GdbFormatError(f'OBJECTID 字段 {name} 不应为 nullable')
        return field

    # ----------------------------------------------------------------------
    def _decode_default(self, r: ByteReader, ftype: int, n: int) -> Any:
        """读一段默认值。GDAL 只对若干定长类型做转换,其余跳过。"""
        start = r.pos
        value: Any = None
        if ftype == C.FGFT_STRING:
            value = r.utf16_bytes(n) if not self.strings_are_utf8 else \
                r.data[r.pos:r.pos + n].decode('utf-8', 'replace')
            # 上面的 utf16 分支已前进,utf8 分支需要手动前进
            if self.strings_are_utf8:
                r.skip(n)
            return value
        if ftype == C.FGFT_INT16 and n == 2:
            value = r.i16()
        elif ftype == C.FGFT_INT32 and n == 4:
            value = r.i32()
        elif ftype == C.FGFT_FLOAT32 and n == 4:
            value = r.f32()
        elif ftype == C.FGFT_FLOAT64 and n == 8:
            value = r.f64()
        elif ftype in (C.FGFT_DATETIME, C.FGFT_DATE) and n == 8:
            value = gdb_days_to_datetime(r.f64())
        elif ftype == C.FGFT_TIME and n == 8:
            value = gdb_days_to_datetime(r.f64())
        elif ftype == C.FGFT_INT64 and n == 8:
            value = r.i64()
        # 无论识别与否,都要跳过整段(GDAL: pabyIter += defaultValueLength)
        r.pos = start + n
        return value

    # ----------------------------------------------------------------------
    def _read_geom_or_raster_field(self, r: ByteReader, name: str, alias: str,
                                   ftype: int) -> GdbField:
        """解析几何字段(或栅格字段)。

        GDAL 里 FGFT_GEOMETRY / FGFT_RASTER 走同一段代码,区别只在于
        栅格字段多一个"栅格列名"和"栅格类型"字节,且尾部是 0/4 个 double
        而不是包围盒 + Z/M 范围。
        """
        flags = r.data[r.pos + 1]
        r.skip(2)
        if ftype == C.FGFT_RASTER:
            r.skip(1)  # 栅格列名字符数,后面才是名字;本项目不解析栅格

        wkt_len = r.u16()
        wkt = r.utf16_bytes(wkt_len)

        geom_flags = r.u8()
        has_m_ost = (geom_flags & 2) != 0
        has_z_ost = (geom_flags & 4) != 0

        field = GdbGeomField(
            name=name,
            field_type=ftype,
            alias=alias,
            nullable=bool(flags & C.MASK_NULLABLE),
            required=bool(flags & C.MASK_REQUIRED),
            editable=bool(flags & C.MASK_EDITABLE),
            wkt=wkt,
            has_m_origin_scale_tolerance=has_m_ost,
            has_z_origin_scale_tolerance=has_z_ost,
        )

        if ftype == C.FGFT_GEOMETRY or geom_flags > 0:
            field.x_origin = r.f64()
            field.y_origin = r.f64()
            field.xy_scale = r.f64()
            if field.xy_scale == 0:
                raise GdbFormatError(f'几何字段 {name}: XYScale 为 0')
            if has_m_ost:
                field.m_origin = r.f64()
                field.m_scale = r.f64()
            if has_z_ost:
                field.z_origin = r.f64()
                field.z_scale = r.f64()
            field.xy_tolerance = r.f64()
            if has_m_ost:
                field.m_tolerance = r.f64()
            if has_z_ost:
                field.z_tolerance = r.f64()

        if ftype == C.FGFT_RASTER:
            r.skip(1)  # 栅格类型字节
            return field

        # 几何字段尾部:全表包围盒 + Z/M 范围 + 空间索引格网分辨率
        field.xmin = r.f64()
        field.ymin = r.f64()
        field.xmax = r.f64()
        field.ymax = r.f64()
        if self.has_z:
            field.zmin = r.f64()
            field.zmax = r.f64()
        if self.has_m:
            field.mmin = r.f64()
            field.mmax = r.f64()

        # 尾部还有一个空间索引格网块:
        #   byte 0x00 | uint32 分辨率个数(1..3) | 每个 double
        # 见 filegdbtable.cpp 中 m_nGeomFieldSpatialIndexGridResSubOffset 那一段。
        # 容易漏掉,漏掉会导致后续字段全部错位。
        r.need(5)
        r.skip(1)  # 恒为 0
        n_grid = r.u32()
        if not (1 <= n_grid <= 3):
            raise GdbFormatError(
                f'几何字段 {name}: 非法的空间索引格网数 {n_grid}'
            )
        r.need(n_grid * 8)
        field.spatial_index_grid_resolutions = [r.f64() for _ in range(n_grid)]
        return field

    # ======================================================================
    # 基本信息
    # ======================================================================
    @property
    def record_count(self) -> int:
        """槽位总数(含空洞),来自 ``.gdbtablx`` 头。"""
        if self.tablx is not None:
            return self.tablx.record_count
        return self.valid_record_count

    @property
    def geometry_type(self) -> str:
        """返回 point / polyline / polygon / multipoint / null。"""
        return {
            C.FGTGT_NONE: 'null',
            C.FGTGT_POINT: 'point',
            C.FGTGT_MULTIPOINT: 'multipoint',
            C.FGTGT_LINE: 'polyline',
            C.FGTGT_POLYGON: 'polygon',
            C.FGTGT_MULTIPATCH: 'multipatch',
        }.get(self.table_geom_type, 'unknown')

    def field_by_name(self, name: str) -> Optional[GdbField]:
        lowered = name.lower()
        for f in self.fields:
            if f.name.lower() == lowered:
                return f
        return None

    @property
    def oid_field_name(self) -> str:
        """OBJECTID 字段的名字;没有 OID 字段时返回空串。

        见 :attr:`oid_field_index`。这个名字的字段不出现在
        :class:`GdbFeature.attributes` 里 —— 它由行号推导,语义上等价于
        OGR 的 FID。
        """
        if self.oid_field_index < 0:
            return ''
        return self.fields[self.oid_field_index].name

    @property
    def geometry_field_name(self) -> str:
        """几何字段的名字;没有几何字段时返回空串。"""
        if self.geom_field_index < 0:
            return ''
        return self.fields[self.geom_field_index].name

    def __repr__(self) -> str:  # pragma: no cover
        return (f'<GdbTable {self.basename} v{self.version} '
                f'fields={len(self.fields)} records={self.record_count}>')

    # ======================================================================
    # 记录读取
    # ======================================================================
    def read_row(self, row: int) -> Optional[Dict[str, Any]]:
        """读取第 ``row`` 条(0 基)记录的原始字段值。

        :returns: 字段名 -> 值 的字典(含几何),空槽返回 ``None``。
            第 ``row`` 条对应的 OID 是 ``row + 1``(GDAL ``SetFID(iRow + 1)``)。
        """
        if self.tablx is None:
            raise GdbFormatError('没有 .gdbtablx,无法按行号读取')
        if row < 0 or row >= self.tablx.record_count:
            return None

        offset = self.tablx.offset_for_row(row)
        if offset == 0:
            return None

        blob = self._read_row_blob(offset)
        if blob is None:
            return None

        values = self._decode_row_blob(blob, row)
        return values

    # ----------------------------------------------------------------------
    def _read_row_blob(self, offset: int) -> Optional[bytes]:
        """按偏移读出记录体。

        记录前面是一个 uint32 长度;最高位为 1 说明是"已删除但索引未同步"
        的槽位(GDAL 会报 warning 并跳过)。
        """
        with open(self.path, 'rb') as f:
            f.seek(offset)
            head = f.read(4)
            if len(head) < 4:
                return None
            length = get_uint32(head, 0)
            if length & 0x80000000:
                # 删除标记
                return None
            if length < self._nullable_bitmap_size or length > 100 * 1024 * 1024:
                raise GdbFormatError(
                    f'{self.basename}: 偏移 {offset} 处的记录长度 {length} 不合理'
                )
            blob = f.read(length)
            if len(blob) < length:
                raise GdbFormatError(
                    f'{self.basename}: 偏移 {offset} 处记录被截断'
                )
            return blob

    # ----------------------------------------------------------------------
    def _decode_row_blob(self, blob: bytes, row: int,
                         lazy_geom: bool = False,
                         geom_span: Optional[List[Tuple[int, int]]] = None,
                         geom_hole: Optional[Tuple[int, Any]] = None
                         ) -> Dict[str, Any]:
        """按字段定义解析记录体。

        对应 GDAL ``FileGDBTable::GetFieldValue``:先按顺序消费空值位图,
        再依字段类型逐个取值。已删除的字段(位图=1)不占字节。

        这个循环是整个读路径最热的地方(2 万条记录 × 十几个字段 = 几十万
        次字段取值),所以写得刻意扁平:

        * 走 :meth:`_build_decode_plan` 预编译好的计划,循环体里没有一次
          ``field.nullable`` / ``field.is_oid`` / ``field.field_type`` 属性
          求值 —— 那些都是 Python 层的方法调用。
        * 不用 :class:`~pyopenfilegdb._util.ByteReader`,把游标摊成局部
          ``pos`` 整数、把缓冲区摊成局部 ``data``。``ByteReader`` 每取一个
          值要过 ``need()`` / ``remaining`` 两道方法调用,实测 ``need()``
          被调 24.5 万次、``remaining`` 26.6 万次,全是簿记。
        * 边界检查合并成一次 ``room = pos + n; if room > end``。
        * 定长数值直接 ``Struct.unpack_from``,字节数用 ``Struct.size``。

        越界一律转成 :class:`GdbFormatError`,`_read_record_lazy` 的探针
        循环靠这个异常判断"前缀读短了",:meth:`iter_rows` 靠它跳过坏记录。

        :param lazy_geom: 几何字段返回未解码的
            :class:`~pyopenfilegdb._datatypes._LazyGeometry` 而不是真的解。
            批量遍历(:meth:`iter_rows`)走这条 —— 几何解码占了读路径 99%
            以上的时间,不用就一点都不该付。单条随机读(:meth:`read_row`)
            保持饥饿求值,以维持"返回的字典里就是真几何"这个底层契约。
        :param geom_span: 传进来一个 list,几何字段的位置会以
            ``(几何数据起点, 长度)`` 追加进去。:meth:`_read_record_lazy`
            用它**探出几何在哪**,好在下一遍读取时把那一段字节整个跳过。
            这是唯一目的 —— 里面记的是相对 ``blob`` 的偏移。

            ⚠️ 追加发生在**边界检查之前**:探针读短了就会被记下来,再由
            后面的越界异常把控制权交回给调用方。顺序反了探针就永远探不到。
        :param geom_hole: ``(绝对文件偏移, 文件句柄)``。表示几何 blob 的字节
            **根本不在** ``blob`` 里:``blob`` 是"几何之前"和"几何之后"
            两段直接拼起来的。此时几何字段只记位置不读字节,而且**不推进
            读取游标** —— 游标跨过这道缝,正好接上后面那段。
        """
        out: Dict[str, Any] = {}
        end = len(blob)
        data = blob
        pos = self._nullable_bitmap_size
        utf8 = self.strings_are_utf8
        geom_field = self.geom_field
        has_z = self.has_z
        has_m = self.has_m
        path = self.path

        try:
            for name, kind, bbyte, bmask, unpack, field in self._plan:
                # -- 空值位图 --------------------------------------------------
                if bbyte >= 0 and (blob[bbyte] & bmask):
                    out[name] = None
                    continue

                # -- 定长数值 + OID(最常见,排在最前) ------------------------
                if kind <= _K_I64:
                    if kind == _K_OID:
                        # OID 不占记录空间,由行号推导
                        out[name] = row + 1
                        continue
                    room = pos + unpack.size
                    if room > end:
                        raise GdbFormatError(
                            f'{self.basename}: 行 {row} 字段 {name} 越界')
                    out[name] = unpack.unpack_from(data, pos)[0]
                    pos = room
                    continue

                # -- 变长字符串 / XML ------------------------------------------
                if kind == _K_STRING:
                    # 内联 varuint 的单字节快路径:长度 < 128 是压倒性的
                    # 多数(实测 18.2 万次调用),省掉一次 Python 函数调用。
                    if pos < end and data[pos] < 0x80:
                        n = data[pos]
                        pos += 1
                    else:
                        n, pos = read_varuint(data, pos, end)
                    room = pos + n
                    if room > end:
                        raise GdbFormatError(
                            f'{self.basename}: 行 {row} 字段 {name} 字符串越界')
                    raw = data[pos:room]
                    pos = room
                    out[name] = raw.decode(
                        'utf-8' if utf8 else 'utf-16-le', 'replace')
                    continue

                # -- 几何 ------------------------------------------------------
                if kind == _K_GEOM:
                    if pos < end and data[pos] < 0x80:
                        n = data[pos]
                        pos += 1
                    else:
                        n, pos = read_varuint(data, pos, end)
                    if geom_hole is not None:
                        # 字节没读进来,只记位置。**故意不推进 pos** ——
                        # blob 里紧跟着的就是记录中几何之后那段字段。
                        out[name] = (_LazyGeometry.deferred(
                            path, geom_hole[0], n, geom_field, has_z,
                            has_m, geom_hole[1]) if n else None)
                        continue
                    if geom_span is not None:
                        geom_span.append((pos, n))
                    room = pos + n
                    if room > end:
                        raise GdbFormatError(
                            f'{self.basename}: 行 {row} 几何越界')
                    raw = data[pos:room]
                    pos = room
                    if not raw:
                        out[name] = None
                    elif lazy_geom:
                        out[name] = _LazyGeometry(raw, geom_field, has_z, has_m)
                    else:
                        out[name] = self._decode_geometry(raw)
                    continue

                # -- 日期时间(double 天数) -------------------------------------
                if kind == _K_DATETIME:
                    room = pos + unpack.size
                    if room > end:
                        raise GdbFormatError(
                            f'{self.basename}: 行 {row} 字段 {name} 越界')
                    out[name] = gdb_days_to_datetime(
                        unpack.unpack_from(data, pos)[0])
                    pos = room
                    continue

                # -- 其余:少见类型,数量少,不心疼 ------------------------------
                pos = self._decode_other(out, name, field, data, pos, end, row)
        except (struct.error, IndexError) as e:
            raise GdbFormatError(
                f'{self.basename}: 行 {row} 记录解码失败: {e}') from None
        return out

    # ----------------------------------------------------------------------
    def _decode_other(self, out: Dict[str, Any], name: str, field: GdbField,
                      data: bytes, pos: int, end: int, row: int) -> int:
        """GUID / BINARY / RASTER / DATETIME_WITH_OFFSET / 未知类型。

        对应 GDAL ``GetFieldValue`` 里除主 switch 之外的几支。这些类型在
        实际数据里极少出现,单独放一边,免得把上面的热循环撑长。

        :returns: 新的游标位置。
        """
        t = field.field_type

        if t == C.FGFT_GUID or t == C.FGFT_GLOBALID:
            room = pos + C.UUID_SIZE_IN_BYTES
            if room > end:
                raise GdbFormatError(f'{self.basename}: 行 {row} GUID 越界')
            out[name] = _format_guid(data[pos:room])
            return room

        if t == C.FGFT_BINARY or t == C.FGFT_RASTER:
            n, pos = read_varuint(data, pos, end)
            room = pos + n
            if room > end:
                raise GdbFormatError(f'{self.basename}: 行 {row} 字段 {name} 越界')
            # 托管栅格(GDAL 的 eRasterColumnType)读进来也只是丢掉
            out[name] = data[pos:room] if t == C.FGFT_BINARY else None
            return room

        if t == C.FGFT_DATETIME_WITH_OFFSET:
            room = pos + 8
            if room > end:
                raise GdbFormatError(f'{self.basename}: 行 {row} 时间戳越界')
            days = _S_F64.unpack_from(data, pos)[0]
            off_end = room + 2
            if off_end > end:
                raise GdbFormatError(f'{self.basename}: 行 {row} 时区越界')
            out[name] = (gdb_days_to_datetime(days),
                         _S_I16.unpack_from(data, room)[0])
            return off_end

        raise GdbFormatError(f'字段 {name}: 未处理的字段类型 {t}')

    # ----------------------------------------------------------------------
    def _decode_string(self, raw: bytes) -> str:
        """字符串解码。

        ``strings_are_utf8`` 来自表头第 9 字节:ArcGIS 10.x 一般是
        UTF-16LE,较新的文件可能是 UTF-8。

        ⚠️ 热循环里的字符串解码已经内联在 :meth:`_decode_row_blob` 里,
        这个方法留给别处按需调用。
        """
        if self.strings_are_utf8:
            return raw.decode('utf-8', 'replace')
        if not raw:
            return ''
        return raw.decode('utf-16-le', 'replace')

    # ----------------------------------------------------------------------
    def _decode_geometry(self, raw: bytes) -> Optional[Geometry]:
        """几何 blob -> :class:`Geometry`。

        ⚠️ 必须把几何字段定义传进去:坐标是相对于字段描述区里的
        ``origin``/``scale`` 做定点量化的,不传就会得到一堆 ~1e11 的裸整数。
        详见 :mod:`._esri_geometry` 的 ``_Quantizer``。
        """
        from ._esri_geometry import decode_geometry
        if not raw:
            return None
        return decode_geometry(raw, geom_field=self.geom_field,
                               has_z=self.has_z, has_m=self.has_m)

    # ======================================================================
    # 迭代
    # ======================================================================
    def iter_rows(self) -> Iterator[Tuple[int, Dict[str, Any]]]:
        """顺序产出 ``(row, values)``,自动跳过空槽。

        对应 GDAL 的 ``GetAndSelectNextNonEmptyRow`` 循环:一次打开文件,
        逐条 seek + 读一条。

        ⚠️ 记录在 ``.gdbtable`` 里既不连续也不按行号排列(见 ``DESIGN.md``
        §2.2),所以这里**不是**顺序读,每条都要 seek —— 省下的只是"每条
        重开一次文件"。

        几何字段是 **惰性** 的,而且比"惰性解码"更进一步 —— **连字节都不读**。
        ``values[几何字段名]`` 拿到的是 :class:`~pyopenfilegdb._datatypes.
        _LazyGeometry`,只记着几何 blob 在文件里的位置;要几何、
        要包围盒的时候才 seek 回去取。实测几何占了记录体的 **99.1%**,
        只读属性时把这 99% 从盘上拖进来纯属浪费 —— 见 :meth:`_read_record_lazy`。
        """
        if self.tablx is None:
            raise GdbFormatError('没有 .gdbtablx,无法迭代记录')

        total = self.tablx.record_count
        bitmap_size = self._nullable_bitmap_size

        with open(self.path, 'rb', buffering=_TABLE_READ_BUFFER) as f:
            file_size = os.fstat(f.fileno()).st_size
            for row in range(total):
                offset = self.tablx.offset_for_row(row)
                if offset == 0:
                    continue
                f.seek(offset)
                head = f.read(4)
                if len(head) < 4:
                    break
                length = get_uint32(head, 0)
                if length & 0x80000000:
                    continue
                if length < bitmap_size or length > 100 * 1024 * 1024:
                    continue
                if offset + 4 + length > file_size:
                    # 记录体越过文件末尾 —— 文件被截断了,到此为止。
                    # (区别于"单条记录损坏",那种是 continue。)
                    break
                try:
                    yield row, self._read_record_lazy(f, offset + 4, length, row)
                except GdbFormatError:
                    # 单条记录损坏不应中断整个图层
                    continue

    def _read_record_lazy(self, f: Any, body_off: int, length: int,
                          row: int) -> Dict[str, Any]:
        """读一条记录,但**跳过几何 blob 的那一段字节**。

        为什么值得这么折腾
        ------------------
        实测 ``村行政区划`` 这张表:记录体合计 261.7 MB,其中几何 blob 占
        **259.3 MB(99.1%)**,非几何部分只有 2.4 MB。而几何字段在字段顺序里
        排第 2 位(``OBJECTID`` 不占字节,它排第一),几何后面还有 14 个属性
        字段 —— 所以既不能"读到几何就收工",也不能整条读。

        ⚠️ **GDAL 不这么做。** ``FileGDBTable::SelectRow`` 是一次
        ``VSIFReadL`` 把整条记录(含几何)读进 ``m_abyBuffer`` 的,几何字节
        照样从盘上过一遍,只是后面不 ``GetAsGeometry`` 而已(见
        ``filegdbtable.cpp:1828``)。所以本方法是在 GDAL 之上多走一步:
        **不读**那 99%,而不只是不解码。

        怎么做到
        --------
        先读一小段探针试解一遍,几何字段的 varuint 长度前缀一定落在里面,
        顺手被 :meth:`_decode_lazy_geometry` 记进 ``span``。拿到
        ``(几何数据起点, 长度)`` 之后,在两种读法里**取读得少的那个**:

        * **跳读**:读 ``[0, 几何起点)`` + ``[几何末尾, 记录末尾)``,中间那段
          整个不进内存,只留个文件偏移交给 :class:`_LazyGeometry.deferred`。
        * **整读**:几何太小的时候,为了跳过它反而要多读一遍前缀,不如老实读完。

        判据是 ``几何末尾 > 已读字节数`` 才值得跳 —— 这样**读的字节数永远
        不会超过优化前**,几何小的表最多打平,几何大的表才赚。

        兜底:探针放大到底还是没探到几何(前面有超长字符串字段),或者几何
        字段为空值(压根不占字节、走不到那一支),就整条读回来 —— 行为与
        优化前完全一致。
        """
        f.seek(body_off)
        if length <= RECORD_GEOM_PROBE:
            return self._decode_row_blob(f.read(length), row, lazy_geom=True)

        head = b''
        span: List[Tuple[int, int]] = []
        probe = RECORD_GEOM_PROBE
        while len(head) < length:
            want = min(probe, length) - len(head)
            if want > 0:
                more = f.read(want)
                head += more
                if want and not more:
                    break                  # 文件短了,交给下面兜底
            span.clear()
            try:
                return self._decode_row_blob(head, row, lazy_geom=True,
                                             geom_span=span)
            except GdbFormatError:
                pass
            if span:
                break
            probe = min(probe * _RECORD_GEOM_PROBE_GROWTH, length)

        if not span:
            # 探不到几何位置(或者记录真的坏了)—— 整条读。
            # 记录坏了的话,GdbFormatError 从这里抛出去给 iter_rows 跳过。
            f.seek(body_off)
            return self._decode_row_blob(f.read(length), row, lazy_geom=True)

        geom_start, geom_len = span[0]
        geom_end = geom_start + geom_len
        if geom_end <= len(head):
            # 几何那一段已经在 head 里读进来了,跳也白跳 —— 补完剩下的就行。
            # 这时总共读的就是 length,与优化前打平。
            return self._decode_row_blob(head + f.read(length - len(head)),
                                         row, lazy_geom=True)

        f.seek(body_off + geom_end)
        tail = f.read(length - geom_end)
        # 拼出来的"去洞"记录:几何那一段整个不在里面。游标跨过这道缝
        # 就直接接上后面的字段 —— 见 _decode_row_blob 的 geom_hole。
        return self._decode_row_blob(head[:geom_start] + tail, row,
                                     lazy_geom=True,
                                     geom_hole=(body_off + geom_start, f))

    def iter_features(self) -> Iterator[GdbFeature]:
        """产出 :class:`GdbFeature`,几何已解析。

        ``attributes`` 里 **不含** 几何字段,也不含 OBJECTID 字段 ——
        OID 由行号推导,放在 :attr:`GdbFeature.oid` 里(等价于 OGR 的 FID)。
        这与 :meth:`read_row` 不同:那个是底层接口,原样返回所有字段。
        """
        for row, values in self.iter_rows():
            yield self._values_to_feature(row + 1, values)

    def _values_to_feature(self, oid: int,
                           values: Dict[str, Any]) -> 'GdbFeature':
        """把 :meth:`read_row` 的原始字典规整成 :class:`GdbFeature`。

        几何字段可能已经是 :class:`Geometry`(单条读),也可能是
        :class:`~pyopenfilegdb._datatypes._LazyGeometry`(批量遍历,还没解)
        —— 两种都原样挂到 :attr:`GdbFeature.geometry` 上,由它按需 resolve。

        OID / 几何的名字由 :meth:`_build_decode_plan` 预先算好,所以这里
        不必逐字段 ``isinstance`` 一遍 —— 那在 14 个字段 × 2 万条上是 28
        万次类型判断。空几何的键**留在属性里**(与原实现一致)。
        """
        attrs = dict(values)
        geom = attrs.get(self._geom_name)
        if type(geom) is _LazyGeometry or isinstance(geom, Geometry):
            del attrs[self._geom_name]
        else:
            geom = None
        attrs.pop(self._oid_name, None)
        return GdbFeature(oid=oid, attributes=attrs, geometry=geom)

    # ======================================================================
    # 写路径
    #
    # 对照 GDAL:
    #   ``filegdbtable_write.cpp``        Create / Sync / CreateFeature /
    #                                     UpdateFeature / DeleteFeature
    #   ``filegdbtable_write_fields.cpp`` WriteFieldDescriptors /
    #                                     WriteFieldDescriptor
    # ======================================================================
    @classmethod
    def create(cls, path: str, fields: List[GdbField],
               table_geom_type: int = C.FGTGT_NONE,
               has_z: bool = False, has_m: bool = False,
               strings_are_utf8: bool = False,
               creator: Optional[str] = None,
               offset_size: int = 5) -> 'GdbTable':
        """新建一个空的 ``.gdbtable``(并同时建好 ``.gdbtablx``)。

        对应 GDAL ``FileGDBTable::Create`` + ``WriteHeader`` +
        ``WriteFieldDescriptors``。

        :param fields: 完整字段清单。必须恰好有一个 ``FGFT_OBJECTID`` 字段,
            且它应当是第一个(GDAL/ArcGIS 都这样写);几何字段至多一个。
        :param table_geom_type: ``FGTGT_*`` 表级几何类型。
        :param creator: 写在 40 字节头之后的自由字符串;``None``(默认)
            **不写**,此时头部布局与 ArcGIS 完全一致(字段描述区从 40
            开始)。GDAL 会写 ``"GDAL <版本>"``,想模仿它传个字符串即可。
        :param offset_size: ``.gdbtablx`` 里每个偏移占几字节;5 足够 1TB。
        """
        self = cls(path)

        fields = list(fields)
        n_oid = sum(1 for f in fields if f.is_oid)
        if n_oid > 1:
            raise GdbWriteError(f'字段清单最多 1 个 OBJECTID 字段,实得 {n_oid}')
        n_geom = sum(1 for f in fields if f.is_geometry)
        if n_geom > 1:
            raise GdbWriteError(f'字段清单最多 1 个几何字段,实得 {n_geom}')
        if table_geom_type != C.FGTGT_NONE and n_geom != 1:
            raise GdbWriteError('声明了几何类型,但字段清单里没有几何字段')

        # ArcGIS 建的每一个表都是 ObjectID 打头(实测所有真实 .gdb),
        # 这里统一把 OID 挪到最前,免得调用方被字段顺序绊住。
        #
        # ⚠️ 但 OID 字段 **不是必需的**:GDAL 自己写的 GDB_DBTune
        # (a00000002)就完全没有 OID 字段(见 CreateGDBDBTune),它的行号即
        # OID。这类表合法,只是 read_row() 的返回值里不会有 OID 键。
        oid_pos = next((i for i, f in enumerate(fields) if f.is_oid), -1)
        if oid_pos > 0:
            fields.insert(0, fields.pop(oid_pos))

        self.fields = fields
        for i, f in enumerate(fields):
            if f.is_geometry:
                self.geom_field_index = i
                self.geom_field = f  # type: ignore[assignment]
            elif f.is_oid:
                self.oid_field_index = i
            if f.nullable:
                self._nullable_field_count += 1
        self._nullable_bitmap_size = C.bit_array_size_in_bytes(
            self._nullable_field_count
        )
        self._build_decode_plan()

        self.table_geom_type = table_geom_type
        self.has_z = has_z
        self.has_m = has_m
        self.strings_are_utf8 = strings_are_utf8

        # --- 头部(40 字节)+ 可选的 creator 串 ---------------------------
        # ⚠️ creator 串 **不是** FileGDB 格式的一部分。GDAL 在 40 字节之后
        # 写一个 "GDAL x.y.z" 字符串,并在注释里明说"这不是规范的一部分,
        # 我们只是利用文件里可能存在幽灵区域":
        #
        #     // Writing the creator is not part of the "spec", but we just
        #     // use the fact that there might be ghost areas in the file
        #     (filegdbtable_write.cpp:159)
        #
        # 真实的 ArcGIS 文件 **没有** 这段:offset 40 处直接就是字段描述区
        # (实测 新建文件地理数据库.gdb 里每个 .gdbtable 的 +32 都是 40)。
        #
        # 因此这里的默认值改成了 ``None`` = 不写 —— 建出来的表与 ArcGIS 的
        # 头部布局逐字节一致(offset_field_desc = 40)。想模仿 GDAL 就传一个
        # 字符串(可用 ``OPENFILEGDB_CREATOR`` 环境变量在 GDAL 侧关闭)。
        creator_bytes = creator.encode('utf-8') if creator else b''
        with open(path, 'wb') as f:
            f.write(b'\x00' * C.GDBTABLE_MAIN_HEADER_SIZE)
            if creator_bytes:
                f.write(len(creator_bytes).to_bytes(4, 'little'))
                f.write(creator_bytes)

        self.offset_field_desc = (
            C.GDBTABLE_MAIN_HEADER_SIZE
            + (4 + len(creator_bytes) if creator_bytes else 0)
        )
        self._fp = open(path, 'r+b')
        self._write_field_descriptors()
        self.records_start = self.offset_field_desc + 4 + self.field_desc_length
        self.file_size = self.records_start

        self.tablx = GdbTablx.create(
            os.path.join(self.dir, self.basename + '.gdbtablx'),
            offset_size=offset_size)
        self._write_header()
        return self

    # ----------------------------------------------------------------------
    @classmethod
    def open_for_write(cls, path: str) -> 'GdbTable':
        """以可写方式打开一个已存在的 ``.gdbtable``。

        对应 GDAL 的 ``bUpdate=TRUE`` 路径。目前只支持 version 3
        (ArcGIS 10.x);version 4 会抛 :class:`GdbVersionError`。
        """
        self = cls.from_file(path)
        if self.version not in C.WRITABLE_VERSIONS:
            raise GdbVersionError(
                f'{os.path.basename(path)}: 版本 {self.version} 不支持写入'
            )
        self._fp = open(path, 'r+b')
        # 把已有的全表包围盒接过来,避免追加要素时把老范围丢掉
        gf = self.geom_field
        if gf is not None and not any(
                abs(v) != abs(v) for v in (gf.xmin, gf.ymin, gf.xmax, gf.ymax)):
            self._bbox = [gf.xmin, gf.ymin, gf.xmax, gf.ymax]
        return self

    # ----------------------------------------------------------------------
    def _write_header(self) -> None:
        """回填 40 字节主头。对应 GDAL ``FileGDBTable::WriteHeader``。

        各字段含义(偏移为文件起始):
        ``+0`` 版本(3)、``+4`` 有效记录数、``+8`` 记录体最大字节数、
        ``+12/16/20`` 魔数 5/0/0、``+24`` 文件总字节数、
        ``+32`` 字段描述区偏移。
        """
        if self._fp is None:
            raise GdbWriteError('表不是以可写方式打开的')
        # 对应 GDAL Sync() 里那一行:
        #   m_nHeaderBufferMaxSize = max(m_nHeaderBufferMaxSize,
        #                                max(m_nRowBufferMaxSize, m_nFieldDescLength))
        # 字段描述区本身也参与取最大 —— 只记"最长记录体"会在字段多而记录短的
        # 表上写出偏小的值(实测 a00000001: fdlen=62 > 最长记录 40)。
        if self.field_desc_length > self.header_buffer_max_size:
            self.header_buffer_max_size = self.field_desc_length
        h = bytearray(C.GDBTABLE_MAIN_HEADER_SIZE)
        h[0:4] = (self.version & 0xFFFFFFFF).to_bytes(4, 'little')
        h[4:8] = (self.valid_record_count & 0xFFFFFFFF).to_bytes(4, 'little')
        h[8:12] = (self.header_buffer_max_size & 0xFFFFFFFF).to_bytes(4, 'little')
        h[12:16] = (5).to_bytes(4, 'little')
        h[16:20] = (0).to_bytes(4, 'little')
        h[20:24] = (0).to_bytes(4, 'little')
        h[24:32] = self.file_size.to_bytes(8, 'little')
        h[32:40] = self.offset_field_desc.to_bytes(8, 'little')
        self._fp.seek(0)
        self._fp.write(bytes(h))
        self._fp.flush()

    # ----------------------------------------------------------------------
    def _write_field_descriptors(self) -> None:
        """把字段描述区写到 :attr:`offset_field_desc`。

        布局(对应 ``WriteFieldDescriptors`` + ``WriteFieldDescriptor``)::

            u32  区长度(不含自身这 4 字节,即 GDAL 的 m_nFieldDescLength)
            u32  二级版本 = 4
            u32  nLayerFlags
            u16  字段个数
            ...  逐个字段描述
            u32  0xEFBEADDE(即 DE AD BE EF 的小端读法)
        """
        body = ByteWriter()
        body.u32(4)                          # 二级版本
        layer_flags = (self.table_geom_type & 0xFF)
        if self.strings_are_utf8:
            layer_flags |= (1 << 8)
        if self.table_geom_type != C.FGTGT_NONE:
            layer_flags |= (1 << 9)          # "高精度存储"标志
        if self.has_m:
            layer_flags |= (1 << 30)
        if self.has_z:
            layer_flags |= (1 << 31)
        body.u32(layer_flags)
        body.u16(len(self.fields))

        for f in self.fields:
            _encode_field_descriptor(body, f, self.has_z, self.has_m,
                                     self.strings_are_utf8)

        body.buf += b'\xDE\xAD\xBE\xEF'

        section = ByteWriter()
        section.u32(len(body.buf))           # 区长度 = 其后所有字节数
        section.buf += body.buf

        self._fp.seek(self.offset_field_desc)
        self._fp.write(section.getvalue())
        self._fp.flush()
        self.field_desc_length = len(body.buf)

    # ----------------------------------------------------------------------
    def encode_feature(self, feature: GdbFeature) -> bytes:
        """把 :class:`GdbFeature` 编码成记录体(对应 ``EncodeFeature``)。

        记录体布局::

            [空值位图: ceil(nullable/8) 字节,bit=1 表示空]
            [各字段值,按字段顺序;OBJECTID 不占空间]
            [几何字段: varuint 长度 + 几何 blob]

        ⚠️ 位图初值 0xFF(全"空"),取到值时才把对应 bit 清 0 —— 与 GDAL
        的写法一致(``m_abyBuffer[...] &= ~(1 << ...)``)。
        """
        out = ByteWriter()
        out.buf += b'\xFF' * self._nullable_bitmap_size

        bit = 0
        for f in self.fields:
            if f.is_oid:
                # OID 不进记录体,但它的 nullable 位仍占一格
                if f.nullable:
                    out.buf[bit >> 3] &= (~(1 << (bit & 7))) & 0xFF
                    bit += 1
                continue

            if f.is_geometry:
                value: Any = feature.geometry
            elif f.name in feature.attributes:
                value = feature.attributes[f.name]
            else:
                value = f.default_value

            if value is None and not f.nullable:
                # 非空字段必须落一个值:用该类型的零值顶上
                value = _zero_value(f)

            if f.nullable:
                if value is None:
                    # 位图里保持 1(=空),记录体里不占字节
                    bit += 1
                    continue
                out.buf[bit >> 3] &= (~(1 << (bit & 7))) & 0xFF
                bit += 1

            if f.is_geometry:
                self._encode_geometry_field(out, value)
            else:
                self._encode_field_value(out, f, value)

        return out.getvalue()

    # ----------------------------------------------------------------------
    def _encode_geometry_field(self, out: ByteWriter, geom: Any) -> None:
        """几何字段 = ``varuint 字节长度`` + 几何 blob(对应 ``EncodeFeature``
        里 ``WriteVarUInt(m_abyBuffer, m_abyGeomBuffer.size())`` 那段)。
        """
        from ._esri_geometry import encode_geometry
        blob = encode_geometry(geom, self.geom_field,
                               has_z=self.has_z, has_m=self.has_m)
        out.varuint(len(blob))
        out.buf += blob

        # 顺带维护几何字段的全表包围盒(见 GDAL 的
        # m_bDirtyGeomFieldBBox;这里在 sync() 时统一落盘)
        if geom is not None and not geom.is_empty:
            try:
                xs, ys = _geom_xy(geom)
            except Exception:
                return
            if not xs:
                return
            if self._bbox is None:
                self._bbox = [min(xs), min(ys), max(xs), max(ys)]
            else:
                b = self._bbox
                b[0] = min(b[0], min(xs))
                b[1] = min(b[1], min(ys))
                b[2] = max(b[2], max(xs))
                b[3] = max(b[3], max(ys))
            self._dirty_geom_bbox = True

    # ----------------------------------------------------------------------
    def _encode_field_value(self, out: ByteWriter, f: GdbField, value: Any) -> None:
        """写一个非几何字段的值。对应 ``EncodeFeature`` 的主 switch。"""
        t = f.field_type

        if t == C.FGFT_INT16:
            out.i16(int(value))
        elif t == C.FGFT_INT32:
            out.i32(int(value))
        elif t == C.FGFT_FLOAT32:
            out.f32(float(value))
        elif t == C.FGFT_FLOAT64:
            out.f64(float(value))
        elif t == C.FGFT_INT64:
            out.i64(int(value))
        elif t in (C.FGFT_DATETIME, C.FGFT_DATE, C.FGFT_TIME):
            out.f64(_to_days(value))
        elif t in (C.FGFT_GUID, C.FGFT_GLOBALID):
            out.buf += _guid_to_bytes(value)
        elif t in (C.FGFT_STRING, C.FGFT_XML):
            raw = _encode_string(value, self.strings_are_utf8)
            out.varuint(len(raw))
            out.buf += raw
        elif t == C.FGFT_BINARY:
            raw = bytes(value)
            out.varuint(len(raw))
            out.buf += raw
        elif t == C.FGFT_DATETIME_WITH_OFFSET:
            dt, off = value
            out.f64(_to_days(dt))
            out.i16(int(off))
        else:
            raise GdbWriteError(f'字段 {f.name}: 不支持写入的类型 {t}')

    # ----------------------------------------------------------------------
    def append_feature(self, feature: GdbFeature) -> int:
        """追加一条要素,返回 OID(从 1 开始)。

        对应 GDAL ``FileGDBTable::CreateFeature``。OID 不进记录体,而是由
        ``.gdbtablx`` 的槽位下标(``OID - 1``)隐式承载,所以写入顺序即
        OID 顺序。
        """
        if self._fp is None or self.tablx is None:
            raise GdbWriteError('表不是以可写方式打开的')

        blob = self.encode_feature(feature)
        offset = self.file_size
        self._fp.seek(offset)
        self._fp.write(len(blob).to_bytes(4, 'little'))
        self._fp.write(blob)
        self.file_size = offset + 4 + len(blob)

        if len(blob) > self.header_buffer_max_size:
            self.header_buffer_max_size = len(blob)

        oid = self.tablx.record_count + 1
        self.tablx.set_offset(oid - 1, offset)
        self.valid_record_count += 1
        return oid

    # ----------------------------------------------------------------------
    def update_feature(self, feature: GdbFeature) -> None:
        """就地更新一条要素。

        对应 GDAL ``UpdateFeature``:新记录体 **不大于** 旧的时候原地重写并用
        0 填满尾部;变大了才追加到文件末尾、把旧槽标记为已删除。
        """
        if self._fp is None or self.tablx is None:
            raise GdbWriteError('表不是以可写方式打开的')
        row = feature.oid - 1
        offset = self.tablx.offset_for_row(row)
        if offset == 0:
            raise GdbWriteError(f'OID {feature.oid} 不存在(该槽已删除)')

        self._fp.seek(offset)
        head = self._fp.read(4)
        old_len = get_uint32(head, 0)
        if old_len & 0x80000000:
            raise GdbWriteError(f'OID {feature.oid} 已被删除')

        blob = self.encode_feature(feature)

        if len(blob) <= old_len:
            self._fp.seek(offset)
            self._fp.write(len(blob).to_bytes(4, 'little'))
            self._fp.write(blob)
            self._fp.write(b'\x00' * (old_len - len(blob)))  # 尾部补零
            if len(blob) > self.header_buffer_max_size:
                self.header_buffer_max_size = len(blob)
            return

        # 变大:追加新的,旧槽置为"已删除"(长度取负)
        new_offset = self.file_size
        self._fp.seek(new_offset)
        self._fp.write(len(blob).to_bytes(4, 'little'))
        self._fp.write(blob)
        self.file_size = new_offset + 4 + len(blob)

        negated = (-old_len) & 0xFFFFFFFF
        self._fp.seek(offset)
        self._fp.write(negated.to_bytes(4, 'little'))

        self.tablx.set_offset(row, new_offset)
        if len(blob) > self.header_buffer_max_size:
            self.header_buffer_max_size = len(blob)

    # ----------------------------------------------------------------------
    def delete_feature(self, oid: int) -> None:
        """删除一条要素(逻辑删除)。

        对应 GDAL ``DeleteFeature``:``.gdbtablx`` 槽位写 0,``.gdbtable`` 里
        的长度字 **取负**。二者合起来让任何读取方都能把该槽判为空。
        索引空间不回收(不维护 ``.freelist``,见 :mod:`._gdbindex` 的说明)。
        """
        if self._fp is None or self.tablx is None:
            raise GdbWriteError('表不是以可写方式打开的')
        row = oid - 1
        offset = self.tablx.offset_for_row(row)
        if offset == 0:
            return

        self._fp.seek(offset)
        old_len = get_uint32(self._fp.read(4), 0)
        if old_len & 0x80000000:
            return

        self._fp.seek(offset)
        self._fp.write(((-old_len) & 0xFFFFFFFF).to_bytes(4, 'little'))
        self.tablx.set_offset(row, 0)
        self.valid_record_count -= 1

    # ----------------------------------------------------------------------
    def sync(self) -> None:
        """把头部、几何包围盒与 ``.gdbtablx`` 刷到磁盘。

        对应 GDAL ``FileGDBTable::Sync``。
        """
        if self._fp is None:
            return
        if self._dirty_geom_bbox and self.geom_field is not None:
            self._write_geom_bbox()
            self._dirty_geom_bbox = False
        self._write_header()
        if self.tablx is not None:
            self.tablx.flush()
        # 必须显式 flush:iter_rows() 会 **另开一个只读句柄** 顺序扫文件
        # (见 GDAL 的 GetAndSelectNextNonEmptyRow 循环),Python 层的写缓冲
        # 不落盘的话它会读到旧内容。
        self._fp.flush()

    # ----------------------------------------------------------------------
    def _write_geom_bbox(self) -> None:
        """把几何字段的全表包围盒回填进字段描述区。

        对应 GDAL ``Sync`` 里用 ``m_nGeomFieldBBoxSubOffset`` 就地改写的那段。
        为了不改动偏移,这里按字段描述区的结构 **重新定位** 一次几何字段的
        bbox 起点(而不是依赖之前记录的 sub-offset)。

        一条几何都没有写过时写 ESRI 的空范围 NaN —— 与 ArcGIS 对空要素类的
        写法一致(实测 ``新建文件地理数据库.gdb`` 的 GDB_Items 就是它)。
        注意用的是 :data:`_constants.ESRI_NAN`,不是 Python 的 ``float('nan')``:
        两者只差最低有效位,但 ArcGIS 用后者会让对比工具报差异。
        """
        gf = self.geom_field
        assert gf is not None and self._fp is not None
        # 重新构造一遍字段描述区,量出 bbox 的相对偏移
        probe = ByteWriter()
        probe.u32(4)
        probe.u32(0)
        probe.u16(len(self.fields))
        bbox_rel = None
        for i, f in enumerate(self.fields):
            if i == self.geom_field_index:
                bbox_rel = _encode_field_descriptor(
                    probe, f, self.has_z, self.has_m,
                    self.strings_are_utf8, with_bbox=False)
            else:
                _encode_field_descriptor(probe, f, self.has_z, self.has_m,
                                         self.strings_are_utf8)
        if bbox_rel is None:
            return

        nan = C.ESRI_NAN
        b = self._bbox if self._bbox is not None else [nan, nan, nan, nan]
        gf.xmin, gf.ymin, gf.xmax, gf.ymax = b[0], b[1], b[2], b[3]

        # bbox_rel 是相对 body 起点的偏移;body 前面还有 4 字节的区长度
        pos = self.offset_field_desc + 4 + bbox_rel
        self._fp.seek(pos)
        self._fp.write(struct.pack('<4d', *b))
        if self.has_z:
            self._fp.write(struct.pack('<2d', gf.zmin, gf.zmax))
        if self.has_m:
            self._fp.write(struct.pack('<2d', gf.mmin, gf.mmax))
        self._fp.flush()

    # ----------------------------------------------------------------------
    def close(self) -> None:
        """落盘并关闭。"""
        try:
            self.sync()
        finally:
            if self._fp is not None:
                self._fp.close()
                self._fp = None


# ============================================================================
# 模块级辅助
# ============================================================================
def _zero_value(f: GdbField) -> Any:
    """非空字段遇到 ``None`` 时顶上去的类型零值(对应 GDAL 的 memset 0)。"""
    t = f.field_type
    if t in (C.FGFT_INT16, C.FGFT_INT32, C.FGFT_INT64):
        return 0
    if t in (C.FGFT_FLOAT32, C.FGFT_FLOAT64):
        return 0.0
    if t in (C.FGFT_DATETIME, C.FGFT_DATE, C.FGFT_TIME):
        return 0.0
    if t == C.FGFT_DATETIME_WITH_OFFSET:
        return (0.0, 0)
    if t in (C.FGFT_GUID, C.FGFT_GLOBALID):
        return '00000000-0000-0000-0000-000000000000'
    if t in (C.FGFT_STRING, C.FGFT_XML):
        return ''
    if t == C.FGFT_BINARY:
        return b''
    return ''


def _geom_xy(geom: Geometry) -> Tuple[List[float], List[float]]:
    """取几何里所有 XY,用于维护全表包围盒。"""
    k = geom.kind
    if k == 'point':
        return [geom.coordinates[0]], [geom.coordinates[1]]
    if k == 'multipoint':
        pts = geom.coordinates
        return [p[0] for p in pts], [p[1] for p in pts]
    if k in ('polyline', 'polygon'):
        _, pts = geom.coordinates
        return [p[0] for p in pts], [p[1] for p in pts]
    return [], []


def _to_days(value: Any) -> float:
    """datetime -> FileGDB 天数(double)。"""
    from datetime import datetime
    if isinstance(value, datetime):
        from ._datatypes import datetime_to_gdb_days
        return datetime_to_gdb_days(value)
    return float(value)


def _encode_string(value: Any, strings_are_utf8: bool) -> bytes:
    """字符串 -> 记录体里的原始字节(不含 varuint 长度前缀)。"""
    if isinstance(value, bytes):
        return value
    text = value if isinstance(value, str) else str(value)
    if strings_are_utf8:
        return text.encode('utf-8')
    return text.encode('utf-16-le')


def _guid_to_bytes(value: Any) -> bytes:
    """``{XXXXXXXX-....}`` -> 16 字节小端混合序(``_decode_guid`` 的逆)。"""
    if isinstance(value, (bytes, bytearray)):
        if len(value) != C.UUID_SIZE_IN_BYTES:
            raise GdbWriteError(f'GUID 必须是 {C.UUID_SIZE_IN_BYTES} 字节')
        return bytes(value)
    s = str(value).strip().strip('{}').replace('-', '')
    if len(s) != 32:
        raise GdbWriteError(f'非法的 GUID: {value!r}')
    b = bytes.fromhex(s)
    return bytes((b[3], b[2], b[1], b[0], b[5], b[4], b[7], b[6],
                  b[8], b[9], b[10], b[11], b[12], b[13], b[14], b[15]))


def _encode_field_descriptor(out: ByteWriter, f: GdbField,
                             has_z: bool, has_m: bool,
                             strings_are_utf8: bool,
                             with_bbox: bool = True) -> Optional[int]:
    """写一个字段描述。对应 GDAL ``WriteFieldDescriptor``。

    :param with_bbox: 几何字段是否写 bbox 段。``_write_geom_bbox`` 需要
        先量出 bbox 的相对偏移,所以会用 ``with_bbox=False`` 走一遍。
    :returns: 几何字段返回 bbox 段相对 **body 起点** 的偏移,其余返回 ``None``。
    """
    # ⚠️ alias 按 **原样** 写,空串就写长度 0。GDAL 走的是
    # ``WriteUTF16String(abyBuffer, psField->GetAlias())``,不会用名字兜底;
    # 真实 ArcGIS 文件里 ObjectID / Shape / NAME 这些字段的 alias 都是空的
    # (实测 a00000001.gdbtable 的字段区)。拿名字兜底会把描述区撑大一倍。
    out.utf16_prefixed(f.name)
    out.utf16_prefixed(f.alias)
    out.u8(f.field_type)

    flag = 0
    if f.nullable:
        flag |= C.MASK_NULLABLE
    if f.required:
        flag |= C.MASK_REQUIRED
    if f.editable:
        flag |= C.MASK_EDITABLE

    t = f.field_type

    if t == C.FGFT_OBJECTID:
        # OBJECTID 特殊:第二个字节是魔数 2,不是 flag
        out.u8(4)
        out.u8(2)
        return None

    if t == C.FGFT_GEOMETRY:
        gf: GdbGeomField = f  # type: ignore[assignment]
        out.u8(0)                 # 未用
        out.u8(flag)
        wkt = gf.wkt or ''
        units = len(wkt.encode('utf-16-le')) // 2
        out.u16(min(units, 65534) * 2)   # 字节数(不是码元数!)
        out.utf16_bytes(wkt)
        gflag = 1
        if gf.has_m_origin_scale_tolerance:
            gflag |= (1 << 1)
        if gf.has_z_origin_scale_tolerance:
            gflag |= (1 << 2)
        out.u8(gflag)
        out.f64(gf.x_origin)
        out.f64(gf.y_origin)
        out.f64(gf.xy_scale)
        if gf.has_m_origin_scale_tolerance:
            out.f64(gf.m_origin)
            out.f64(gf.m_scale)
        if gf.has_z_origin_scale_tolerance:
            out.f64(gf.z_origin)
            out.f64(gf.z_scale)
        out.f64(gf.xy_tolerance)
        if gf.has_m_origin_scale_tolerance:
            out.f64(gf.m_tolerance)
        if gf.has_z_origin_scale_tolerance:
            out.f64(gf.z_tolerance)
        bbox_rel = len(out.buf)
        if with_bbox:
            out.f64(gf.xmin)
            out.f64(gf.ymin)
            out.f64(gf.xmax)
            out.f64(gf.ymax)
            if has_z:
                out.f64(gf.zmin)
                out.f64(gf.zmax)
            if has_m:
                out.f64(gf.mmin)
                out.f64(gf.mmax)
        out.u8(0)                 # 空间索引指示位
        grids = gf.spatial_index_grid_resolutions or []
        out.u32(len(grids))
        for g in grids:
            out.f64(g)
        return bbox_rel

    if t == C.FGFT_STRING:
        out.u32(f.length)
        out.u8(flag)
        _write_default(out, f, strings_are_utf8)
        return None

    if t in (C.FGFT_BINARY, C.FGFT_XML):
        out.u8(0)
        out.u8(flag)
        return None

    if t in (C.FGFT_GUID, C.FGFT_GLOBALID):
        out.u8(38)                # 带花括号的 GUID 字符串显示宽度
        out.u8(flag)
        return None

    # 其余定长类型: u8 字节宽度 | u8 flag | 默认值
    width = {
        C.FGFT_INT16: 2,
        C.FGFT_INT32: 4,
        C.FGFT_FLOAT32: 4,
        C.FGFT_FLOAT64: 8,
        C.FGFT_DATETIME: 8,
        C.FGFT_DATE: 8,
        C.FGFT_TIME: 8,
        C.FGFT_INT64: 8,
        C.FGFT_DATETIME_WITH_OFFSET: 10,
    }.get(t)
    if width is None:
        raise GdbWriteError(f'字段 {f.name}: 不支持写入的类型 {t}')
    out.u8(width)
    out.u8(flag)
    _write_default(out, f, strings_are_utf8)
    return None


def _write_default(out: ByteWriter, f: GdbField,
                   strings_are_utf8: bool) -> None:
    """写默认值段:``u8 长度 + 内容``;没有默认值时长度写 0。

    对应 GDAL 里 ``WriteUInt8(abyBuffer, 0); // size of default value``。
    """
    v = f.default_value
    if v is None:
        out.u8(0)
        return
    t = f.field_type
    if t == C.FGFT_INT16:
        out.u8(2)
        out.i16(int(v))
    elif t == C.FGFT_INT32:
        out.u8(4)
        out.i32(int(v))
    elif t == C.FGFT_FLOAT32:
        out.u8(4)
        out.f32(float(v))
    elif t == C.FGFT_FLOAT64:
        out.u8(8)
        out.f64(float(v))
    elif t in (C.FGFT_DATETIME, C.FGFT_DATE, C.FGFT_TIME):
        out.u8(8)
        out.f64(_to_days(v))
    elif t == C.FGFT_INT64:
        out.u8(8)
        out.i64(int(v))
    elif t == C.FGFT_STRING:
        raw = _encode_string(v, strings_are_utf8)
        out.varuint(len(raw))
        out.buf += raw
    else:
        out.u8(0)
