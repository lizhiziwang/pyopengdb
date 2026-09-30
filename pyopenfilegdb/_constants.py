"""FileGDB 格式常量定义。

本模块所有数值均为 **GDAL 源码逐行核对** 得到,不是推测。参考位置:

- ``ogr/ogrsf_frmts/openfilegdb/filegdbtable.h``
    - ``enum FileGDBFieldType``            (FGFT_*)
    - ``enum FileGDBTableGeometryType``    (FGTGT_*)
    - ``FileGDBField::MASK_NULLABLE/...``
- ``ogr/ogrsf_frmts/openfilegdb/filegdbtable_priv.h``
    - ``BIT_ARRAY_SIZE_IN_BYTES``
- ``ogr/ogrsf_frmts/openfilegdb/filegdbtable.cpp``
    - ``FileGDBTable::Open``        表头解析
    - ``FileGDBTable::ReadTableXHeaderV3``  .gdbtablx 头解析
    - ``FileGDBTable::GetOffsetInTableForRow`` .gdbtablx 偏移查找
    - ``FileGDBTable::ReadVarUInt`` 变长整数

⚠️ 关于"block"与"RLE 压缩"的重要澄清
--------------------------------------------------------------------------
网上流传的"gdbtable 由若干 block 组成、block 用 RLE 压缩"的说法 **在本
驱动的实现中不成立**。核对 filegdbtable.cpp 后可以确认:

* ``.gdbtable`` 是 **扁平** 的记录序列:头部 + 字段描述区 + 一条接一条的
  ``[uint32 记录长度][记录体]``。没有任何 block 头、没有 block 链、没有
  记录级压缩。读取时靠 ``.gdbtablx`` 给出的绝对文件偏移直接 seek。
* FileGDB 里唯一真正的"压缩"是 **几何 blob 内部的点串编码**
  (见 ``_esri_geometry.py``),由创建时的 ``GEOMETRY_FORMAT`` 选项决定
  是 ``Compressed`` 还是 ``Uncompressed``。
* ``.gdbtablx`` 里确实有"block"概念,但它指的是 **每 1024 条记录一页**
  的索引分页单位(``m_n1024BlocksPresent``),不是字节块。

因此本实现不提供 record 级解压;``_util.py`` 里的读取函数直接按上表布局
解析即可。
"""
from __future__ import annotations

import struct

# ----------------------------------------------------------------------------
# 文件格式版本
# ----------------------------------------------------------------------------
# FileGDB 的 magic 就是版本号本身(小端 uint32 写在文件最开头)。
#   3 = ArcGIS 10.x / Pro 早期  (本项目主要目标)
#   4 = 较新的 ArcGIS Pro      (GDAL 明确不支持对其做 update)
FGDB_VERSION_10 = 3
FGDB_VERSION_11 = 4
SUPPORTED_VERSIONS = (FGDB_VERSION_10, FGDB_VERSION_11)
# 只允许写入的版本(GDAL 在 bUpdate 时对 version==4 直接报错)
WRITABLE_VERSIONS = (FGDB_VERSION_10,)

# .gdbtable 主头固定长度。前面 40 字节是主头,字段描述区从 nOffsetFieldDesc
# (通常就是 40) 开始。见 FileGDBTable::Open 中 `GByte abyHeader[40]`。
GDBTABLE_MAIN_HEADER_SIZE = 40

# 字段描述区(secondary header)的固定前缀长度:
#   uint32 nFieldDescLength | uint32 nSecondaryHeaderVersion | byte[4] geom type
# 之后的字段数 uint16 在第 12 字节,故实际读取 14 字节。
FIELD_DESC_PREFIX_SIZE = 14

# 字段描述区的二级版本号;10.x 为 4,某些表(如 big_int)为 6
SECONDARY_HEADER_VERSION_V10 = 4
SECONDARY_HEADER_VERSION_BIGINT = 6
SECONDARY_HEADER_VERSION_V9 = 3

# ----------------------------------------------------------------------------
# .gdbtablx 布局 (FileGDBTable::ReadTableXHeaderV3)
# ----------------------------------------------------------------------------
#   offset 0  : uint32  version            (= 3,与 .gdbtable 一致)
#   offset 4  : uint32  n1024BlocksPresent 已分配索引页数
#   offset 8  : int32   nTotalRecordCount  总记录槽位数(含已删除)
#   offset 12 : uint32  nTablxOffsetSize   每个偏移占 4/5/6 字节
#   offset 16 : 偏移表, 共 n1024BlocksPresent * 1024 项, 每项 nTablxOffsetSize 字节
#   offset 16 + nTablxOffsetSize*1024*n1024BlocksPresent : 16 字节 trailer
TABLX_HEADER_SIZE = 16
TABLX_TRAILER_SIZE = 16
# 每页(block)记录数。注意不是 512。
TABLX_RECORDS_PER_BLOCK = 1024

# 记录偏移取值的特殊含义
#   FileGDBTable::GetOffsetInTableForRow 返回 0 表示该槽位为空(已删除)。
TABLX_OFFSET_EMPTY = 0

# ----------------------------------------------------------------------------
# 字段类型 (FileGDBFieldType, FGFT_*)
#
# 注意:这些数值**必须**与 GDAL filegdbtable.h 完全一致。
# 早期本仓库版本把它们整体 +1 了(INT16 写成 1),那是错的。
# ----------------------------------------------------------------------------
FGFT_UNDEFINED = -1
FGFT_INT16 = 0
FGFT_INT32 = 1
FGFT_FLOAT32 = 2
FGFT_FLOAT64 = 3
FGFT_STRING = 4
FGFT_DATETIME = 5
FGFT_OBJECTID = 6
FGFT_GEOMETRY = 7
FGFT_BINARY = 8          # 二进制/BLOB
FGFT_RASTER = 9
FGFT_GUID = 10           # 16 字节,ArcGIS 存为 {..} 字符串
FGFT_GLOBALID = 11
FGFT_XML = 12
FGFT_INT64 = 13          # ArcGIS Pro 3.2+
FGFT_DATE = 14           # ArcGIS Pro 3.2+
FGFT_TIME = 15           # ArcGIS Pro 3.2+
FGFT_DATETIME_WITH_OFFSET = 16  # ArcGIS Pro 3.2+

# 兼容别名:本仓库历史代码曾用 FieldType.* 的命名方式
INT16 = FGFT_INT16
INT32 = FGFT_INT32
FLOAT32 = FGFT_FLOAT32
FLOAT64 = FGFT_FLOAT64
STRING = FGFT_STRING
DATETIME = FGFT_DATETIME
OBJECTID = FGFT_OBJECTID
GEOMETRY = FGFT_GEOMETRY
BINARY = FGFT_BINARY
RASTER = FGFT_RASTER
GUID = FGFT_GUID
GLOBALID = FGFT_GLOBALID
XML = FGFT_XML
INT64 = FGFT_INT64
DATE = FGFT_DATE
TIME = FGFT_TIME
DATETIME_WITH_OFFSET = FGFT_DATETIME_WITH_OFFSET

# ESRI 字段类型名风格的别名。ArcGIS 叫 esriFieldTypeDouble/Short/Long/Text,
# 而 GDAL/FileGDB 内部叫 FLOAT64/INT16/INT32/STRING;两边都是"官方"叫法,
# 使用方经常直接写 double/short/text,这里一并给出,省得查表。
FGFT_SHORT = FGFT_INT16              # esriFieldTypeSmallInteger
FGFT_LONG = FGFT_INT32               # esriFieldTypeInteger
FGFT_SINGLE = FGFT_FLOAT32           # esriFieldTypeSingle
FGFT_DOUBLE = FGFT_FLOAT64           # esriFieldTypeDouble
FGFT_TEXT = FGFT_STRING              # esriFieldTypeString
FGFT_BLOB = FGFT_BINARY              # esriFieldTypeBlob
FGFT_BIGINT = FGFT_INT64             # esriFieldTypeBigInteger
FGFT_DATEONLY = FGFT_DATE            # esriFieldTypeDateOnly
FGFT_TIMEONLY = FGFT_TIME            # esriFieldTypeTimeOnly

UUID_SIZE_IN_BYTES = 16

# 固定长度字段的字节数。未列出的类型(STRING/GEOMETRY/BINARY/XML/RASTER ...)
# 在记录中带 varuint 长度前缀,属于变长。
FIXED_FIELD_SIZES = {
    FGFT_INT16: 2,
    FGFT_INT32: 4,
    FGFT_FLOAT32: 4,
    FGFT_FLOAT64: 8,
    FGFT_DATETIME: 8,               # double:自 1899-12-30 起的天数
    FGFT_DATE: 8,
    FGFT_TIME: 8,
    FGFT_OBJECTID: 0,               # OID 不占记录空间,由行号推导
    FGFT_GUID: UUID_SIZE_IN_BYTES,
    FGFT_GLOBALID: UUID_SIZE_IN_BYTES,
    FGFT_INT64: 8,
    FGFT_DATETIME_WITH_OFFSET: 10,  # double + int16
}

# 变长字段(记录里是 varuint 长度 + 数据)
VARIABLE_FIELD_TYPES = frozenset(
    {FGFT_STRING, FGFT_GEOMETRY, FGFT_BINARY, FGFT_XML, FGFT_RASTER}
)

# 字段描述区中 flags 字节的位定义 (FileGDBField::BIT_*)
BIT_NULLABLE = 0
BIT_REQUIRED = 1
BIT_EDITABLE = 2
MASK_NULLABLE = 1 << BIT_NULLABLE   # 0x01
MASK_REQUIRED = 1 << BIT_REQUIRED   # 0x02
MASK_EDITABLE = 1 << BIT_EDITABLE   # 0x04

# ----------------------------------------------------------------------------
# 表几何类型 (FileGDBTableGeometryType, FGTGT_*)
#
# 这是 **表级** 几何类型,写在字段描述区头部第 8 字节。
# 与记录内几何 blob 的 shape type(POINT=1/POLYLINE=3/...)是两套不同编号!
# ----------------------------------------------------------------------------
FGTGT_NONE = 0
FGTGT_POINT = 1
FGTGT_MULTIPOINT = 2
FGTGT_LINE = 3
FGTGT_POLYGON = 4
FGTGT_MULTIPATCH = 9

VALID_TABLE_GEOM_TYPES = frozenset(
    {FGTGT_POINT, FGTGT_MULTIPOINT, FGTGT_LINE, FGTGT_POLYGON, FGTGT_MULTIPATCH}
)


def is_valid_layer_geom_type(b: int) -> bool:
    """对应 GDAL 的 IS_VALID_LAYER_GEOM_TYPE 宏。"""
    return b in VALID_TABLE_GEOM_TYPES


# ----------------------------------------------------------------------------
# 记录内几何 blob 的 shape type (Esri shapefile 风格编号)
# ----------------------------------------------------------------------------
class ShapeType:
    """几何 blob 内的 shape type。

    数值 **逐行核对** ``ogr/ogrpgeogeometry.h``(见该文件 ``#ifndef
    SHPT_POINT`` 段)。⚠️ 注意:GDAL 用的是自己的一套编号,与公开的
    ESRI shapefile 规范 **不一致**。最典型的例子:

    ===========  ============  ==================
    类型          GDAL 编号      shapefile 公开值
    ===========  ============  ==================
    POINTZ        9             11
    POINTZM       11            (无此档,常按 11 误当 POINTZ)
    POLYGONZ      19            15
    POLYGONZM     15            (常按 15 误当 POLYGONZ)
    MULTIPOINTZ   20            18
    MULTIPOINTZM  18            (常按 18 误当 MULTIPOINTZ)
    ARCZ          10            13
    ARCZM         13            (常按 13 误当 POLYLINEZ)
    ===========  ============  ==================

    也就是说 **ZM 档恰好占用了 shapefile 里 Z 档的编号**。照着 shapefile
    规范写文件,GDAL 会把 3D+M 的图形读成纯 Z(或反过来),必须按上表来。
    """

    NULL = 0

    POINT = 1
    POINTZ = 9
    POINTZM = 11
    POINTM = 21

    POLYLINE = 3        # GDAL 里叫 SHPT_ARC
    POLYLINEZ = 10
    POLYLINEZM = 13
    POLYLINEM = 23

    POLYGON = 5
    POLYGONZ = 19
    POLYGONZM = 15
    POLYGONM = 25

    MULTIPOINT = 8
    MULTIPOINTZ = 20
    MULTIPOINTZM = 18
    MULTIPOINTM = 28

    MULTIPATCH = 32
    MULTIPATCHM = 31

    #: ``GEOMETRYCOLLECTION`` —— ⚠️ **这不是 Esri 的值。**
    #:
    #: FileGDB 的 ``ShapeType`` 里**没有** GEOMETRYCOLLECTION 这一档,所以它是
    #: 一个**纯内存类型**:只作为 overlay 的结果出现(``POLYGON ∪ 面外的 POINT``),
    #: **永远写不进 .gdb** —— :func:`~pyopenfilegdb._esri_geometry.encode_geometry`
    #: 会明确报错,不是悄悄丢一个 NULL 出去。
    #:
    #: 取 ``-1`` 是刻意的:上面这一套编号的取值范围是 ``0``–``54``,GDAL 的
    #: ``OGRwkbGeometryType`` 又是 ``1``–``7`` 那一套,``-1`` 落在**两个编号
    #: 空间之外**,任何"按编号查表"的老代码都不会把它误认成某个真类型。
    #: (GDAL 自己在 WKB 里用 ``wkbGeometryCollection = 7``,但那是另一套编号,
    #: 拿来塞进这个类只会让人以为它能编码。)
    GEOMETRYCOLLECTION = -1

    # "GENERAL" 系列:FileGDB 里表示"混合/任意"的同类几何
    GENERALPOLYLINE = 50
    GENERALPOLYGON = 51
    GENERALPOINT = 52
    GENERALMULTIPOINT = 53
    GENERALMULTIPATCH = 54

    # 兼容别名(历史命名)
    POINT_Z = POINTZ
    POINT_M = POINTM
    POINT_ZM = POINTZM
    POLYLINE_Z = POLYLINEZ
    POLYLINE_M = POLYLINEM
    POLYLINE_ZM = POLYLINEZM
    POLYGON_Z = POLYGONZ
    POLYGON_M = POLYGONM
    POLYGON_ZM = POLYGONZM
    MULTIPOINT_Z = MULTIPOINTZ
    MULTIPOINT_M = MULTIPOINTM
    MULTIPOINT_ZM = MULTIPOINTZM

    # 每个基础族里带 Z / 带 M 的编号集合(由上面的常量推出,避免手写出错)
    _POINT_ALL = (POINT, POINTZ, POINTZM, POINTM, GENERALPOINT)
    _POLYLINE_ALL = (POLYLINE, POLYLINEZ, POLYLINEZM, POLYLINEM,
                     GENERALPOLYLINE)
    _POLYGON_ALL = (POLYGON, POLYGONZ, POLYGONZM, POLYGONM,
                    GENERALPOLYGON)
    _MULTIPOINT_ALL = (MULTIPOINT, MULTIPOINTZ, MULTIPOINTZM, MULTIPOINTM,
                       GENERALMULTIPOINT)
    _MULTIPATCH_ALL = (MULTIPATCH, MULTIPATCHM, GENERALMULTIPATCH)

    @classmethod
    def has_z(cls, t: int) -> bool:
        """该 shape type 是否带 Z(按 GDAL 编号)。"""
        return t in (cls.POINTZ, cls.POINTZM,
                     cls.POLYLINEZ, cls.POLYLINEZM,
                     cls.POLYGONZ, cls.POLYGONZM,
                     cls.MULTIPOINTZ, cls.MULTIPOINTZM,
                     cls.MULTIPATCH, cls.MULTIPATCHM)

    @classmethod
    def has_m(cls, t: int) -> bool:
        """该 shape type 是否带 M(按 GDAL 编号)。"""
        return t in (cls.POINTM, cls.POINTZM,
                     cls.POLYLINEM, cls.POLYLINEZM,
                     cls.POLYGONM, cls.POLYGONZM,
                     cls.MULTIPOINTM, cls.MULTIPOINTZM,
                     cls.MULTIPATCHM)

    @classmethod
    def base_kind(cls, t: int) -> str:
        """归一化到 point / polyline / polygon / multipoint / multipatch /
        geometrycollection / null。

        ⚠️ ``geometrycollection`` **不是** Esri 的类型(见常量定义处的说明)——
        它只可能来自内存里构造出来的几何,盘上读不出来。
        """
        if t == cls.GEOMETRYCOLLECTION:
            return 'geometrycollection'
        if t in cls._POINT_ALL:
            return 'point'
        if t in cls._POLYLINE_ALL:
            return 'polyline'
        if t in cls._POLYGON_ALL:
            return 'polygon'
        if t in cls._MULTIPOINT_ALL:
            return 'multipoint'
        if t in cls._MULTIPATCH_ALL:
            return 'multipatch'
        return 'null'

    @classmethod
    def make_zm(cls, base: int, has_z: bool, has_m: bool) -> int:
        """由基础 XY 类型 + Z/M 标志推出最终 shape type。

        :param base: 必须是 ``POINT``/``POLYLINE``/``POLYGON``/``MULTIPOINT``
            之一(即 1/3/5/8)。
        """
        if base == cls.POINT:
            return (cls.POINTZM if (has_z and has_m) else
                    cls.POINTZ if has_z else
                    cls.POINTM if has_m else cls.POINT)
        if base == cls.POLYLINE:
            return (cls.POLYLINEZM if (has_z and has_m) else
                    cls.POLYLINEZ if has_z else
                    cls.POLYLINEM if has_m else cls.POLYLINE)
        if base == cls.POLYGON:
            return (cls.POLYGONZM if (has_z and has_m) else
                    cls.POLYGONZ if has_z else
                    cls.POLYGONM if has_m else cls.POLYGON)
        if base == cls.MULTIPOINT:
            return (cls.MULTIPOINTZM if (has_z and has_m) else
                    cls.MULTIPOINTZ if has_z else
                    cls.MULTIPOINTM if has_m else cls.MULTIPOINT)
        raise ValueError(f'不是基础 shape type: {base}')


# 表级几何类型 -> 基础 shape type 的换算(用于创建要素类)
FGTGT_TO_BASE_SHAPE = {
    FGTGT_POINT: ShapeType.POINT,
    FGTGT_MULTIPOINT: ShapeType.MULTIPOINT,
    FGTGT_LINE: ShapeType.POLYLINE,
    FGTGT_POLYGON: ShapeType.POLYGON,
}

# 字符串字段默认长度上限(对齐 ArcGIS 新建文本字段的默认值)
DEFAULT_STRING_LENGTH = 255

# datetime 时间基准:FileGDB 用 double 表示自 1899-12-30 起的天数。
# 1899-12-30 00:00 UTC 对应的 Unix 时间戳(秒)。
DATETIME_EPOCH_UNIX = -2209161600.0
# 1 天 = 86400 秒
SECONDS_PER_DAY = 86400.0


# ----------------------------------------------------------------------------
# ESRI 的 NaN
# ----------------------------------------------------------------------------
#: "没有范围"时几何字段 bbox 里写的 NaN。
#:
#: ⚠️ 它 **不是** IEEE 的规范 quiet NaN。Python 的 ``float('nan')`` 位模式是
#: ``0x7FF8000000000000``,而 FileGDB SDK 写的是 ``0x7FF8000000000001``
#: —— 最低有效位为 1。GDAL 为此专门写了个 ``getESRI_NAN()``
#: (filegdbtable.cpp:3124,注释原文:"Use exact same quiet NaN value as
#: generated by the ESRI SDK, just for the purpose of ensuring binary identical
#: output"),本库照抄,以便新建的 .gdb 与 ArcGIS 逐字节一致。
#: 实测 ``新建文件地理数据库.gdb/a00000004.gdbtable`` 的 Shape bbox 就是这个
#: 位模式连写 4 次。
ESRI_NAN_BITS = 0x7FF8000000000001
ESRI_NAN = struct.unpack('<d', struct.pack('<Q', ESRI_NAN_BITS))[0]


# ----------------------------------------------------------------------------
# 位图 / 索引辅助
# ----------------------------------------------------------------------------
def bit_array_size_in_bytes(bit_count: int) -> int:
    """对应 GDAL ``BIT_ARRAY_SIZE_IN_BYTES`` 宏。"""
    return (bit_count + 7) // 8


def test_bit(data: bytes, idx: int) -> bool:
    """对应 GDAL ``TEST_BIT`` 宏:LSB-first 位读取。"""
    return (data[idx >> 3] & (1 << (idx & 7))) != 0


def set_bit(data: bytearray, idx: int) -> None:
    """置位(Little-endian 位序,与 TEST_BIT 对应)。"""
    data[idx >> 3] |= 1 << (idx & 7)


# ----------------------------------------------------------------------------
# 系统表物理文件名
#
# FileGDB 的每个表/要素类在磁盘上是一个 8 位十六进制前缀的 .gdbtable,
# 系统表固定占用前几个编号。
#
# ⚠️ 下面这张对照表是 **用真实数据核对出来的**,不是猜的:
#   把每个 a0000000X.gdbtable 的字段清单读出来,与 GDAL 的
#   CreateGDBxxx 函数写出的列一一对上(a00000001 是 ID/Name/FileFormat,
#   a00000003 是 SRTEXT/FalseX/... ,a00000004 是 UUID/Type/PhysicalName/
#   Path/Definition)。
#
# ⚠️ 注意:定位表时**不要**靠固定编号,要靠 GDB_SystemCatalog 里的 Name
# 反查(见 :mod:`._system_catalog` 的 find_system_tables)。编号只是
# "通常如此"。
# ----------------------------------------------------------------------------
SYSTEM_TABLE_SYSCATALOG = 'a00000001'   # GDB_SystemCatalog
SYSTEM_TABLE_DBTUNE = 'a00000002'       # GDB_DBTune
SYSTEM_TABLE_SPATIALREFS = 'a00000003'  # GDB_SpatialRefs
SYSTEM_TABLE_ITEMS = 'a00000004'        # GDB_Items
SYSTEM_TABLE_ITEMTYPES = 'a00000005'    # GDB_ItemTypes
SYSTEM_TABLE_RELATIONSHIPS = 'a00000006'  # GDB_ItemRelationships
SYSTEM_TABLE_RELATIONSHIPTYPES = 'a00000007'  # GDB_ItemRelationshipTypes
SYSTEM_TABLE_REPLICALOG = 'a00000008'   # GDB_ReplicaLog

# 第一个可用于用户要素类的物理文件编号(系统表占 1..8)
FIRST_USER_TABLE_ID = 0x09


# ----------------------------------------------------------------------------
# GDB_Items 中 Definition XML 的 item type
# ----------------------------------------------------------------------------
class ItemType:
    FEATURE_CLASS = 'Feature Class'
    TABLE = 'Table'
    OBJECT_CLASS = 'Object Class'
    INDEX = 'Index'
    DOMAIN = 'Domain'
    RELATIONSHIP_CLASS = 'Relationship Class'
    RASTER_CATALOG = 'Raster Catalog'
    RASTER_DATASET = 'Raster Dataset'
    TOOLBOX = 'Toolbox'
    REPLICA = 'Replica'
    FEATURE_DATASET = 'Feature Dataset'


# GDB_Items.Type 数字编码(取 GDAL 对 GDB_ItemTypes 的解析结果)
ITEM_TYPE_CODE_FEATURE_CLASS = 1
ITEM_TYPE_CODE_TABLE = 0
