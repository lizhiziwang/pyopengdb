"""核心数据类型定义。

用 dataclass 重新建模 GDAL ``ogr/ogrsf_frmts/openfilegdb`` 里的对象:

===================  ==========================================
GDAL C++ 概念        本模块对应
===================  ==========================================
``OGRFieldDefn``     :class:`GdbField`
``OGRGeomFieldDefn`` :class:`GdbGeomField`
``OGRSpatialReference`` :class:`GdbSpatialRef`
``OGRGeometry``      :class:`Geometry`
``OGRFeature``       :class:`GdbFeature`
GDB_Items 的一行       :class:`GdbItem`
===================  ==========================================
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import _constants as C
from .geometry import Geometry, _UNSET


# ----------------------------------------------------------------------------
# 字段
# ----------------------------------------------------------------------------
@dataclass
class GdbField:
    """一个字段的定义。

    对应 ``FileGDBField``(见 filegdbtable.h)。

    :param field_type: 见 ``_constants.FGFT_*``;注意是 FGFT 编号,不是 OGR 编号。
    :param length: 仅 STRING 有意义,即 ``m_nMaxWidth``(字符数)。
    :param nullable: flags 的 MASK_NULLABLE 位。只有 nullable 的字段在记录的
        空值位图里占 1 个 bit。
    :param required: flags 的 MASK_REQUIRED 位。
    :param editable: flags 的 MASK_EDITABLE 位;置位时字段描述区里才跟默认值。
    """

    name: str
    field_type: int = C.FGFT_UNDEFINED
    length: int = 0
    nullable: bool = True
    required: bool = False
    editable: bool = False
    alias: str = ''
    default_value: Any = None

    # -- 只读衍生属性 ---------------------------------------------------------
    @property
    def fixed_size(self) -> int:
        """记录内占用的固定字节数;变长字段返回 0。

        注意 OBJECTID 返回 0:FileGDB 不把 OID 存进记录体,而是用行号推导。
        """
        return C.FIXED_FIELD_SIZES.get(self.field_type, 0)

    @property
    def is_variable(self) -> bool:
        return self.field_type in C.VARIABLE_FIELD_TYPES

    @property
    def is_geometry(self) -> bool:
        return self.field_type == C.FGFT_GEOMETRY

    @property
    def is_oid(self) -> bool:
        return self.field_type == C.FGFT_OBJECTID

    @property
    def display_name(self) -> str:
        """别名优先,用于输出。"""
        return self.alias or self.name


@dataclass
class GdbGeomField(GdbField):
    """几何字段,额外携带坐标系与量化参数。

    对应 ``FileGDBGeomField``。FileGDB 在字段描述区里存的是 **WKT + 量化
    参数**(origin/scale/tolerance),几何坐标在记录里是以这些参数为基准做
    定点量化的(见 :mod:`._esri_geometry`)。

    ``xy_scale`` 是很关键的一项:ArcGIS 默认 1e9,意味着坐标精度 1e-9 度。
    """

    wkt: str = ''
    has_m_origin_scale_tolerance: bool = False
    has_z_origin_scale_tolerance: bool = False

    x_origin: float = 0.0
    y_origin: float = 0.0
    xy_scale: float = 1e9
    xy_tolerance: float = 0.0

    m_origin: float = 0.0
    m_scale: float = 0.0
    m_tolerance: float = 0.0

    z_origin: float = 0.0
    z_scale: float = 0.0
    z_tolerance: float = 0.0

    # 全表范围(存在字段描述区尾部)
    xmin: float = 0.0
    ymin: float = 0.0
    xmax: float = 0.0
    ymax: float = 0.0
    zmin: float = 0.0
    zmax: float = 0.0
    mmin: float = 0.0
    mmax: float = 0.0

    # 空间索引格网分辨率(1~3 个)。写在字段描述区最后,创建空间索引后才有意义。
    spatial_index_grid_resolutions: List[float] = field(default_factory=list)

    def __post_init__(self) -> None:  # pragma: no cover - 简单赋值
        self.field_type = C.FGFT_GEOMETRY


# ----------------------------------------------------------------------------
# 空间参考
# ----------------------------------------------------------------------------
@dataclass
class GdbSpatialRef:
    """空间参考。

    FileGDB 只存 WKT(在字段描述区),WKID 需要从 WKT 里反推,或者从
    GDB_Items 的 ``<LatestWKID>`` 元素读。两者都保留。
    """

    wkid: int = 0
    latest_wkid: int = 0
    wkt: str = ''
    name: str = ''

    @property
    def effective_wkid(self) -> int:
        """优先返回 LatestWKID(ArcGIS 用它标识"当前推荐"的编码)。"""
        return self.latest_wkid or self.wkid

    def __bool__(self) -> bool:
        return bool(self.wkt or self.wkid or self.latest_wkid)


# ----------------------------------------------------------------------------
# 要素
# ----------------------------------------------------------------------------
class _LazyGeometry:
    """几何 blob 的"还没读/还没解码"占位符。

    对应 GDAL ``OGRFeature::GetGeomFieldRef`` 的延迟构造:几何只在
    **第一次被要** 的时候才解码,解完就缓存(同一个几何不会解两遍)。

    为什么值得单独搞一层
    --------------------
    实测解码一个顶点要 ~1.4 µs(纯 Python),而一个县的多边形边界可以有几
    百万个顶点。遍历图层时如果只读属性(``feat['NAME']``),把几何解出来
    就是纯浪费 —— 这正是 GDAL ``OGROpenFileGDBLayer::SetIgnoredFields``
    省掉的那一段。

    两种形态
    --------
    * **已读进内存**(``raw`` 非空):blob 字节就在手上,只差解码。
    * **只剩位置**(:meth:`deferred`):连字节都没读,只记着
      ``(路径, 文件偏移, 长度)``。这一档省的是 **IO** —— 实测一个村界图层
      里几何占了记录体的 **99.1%**(259.3 MB / 261.7 MB),只读属性的遍历
      完全没必要把这 259 MB 从盘上拖进来。

      :meth:`resolve` 时会重新 seek 回去读。迭代期间共享 ``iter_rows``
      已经打开的文件句柄(``fp``);迭代结束后句柄关了,就临时开一次 ——
      只对真正用到几何的要素付这一笔。

    本类只在 :mod:`._gdbtable` 内部出现,外面拿到的一律是
    :class:`Geometry`(:attr:`GdbFeature.geometry` 会自动 resolve)。
    """

    __slots__ = ('_raw', '_path', '_off', '_len', '_fp',
                 '_field', '_has_z', '_has_m', '_geom', '_env')

    def __init__(self, raw: bytes, geom_field: Any,
                 has_z: bool, has_m: bool) -> None:
        self._raw = raw
        self._path = None
        self._off = 0
        self._len = len(raw)
        self._fp = None
        self._field = geom_field
        self._has_z = has_z
        self._has_m = has_m
        self._geom: Any = _UNSET
        self._env: Any = _UNSET

    @classmethod
    def deferred(cls, path: str, off: int, length: int, geom_field: Any,
                 has_z: bool, has_m: bool, fp: Any = None) -> '_LazyGeometry':
        """只记位置,不读字节。

        :param off: 几何 blob **数据**在文件里的绝对偏移(不含前面的
            varuint 长度前缀)。
        :param fp: 迭代期间共享的文件句柄;可以是 ``None``,也可以是已经
            关闭的句柄 —— :meth:`_read_bytes` 两种情况都会自己兜住。
        """
        self = cls.__new__(cls)
        self._raw = None
        self._path = path
        self._off = off
        self._len = length
        self._fp = fp
        self._field = geom_field
        self._has_z = has_z
        self._has_m = has_m
        self._geom = _UNSET
        self._env = _UNSET
        return self

    # -- 取字节 -------------------------------------------------------------
    def _read_bytes(self, n: int) -> bytes:
        """从盘上读 blob 的前 ``n`` 字节。

        先用共享句柄(迭代期间),它关了或者没有就临时开一个。用 ``len``
        兜一下:文件被截断时 ``read`` 会少给字节,交给调用方按"不完整"
        处理,而不是在这里炸。
        """
        fp = self._fp
        if fp is not None and not fp.closed:
            fp.seek(self._off)
            return fp.read(n)
        with open(self._path, 'rb') as f:
            f.seek(self._off)
            return f.read(n)

    def resolve(self) -> Optional['Geometry']:
        """解出几何。只解一次,之后走缓存。"""
        geom = self._geom
        if geom is _UNSET:
            raw = self._raw
            if raw is None:
                raw = self._read_bytes(self._len)
                self._raw = raw
            if not raw:
                geom = None
            else:
                # 局部 import:``_esri_geometry`` 依赖本模块,模块级 import
                # 会成环。同 :meth:`Geometry.wkt` 的做法。
                from ._esri_geometry import decode_geometry
                geom = decode_geometry(raw, geom_field=self._field,
                                       has_z=self._has_z, has_m=self._has_m)
            self._geom = geom
        return geom

    def envelope(self) -> Optional[Tuple[float, float, float, float]]:
        """存储包围盒(已放宽一个量化步长);拿不到返回 ``None``。

        比 :meth:`resolve` 便宜几个数量级:包围盒在 blob 的**最前面**
        (形状类型 / 点数 / 环数之后就是),点数组还在好几 KB 之后,所以
        只读一小段前缀就够 —— 见 :data:`._esri_geometry.
        GEOM_ENVELOPE_PREFIX`。这一档**不解码任何顶点**。
        """
        env = self._env
        if env is _UNSET:
            from ._esri_geometry import (GEOM_ENVELOPE_PREFIX, peek_envelope,
                                         _IncompletePeek)
            raw = self._raw
            if raw is None:
                raw = self._read_bytes(min(self._len, GEOM_ENVELOPE_PREFIX))
            try:
                env = peek_envelope(raw, self._field, self._has_z, self._has_m,
                                    complete=len(raw) >= self._len)
            except _IncompletePeek:
                # 前缀不够放下包围盒(罕见):退回去把整块读了
                if self._raw is None:
                    self._raw = self._read_bytes(self._len)
                env = peek_envelope(self._raw, self._field,
                                    self._has_z, self._has_m)
            self._env = env
        return env

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        state = '未解码' if self._geom is _UNSET else '已解码'
        where = (f'内存 {len(self._raw)}B' if self._raw is not None
                 else f'盘上 off={self._off} len={self._len}')
        return f'<LazyGeometry {state} {where}>'




class GdbFeature:
    """一条要素 = OID + 属性 + 几何。

    ``attributes`` 只放 **非几何、非 OID** 的字段,键是字段名。

    :attr:`geometry` 是 **惰性** 的:从 ``read_features()`` 出来的要素带的是
    几何 blob 的未解码引用,第一次访问 ``feat.geometry`` 时才真正解析。
    只读属性的遍历不会为几何付任何代价。
    """

    def __init__(self, oid: int = 0, attributes: Optional[Dict[str, Any]] = None,
                 geometry: Any = None) -> None:
        self.oid = oid
        self.attributes: Dict[str, Any] = {} if attributes is None else attributes
        self._geometry = geometry

    # -- 几何(惰性) --------------------------------------------------------
    @property
    def geometry(self) -> Optional['Geometry']:
        """几何对象;没有几何或几何为空时返回 ``None``。

        第一次访问时才解码,之后走缓存 —— 与 GDAL 在 feature 上缓存
        geometry 的行为一致。用 ``type(...) is`` 而不是 ``isinstance`` 是
        为了排除子类,判据更严也更省一次 MRO 查找。
        """
        geom = self._geometry
        if type(geom) is _LazyGeometry:
            geom = geom.resolve()
            self._geometry = geom
        return geom

    @geometry.setter
    def geometry(self, value: Any) -> None:
        self._geometry = value

    def stored_envelope(self) -> Optional[Tuple[float, float, float, float]]:
        """几何 blob 里存的包围盒(已放宽一个量化步长);拿不到返回 ``None``。

        只对 **还没解码** 的惰性几何有效 —— 几何已经解出来的话直接算它的
        包围盒就行,没必要再回读 blob。空间过滤用它做粗筛。
        """
        geom = self._geometry
        if type(geom) is _LazyGeometry:
            return geom.envelope()
        return None

    # -- 属性字典式访问 -----------------------------------------------------
    def __getitem__(self, name: str) -> Any:
        return self.attributes[name]

    def __setitem__(self, name: str, value: Any) -> None:
        """属性赋值;值若是 :class:`Geometry`,自动落到 :attr:`geometry`。

        这样读出来的要素可以就地改完再交给 ``update_feature()``::

            feat = layer.read_feature(3)
            feat['HEIGHT'] = 12.5
            feat['Shape'] = Geometry.from_wkt('POINT (1 2)')
            layer.update_feature(feat)

        几何值不会被放进 :attr:`attributes` —— 那里只存非几何字段,
        ``encode_feature()`` 也是这么认的(属性字典里出现 Geometry
        会直接报字段类型不符)。键名怎么写都行(``'Shape'``/``'SHAPE'``),
        因为判据是值的类型而不是键名。
        """
        if isinstance(value, Geometry):
            self.geometry = value
        else:
            self.attributes[name] = value

    def __contains__(self, name: str) -> bool:
        return name in self.attributes

    def keys(self) -> Any:
        return self.attributes.keys()

    def items(self) -> Any:
        return self.attributes.items()

    def get(self, name: str, default: Any = None) -> Any:
        return self.attributes.get(name, default)

    def copy(self) -> 'GdbFeature':
        """浅拷贝(属性字典另存一份,几何对象共享)。

        **不** 会强制解出惰性几何 —— 拷贝出来仍是惰性的。只读属性的
        循环里 copy 不会有额外代价。
        """
        return GdbFeature(oid=self.oid, attributes=dict(self.attributes),
                          geometry=self._geometry)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (f'GdbFeature(oid={self.oid!r}, attributes={self.attributes!r}, '
                f'geometry={self._geometry!r})')


@dataclass
class GdbItem:
    """GDB_Items 表里的一条记录。

    ``definition_xml`` 是定义该 item 的完整 XML;要素类的字段清单、几何
    类型、空间参考全在里面。
    """

    uuid: str = ''
    name: str = ''
    item_type: str = ''
    physical_name: str = ''   # 例如 'a0000000b'
    path: str = ''
    definition_xml: str = ''
    properties: Dict[str, Any] = field(default_factory=dict)


# ----------------------------------------------------------------------------
# 错误类型
# ----------------------------------------------------------------------------
class GdbError(Exception):
    """所有 pyopenfilegdb 错误的基类。"""


class GdbFormatError(GdbError):
    """二进制格式与预期不符(头部损坏、偏移越界等)。"""


class GdbVersionError(GdbError):
    """FileGDB 版本不受支持(只支持 version 3/4)。"""


class GdbNotFoundError(GdbError):
    """请求的要素类 / 表项不存在。"""


class GdbWriteError(GdbError):
    """写入失败(不支持的操作、字段不匹配、索引不一致等)。"""


# ----------------------------------------------------------------------------
# 日期时间辅助
#
# FileGDB 用 double 表示"自 1899-12-30 起的天数"。参考 GDAL 在
# filegdbtable.cpp 中读取 FGFT_DATETIME 后交给 OGR 的处理。
# ----------------------------------------------------------------------------
def gdb_days_to_datetime(days: float) -> Optional[datetime]:
    """FileGDB 天数 -> :class:`datetime`。NaN 表示空值。"""
    if days != days:  # NaN
        return None
    return datetime.fromtimestamp(
        C.DATETIME_EPOCH_UNIX + days * C.SECONDS_PER_DAY, tz=timezone.utc
    )


def datetime_to_gdb_days(dt: datetime) -> float:
    """:class:`datetime` -> FileGDB 天数。"""
    if dt.tzinfo is None:
        # 按 UTC 解释 naive datetime,与 ArcGIS 的处理一致
        unix = dt.replace(tzinfo=timezone.utc).timestamp()
    else:
        unix = dt.timestamp()
    return (unix - C.DATETIME_EPOCH_UNIX) / C.SECONDS_PER_DAY
