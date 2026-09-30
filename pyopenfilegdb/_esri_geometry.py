"""Esri FileGDB 私有几何二进制编解码。

⚠️ 这不是 WKB,也不是 shapefile 的 shape blob,而是 FileGDB 自己的一套
**变长整数 + 增量编码** 格式。三个关键点:

1. 坐标是 **量化整数**,不是裸 double。还原公式::

       数组形式(线/面/多点):  X = dx / xy_scale + x_origin
       单点形式(POINT):       X = (x - 1) / xy_scale + x_origin

   ``xy_scale``/``x_origin`` 来自几何字段描述区(ArcGIS 默认 1e9 / -400)。
   单点那个 ``-1`` 是 GDAL 的行为(见 filegdbtable.cpp 里 POINT 分支),
   数组形式则没有 —— 这个不对称必须照抄,否则单点会整体偏移一个量化单位。

2. 点序列用 **增量(delta)** 编码,且增量跨 part 连续累积;Z、M 各自
   独立的累加器(从 0 开始),放在所有 XY 之后。

3. 几何类型本身也是一个 varuint,高位带标志::

       0x80000000  Z
       0x40000000  M
       0x20000000  曲线段(圆弧/贝塞尔/椭圆),本项目仅识别不解码
       低 8 位       shape type

GDAL 参考位置:
- ``filegdbtable.cpp::FileGDBOGRGeometryConverterImpl::GetAsGeometry``
- ``filegdbtable.cpp::ReadXYArray / ReadZArray / ReadMArray``
- ``filegdbtable.cpp::ReadPartDefs``
- ``filegdbtable.cpp::ReadVarIntAndAddNoCheck``
- ``filegdbtable_write.cpp``(写侧)
"""
from __future__ import annotations

import json
import math
import re
from array import array
from itertools import repeat
from operator import add, truediv
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import _accel
from . import _constants as C
from ._datatypes import GdbFormatError, GdbWriteError
from ._geometry_ops import ring_signed_area2
from .geometry import Geometry, _FlatCoords
from ._util import ByteReader, read_varuint, write_varuint

#: delta-varint 续字节的 shift 上限,与 ``_gdbaccel.c`` 的 ``_MAX_SHIFT``
#: **必须取同一个值**。
#:
#: shift 取值是 6, 13, 20, ...(第 k 个续字节用 6 + 7*(k-1))。取 57 意味着
#: 最多接受 8 个续字节:第 8 个用 shift 55,贡献的最高位是 61,稳稳落在
#: 64 位累加器里。
#:
#: 为什么不沿用 :func:`read_varint_delta` 的 64?因为第 9 个续字节的 shift
#: 是 62,贡献能摸到 68 位 —— C 侧要么未定义行为、要么悄悄截掉高位,而
#: Python 的大整数不截,两边就会分叉。取 57 则 8 个续字节以内两边算术都
#: 精确,第 9 个起两边同时判非法,**接受域严格相同**。
#:
#: 真实数据碰不到:量化增量的量级取决于图层跨度,整个县的边界也才 2^39
#: 上下,而 8 个续字节能表到 2^61。
_MAX_SHIFT = 57

# 与 GDAL filegdbtable.cpp 顶部的常量一一对应
EXT_SHAPE_Z_FLAG = 0x80000000
EXT_SHAPE_M_FLAG = 0x40000000
EXT_SHAPE_CURVE_FLAG = 0x20000000

# shape type 的权威定义在 _constants.ShapeType(逐行核对 ogrpgeogeometry.h)。
# 这里做一层短别名,方便阅读 GDAL 源码时对照。
ST = C.ShapeType
SHPT_NULL = ST.NULL
SHPT_POINT = ST.POINT
SHPT_POINTZ = ST.POINTZ
SHPT_POINTZM = ST.POINTZM
SHPT_POINTM = ST.POINTM
SHPT_ARC = ST.POLYLINE
SHPT_ARCZ = ST.POLYLINEZ
SHPT_ARCZM = ST.POLYLINEZM
SHPT_ARCM = ST.POLYLINEM
SHPT_POLYGON = ST.POLYGON
SHPT_POLYGONZ = ST.POLYGONZ
SHPT_POLYGONZM = ST.POLYGONZM
SHPT_POLYGONM = ST.POLYGONM
SHPT_MULTIPOINT = ST.MULTIPOINT
SHPT_MULTIPOINTZ = ST.MULTIPOINTZ
SHPT_MULTIPOINTZM = ST.MULTIPOINTZM
SHPT_MULTIPOINTM = ST.MULTIPOINTM
SHPT_MULTIPATCH = ST.MULTIPATCH
SHPT_GENERALPOLYLINE = ST.GENERALPOLYLINE
SHPT_GENERALPOLYGON = ST.GENERALPOLYGON
SHPT_GENERALPOINT = ST.GENERALPOINT
SHPT_GENERALMULTIPOINT = ST.GENERALMULTIPOINT

# M 数组"缺失"的标记字节。GDAL 的注释:
# "It seems that absence of M is marked with a single byte with value 66"
_NO_M_MARKER = 66


# ----------------------------------------------------------------------------
# 有符号增量整数(variant of LEB128,带符号位)
# ----------------------------------------------------------------------------
def read_varint_delta(blob: bytes, pos: int) -> Tuple[int, bool, int]:
    """读一个"增量整数"。

    与 varuint 的区别:第一个字节的 0x40 位是符号位,首字节只有低 6 位是
    数据;后续字节仍是 7 位一组。

    :returns: ``(绝对值, 是否为负, 新位置)``
    """
    b = blob[pos]
    val = b & 0x3F
    negative = (b & 0x40) != 0

    if (b & 0x80) == 0:
        return val, negative, pos + 1

    pos += 1
    shift = 6
    while True:
        if pos >= len(blob):
            raise GdbFormatError('read_varint_delta: 截断')
        b = blob[pos]
        pos += 1
        val |= (b & 0x7F) << shift
        if (b & 0x80) == 0:
            return val, negative, pos
        shift += 7
        if shift >= 64:
            raise GdbFormatError('read_varint_delta: 过长')


class _DeltaReader:
    """带位置状态的有符号增量读取器。

    对应 GDAL 的 ``ReadVarIntAndAddNoCheck``:每读一次就把累加器加上(或
    减去)增量。线/面的点序列就是靠这个累加器还原绝对坐标的。
    """

    __slots__ = ('blob', 'pos')

    def __init__(self, blob: bytes, pos: int) -> None:
        self.blob = blob
        self.pos = pos

    def delta(self, acc: int) -> Tuple[int, int]:
        """读一个增量并累加到 ``acc``,返回 ``(新 acc, 新位置)``。"""
        val, negative, self.pos = read_varint_delta(self.blob, self.pos)
        return (acc - val if negative else acc + val), self.pos


def _skip_varuint(blob: bytes, pos: int, n: int = 1) -> int:
    """跳过 n 个 varuint,对应 GDAL ``SkipVarUInt``。"""
    for _ in range(n):
        if pos >= len(blob):
            raise GdbFormatError('_skip_varuint: 越界')
        while True:
            b = blob[pos]
            pos += 1
            if (b & 0x80) == 0:
                break
            if pos >= len(blob):
                raise GdbFormatError('_skip_varuint: 截断')
    return pos


# ----------------------------------------------------------------------------
# 量化参数
# ----------------------------------------------------------------------------
class _Quantizer:
    """把几何字段描述区里的 origin/scale 打包,供坐标还原使用。

    对应 GDAL ``FileGDBGeomField`` 的 GetXYScale/GetXOrigin/...
    ``SanitizeScale`` 处理 scale 为 0 的情形(GDAL 里会退化成 1)。
    """

    __slots__ = ('x_origin', 'y_origin', 'xy_scale',
                 'z_origin', 'z_scale', 'm_origin', 'm_scale')

    def __init__(self, geom_field: Any = None) -> None:
        if geom_field is None:
            self.x_origin = self.y_origin = 0.0
            self.xy_scale = 1.0
            self.z_origin = self.m_origin = 0.0
            self.z_scale = self.m_scale = 1.0
        else:
            self.x_origin = geom_field.x_origin
            self.y_origin = geom_field.y_origin
            self.xy_scale = _sanitize_scale(geom_field.xy_scale)
            self.z_origin = geom_field.z_origin
            self.z_scale = _sanitize_scale(geom_field.z_scale)
            self.m_origin = geom_field.m_origin
            self.m_scale = _sanitize_scale(geom_field.m_scale)


def _sanitize_scale(s: float) -> float:
    """对应 GDAL ``SanitizeScale``:scale 为 0/非数时退化为 1。"""
    if s == 0 or s != s:
        return 1.0
    return s


# ----------------------------------------------------------------------------
# 解码
# ----------------------------------------------------------------------------
def decode_geometry(blob: bytes, geom_field: Any = None,
                    has_z: bool = False, has_m: bool = False) -> Optional[Geometry]:
    """FileGDB 几何 blob -> :class:`Geometry`(不关心消费了多少字节)。"""
    return _decode_at(blob, 0, geom_field, has_z, has_m)[0]


def decode_geometry_ex(blob: bytes, pos: int = 0, geom_field: Any = None,
                       has_z: bool = False, has_m: bool = False):
    """同 :func:`decode_geometry`,但额外返回消费到的位置。

    :returns: ``(Geometry | None, 结束位置)``
    """
    return _decode_at(blob, pos, geom_field, has_z, has_m)


#: 只取包围盒时需要读的 blob 前缀字节数。
#:
#: 包围盒紧跟在 形状类型 / 点数 / 环数 / (曲线标志) 这几个 varuint 之后,
#: 全是小整数时十来字节就够;这里给到 256 是为了容忍各段都用 5 字节 varuint
#: 的极端情况。⚠️ 这是**性能参数不是正确性参数**:给少了只会让
#: :class:`_IncompletePeek` 触发、退化成整块读,不会算错。
GEOM_ENVELOPE_PREFIX = 256


class _IncompletePeek(Exception):
    """``peek_envelope`` 拿到的 blob 是被截断的前缀,解析所需的字节不够。

    只在 ``complete=False`` 时抛出 —— 调用方(:class:`~pyopenfilegdb.
    _datatypes._LazyGeometry`)接住它去把整块读回来,而不是把它当成
    "这块几何没有包围盒"。
    """


def peek_envelope(blob: bytes, geom_field: Any = None,
                  has_z: bool = False, has_m: bool = False,
                  complete: bool = True
                  ) -> Optional[Tuple[float, float, float, float]]:
    """只读几何 blob 头部的 **存储包围盒**,不碰点数组。

    这是空间过滤的第一道闸。对应 GDAL 做空间过滤的路子:先用记录自带的
    包围盒快速排除,只对可能相交的记录才走完整的 ``GetAsGeometry``。
    省掉的是绝大部分解码开销 —— 一个县的多边形动辄几万个顶点,而包围盒
    只要读 4 个 varuint。

    :param complete: ``blob`` 是否是完整的几何 blob。传 ``False`` 表示
        这是从盘上截的一段前缀 —— 此时若解析途中字节不够,抛
        :class:`_IncompletePeek` 而不是返回 ``None``,免得把"没读够"误判
        成"没有包围盒"。
    :returns: ``(xmin, ymin, xmax, ymax)``。拿不到时返回 ``None``:
        空几何、``POINT``(点不存包围盒,坐标本身就是全部)、解析失败。

    ⚠️ **返回值按一个量化步长的量级放宽过。** 存储的包围盒是量化之后写的
    (``round((v-origin)*scale)``),四舍五入可能让它比真实范围小不到一个
    量化单位。放宽之后才能保证"真实包围盒 ⊆ 返回值" —— 用它排除才不会
    错杀本该命中的要素。宁可多放几条给后续的精确判断,也不能漏。
    """
    q = _Quantizer(geom_field)
    try:
        geom_type, pos = read_varuint(blob, 0)
    except Exception:
        if not complete:
            raise _IncompletePeek() from None
        return None

    shp = geom_type & 0xFF
    if shp == SHPT_NULL or shp in ST._POINT_ALL:
        return None

    try:
        n_points, pos = read_varuint(blob, pos)
        if n_points == 0:
            return None
        if shp in ST._MULTIPOINT_ALL:
            pass                       # 多点:点数之后直接就是包围盒
        elif shp in ST._POLYLINE_ALL or shp in ST._POLYGON_ALL:
            n_parts, pos = read_varuint(blob, pos)
            if n_parts == 0:
                return None
            if (geom_type & EXT_SHAPE_CURVE_FLAG) != 0:
                pos = _skip_varuint(blob, pos, 1)
        else:
            return None

        v_min_x, pos = read_varuint(blob, pos)
        v_min_y, pos = read_varuint(blob, pos)
        v_span_x, pos = read_varuint(blob, pos)
        v_span_y, pos = read_varuint(blob, pos)
    except Exception:
        if not complete:
            # 前缀不够 —— 让调用方多读点再来,别当成"没有包围盒"
            raise _IncompletePeek() from None
        # 布局与预期不符:交给完整的解码路径去报错/兜底
        return None

    scale = q.xy_scale
    xmin = v_min_x / scale + q.x_origin
    ymin = v_min_y / scale + q.y_origin
    pad = 4.0 / max(abs(scale), 1.0)
    return (xmin - pad, ymin - pad,
            xmin + v_span_x / scale + pad,
            ymin + v_span_y / scale + pad)


def _decode_at(blob: bytes, pos: int, geom_field: Any,
               has_z: bool, has_m: bool):
    """几何解码主入口。``pos`` 是几何 varuint 在 ``blob`` 中的位置。

    分派逻辑完全照抄 GDAL ``GetAsGeometry``
    (``filegdbtable.cpp`` 的 ``switch ((nGeomType & 0xff))``):
    每个"纯 Z"和"ZM"档都落到同一段代码,只在 case 里把 ``bHasZ``/``bHasM``
    置真。所以下面的类型集合必须与 ``_constants.ShapeType`` 一致。
    """
    q = _Quantizer(geom_field)
    try:
        geom_type, pos = read_varuint(blob, pos)
    except Exception:
        return None, pos

    b_has_z = (geom_type & EXT_SHAPE_Z_FLAG) != 0 or ST.has_z(geom_type & 0xFF)
    b_has_m = (geom_type & EXT_SHAPE_M_FLAG) != 0 or ST.has_m(geom_type & 0xFF)
    is_curve = (geom_type & EXT_SHAPE_CURVE_FLAG) != 0
    shp = geom_type & 0xFF

    if shp == SHPT_NULL:
        return None, pos

    # 热点数组解码(``_read_xy_array`` / ``_read_scalar_array``)为了速度把
    # 边界检查去掉了,截断的 blob 会直接抛 IndexError。在这里统一转成
    # GdbFormatError —— ``iter_rows`` 是按"单条记录损坏不中断整表"的口径
    # 写的,只 catch GdbFormatError;漏出去会让一条坏记录废掉整个迭代。
    try:
        if shp in ST._POINT_ALL:
            return _decode_point(blob, pos, q, b_has_z, b_has_m)

        if shp in ST._MULTIPOINT_ALL:
            return _decode_multipoint(blob, pos, q, b_has_z, b_has_m)

        if shp in ST._POLYLINE_ALL:
            return _decode_parts(blob, pos, q, b_has_z, b_has_m, is_curve,
                                 kind='polyline')

        if shp in ST._POLYGON_ALL:
            return _decode_parts(blob, pos, q, b_has_z, b_has_m, is_curve,
                                 kind='polygon')
    except IndexError:
        raise GdbFormatError(
            f'几何 blob 被截断(shape type {shp},起点 {pos},'
            f'总长 {len(blob)})'
        ) from None

    raise GdbFormatError(
        f'未知的 FileGDB shape type: {shp} (geom_type=0x{geom_type:x})'
    )


# ----------------------------------------------------------------------
def _decode_point(blob: bytes, pos: int, q: _Quantizer,
                  has_z: bool, has_m: bool) -> Geometry:
    """POINT:varuint x, varuint y[, z][, m]。

    量化值 0 表示"该维为空",还原成 NaN。注意这里带 ``-1``。
    """
    n = len(blob)
    x = y = 0
    x, pos = read_varuint(blob, pos)
    y, pos = read_varuint(blob, pos)

    dfx = math.nan if x == 0 else (x - 1) / q.xy_scale + q.x_origin
    dfy = math.nan if y == 0 else (y - 1) / q.xy_scale + q.y_origin

    shape_type = C.ShapeType.POINT
    coords: Tuple[float, ...] = (dfx, dfy)

    if has_z:
        z, pos = read_varuint(blob, pos)
        dfz = math.nan if z == 0 else (z - 1) / q.z_scale + q.z_origin
        coords = coords + (dfz,)
        shape_type = C.ShapeType.POINT_Z
        if has_m:
            m, pos = read_varuint(blob, pos)
            dfm = math.nan if m == 0 else (m - 1) / q.m_scale + q.m_origin
            coords = coords + (dfm,)
            shape_type = C.ShapeType.POINT_ZM
    elif has_m:
        m, pos = read_varuint(blob, pos)
        dfm = math.nan if m == 0 else (m - 1) / q.m_scale + q.m_origin
        coords = coords + (dfm,)
        shape_type = C.ShapeType.POINT_M

    return Geometry(shape_type=shape_type, coordinates=coords,
                       has_z=has_z, has_m=has_m), pos


# ----------------------------------------------------------------------
def _read_xy_array(blob: bytes, dr: _DeltaReader, n_points: int,
                   q: _Quantizer, dx: int, dy: int
                   ) -> Tuple[Any, int, int]:
    """读 n 个 XY 点(增量编码),返回交错的 ``array('d')``。对应 GDAL
    ``ReadXYArray``。

    增量累加器 ``dx/dy`` 由调用方保留,所以 part 之间是连续的。

    返回什么(以及为什么不是元组)
    ----------------------------
    返回的是 ``array('d')``,``(x0, y0, x1, y1, ...)`` **交错**存放 ——
    **不建逐点的** ``float`` / ``tuple``。

    实测:整层 89,935,418 个 varint,varint 循环本身只花 0.33 s,而把结果
    包成 4,496 万个 ``float`` + 2,121 万个 ``tuple`` 要再花 **1.7 s** ——
    占了这条路径 8 成以上。GDAL 的 ``OGRLineString`` 是一条 ``double*``,
    这笔钱一分不付,所以差距只能靠**换容器**抹平,改算法、改 IO 都够不着
    (见 DESIGN.md §2.19.5)。需要元组的调用方走
    :attr:`_datatypes.Geometry.coordinates`,那里按需物化。

    为什么这里写得这么"展开"
    --------------------------
    参照 GDAL 的 ``ReadVarIntAndAddNoCheck`` —— 它在 C++ 里是 ``inline`` 的,
    并且直接推着 ``const GByte*& pabyIter`` 走。Python 里没法和它比常数,
    但可以把**纯 Python 的开销**砍掉:

    * 把 :func:`read_varint_delta` **内联**进来 —— 原先每个坐标要 2 次
      函数调用 + 3 次元组建/拆;
    * 循环体里只做整数运算,``/ scale + origin`` 整个甩给循环外的
      ``map``,让浮点运算跑在 C 层(``operator.truediv`` / ``add`` 都是
      C 函数,``map`` 也是 C 循环),最后用**跨步切片赋值**一次性写进缓冲
      (``out[0::2] = ...`` 也是 C 层循环)。

    ``v / s + o`` 与 ``map(add, map(truediv, ...))`` 的运算顺序完全一致,
    结果逐位相同 —— 不是近似。

    等价于::

        for _ in range(n_points):
            dx, pos = delta(blob, pos, dx)
            dy, pos = delta(blob, pos, dy)
            xs.append(dx); ys.append(dy)
        out = array('d', [0.0]) * (2 * n_points)
        out[0::2] = array('d', (x / s + ox for x in xs))
        out[1::2] = array('d', (y / s + oy for y in ys))

    可选 C 加速
    ----------
    有 ``_gdbaccel`` 时直接把它交给 C 的 ``decode_xy_flat``
    (``pyopenfilegdb/_gdbaccel.c``),算法逐字相同、结果逐位相同,只是
    省掉了解释器逐字节的过路费。**C 版也直接返回 array('d')** —— 这不是
    "快版返回另一种东西",两条路的容器形状完全一样。回退是正常状态
    (见 :mod:`._accel`)。

    ⚠️ ``dr.pos`` 只在**成功返回之后**才写。C 版抛异常时游标必须原地不动,
    与下面这段"循环没走完就不赋值"的语义一致。
    """
    accel = _accel.decode_xy_flat
    if accel is not None:
        pts, end_pos, dx, dy = accel(blob, dr.pos, n_points, q.xy_scale,
                                     q.x_origin, q.y_origin, dx, dy)
        dr.pos = end_pos
        return pts, dx, dy

    pos = dr.pos
    xs: List[int] = []
    ys: List[int] = []
    x_append = xs.append
    y_append = ys.append

    for _ in range(n_points):
        # ---- X 增量 ----
        b = blob[pos]
        val = b & 0x3F
        neg = b & 0x40
        if b & 0x80:
            pos += 1
            shift = 6
            while True:
                b = blob[pos]
                pos += 1
                val |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
                if shift >= _MAX_SHIFT:
                    # 与 _gdbaccel.c 的 _MAX_SHIFT 同一个上限,保证两条路的
                    # 接受域严格相同。抛 IndexError 而非 GdbFormatError:
                    # _decode_at 会把 IndexError 归一成 GdbFormatError,这里
                    # 与 C 版保持一致,差分对拍才能直接比异常类型。
                    raise IndexError('delta-varint 过长(损坏的 blob)')
        else:
            pos += 1
        dx = dx - val if neg else dx + val
        x_append(dx)

        # ---- Y 增量 ----
        b = blob[pos]
        val = b & 0x3F
        neg = b & 0x40
        if b & 0x80:
            pos += 1
            shift = 6
            while True:
                b = blob[pos]
                pos += 1
                val |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
                if shift >= _MAX_SHIFT:
                    # 与 _gdbaccel.c 的 _MAX_SHIFT 同一个上限,保证两条路的
                    # 接受域严格相同。抛 IndexError 而非 GdbFormatError:
                    # _decode_at 会把 IndexError 归一成 GdbFormatError,这里
                    # 与 C 版保持一致,差分对拍才能直接比异常类型。
                    raise IndexError('delta-varint 过长(损坏的 blob)')
        else:
            pos += 1
        dy = dy - val if neg else dy + val
        y_append(dy)

    dr.pos = pos
    scale = q.xy_scale
    out = array('d', [0.0]) * (2 * n_points)     # 交错:(x0, y0, x1, y1, ...)
    out[0::2] = array('d', map(add, map(truediv, xs, repeat(scale)),
                               repeat(q.x_origin)))
    out[1::2] = array('d', map(add, map(truediv, ys, repeat(scale)),
                               repeat(q.y_origin)))
    return out, dx, dy


def _read_scalar_array(blob: bytes, dr: _DeltaReader, n_points: int,
                       scale: float, origin: float, acc: int
                       ) -> Tuple[Any, int]:
    """读 n 个 Z 或 M 值(增量编码),返回 ``array('d')``(不交错)。

    与 :func:`_read_xy_array` 同一套内联手法,也同一套容器口径(见那边的
    说明:容器形状与是否走 C 无关)。

    有 ``_gdbaccel.decode_scalar_flat`` 时交给 C,否则跑下面这段。Z / M
    共用本函数,只差 ``scale`` / ``origin``,累加器各自独立。
    """
    accel = _accel.decode_scalar_flat
    if accel is not None:
        vals, end_pos, acc = accel(blob, dr.pos, n_points, scale, origin, acc)
        dr.pos = end_pos
        return vals, acc

    pos = dr.pos
    ints: List[int] = []
    append = ints.append

    for _ in range(n_points):
        b = blob[pos]
        val = b & 0x3F
        neg = b & 0x40
        if b & 0x80:
            pos += 1
            shift = 6
            while True:
                b = blob[pos]
                pos += 1
                val |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
                if shift >= _MAX_SHIFT:
                    # 与 _gdbaccel.c 的 _MAX_SHIFT 同一个上限,保证两条路的
                    # 接受域严格相同。抛 IndexError 而非 GdbFormatError:
                    # _decode_at 会把 IndexError 归一成 GdbFormatError,这里
                    # 与 C 版保持一致,差分对拍才能直接比异常类型。
                    raise IndexError('delta-varint 过长(损坏的 blob)')
        else:
            pos += 1
        acc = acc - val if neg else acc + val
        append(acc)

    dr.pos = pos
    return array('d', map(add, map(truediv, ints, repeat(scale)),
                          repeat(origin))), acc


# ----------------------------------------------------------------------
def _decode_multipoint(blob: bytes, pos: int, q: _Quantizer,
                       has_z: bool, has_m: bool) -> Geometry:
    """MULTIPOINT:nPoints | 4 个 varuint(包围盒) | XY 数组 | [Z] | [M]。"""
    n_points, pos = read_varuint(blob, pos)
    shape_type = C.ShapeType.MULTIPOINT_ZM if (has_z and has_m) else \
        C.ShapeType.MULTIPOINT_Z if has_z else \
        C.ShapeType.MULTIPOINT_M if has_m else C.ShapeType.MULTIPOINT

    if n_points == 0:
        return Geometry(shape_type=shape_type, coordinates=[],
                           has_z=has_z, has_m=has_m), pos

    pos = _skip_varuint(blob, pos, 4)  # 包围盒

    dr = _DeltaReader(blob, pos)
    xy, dx, dy = _read_xy_array(blob, dr, n_points, q, 0, 0)
    pos = dr.pos

    zs = ms = None
    if has_z:
        dr.pos = pos
        zs, _ = _read_scalar_array(blob, dr, n_points, q.z_scale, q.z_origin, 0)
        pos = dr.pos

    if has_m and pos + n_points <= len(blob):
        dr.pos = pos
        ms, _ = _read_scalar_array(blob, dr, n_points, q.m_scale, q.m_origin, 0)
        pos = dr.pos

    return Geometry(shape_type=shape_type,
                       _flat=_FlatCoords([xy],
                                         None if zs is None else [zs],
                                         None if ms is None else [ms]),
                       has_z=has_z, has_m=has_m), pos


# ----------------------------------------------------------------------
def _decode_parts(blob: bytes, pos: int, q: _Quantizer, has_z: bool, has_m: bool,
                  is_curve: bool, kind: str) -> Geometry:
    """POLYLINE / POLYGON。

    布局::

        varuint nPoints
        [如果 multipatch: varuint 跳过]
        varuint nParts
        [如果有曲线描述: varuint nCurves]
        [4 个 varuint 包围盒]
        varuint * (nParts-1)     每个 part 的点数(最后一个不算)
        XY 增量数组(所有 part 的点首尾相接)
        [Z 增量数组]
        [M 增量数组]
    """
    n_points, pos = read_varuint(blob, pos)

    base = C.ShapeType.POLYGON if kind == 'polygon' else C.ShapeType.POLYLINE
    shape_type = C.ShapeType.make_zm(base, has_z, has_m)

    if n_points == 0:
        return Geometry(shape_type=shape_type, coordinates=([], []),
                           has_z=has_z, has_m=has_m), pos

    n_parts, pos = read_varuint(blob, pos)
    n_curves = 0
    if is_curve:
        n_curves, pos = read_varuint(blob, pos)

    if n_parts == 0:
        return Geometry(shape_type=shape_type, coordinates=([], []),
                           has_z=has_z, has_m=has_m), pos

    if is_curve and n_curves:
        # 圆弧/贝塞尔/椭圆段:本实现只解析直线段部分(见模块 docstring)。
        # 剩余部分交给 _decode_curve_fallback,它按直线段尽力还原。
        pass

    pos = _skip_varuint(blob, pos, 4)  # 包围盒

    # 每个 part 的点数;最后一个由总数减去前面之和得到
    part_point_counts: List[int] = []
    total = 0
    for _ in range(n_parts - 1):
        cnt, pos = read_varuint(blob, pos)
        part_point_counts.append(cnt)
        total += cnt
    part_point_counts.append(n_points - total)

    # 起点索引(本模块用"起始下标"表示 parts,便于上层使用)。这里只为了
    # 知道每个 part 有多少个点;真正给上层的 parts 由 flat 分块现算。
    dr = _DeltaReader(blob, pos)
    xy_parts: List[Any] = []
    dx = dy = 0
    for cnt in part_point_counts:
        part_xy, dx, dy = _read_xy_array(blob, dr, cnt, q, dx, dy)
        xy_parts.append(part_xy)
    pos = dr.pos

    zs_parts: Optional[List[Any]] = None
    ms_parts: Optional[List[Any]] = None

    if has_z:
        dr.pos = pos
        dz = 0
        zs_parts = []
        for cnt in part_point_counts:
            part_z, dz = _read_scalar_array(blob, dr, cnt, q.z_scale,
                                            q.z_origin, dz)
            zs_parts.append(part_z)
        pos = dr.pos

    if has_m:
        # M 可能整体缺失,此时数据里只有一个标记字节(值 66)。
        # GDAL 的做法:只有当剩余字节数 >= 点数时才尝试解析。
        if pos < len(blob) and blob[pos] == _NO_M_MARKER:
            pass
        elif pos + n_points <= len(blob):
            dr.pos = pos
            dm = 0
            ms_parts = []
            for cnt in part_point_counts:
                part_m, dm = _read_scalar_array(blob, dr, cnt, q.m_scale,
                                                q.m_origin, dm)
                ms_parts.append(part_m)
            pos = dr.pos

    if kind == 'polygon':
        xy_parts, zs_parts, ms_parts = _strip_part_closures_flat(
            xy_parts, zs_parts, ms_parts)

    return Geometry(shape_type=shape_type,
                       _flat=_FlatCoords(xy_parts, zs_parts, ms_parts),
                       has_z=has_z, has_m=has_m), pos


def _flat_ring_closed(a: Any) -> bool:
    """``array('d')`` 交错缓冲的首尾两点 XY 是否重合(即环是闭合的)。

    判据与元组版 :func:`_strip_part_closures` 里那句
    ``len(seg) > 1 and seg[0][:2] == seg[-1][:2]`` **逐字对应**:只比 XY,
    Z/M 不参与(环闭合是 XY 层面的性质)。
    """
    n = len(a) // 2
    return n > 1 and a[0] == a[2 * n - 2] and a[1] == a[2 * n - 1]


def _strip_part_closures_flat(xy_parts: List[Any],
                              zs_parts: Optional[List[Any]],
                              ms_parts: Optional[List[Any]]
                              ) -> Tuple[List[Any], Optional[List[Any]],
                                         Optional[List[Any]]]:
    """flat 版:逐环削掉末尾重复的闭合点。

    与元组版 :func:`_strip_part_closures` 是同一件事、同一判据,区别只在
    表示:那边是"把整个点序列重建成两个新 list",这边每块自己
    ``[:-2]`` 一下就行 —— 逐点搬运整个没了,只剩逐 part 的切片。
    """
    new_xy: List[Any] = []
    new_zs = None if zs_parts is None else []
    new_ms = None if ms_parts is None else []
    for i, a in enumerate(xy_parts):
        closed = _flat_ring_closed(a)
        new_xy.append(a[:-2] if closed else a)
        if zs_parts is not None:
            new_zs.append(zs_parts[i][:-1] if closed else zs_parts[i])
        if ms_parts is not None:
            new_ms.append(ms_parts[i][:-1] if closed else ms_parts[i])
    return new_xy, new_zs, new_ms


def _strip_part_closures(part_starts: List[int],
                         points: List[Tuple[float, ...]]
                         ) -> Tuple[List[int], List[Tuple[float, ...]]]:
    """去掉每个环末尾那个重复的闭合点,返回新的 ``(part_starts, points)``。

    ArcGIS 往盘上写多边形时环是 **闭合** 的(首尾点相同;实测 1746/1746 个
    环都是这样,无一例外),但本库 :class:`Geometry` 的内部约定是环
    **不闭合**(见该类 docstring:"首尾点不重复")。所以在解码的最后一步统一
    削掉,写回时由 :func:`_close_parts` 补回来 —— 盘上形态与 ArcGIS 一致,
    内部形态则与 :func:`from_wkt` 一致。

    判重只比 XY(与 :func:`_strip_ring_closure` 口径相同):环闭合是 XY 层面
    的性质,Z/M 理论上可以不同。
    """
    if not part_starts:
        return part_starts, points
    new_starts: List[int] = []
    new_points: List[Tuple[float, ...]] = []
    n = len(part_starts)
    for i, start in enumerate(part_starts):
        end = part_starts[i + 1] if i + 1 < n else len(points)
        seg = points[start:end]
        if len(seg) > 1 and seg[0][:2] == seg[-1][:2]:
            seg = seg[:-1]
        new_starts.append(len(new_points))
        new_points.extend(seg)
    return new_starts, new_points


# ============================================================================
# WKT 输出(便于调试/与人比对)
# ============================================================================
def _fmt(v: float) -> str:
    if v != v:
        return 'NaN'
    if v == int(v) and abs(v) < 1e15:
        return str(int(v))
    return repr(v)


def to_wkt(geom: Optional[Geometry]) -> str:
    """``Geometry`` -> OGC WKT。

    ⚠️ 读的是 :attr:`Geometry.xy_parts`,**不碰** ``coordinates`` —— 后者会把
    flat 存储整层物化成逐点元组(值 ~19 ns/顶点,见 DESIGN.md §2.19.5),
    而 WKT 本来就要逐点格式化,再先建一遍元组是白花钱。

    ⚠️ 与旧版相比的三处行为变化:

    1. **多壳面输出 ``MULTIPOLYGON``。** 旧版把面的所有环塞进一个
       ``POLYGON(...)``,多壳时产出的是"一个面里有多个外环"的**非法** WKT。
       环的归属按 :meth:`Geometry._ring_groups`(即 GDAL
       ``OGRGeometryFactory::organizePolygons``)还原。
    2. **非空 multipatch 抛 ``NotImplementedError``**,不再谎报成
       ``GEOMETRYCOLLECTION EMPTY`` —— 把一个非空几何导出成"空"是撒谎。
    3. **Z / M 按存储原样输出,并且写出 ``Z`` / ``M`` / ``ZM`` 维度后缀**
       (与 GDAL 的 ``OGRGeometry::exportToWkt`` 一致)。⚠️ 光输出第三个分量
       是不够的:``LINESTRING (0 0 7, 1 1 8)`` 在 WKT1 里被理解为 **Z**,带 M
       的几何往返一次维度就变了 —— 本库的 ``from_wkt`` 和 GDAL 都这么理解。

    ``from_wkt`` 本来就把 ``MULTIPOLYGON`` 摊成 parts,所以**往返仍然成立**。
    """
    if geom is None:
        return 'GEOMETRYCOLLECTION EMPTY'

    kind = geom.kind
    if kind == 'multipatch':
        raise NotImplementedError(
            'multipatch 不能导出成 WKT:FileGDB 存的是 triangle strip / '
            'triangle fan 的片段流,而 WKT 没有对应的几何类型,硬凑要自己编'
            '一套约定。见 DESIGN.md §2.21。')

    if kind == 'geometrycollection':
        kids = geom.geometries()
        if not kids:
            return 'GEOMETRYCOLLECTION EMPTY'
        # ⚠️ **外层不写 ``Z`` / ``M`` 后缀。** WKT1 的 ``GEOMETRYCOLLECTION Z
        # (...)`` 表示"里面**每个**子几何都是 Z",而本库的 GC 允许混合维度
        # (``POLYGON ∪ 面外的 POINT`` 出来的就是 2D + 2D)。写上去的话,
        # ``from_wkt`` 会把一个 2D 子几何按 Z 读回来、Z 分量填 NaN —— 一次
        # 往返就把几何改坏了。**每个子几何自己写自己的后缀**,读侧各自的
        # 后缀优先,于是混合维度也能原样往返。
        # (GDAL 的 ``exportToWkt`` 在 GC 上是写外层后缀的,这是本库与它的一处
        # 刻意分岔 —— 本库选择"能往返"而不是"与 GDAL 的字符串一致"。)
        return ('GEOMETRYCOLLECTION ('
                + ', '.join(to_wkt(k) for k in kids) + ')')

    # ⚠️ **维度后缀必须写出来。** 只把第三个分量打出来是不够的:
    # ``LINESTRING (0 0 7, 1 1 8)`` 在 WKT1 里被理解成 **Z**,而这个几何是
    # **M** —— 本库自己的 ``from_wkt`` 读回来就会变成 Z,GDAL / PostGIS 同此,
    # 于是带 M 的几何往返一次维度就变了。GDAL 导出时是带后缀的
    # (``OGRGeometry::exportToWkt`` 里的 ``WktType`` 拼装),这里跟它一致。
    if geom.has_z and geom.has_m:
        tag = ' ZM'
    elif geom.has_z:
        tag = ' Z'
    elif geom.has_m:
        tag = ' M'
    else:
        tag = ''

    # 空几何按**自己的类型**渲染。
    # ⚠️ 这里曾经是一句无条件的 ``return 'GEOMETRYCOLLECTION EMPTY'``,把下面
    #    那些按 kind 分的分支全变成了死代码。后果实打实:overlay 的空结果本来
    #    就该是 ``POLYGON EMPTY`` / ``LINESTRING EMPTY`` / ``POINT EMPTY``
    #    (``_overlay_ops._empty_result`` 造的就是这三种 kind),导出来却一律成了
    #    ``GEOMETRYCOLLECTION EMPTY`` —— 类型变了,喂给 GEOS 一比就是"成员表空"
    #    对一个空成员。``null``(``ST.NULL`` 那种真的没有类型的空几何)才回
    #    ``GEOMETRYCOLLECTION EMPTY``,那是原来就对的用法。
    if geom.is_empty:
        return ({'point': 'POINT', 'multipoint': 'MULTIPOINT',
                 'polyline': 'LINESTRING', 'polygon': 'POLYGON'}
                .get(kind, 'GEOMETRYCOLLECTION') + tag + ' EMPTY')

    parts = geom.xy_parts
    zs = geom.z_parts
    ms = geom.m_parts

    def z_at(i):
        return None if zs is None else zs[i]

    def m_at(i):
        return None if ms is None else ms[i]

    def seq_of(i, close):
        a = parts[i]
        # ⚠️ **不带括号** —— WKT 里边和环就是"点、逗号、点"。
        # 用 ``_pt_wkt_flat`` 会产出 ``LINESTRING ((0 0), (10 10))`` 和
        # ``POLYGON (((0 0), ...))``,两者都是**非法 WKT**,连本库自己的
        # ``from_wkt`` 都读不回来(实测)。
        body = ', '.join(_pt_wkt_bare(a, k, z_at(i), m_at(i))
                         for k in range(len(a) // 2))
        if close and len(a) >= 2:
            # OGC WKT 要求环闭合;内存里的环不存闭合点
            body += ', ' + _pt_wkt_bare(a, 0, z_at(i), m_at(i))
        return '(' + body + ')'

    if kind == 'point':
        if not parts or not parts[0]:
            return 'POINT' + tag + ' EMPTY'
        return ('POINT' + tag + ' '
                + _pt_wkt_flat(parts[0], 0, z_at(0), m_at(0)))

    if kind == 'multipoint':
        if not parts or not parts[0]:
            return 'MULTIPOINT' + tag + ' EMPTY'
        a = parts[0]
        return ('MULTIPOINT' + tag + ' (' + ', '.join(
            _pt_wkt_flat(a, k, z_at(0), m_at(0))
            for k in range(len(a) // 2)) + ')')

    if kind not in ('polyline', 'polygon'):
        return 'GEOMETRYCOLLECTION EMPTY'

    if not parts or geom.point_count == 0:
        return (('POLYGON' if kind == 'polygon' else 'LINESTRING')
                + tag + ' EMPTY')

    if kind == 'polyline':
        segs = [seq_of(i, False) for i in range(len(parts))]
        if len(segs) == 1:
            return 'LINESTRING' + tag + ' ' + segs[0]
        return 'MULTILINESTRING' + tag + ' (' + ', '.join(segs) + ')'

    polys = []
    for shell, holes in geom._ring_groups():
        polys.append('(' + ', '.join([seq_of(shell, True)] +
                                     [seq_of(h, True) for h in holes]) + ')')
    if len(polys) == 1:
        return 'POLYGON' + tag + ' ' + polys[0]
    return 'MULTIPOLYGON' + tag + ' (' + ', '.join(polys) + ')'


def _pt_wkt_coords(a: Any, i: int, z: Any, m: Any) -> List[str]:
    """交错坐标数组里第 ``i`` 个点的**分量串** ``['0', '0']``。

    ``z`` / ``m`` 是**同一个 part** 的标量数组(没有就是 ``None``)。
    只到分量这一层,加不加括号由调用方决定 —— 见下面两个包装函数。
    """
    v = [_fmt(a[2 * i]), _fmt(a[2 * i + 1])]
    if z is not None:
        v.append(_fmt(z[i]))
    if m is not None:
        v.append(_fmt(m[i]))
    return v


def _pt_wkt_flat(a: Any, i: int, z: Any, m: Any) -> str:
    """**带**括号的一个点 ``'(0 0)'``。

    ⚠️ 只有 ``POINT`` 和 ``MULTIPOINT`` 用这个 —— 这两种类型的 WKT 里点
    本身就是带括号的元素。**点序列(LINESTRING / 环)不准用它**,见
    :func:`_pt_wkt_bare`。
    """
    return '(' + ' '.join(_pt_wkt_coords(a, i, z, m)) + ')'


def _pt_wkt_bare(a: Any, i: int, z: Any, m: Any) -> str:
    """**不带**括号的一个点 ``'0 0'`` —— 点序列(线、环)里的元素形态。"""
    return ' '.join(_pt_wkt_coords(a, i, z, m))


# ============================================================================
# GeoJSON
#
# 与 WKT 同一层的"几何 -> 文本"出口。对应 GDAL 的 ``OGR_G_ExportToJson``
# (``ogr/ogrgeometry.cpp`` 里手写的 ``OGRGeoJSONWriteGeometry``)。
#
# ⚠️ 与 RFC 7946 的两处**刻意**不一致(两处都跟 GDAL 一致):
#
# 1. **不重绕环。** RFC 7946 §3.1.6 要求外环逆时针、内环顺时针,而 Esri
#    (以及本库)是**外环顺时针**,恰好相反。GDAL 同样照原样导出,只有
#    GeoJSON **驱动**的 ``RFC7946=YES`` 选项才重绕。需要合规的消费者请自己
#    重绕 —— 在这里隐形改写比不合规更糟(HASH 值、与 WKT 的一致性都会变)。
# 2. **M 没有位置。** Z 写在第三个分量(RFC 7946 允许),M 无处可去,导出时
#    丢弃。⚠️ 推论:**带 M 的几何往返不回来** —— :meth:`Geometry.__eq__` 比
#    ``has_m``,而 GeoJSON 里根本没有 M 的位置。``from_geojson`` 因此留了
#    ``has_m`` 参数,让调用方能显式要回一个 M 维几何(值只能是 NaN)。
#
# 另外**不做四舍五入**:坐标在上游已经量化过了,再压精度只是白丢信息。
# ============================================================================
def to_geo_dict(geom: Optional[Geometry]) -> Dict[str, Any]:
    """``Geometry`` -> GeoJSON 的 ``geometry`` 对象(dict)。

    空/``None`` 几何返回 ``{'type': 'GeometryCollection', 'geometries': []}``
    —— 这是 GDAL ``OGR_G_ExportToJson`` 对空几何的做法,也是 GeoJSON 里
    唯一能表达"什么都没有"的类型。

    ``kind`` -> 类型的映射(多 part 时升级成 Multi):point → ``Point``,
    multipoint → ``MultiPoint``,polyline → ``LineString`` /
    ``MultiLineString``,polygon → ``Polygon`` / ``MultiPolygon``。

    这是 :attr:`Geometry.__geo_interface__` 的落点 —— Python 地理生态
    (geopandas / folium / shapely)直接认这个属性。
    """
    if geom is None or geom.is_empty:
        return {'type': 'GeometryCollection', 'geometries': []}

    kind = geom.kind
    if kind == 'multipatch':
        raise NotImplementedError(
            'multipatch 不能导出成 GeoJSON:FileGDB 存的是 triangle strip / '
            'triangle fan 的片段流,与 GeoJSON 的几何模型对不上。'
            '见 DESIGN.md §2.21。')

    if kind == 'geometrycollection':
        # GeoJSON **本来就有** GeometryCollection(而且是唯一能装混合维度
        # 的类型),所以这一档不需要任何约定,直接递归。
        return {'type': 'GeometryCollection',
                'geometries': [to_geo_dict(k) for k in geom.geometries()]}

    parts = geom.xy_parts
    zs = geom.z_parts            # M 不在这里 —— 导出时按约定丢弃

    def z_at(i):
        return None if zs is None else zs[i]

    def seq_of(i, close):
        a = parts[i]
        z = z_at(i)
        pts = [_pt_json(a, k, z) for k in range(len(a) // 2)]
        if close and pts:
            pts.append(list(pts[0]))
        return pts

    if kind == 'point':
        return {'type': 'Point',
                'coordinates': [] if not parts or not parts[0]
                else _pt_json(parts[0], 0, z_at(0))}

    if kind == 'multipoint':
        if not parts or not parts[0]:
            return {'type': 'MultiPoint', 'coordinates': []}
        a = parts[0]
        return {'type': 'MultiPoint',
                'coordinates': [_pt_json(a, k, z_at(0))
                                for k in range(len(a) // 2)]}

    if kind not in ('polyline', 'polygon'):
        return {'type': 'GeometryCollection', 'geometries': []}

    if not parts or geom.point_count == 0:
        return {'type': 'Polygon' if kind == 'polygon' else 'LineString',
                'coordinates': []}

    if kind == 'polyline':
        segs = [seq_of(i, False) for i in range(len(parts))]
        if len(segs) == 1:
            return {'type': 'LineString', 'coordinates': segs[0]}
        return {'type': 'MultiLineString', 'coordinates': segs}

    polys = [[seq_of(shell, True)] + [seq_of(h, True) for h in holes]
             for shell, holes in geom._ring_groups()]
    if len(polys) == 1:
        return {'type': 'Polygon', 'coordinates': polys[0]}
    return {'type': 'MultiPolygon', 'coordinates': polys}


def to_geojson(geom: Optional[Geometry]) -> str:
    """``Geometry`` -> GeoJSON 字符串(只有 geometry,没有 Feature 外壳)。

    ``ensure_ascii=False`` —— 坐标里不会有非 ASCII,但这样将来加 ``id`` /
    属性时不必再改一次。

    ⚠️ ``json.dumps`` 用的是 ``repr`` 的浮点格式(最短往返表示),所以
    **坐标是逐位精确的**,不会因为转成 JSON 而丢精度。空值会写成裸的
    ``NaN``(Python 的扩展,严格 JSON 不认;GeoJSON 生态普遍接受,GDAL 也
    这么写)。空白字符的排布不属于契约,别拿字符串做逐字节比对。
    """
    return json.dumps(to_geo_dict(geom), ensure_ascii=False)


def _pt_json(a: Any, i: int, z: Any) -> List[float]:
    """交错坐标数组里第 ``i`` 个点的 GeoJSON 坐标数组。"""
    if z is None:
        return [a[2 * i], a[2 * i + 1]]
    return [a[2 * i], a[2 * i + 1], z[i]]


#: GeoJSON 类型名 -> (Esri 基础 shape type, 是否需要"多部分"的嵌套)。
_GEOJSON_TYPES = {
    'Point': ST.POINT,
    'MultiPoint': ST.MULTIPOINT,
    'LineString': ST.POLYLINE,
    'MultiLineString': ST.POLYLINE,
    'Polygon': ST.POLYGON,
    'MultiPolygon': ST.POLYGON,
}


def _geojson_parts(type_name: str, coords: Any, where: str
                   ) -> Tuple[List[List[Tuple[float, ...]]], List[bool], int]:
    """把 GeoJSON 的 ``coordinates`` 归一成"part 列表 + 角色 + 分量数"。

    归一的目标是 :class:`Geometry` 的存储形态(环不闭合、多面摊成 parts),
    以及**结构上**能确定的环角色 —— GeoJSON 是显式分层的,不像 Esri 那样
    只有绕向,所以这里直接给出 ``shells``。
    """
    def pt(raw: Any, w: str) -> Tuple[float, ...]:
        if not isinstance(raw, (list, tuple)) or len(raw) < 2:
            raise GdbFormatError(f'{w}: 一个坐标至少要有 x、y 两个数')
        if len(raw) > 3:
            raise GdbFormatError(
                f'{w}: 一个坐标最多 3 个分量(x, y, z);RFC 7946 里没有第四'
                f'个分量的位置,实得 {len(raw)} 个')
        return tuple(float(v) for v in raw)

    def line(raw: Any, w: str, least: int) -> List[Tuple[float, ...]]:
        if not isinstance(raw, (list, tuple)):
            raise GdbFormatError(f'{w}: 期望一个坐标数组')
        if len(raw) < least:
            raise GdbFormatError(f'{w}: 至少要有 {least} 个顶点,实得 {len(raw)} 个')
        return [pt(p, w) for p in raw]

    def ring(raw: Any, w: str) -> List[Tuple[float, ...]]:
        pts = line(raw, w, 3)
        # GeoJSON 的环首尾闭合;本库的内存表示**不闭合**
        if pts[0] == pts[-1]:
            pts.pop()
        if len(pts) < 3:
            raise GdbFormatError(f'{w}: 去掉闭合点后只剩 {len(pts)} 个顶点,'
                                 f'环至少要 3 个')
        return pts

    if type_name == 'Point':
        if not coords:
            return [], [], 0
        return [[pt(coords, where)]], [], len(coords)

    if type_name == 'MultiPoint':
        pts = line(coords, where, 1)
        return [pts], [], len(pts[0])

    if type_name == 'LineString':
        pts = line(coords, where, 2)
        return [pts], [], len(pts[0])

    if type_name == 'MultiLineString':
        if not isinstance(coords, (list, tuple)):
            raise GdbFormatError(f'{where}: 期望一个坐标数组')
        parts = [line(g, f'{where} 第 {i + 1} 段', 2)
                 for i, g in enumerate(coords)]
        return parts, [], (len(parts[0][0]) if parts else 0)

    if type_name == 'Polygon':
        if not isinstance(coords, (list, tuple)):
            raise GdbFormatError(f'{where}: 期望一个环数组')
        parts = [ring(g, f'{where} 第 {i + 1} 个环')
                 for i, g in enumerate(coords)]
        return parts, [True] + [False] * (len(parts) - 1), (
            len(parts[0][0]) if parts else 0)

    # MultiPolygon
    if not isinstance(coords, (list, tuple)):
        raise GdbFormatError(f'{where}: 期望一个"面的环"数组')
    parts = []
    shells: List[bool] = []
    width = 0
    for i, polygon in enumerate(coords):
        if not isinstance(polygon, (list, tuple)) or not polygon:
            raise GdbFormatError(f'{where} 第 {i + 1} 个面: 至少要有一个环')
        for j, g in enumerate(polygon):
            pts = ring(g, f'{where} 第 {i + 1} 个面第 {j + 1} 个环')
            if not width:
                width = len(pts[0])
            parts.append(pts)
            shells.append(j == 0)
    return parts, shells, width


def from_geojson(data: Any, has_m: bool = False) -> Geometry:
    """GeoJSON(字符串或 dict)-> :class:`Geometry`。

    认 ``Point`` / ``MultiPoint`` / ``LineString`` / ``MultiLineString`` /
    ``Polygon`` / ``MultiPolygon`` / 空的 ``GeometryCollection``,以及
    ``Feature`` 外壳(取里面的 ``geometry``)。格式不对一律抛
    :class:`GdbFormatError`,不做"猜一猜"的容错。

    三个**必须知道**的口径:

    1. **第三个分量一律当 Z 读。** RFC 7946 规定如此,没有"这是 M"的表示法。
       ``has_m=True`` 不是"把 Z 读成 M",而是给每个点**追加一个 NaN 的 M**,
       好让 M 维几何的 ``shape_type`` 能往返(值回不来)。
    2. **环不要求首尾闭合,但闭了就削掉。** RFC 7946 要求环闭合,本库的内存
       表示**不闭合**,所以闭合点在这里去掉。反过来,没闭合的环也照收 ——
       真实世界里的 GeoJSON 常有不闭合的环,而本库的表示本来就不需要它。
       收下之后仍然要求"去掉闭合点后 ≥ 3 个顶点"。
    3. **环角色按结构定,不按绕向。** GeoJSON 是显式分层的(第 0 个环是外环),
       所以 ``_shells`` 直接记下来,不用 ``organizePolygons`` 的绕向推断
       —— 这也顺带避免了"OGC 绕向恰好与 Esri 相反"引发的误判。

    ⚠️ 是 **``FeatureCollection``** 的话,只有空的那种能转成几何(返回空几何),
    非空的抛错 —— 一个几何装不下一堆要素,该用 :class:`GdbLayer`。
    """
    obj = data
    if isinstance(obj, (str, bytes, bytearray)):
        try:
            obj = json.loads(obj)
        except ValueError as exc:
            raise GdbFormatError(f'GeoJSON 解析失败: {exc}') from None
    if not isinstance(obj, dict):
        raise GdbFormatError(
            f'GeoJSON 顶层必须是对象(dict 或 JSON 串),得到 '
            f'{type(obj).__name__}')

    type_name = obj.get('type')
    if type_name == 'Feature':
        obj = obj.get('geometry')
        if obj is None:
            return Geometry()
        if not isinstance(obj, dict):
            raise GdbFormatError('Feature.geometry 必须是对象或 null')
        type_name = obj.get('type')
    elif type_name == 'FeatureCollection':
        features = obj.get('features') or []
        if features:
            raise GdbFormatError(
                f'FeatureCollection 里有 {len(features)} 个要素,装不进一个'
                f'Geometry —— 请逐个 from_geojson(),或者用 GdbLayer 写整层')
        return Geometry()

    if type_name == 'GeometryCollection':
        geometries = obj.get('geometries') or []
        if not geometries:
            return Geometry()
        # GeoJSON 本来就有 GeometryCollection,递归解析即可 —— 里面是什么维度
        # 都行(这正是 GC 存在的理由)。⚠️ 空的 GC 仍然解析成空几何(NULL 类型),
        # 与 WKT 那边同口径:全库的"空几何"只有一个表示。
        return Geometry.geometry_collection(
            [from_geojson(g, has_m=has_m) for g in geometries])

    base = _GEOJSON_TYPES.get(type_name)
    if base is None:
        raise GdbFormatError(
            f'不支持的 GeoJSON 类型 {type_name!r};支持: '
            f"{', '.join(sorted(_GEOJSON_TYPES))}、空 GeometryCollection / "
            f'Feature')

    coords = obj.get('coordinates')
    parts, shells, width = _geojson_parts(type_name, coords, type_name)
    if not parts or width == 0:
        return Geometry()

    has_z = width >= 3
    if has_m:
        # M 在 GeoJSON 里没有位置,只能补 NaN —— 目的是让 shape_type 往返,
        # 不是让值往返。
        parts = [[p + (float('nan'),) for p in group] for group in parts]

    shape_type = ST.make_zm(base, has_z, has_m)
    if base == ST.POINT:
        return Geometry(shape_type=shape_type, coordinates=parts[0][0],
                        has_z=has_z, has_m=has_m)
    if base == ST.MULTIPOINT:
        return Geometry(shape_type=shape_type, coordinates=list(parts[0]),
                        has_z=has_z, has_m=has_m)

    offsets: List[int] = []
    all_points: List[Tuple[float, ...]] = []
    for group in parts:
        offsets.append(len(all_points))
        all_points.extend(group)
    return Geometry(shape_type=shape_type,
                    coordinates=(offsets, all_points),
                    has_z=has_z, has_m=has_m,
                    _shells=shells if base == ST.POLYGON else None)


# ============================================================================
# WKT 解析(写路径的便捷入口)
#
# 这是个 **够用就好** 的解析器:只认 POINT / MULTIPOINT / LINESTRING /
# MULTILINESTRING / POLYGON / MULTIPOLYGON 六种,支持 Z / M / ZM 后缀和
# ``EMPTY``。不做坐标系定义解析(PROJCS/GEOGCS 这种由 WKT 描述坐标系,
# 不描述几何,不属于本函数职责)。
#
# 与 Esri 表示的三个转换点:
#   1. **环要拆开 + 去掉重复的闭合点** —— WKT 的环首尾点相同,而本库
#      :class:`Geometry` 内部表示不闭合。注意 Esri **盘上** 存的环是
#      闭合的,闭合点的增删由解码末端的 `_strip_part_closures` 与编码开头的
#      `_close_parts` 负责,这一层只跟内部表示打交道。
#   2. **环方向**:Esri 要求外环顺时针、内环逆时针。这里 **不** 做调整,
#      交给 :func:`encode_geometry` 统一处理(GDAL 也是在编码时按
#      ``bReverseOrder = bFirstRing != bIsClockwise`` 整环反转)。
#   3. **MULTIPOLYGON 要压成 POLYGON** —— Esri 没有"多面"这个类型,多个
#      面的所有环放进同一个 POLYGON 的 parts 里就行(GDAL 在
#      ``wkbPolygon->wkbMultiPolygon`` 那一步做的正是同类归并)。
# ============================================================================
_WKT_TOKEN_RE = re.compile(
    r'\(|\)|,'
    r'|[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?'
    r'|[-+]?(?:nan|inf(?:inity)?)',
    re.IGNORECASE,
)

#: WKT 类型名 -> Esri 基础 shape type。
_WKT_TYPES = {
    'POINT': ST.POINT,
    'MULTIPOINT': ST.MULTIPOINT,
    'LINESTRING': ST.POLYLINE,
    'MULTILINESTRING': ST.POLYLINE,
    'POLYGON': ST.POLYGON,
    'MULTIPOLYGON': ST.POLYGON,
}


def _tokenize_wkt(text: str) -> List[str]:
    """把 WKT 正文切成 ``(`` / ``)`` / ``,`` / 数字 四类记号。"""
    tokens: List[str] = []
    pos = 0
    while pos < len(text):
        ch = text[pos]
        if ch in ' \t\r\n':
            pos += 1
            continue
        match = _WKT_TOKEN_RE.match(text, pos)
        if match is None:
            raise GdbWriteError(f'WKT 里有无法识别的字符 {ch!r}(位置 {pos})')
        tokens.append(match.group(0))
        pos = match.end()
    return tokens


def _wkt_number(token: str) -> float:
    low = token.lower()
    if 'nan' in low:
        return float('nan')
    if 'inf' in low:
        return float('-inf') if low.startswith('-') else float('inf')
    return float(token)


def _parse_wkt_group(tokens: Sequence[str], pos: int
                     ) -> Tuple[List[Any], int]:
    """解析一个括号组,返回 ``(条目列表, 新位置)``。

    条目按 **顶层逗号** 切分,每个条目要么是嵌套组(列表),要么是一串
    空格分隔的数(坐标)。这样 ``(1 2, 3 4)`` 得到 ``[[1,2],[3,4]]``,
    而 ``((1 2), (3 4))`` 得到 ``[[[1,2]], [[3,4]]]`` —— 后者由
    :func:`_wkt_coord` 拆掉多余的一层。
    """
    if pos >= len(tokens) or tokens[pos] != '(':
        raise GdbWriteError('WKT 语法错误:期望 "("')
    pos += 1
    entries: List[Any] = []
    while True:
        if pos >= len(tokens):
            raise GdbWriteError('WKT 语法错误:括号没有闭合')
        token = tokens[pos]
        if token == ')':
            pos += 1
            return entries, pos
        if token == ',':
            pos += 1
            continue
        if token == '(':
            sub, pos = _parse_wkt_group(tokens, pos)
            entries.append(sub)
            continue
        # 一串空格分隔的数字 = 一个坐标
        numbers: List[float] = []
        while pos < len(tokens) and tokens[pos] not in '(),':
            numbers.append(_wkt_number(tokens[pos]))
            pos += 1
        if not numbers:
            raise GdbWriteError(f'WKT 语法错误:位置 {pos} 处出现 {token!r}')
        entries.append(numbers)


def _dig_to_coord(entry: Any) -> List[Any]:
    """一路下钻到最里层的坐标数字列表(只为嗅探分量个数,不做校验)。"""
    while isinstance(entry, list) and entry and isinstance(entry[0], list):
        entry = entry[0]
    return entry if isinstance(entry, list) else []


def _wkt_depth(entry: Any) -> int:
    """列表的嵌套层数(碰到标量就停)。

    ``[[1, 2], [3, 4]]`` 是 2,``[[[1, 2], [3, 4]]]`` 是 3。用来区分
    ``(0 0, 1 1)``(一条线)和 ``((0 0, 1 1))``(一条线的一个 part)、以及
    ``MULTIPOLYGON`` 里的"面的环"与"环"。
    """
    depth = 0
    while isinstance(entry, list) and entry:
        entry = entry[0]
        depth += 1
    return depth


def _wkt_coord(entry: Any, where: str) -> List[float]:
    """把一个条目归一成坐标数字列表,容忍 ``((1 2))`` 这种多包一层。"""
    while (isinstance(entry, list) and len(entry) == 1
           and isinstance(entry[0], list)):
        entry = entry[0]
    if not isinstance(entry, list) or not entry or isinstance(entry[0], list):
        raise GdbWriteError(f'{where}: 这里应该是一个坐标点,实得 {entry!r}')
    return [float(v) for v in entry]


def _wkt_dimensions(suffix: str, has_z: bool, has_m: bool,
                    n_values: int) -> Tuple[bool, bool]:
    """决定 ``(has_z, has_m)``。

    优先级:WKT 的 ``Z``/``M``/``ZM`` 后缀 > 显式参数 > 从坐标值个数推断。

    前两条路径下,声明的维数是 **硬** 的 —— 坐标没给够的分量由
    :func:`_wkt_point_tuple` 用 NaN 补(Esri 自己就用 NaN 表示"这个点没有
    Z / 没有 M")。这样返回的 ``shape_type`` 与调用方的声明一致,不会
    悄悄把 M 图层降级成非 M 图层。只有第三条"靠数个数猜"的路径才可能
    给出 ``(False, False)``。
    """
    if suffix:
        return 'Z' in suffix, 'M' in suffix
    if has_z or has_m:
        return has_z, has_m
    if n_values >= 4:
        return True, True
    if n_values == 3:
        # 无后缀的三值坐标:WKT1 里极少见,按 Z 解释(与 GEOS/PROJ 一致)
        return True, False
    return False, False


def _wkt_point_tuple(values: Sequence[float], has_z: bool, has_m: bool,
                     where: str) -> Tuple[float, ...]:
    """按 Z/M 标志把数字列表排成 ``(x, y[, z][, m])``。

    分量不够就用 NaN 补上(编码侧会把 NaN 写成 Esri 的"该维为空"),
    多出来的忽略。只有连 ``x, y`` 都凑不齐才算真错误。
    """
    if len(values) < 2:
        raise GdbWriteError(
            f'{where}: 坐标至少要 2 个分量(x, y),实得 {len(values)} 个'
        )
    out = [float(values[0]), float(values[1])]
    i = 2
    for present in (has_z, has_m):
        if present:
            out.append(float(values[i]) if i < len(values) else math.nan)
            i += 1
    return tuple(out)


def _strip_ring_closure(points: List[Tuple[float, ...]], where: str
                        ) -> List[Tuple[float, ...]]:
    """去掉环的重复闭合点(只比 XY,与 Esri/GDAL 的判重口径一致)。"""
    if len(points) > 1 and points[0][:2] == points[-1][:2]:
        points = points[:-1]
    if len(points) < 3:
        raise GdbWriteError(
            f'{where}: 环至少要 3 个不同的顶点(去掉闭合点后),实得 {len(points)}'
        )
    return points


def _split_wkt_children(body: str) -> List[str]:
    """把 ``GEOMETRYCOLLECTION ( ... )`` 的**括号里面**按顶层逗号切开。

    ``body`` 是**不含**最外层那对括号的内容。返回每个子几何的 WKT 文本
    (已经 strip),交给 :func:`from_wkt` 递归。

    ⚠️ 必须按**括号深度**切,不能直接 ``body.split(',')`` —— 子几何自己就带
    逗号(``POINT (1 2)`` 没有,但 ``POLYGON ((0 0, 1 0, 1 1, 0 0))`` 有一堆)。

    ⚠️ 这里**不做浮点数的格式化往返**:切出来的是原始文本片段,原样丢给递归的
    ``from_wkt``。要是先解析成 ``float`` 再拼回字符串,末位就变了。
    """
    out: List[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(body):
        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth < 0:
                raise GdbWriteError(f'GEOMETRYCOLLECTION 的括号不平衡: {body!r}')
        elif ch == ',' and depth == 0:
            out.append(body[start:i])
            start = i + 1
    if depth != 0:
        raise GdbWriteError(f'GEOMETRYCOLLECTION 的括号不平衡: {body!r}')
    out.append(body[start:])
    kids = [chunk.strip() for chunk in out]
    if not kids or not kids[0]:
        raise GdbWriteError('GEOMETRYCOLLECTION 里没有子几何')
    for index, kid in enumerate(kids):
        if not kid:
            raise GdbWriteError(
                f'GEOMETRYCOLLECTION 第 {index + 1} 个子几何是空的(多了个逗号?)')
    return kids


def from_wkt(wkt: str, has_z: bool = False, has_m: bool = False) -> Geometry:
    """OGC WKT -> :class:`Geometry`。

    支持 ``POINT`` / ``MULTIPOINT`` / ``LINESTRING`` / ``MULTILINESTRING`` /
    ``POLYGON`` / ``MULTIPOLYGON`` / ``GEOMETRYCOLLECTION``(可嵌套),可选
    ``Z`` / ``M`` / ``ZM`` 后缀,以及 ``EMPTY``(返回空几何)。可选的前导
    ``SRID=n;`` 会被忽略。

    :param has_z: 坐标里没有 Z 分量时是否按带 Z 处理(会被 WKT 后缀覆盖)。
    :param has_m: 同上,针对 M。

    ⚠️ ``GEOMETRYCOLLECTION`` 是**纯内存类型** —— FileGDB 的 ``ShapeType``
    里没有这一档,解析出来的 GC **写不进 .gdb**(``encode_geometry`` 会报错)。
    另外 ``GEOMETRYCOLLECTION Z (...)`` 这种**外层**后缀本库不收(会把 2D
    子几何读成 Z 值全 NaN),理由见那个分支的注释。``GEOMETRYCOLLECTION EMPTY``
    按全库惯例解析成空几何(NULL 类型),不是空 GC —— 与 ``POLYGON EMPTY``
    一样,空几何的 WKT 本来就往返不回来。

    .. note:: ``MULTIPOLYGON`` 是 **有损** 的。Esri 的几何 blob 里没有"多个
       面"这个类型,所有环摊在同一个 POLYGON 的 parts 里;而 parts 的方向
       规则(第 1 个环顺时针、其余逆时针,见 :func:`_orient_rings`)会把第 2
       个及以后的"外环"一律当成内环翻转。GDAL 也是这么做的 —— 它的
       ``ProcessSurface`` 只认 ``OGRPolygon``,同样的 ``bReverseOrder =
       bFirstRing != bIsClockwise``。要保住多个外环,请给每个面单独写一条
       要素,或者把原始环方向信息放在属性字段里。
    """
    text = (wkt or '').strip()
    if not text:
        raise GdbWriteError('WKT 为空')

    # 去掉 "SRID=4326;" 前缀(WKT 方言,常见于 PostGIS 导出)
    if text[:5].upper() == 'SRID=':
        semi = text.find(';')
        if semi < 0:
            raise GdbWriteError('WKT 的 SRID= 前缀后面缺少 ";"')
        text = text[semi + 1:].strip()

    head = text.split('(', 1)[0].strip()
    words = head.replace('(', ' ').split()
    if not words:
        raise GdbWriteError(f'WKT 缺少几何类型: {wkt!r}')
    type_name = words[0].upper()

    # 类型名后可能跟 Z / M / ZM(旧写法,如 "POINT Z (1 2 3)")
    suffix = ''
    for word in words[1:]:
        upper = word.upper()
        if upper in ('Z', 'M', 'ZM'):
            suffix = upper
        elif upper == 'EMPTY':
            return Geometry(shape_type=ST.NULL, coordinates=None,
                               has_z=has_z, has_m=has_m)
        else:
            raise GdbWriteError(f'WKT 类型名后面出现无法识别的词 {word!r}')

    if type_name == 'GEOMETRYCOLLECTION':
        # ⚠️ 递归分支,在 ``_WKT_TYPES`` 那层之前 —— 因为它不对应任何 Esri
        # 基础 shape type(FileGDB 的 ShapeType 里没有这一档),塞进那张表就
        # 会让人以为它编码得出来。见 ``_constants.ShapeType.GEOMETRYCOLLECTION``。
        if suffix:
            # ``GEOMETRYCOLLECTION Z (...)`` 是合法 WKT,但语义是"每个子几何
            # 都是 Z"。本库允许混合维度的 GC,把外层后缀硬套下去会把 2D 子几何
            # 改成"Z 值全 NaN",所以干脆不收 —— 让调用方给每个子几何写清后缀。
            raise GdbWriteError(
                'GEOMETRYCOLLECTION 不支持外层 Z / M 后缀:那表示"每个子几何'
                '都是 Z",而本库的 GEOMETRYCOLLECTION 允许混合维度,硬套会把'
                '2D 子几何改成 Z 值全 NaN。请给每个子几何单独写后缀,'
                '例如 GEOMETRYCOLLECTION (POINT Z (1 2 3), POLYGON ((...)))。')
        open_paren = text.find('(')
        if open_paren < 0:
            raise GdbWriteError(f'GEOMETRYCOLLECTION 里没有子几何: {wkt!r}')
        body = text[open_paren + 1:]
        if not body.rstrip().endswith(')'):
            raise GdbWriteError(f'GEOMETRYCOLLECTION 的括号不平衡: {wkt!r}')
        body = body.rstrip()[:-1]
        return Geometry.geometry_collection(
            [from_wkt(chunk, has_z=has_z, has_m=has_m)
             for chunk in _split_wkt_children(body)])

    if type_name not in _WKT_TYPES:
        raise GdbWriteError(
            f'不支持的 WKT 类型 {type_name!r};'
            f"支持: {', '.join(sorted(_WKT_TYPES))}"
        )
    base = _WKT_TYPES[type_name]

    open_paren = text.find('(')
    if open_paren < 0:
        raise GdbWriteError(f'WKT 里没有坐标: {wkt!r}')
    tokens = _tokenize_wkt(text[open_paren:])
    entries, end = _parse_wkt_group(tokens, 0)
    if end != len(tokens):
        raise GdbWriteError(f'WKT 多余的内容: {"".join(tokens[end:])!r}')

    # ---- 逐类型搬运 ------------------------------------------------------
    if type_name == 'POINT':
        if len(entries) != 1:
            raise GdbWriteError('POINT 只能有一个坐标')
        values = _wkt_coord(entries[0], 'POINT')
        hz, hm = _wkt_dimensions(suffix, has_z, has_m, len(values))
        coords: Any = _wkt_point_tuple(values, hz, hm, 'POINT')
        return Geometry(shape_type=ST.make_zm(ST.POINT, hz, hm),
                           coordinates=coords, has_z=hz, has_m=hm)

    if type_name == 'MULTIPOINT':
        values = _wkt_coord(entries[0], 'MULTIPOINT')
        hz, hm = _wkt_dimensions(suffix, has_z, has_m, len(values))
        points = [_wkt_point_tuple(_wkt_coord(e, 'MULTIPOINT'), hz, hm,
                                   'MULTIPOINT') for e in entries]
        return Geometry(shape_type=ST.make_zm(ST.MULTIPOINT, hz, hm),
                           coordinates=points, has_z=hz, has_m=hm)

    # ---- 线 / 面:先归一成 "环或part的列表,每个是坐标列表" -------------
    # ``_wkt_depth(entries)`` 就是"最外层往下数多少层才碰到数字":
    #   2 = 一个坐标序列(线)            3 = 一串环(面)
    #   3 = 一串 part(线,多段)          4 = 一串"面的环"(多面)
    depth = _wkt_depth(entries)
    # ``shells`` 只在面几何上有意义。**结构上**能确定环角色是 WKT 相对 Esri
    # 盘上格式的一个优势 —— 盘上只有绕向,OGC 的绕向又恰好与 Esri 相反,所以
    # 那边只能靠 ``OGR_ORGANIZE_POLYGONS`` 的默认策略猜(见
    # ``_geometry_ops.organize_polygons``)。这里直接记下来,顺带让
    # "多面压成一个 POLYGON" 之后仍然分得清哪个环属于哪个面。
    shells: Optional[List[bool]] = None
    if type_name in ('LINESTRING', 'POLYGON'):
        parts_of = entries if depth >= 3 else [entries]
        if base == ST.POLYGON and parts_of:
            # POLYGON 的第 0 个环是外环,其余都是它的洞
            shells = [True] + [False] * (len(parts_of) - 1)
    elif type_name == 'MULTILINESTRING':
        parts_of = entries if depth >= 3 else [entries]
    else:                       # MULTIPOLYGON
        if depth >= 4:
            # Esri 没有"多面"类型:各个面的环全部摊进同一个 POLYGON 的 parts
            parts_of = [ring for polygon in entries for ring in polygon]
            shells = [i == 0 for polygon in entries
                      for i in range(len(polygon))]
        else:
            parts_of = entries
            if parts_of:
                shells = [True] + [False] * (len(parts_of) - 1)

    first_values = _dig_to_coord(entries)
    hz, hm = _wkt_dimensions(suffix, has_z, has_m, len(first_values))

    part_offsets: List[int] = []
    all_points: List[Tuple[float, ...]] = []
    for part_index, part in enumerate(parts_of):
        raw = [_wkt_coord(e, type_name) for e in part]
        where = (f'{type_name} 第 {part_index + 1} 个'
                 f'{"环" if base == ST.POLYGON else "part"}')
        points = [_wkt_point_tuple(v, hz, hm, where) for v in raw]
        if base == ST.POLYGON:
            points = _strip_ring_closure(points, where)
        elif len(points) < 2:
            raise GdbWriteError(f'{where}: 至少要有 2 个顶点')
        part_offsets.append(len(all_points))
        all_points.extend(points)

    if not all_points:
        raise GdbWriteError(f'{type_name} 里没有坐标')
    return Geometry(shape_type=ST.make_zm(base, hz, hm),
                       coordinates=(part_offsets, all_points),
                       has_z=hz, has_m=hm, _shells=shells)


# ============================================================================
# 编码(写路径)
#
# 逐行对照 GDAL ``filegdbtable_write.cpp``:
#   - ``EncodeEnvelope``                : 包围盒 = minX, minY, (maxX-minX), (maxY-minY)
#   - ``FileGDBTable::EncodeGeometry``  : 主分派
#   - ``WriteEndOfCurveOrSurface``      : nPoints / nParts / 各 part 点数 / XY / Z / M
#   - ``WriteVarInt`` (filegdbtable_priv.h) : 有符号增量整数
#
# 三个最容易写错的地方:
#
# 1. **包围盒的后两个分量是"跨度(delta)"而不是 max 值**,并且 **没有 POINT
#    那种 +1 偏移**(``+ 0.5`` 只是四舍五入)。
# 2. **坐标网格化的偏移不对称**:POINT 是 ``(v-origin)*scale + 1``,
#    数组形式(线/面/多点)是 ``(v-origin)*scale``,差 1 个量化单位。
# 3. **环的方向**:第 1 个环必须顺时针、其余环必须逆时针,不符合就整环反转
#    (GDAL 的 ``bReverseOrder = bFirstRing != bIsClockwise``)。
# ============================================================================
def write_varint_delta(out: bytearray, val: int) -> None:
    """写一个"有符号增量整数",与 GDAL ``WriteVarInt`` 完全等价。

    编码规则(注意 **不是** zigzag):首字节 bit6 是符号位、低 6 位是数据;
    若数据放不下则置 bit7,后续按 **无符号** LEB128 分组续写。
    """
    if val < 0:
        uval = -val
        if uval >= 0x40:
            out.append(0x80 | 0x40 | (uval & 0x3F))
            uval >>= 6
        else:
            out.append(0x40 | (uval & 0x3F))
            return
    else:
        uval = val
        if uval >= 0x40:
            out.append(0x80 | (uval & 0x3F))
            uval >>= 6
        else:
            out.append(uval & 0x3F)
            return
    write_varuint(out, uval)


def _round_half_up(v: float) -> int:
    """四舍五入到整数(对应 GDAL 的 ``std::round`` + ``static_cast<int64_t>``)。

    不用 Python 的 ``round``:它是"银行家舍入"(round-half-to-even),
    在 .5 处会与 GDAL 差 1,导致写出的坐标与 ArcGIS 逐位不一致。
    """
    if v >= 0:
        return int(v + 0.5)
    return -int(-v + 0.5)


def _grid(v: float, origin: float, scale: float) -> int:
    """``round((v - origin) * scale)``。NaN 返回 0(表示空值)。"""
    if v != v:
        return 0
    return _round_half_up((v - origin) * scale)


class _Encoder:
    """把点数组按增量写进 ``out``。

    与解码侧的 :class:`_DeltaReader` 对称:XY 的增量累加器 **跨 part 连续**,
    Z、M 各自独立累加器(都从 0 开始)。
    """

    __slots__ = ('out', 'q', 'has_z', 'has_m')

    def __init__(self, out: bytearray, q: '_Quantizer',
                 has_z: bool, has_m: bool) -> None:
        self.out = out
        self.q = q
        self.has_z = has_z
        self.has_m = has_m

    def points(self, xs: Sequence[float], ys: Sequence[float],
               zs: Optional[Sequence[float]] = None,
               ms: Optional[Sequence[float]] = None) -> None:
        """写 XY(及可选的 Z/M)增量数组。"""
        q = self.q
        out = self.out
        last_x = last_y = 0
        for x, y in zip(xs, ys):
            nx = _grid(x, q.x_origin, q.xy_scale)
            ny = _grid(y, q.y_origin, q.xy_scale)
            write_varint_delta(out, nx - last_x)
            write_varint_delta(out, ny - last_y)
            last_x, last_y = nx, ny

        if self.has_z:
            if zs is None:
                raise GdbWriteError('几何标记为带 Z,但没提供 Z 坐标')
            last = 0
            for z in zs:
                # NaN -> 量化值 0,即"这一维为空"。数组形式的量化 **没有**
                # 那个 +1,所以 0 天然就是空值哨兵(读侧
                # ``_read_scalar_array(..., 0)`` 也这么认)。
                nz = 0 if z != z else _grid(z, q.z_origin, q.z_scale)
                write_varint_delta(out, nz - last)
                last = nz

        if self.has_m and ms is not None:
            # M 整体缺失时 GDAL 不写任何东西(读侧只是"容忍"那个 0x42 标记
            # 字节,写侧从不生成),这里保持一致。
            last = 0
            for m in ms:
                nm = 0 if m != m else _grid(m, q.m_origin, q.m_scale)
                write_varint_delta(out, nm - last)
                last = nm


def _envelope(out: bytearray, q: '_Quantizer',
              xs: Sequence[float], ys: Sequence[float]) -> None:
    """写包围盒:minX, minY, (maxX-minX), (maxY-minY) 共 4 个 varuint。

    对应 GDAL ``EncodeEnvelope``。两点必须注意:
    * 前两个分量相对 origin 量化,**没有 +1**;
    * 后两个分量是 **跨度**,只乘 scale,不减 origin。
    """
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    write_varuint(out, _grid(minx, q.x_origin, q.xy_scale))
    write_varuint(out, _grid(miny, q.y_origin, q.xy_scale))
    write_varuint(out, _round_half_up((maxx - minx) * q.xy_scale))
    write_varuint(out, _round_half_up((maxy - miny) * q.xy_scale))


def _is_clockwise(pts: Sequence[Sequence[float]]) -> bool:
    """用带符号面积(鞋带公式)判断环的绕向。

    Esri 的约定是"外环顺时针、内环逆时针"。GDAL 用 ``OGRLinearRing::
    isClockwise``,同样是基于带符号面积。按数学坐标系(y 向上):
    **面积为负 = 顺时针**。

    ⚠️ **鞋带本身不在这里实现** —— 全库只有 :func:`_geometry_ops.
    ring_signed_area2` 一份,这里只把 ``(x, y)`` 元组摊平成交错数组喂给它。
    这么绕一道是因为那个版本会**先平移到首顶点**,而这在有量纲的大坐标上
    (UTM 带号 ~3.9e7)是**正确性**要求,不是优化:不平移时近零面积的环会因
    灾难性抵消**把符号判反**,而这个符号决定**写盘时环的绕向** —— 判反了,
    读回去就是"洞当成壳"。

    实测:真实语料 22,044 个环里,**有 1 个**(``oid=14883`` 的 3 顶点退化环,
    平移后 ``area2 = 4.9e-4``)符号确实被不平移的旧写法判反。理由与完整实测
    见 ``_geometry_ops`` 模块开头的「大坐标抵消」一节。
    """
    if len(pts) < 3:
        return False
    flat = array('d')
    for p in pts:
        flat.append(p[0])
        flat.append(p[1])
    return ring_signed_area2(flat) < 0


def encode_geometry(geom: Optional[Geometry], geom_field: Any = None,
                    has_z: Optional[bool] = None,
                    has_m: Optional[bool] = None) -> bytes:
    """把 :class:`Geometry` 编码成 FileGDB 几何 blob。

    与 :func:`decode_geometry` 在同一组 origin/scale 下互为逆运算。

    :param geom: 待编码的几何;``None`` / 空几何写成 NULL 类型码。
    :param geom_field: :class:`GdbGeomField`,提供量化参数;缺省为
        origin=0、scale=1(坐标被原样当整数写,只适合调试)。
    :param has_z: 是否写 Z;缺省取 ``geom.has_z``。
    :param has_m: 是否写 M;缺省取 ``geom.has_m``。

    :returns: 几何 blob(**不含** 记录里的 varuint 长度前缀)。
    """
    q = _Quantizer(geom_field)
    if has_z is None:
        has_z = bool(geom.has_z) if geom is not None else False
    if has_m is None:
        has_m = bool(geom.has_m) if geom is not None else False

    out = bytearray()

    if geom is None or geom.is_empty:
        write_varuint(out, SHPT_NULL)
        return bytes(out)

    kind = geom.kind

    if kind == 'geometrycollection':
        # 放在 ``is_empty`` 早退**之后**:一个"子几何全空"的 GC 与其它空几何
        # 一样写 NULL —— 那是同一个含义,没必要报错。非空的 GC 则明确拒绝:
        # 悄悄丢一个 NULL 出去等于把整个几何吃掉。
        raise GdbWriteError(
            'GEOMETRYCOLLECTION 写不进 FileGDB:Esri 的 ShapeType 里**没有**'
            '这一档(见 _constants.ShapeType.GEOMETRYCOLLECTION),'
            '盘上根本没有能表达"一个面加一个点"的记录。它只是内存里的运算'
            '结果(overlay 出来的),要落盘请先把子几何拆开、每个写一条要素,'
            '或者只写其中一维。')

    if kind == 'point':
        return _encode_point(out, q, geom.coordinates, has_z, has_m)

    if kind == 'multipoint':
        pts = [tuple(p) for p in geom.coordinates]
        write_varuint(out, ST.make_zm(SHPT_MULTIPOINT, has_z, has_m))
        write_varuint(out, len(pts))
        if not pts:
            return bytes(out)
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        _envelope(out, q, xs, ys)
        _Encoder(out, q, has_z, has_m).points(
            xs, ys,
            [p[2] for p in pts] if has_z else None,
            [p[3 if has_z else 2] for p in pts] if has_m else None)
        return bytes(out)

    if kind in ('polyline', 'polygon'):
        parts, raw_points = geom.coordinates
        base = SHPT_POLYGON if kind == 'polygon' else SHPT_ARC
        write_varuint(out, ST.make_zm(base, has_z, has_m))

        points = [tuple(p) for p in raw_points]
        if not points:
            write_varuint(out, 0)   # nPoints == 0 即结束(读侧同款早退)
            return bytes(out)

        if kind == 'polygon':
            # 盘上环是闭合的(ArcGIS 这么写,实测 1746/1746),而内部表示
            # 不闭合,所以在这里补回来。必须在算 counts 之前做,补点会
            # 改变每个 part 的长度。
            parts, points = _close_parts(parts, points)

        # 把 parts(起始下标)展开成每段的点数
        counts: List[int] = []
        n = len(parts)
        for i, start in enumerate(parts):
            end = parts[i + 1] if i + 1 < n else len(points)
            counts.append(end - start)

        if kind == 'polygon':
            # 闭合在前、定向在后:整环反转后仍然闭合
            counts = _orient_rings(counts, points)

        xs = [p[0] for p in points]
        ys = [p[1] for p in points]

        write_varuint(out, len(points))
        write_varuint(out, len(counts))
        _envelope(out, q, xs, ys)
        for c in counts[:-1]:       # 最后一个 part 的点数不写,由总数推出
            write_varuint(out, c)
        _Encoder(out, q, has_z, has_m).points(
            xs, ys,
            [p[2] for p in points] if has_z else None,
            [p[3 if has_z else 2] for p in points] if has_m else None)
        return bytes(out)

    raise GdbWriteError(f'不支持的几何类型: {kind!r}')


def _close_parts(part_starts: List[int], points: List[Tuple[float, ...]]
                 ) -> Tuple[List[int], List[Tuple[float, ...]]]:
    """给每个环补上闭合点(末尾重复一次首点),返回新的 ``(parts, points)``。

    :func:`_strip_part_closures` 的逆操作。已经闭合的环不再补第二个点。
    只用于 **多边形**:Esri 的 POLYLINE 不要求 part 闭合。
    """
    if not part_starts:
        return part_starts, points
    new_starts: List[int] = []
    new_points: List[Tuple[float, ...]] = []
    n = len(part_starts)
    for i, start in enumerate(part_starts):
        end = part_starts[i + 1] if i + 1 < n else len(points)
        seg = list(points[start:end])
        if len(seg) > 1 and seg[0][:2] != seg[-1][:2]:
            seg.append(seg[0])
        new_starts.append(len(new_points))
        new_points.extend(seg)
    return new_starts, new_points


def _orient_rings(counts: List[int],
                  points: List[Tuple[float, ...]]) -> List[int]:
    """让第 1 个环顺时针、其余环逆时针。

    ⚠️ 会 **就地改写** ``points`` 以反映反转后的顺序。对应 GDAL 里
    ``bReverseOrder = bFirstRing != bIsClockwise`` 那段逻辑。
    """
    idx = 0
    first = True
    for c in counts:
        ring = list(points[idx:idx + c])
        if _is_clockwise(ring) != first:
            ring.reverse()
            for k in range(c):
                points[idx + k] = ring[k]
        idx += c
        first = False
    return counts


def _encode_point(out: bytearray, q: '_Quantizer', coords: Sequence[float],
                  has_z: bool, has_m: bool) -> bytes:
    """单点编码。网格值 = ``(v-origin)*scale + 1``,空值用 0 表示。"""
    write_varuint(out, ST.make_zm(SHPT_POINT, has_z, has_m))

    for v, origin in ((coords[0], q.x_origin), (coords[1], q.y_origin)):
        if v != v:      # NaN -> 空
            write_varuint(out, 0)
        else:
            write_varuint(out, _grid(v, origin, q.xy_scale) + 1)

    if has_z:
        z = coords[2]
        write_varuint(out, 0 if z != z
                      else _grid(z, q.z_origin, q.z_scale) + 1)
    if has_m:
        m = coords[3] if has_z else coords[2]
        write_varuint(out, 0 if m != m
                      else _grid(m, q.m_origin, q.m_scale) + 1)
    return bytes(out)
