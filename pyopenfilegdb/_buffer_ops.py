# -*- coding: utf-8 -*-
"""``Geometry.buffer()`` 的实现 —— 抄的是 **JTS**,不是 GDAL。

为什么不能"严格按 GDAL 实现"
---------------------------
本库其余部分的口径是"读写的逻辑严格按照 gdal 的来实现"。**这一档做不到**:
``OGRGeometry::Buffer( double dfDist, int nQuadSegs )`` 在 ``ogrgeometry.cpp``
里是一句转发 —— GDAL 把整个 buffer 交给 GEOS,自己一行算法都没有。所以**没有
纯 C++ 可抄**。GEOS 的上游是 JTS,本模块逐段对照的是:

    modules/core/src/main/java/org/locationtech/jts/
        operation/buffer/BufferOp.java              入口 + 精度降级重试梯
        operation/buffer/BufferBuilder.java         节点化 / 边表 / 结果启发式
        operation/buffer/BufferCurveSetBuilder.java 输入 -> 带标签的原始偏移曲线
        operation/buffer/OffsetCurveBuilder.java    曲线装配(左右两侧 / 环 / 单侧)
        operation/buffer/OffsetSegmentGenerator.java 偏移曲线逐段生成
        operation/buffer/OffsetSegmentString.java   顶点表(最小顶点距剔除)
        operation/buffer/BufferInputLineSimplifier.java  输入预处理
        operation/buffer/BufferParameters.java      常量与默认值
        operation/overlay/PolygonBuilder.java       成环 -> 分壳/洞
        algorithm/Angle.java                        角度辅助(含 cosSnap/sinSnap)
        algorithm/Intersection.java                 线线 / 线段的求交(斜接角用)

**每一处实现都在注释里标了对应的 JTS 类与方法名**,与其余模块标注 GDAL 出处
的做法一致。

与 JTS 的**五处**分歧(前两处是有意换掉、后三处是 JTS / GEOS 上游本身就分岔;
都写在 DESIGN.md §2.23,不许悄悄换)
---------------------------------------------------------------------------
⚠️ 先说清口径:**本模块以 GEOS 为参照,不是以 JTS 为参照。** 因为 GDAL 的
``OGRGeometry::Buffer`` 就是一句转发给 GEOS 的话(自己一行算法都没有),
``tools/verify_buffer.py`` 的 oracle 也是 GEOS。JTS 只是"读得懂的算法导读"。
上游已经分岔的地方(GDAL 侧只认 GEOS),照抄 JTS 会把对拍弄红。
1. **面深度不用 ``BufferSubgraph`` + ``SubgraphDepthLocater``。** JTS 那套是
   "每个连通分量取最右点、向左打射线、累加 depthDelta"的性能捷径。本库改成
   **建真平面图 + 面遍历 + 对偶图 BFS**(落在 :mod:`._planar`):数学上等价
   (深度的定义就是"从无穷远
   走到该面所穿过的边的 depthDelta 之和"),但**没有 ε 偏移、没有"最右点选哪个"
   的隐含假设** —— 而"重算出来的点不能拿去问浮点"正是本库用
   ``tools/verify_topology.py`` 抓出过两个真 bug 的坑。
   每个连通分量另用**一次精确射线投射**定它的基准深度:射线方向从"不指向任何
   顶点的角度空隙"里挑,所以相交判定全部是严格的符号判定,不需要容差。
2. **没有 snap-rounding。** JTS ``BufferOp`` 的最后一级降级是
   ``SnapRoundingNoder(new PrecisionModel(1.0))`` 外套 ``ScaledNoder``,本库整体
   口径就是"不做 snap-rounding、不做精确算术"(见 ``_geometry_ops`` 的模块
   docstring)。所以前两级降级照抄,第三级**降级为直接返回**。后果是:GEOS 靠
   snap-rounding 救回来的退化构型,本库可能返回空。这条写在 docstring 里。
3. **``OFFSET_SEGMENT_SEPARATION_FACTOR`` 取 GEOS 的 ``1.0E-3``,不取 JTS 的
   ``0.05``。** 这个常量只在 ``_add_outside_turn`` 里用一次,决定"两个偏移端点
   挨得够近就当同一个角点"这条捷径的阈值。取 GEOS 是因为**本模块整体以 GEOS
   为参照**(GDAL 的 ``OGRGeometry::Buffer`` 就是转手 GEOS;``tools/verify_buffer.py``
   的 oracle 也是 GEOS),而这一档的差异实测有 1e-4 量级、不是次 ULP —— 见
   ``_OFFSET_SEGMENT_SEPARATION_FACTOR`` 处的长注释。JTS 的 ``0.05`` 会让
   ``_add_outside_turn`` 少吐一个顶点,整条环跟着少十几个顶点。
   ⚠️ 顺带钉住一对**看起来像分歧、其实不是**的:``addMitreJoin`` 里 JTS 老版本
   要求 ``hasIntersection() && isProper()``,GEOS(``oseggen.cpp:470``)只看
   ``!intPt.isNull() && intPt.distance(cornerPt) <= mitreLimitDistance`` ——
   本模块跟 GEOS,用**直线**求交、不查"交点是否落在端点之外",已实测与 GEOS
   逐位相同(``sq_mitre`` 真值行)。
4. **``BufferInputLineSimplifier`` 跟 GEOS,不跟 JTS。** 两处实参/起点差异,
   都在 :func:`_simplify_input_line` 的 docstring 里逐字写清了:``isShallowSampled``
   里 JTS 是 ``isShallow(p0, inputLine[i], 中间点)``、GEOS 是
   ``isShallow(p0, 中间点, inputLine[i])``(函数体一样,传进去的三项顺序不同);
   主循环起点 JTS 是 ``isRing ? 0 : 1``、GEOS 恒为 1(GEOS 根本没有 ``isRing``
   这个成员)。**GEOS 那条严得多**:同一份 ``乡行政区划`` 的 ``POLYGON`` ``d=1``,
   JTS 顺序削掉 8 个浅凹点、输出 127 顶点,GEOS 顺序一个都不削、输出 136 顶点,
   后者与 GEOS 逐位相同。整份语料 260 对:JTS 顺序 112 对逐位相同 / 148 对超差,
   GEOS 顺序 250 对逐位相同 / **0 对超差**。
5. **``single_sided`` 在闭合输入上跟 JTS,不跟 GEOS。** GEOS 跑完同一条单侧
   管线后还有一步 JTS 没有的后处理(``BufferBuilder::buffer`` 结尾):
   ``OverlayNG::overlay(输入边界, 结果边界, UNION)`` + ``Polygonizer``,
   面多于一个时只留面积最大的。对**开放折线**这步是恒等的(只围得出一个面);
   对**面 / 闭合折线**它会换掉结果(10×10 方框 ``d=1``:GEOS 给 100,本库按 JTS
   给 143.12)。本库不做这步。⚠️ **2026-09-30 补注**:overlay 引擎后来做了
   (``difference`` / ``union`` / ``intersection`` / ``symmetric_difference``,
   见 :mod:`~pyopenfilegdb._overlay_ops`),所以这里缺的**不是** overlay —— 还差
   一个 ``Polygonizer``(JTS ``operation/polygonize``),而且"面多于一个时只留
   最大面"是**丢几何**的取舍,不是无副作用的后处理。``tools/verify_buffer.py``
   把那 44 对**显式列出并计数**排除,不静默跳过。

算法管线
--------
::

    输入几何
      -> _CurveSet            生成带 LEFT/RIGHT 标签的原始偏移曲线
      -> _planar._noded_edges 在交点处打断 + 按无向节点对去重(delta 累加合并)
      -> _planar._PlanarGraph 角度序 + 面遍历
      -> _planar._face_depths 每分量一次精确射线投射定基准 + 对偶图 BFS
      -> _planar._result_rings 留 depth(左)>=1 且 depth(右)<=0 的有向边,走成环
      -> _planar._assemble    分壳/洞(复用 _geometry_ops.organize_polygons)

⚠️ **后半段从"节点化"起是 buffer 与 overlay 共用的一层**,已经抽到
:mod:`~pyopenfilegdb._overlay_ops` 旁边的 :mod:`~pyopenfilegdb._planar`;
本模块把它们**原样重新导出**(所以老的内部引用点、以及 ``tests/test_buffer.py``
的 ``B._noded_edges`` 都照旧能用)。两个模块的差别只有"曲线怎么带标签"与
"哪些面算结果面"这两件事,其余全部相同。

⚠️ 环的绕向
-----------
内部走出来的环是**数学正向(逆时针)**,而本库(以及 Esri、以及 GEOS 自己)
的约定是**外环顺时针**。所以最后统一翻一次。三个来源一致这件事是实测确认的,
不是推断:``shapely.geometry.Point(0,0).buffer(1).exterior.is_ccw is False``。
"""
from __future__ import annotations

import math
from array import array
from itertools import chain
from typing import Any, List, Optional, Sequence, Tuple

from ._geometry_ops import (
    EXTERIOR,
    INTERIOR,
    SEG_NONE,
    #: 点在线段上的垂距。**复用而不是重写**:``BufferCurveSetBuilder``
    #: 的 ``hasPointOnBuffer`` / ``isTriangleErodedCompletely`` 与
    #: ``BufferInputLineSimplifier.isShallow`` 都要它,三处用同一个。
    _point_seg_distance,
    organize_polygons,
    orient,
    ring_signed_area2,
    seg_seg_classify,
)

# 平面图那一层(buffer 与 overlay 共用)已经从本模块抽到 :mod:`._planar`。
# 这里**原样重新导出**,所以 ``pyopenfilegdb._buffer_ops.<名字>`` 的老叫法
# 依旧可用。
#
# ⚠️ 其中四个与 delta 打交道的名字要**过一层一元适配**:``_planar`` 里那条
# 链路是**向量**的(N 个输入,N 个 depth 分量,见那边的模块 docstring),而
# buffer 只有**一个**输入。适配就是"包成单元素向量 / 摊回标量",四个薄壳
# 加起来的净效果是 ``_buffer_once`` **一行都不用改**。
from ._planar import (
    PlanarTopologyError,
    _add_split,
    _assemble,
    _clean,
    _clean_ring,
    _close_ring,
    _components,
    _coords,
    _depth_delta,
    _keep_largest_group,
    _num_geometries,
    _pick_ray,
    _ray_depth,
)
from ._planar import _face_depths as _face_depths_v
from ._planar import _noded_edges as _noded_edges_v
from ._planar import _result_rings as _result_rings_v
from ._planar import _PlanarGraph as _PlanarGraph_v

#: 老名字(只在 DESIGN.md 里出现过),保留成别名免得引用点到处改。
_BufferTopologyError = PlanarTopologyError


def _noded_edges(curves):
    """一元包装 —— 签名与抽 ``_planar`` 之前**逐字一致**。

    :param curves: ``(pts, left_loc, right_loc)`` 三元组表。
    :returns: ``(node_pts, eu, ev, edelta)``,``edelta[e]`` 是**标量**。

    ``tests/test_buffer.py`` 直接调这个(它按老签名调),所以要保住。

    ⚠️ ``_planar`` 那一侧的第六项 ``node_z`` 在 buffer 这条路上**整条都丢掉**
    —— buffer 的输出不带 Z(见 ``_buffer_ops`` 的诚实边界),所以不该让它
    渗透到这里。
    """
    node_pts, eu, ev, edelta, _edim, _node_z = _noded_edges_v(
        [(pts, ((left, right),)) for pts, left, right in curves])
    return node_pts, eu, ev, [d[0] for d in edelta]


class _PlanarGraph(_PlanarGraph_v):
    """一元包装 —— 把标量 delta 包成单元素向量再交给 :mod:`._planar`。"""

    def __init__(self, node_pts, eu, ev, edelta) -> None:
        super().__init__(node_pts, eu, ev, [(int(d),) for d in edelta])


def _face_depths(graph, face_of, faces):
    """一元包装 —— 返回**标量**深度表(输入也是标量 delta 的图)。"""
    return [d[0] for d in _face_depths_v(graph, face_of, faces)]


def _result_rings(graph, face_of, depth):
    """一元包装 —— buffer 的结果面判据(内部在左 = 深度 ≥ 1)。

    与抽 ``_planar`` 之前那句 ``depth[face_of[h]] >= 1 and
    depth[face_of[h^1]] <= 0`` 逐字等价,只是现在"深度"是个单元素向量。
    """
    return _result_rings_v(
        graph, face_of, [(int(d),) for d in depth],
        lambda dl, dr: dl[0] >= 1 and dr[0] <= 0)

# ----------------------------------------------------------------------------
# 常量(BufferParameters.java / OffsetSegmentGenerator.java)
# ----------------------------------------------------------------------------
CAP_ROUND = 1
CAP_FLAT = 2
CAP_SQUARE = 3

JOIN_ROUND = 1
JOIN_MITRE = 2
JOIN_BEVEL = 3

DEFAULT_QUADRANT_SEGMENTS = 8
DEFAULT_MITRE_LIMIT = 5.0
DEFAULT_SIMPLIFY_FACTOR = 0.01

#: 端帽 / 接合风格的字符串名 -> JTS 常量。同时**接受整数 1/2/3**
#: (就是 JTS 常量本身),方便从 shapely 的 ``cap_style=2`` 直接搬过来。
#: ``'butt'`` / ``'miter'`` 是 GEOS / PostGIS 里的别名,一并收下。
_CAP_NAMES = {'round': CAP_ROUND, 'flat': CAP_FLAT, 'butt': CAP_FLAT,
              'square': CAP_SQUARE}
_JOIN_NAMES = {'round': JOIN_ROUND, 'mitre': JOIN_MITRE, 'miter': JOIN_MITRE,
               'bevel': JOIN_BEVEL}


def _parse_style(value, names, what: str, allowed: str) -> int:
    if isinstance(value, str):
        try:
            return names[value.strip().lower()]
        except KeyError:
            pass
    elif value in (1, 2, 3):
        return int(value)
    raise ValueError(
        f'{what} 只接受 {allowed} 或对应的整数 1/2/3,得到 {value!r}')

#: ``LinearRing.MINIMUM_VALID_SIZE``
MINIMUM_VALID_SIZE = 4

_PI_2 = 2.0 * math.pi
_PI_OVER_2 = math.pi / 2.0

#: JTS ``Position.LEFT`` / ``Position.RIGHT``。⚠️ 与 ``Orientation.LEFT``(=
#: ``COUNTERCLOCKWISE`` = 1)是**两套不同的常量**,别混:前者是"偏移在哪一侧",
#: 后者是"拐弯往哪转"。JTS 里它们恰好都叫 LEFT。
_LEFT = 0
_RIGHT = 1

#: JTS ``Orientation`` 的转向码。
_CLOCKWISE = -1
_COLLINEAR = 0
_COUNTERCLOCKWISE = 1

#: ``OffsetSegmentGenerator`` 的几个启发式因子,数值**照抄 GEOS**(不是 JTS)。
#:
#: ⚠️ ``_OFFSET_SEGMENT_SEPARATION_FACTOR`` 是本模块里**唯一**一处 JTS 与 GEOS
#: 数值不同的常量:JTS ``OffsetSegmentGenerator.java:37`` 是 ``.05``,GEOS
#: ``OffsetSegmentGenerator.cpp:48`` 是 ``1.0E-3``(**差 50 倍**)。取 GEOS,
#: 理由与整个 buffer 一档的口径一致 —— 见本文件开头"与 JTS 的分歧"第 3 条。
#:
#: 这个数不是可有可无的微调,它决定 ``_add_outside_turn`` 里"两个偏移端点够近,
#: 就当同一个角点"这条捷径**什么时候**生效,而捷径一开就**只吐一个顶点**。
#: 实测(``乡行政区划`` 的 ``POLYGON`` 缓冲区,d=1):某个凸角上
#: ``|offset0.p1 - offset1.p0| = 0.0369``。按 JTS 的 0.05 阈值捷径生效 → 1 个顶点;
#: 按 GEOS 的 1e-3 阈值不生效 → 走圆角分支,而该角转角极小,
#: ``addDirectedFillet`` 算出 ``nSegs = (int)(tiny/quantum + 0.5) = 0`` →
#: ``if(nSegs < 1) return`` → 仍然只有 ``p0`` 和 ``p1`` **两个**顶点。
#: 就是这"一个顶点 vs 两个顶点"的差别,让整条环少 13 个顶点、面积偏差 6.8e-4。
_OFFSET_SEGMENT_SEPARATION_FACTOR = 1.0e-3
_INSIDE_TURN_VERTEX_SNAP_DISTANCE_FACTOR = 1.0e-3
_CURVE_VERTEX_SNAP_DISTANCE_FACTOR = 1.0e-4
_MAX_CLOSING_SEG_LEN_FACTOR = 80

#: ``BufferCurveSetBuilder`` 的反退化守卫常量。
_MAX_INVERTED_RING_SIZE = 9
_INVERTED_CURVE_VERTEX_FACTOR = 4
_NEARNESS_FACTOR = 0.99

#: ``BufferOp.MAX_PRECISION_DIGITS``
_MAX_PRECISION_DIGITS = 12


# ----------------------------------------------------------------------------
# 角度辅助(Angle.java)
# ----------------------------------------------------------------------------
def _cos_snap(a: float) -> float:
    """``Math.cos`` + "贴着 0 的一律当 0"。

    ``Angle.cosSnap``:``|res| < 5e-16`` 返回 **精确的 0.0**。这不是洁癖 ——
    ``cos(π/2)`` 在 IEEE 下是 ``6.123233995736766e-17``,而方形端帽、方形点
    缓冲的顶点就是靠它算出来的;**不拉直的话"正方形"会变成面积差 1e-16 的
    四边形**,后续节点化多出四个几乎重合的点。
    """
    r = math.cos(a)
    return 0.0 if abs(r) < 5e-16 else r


def _sin_snap(a: float) -> float:
    """见 :func:`_cos_snap`。"""
    r = math.sin(a)
    return 0.0 if abs(r) < 5e-16 else r


def _normalize_angle(a: float) -> float:
    """折算到 ``(-π, π]``(``Angle.normalize``)。"""
    while a > math.pi:
        a -= _PI_2
    while a <= -math.pi:
        a += _PI_2
    return a


def _angle_between_oriented(tip1, tail, tip2) -> float:
    """``Angle.angleBetweenOriented`` —— 带方向的夹角,折算到 ``(-π, π]``。"""
    a1 = math.atan2(tip1[1] - tail[1], tip1[0] - tail[0])
    a2 = math.atan2(tip2[1] - tail[1], tip2[0] - tail[0])
    d = a2 - a1
    if d <= -math.pi:
        return d + _PI_2
    if d > math.pi:
        return d - _PI_2
    return d


def _intersection(p1, p2, q1, q2):
    """两条**直线**的交点;平行/共线返回 ``None``(``Intersection.intersection``)。

    ⚠️ JTS 走的是 ``CGAlgorithmsDD`` 的自适应双精度,本库没有精确算术。这里用
    JTS 自己留的那个后备实现 ``intersectionFP`` 的**中点条件化**做法:先把四个
    点平移到"核包围盒"的中点,再解齐次方程。对 buffer 这种坐标量级一致、
    四点距离不远的场景,条件化之后的误差与 DD 版肉眼无差;但**退化构型上不保证**,
    这与本库整体口径一致。
    """
    min_x0, max_x0 = (p1[0], p2[0]) if p1[0] < p2[0] else (p2[0], p1[0])
    min_y0, max_y0 = (p1[1], p2[1]) if p1[1] < p2[1] else (p2[1], p1[1])
    min_x1, max_x1 = (q1[0], q2[0]) if q1[0] < q2[0] else (q2[0], q1[0])
    min_y1, max_y1 = (q1[1], q2[1]) if q1[1] < q2[1] else (q2[1], q1[1])
    mid_x = (max(min_x0, min_x1) + min(max_x0, max_x1)) / 2.0
    mid_y = (max(min_y0, min_y1) + min(max_y0, max_y1)) / 2.0

    px, py = p1[0] - mid_x, p1[1] - mid_y
    qx_, qy_ = p2[0] - mid_x, p2[1] - mid_y
    rx, ry = q1[0] - mid_x, q1[1] - mid_y
    sx, sy = q2[0] - mid_x, q2[1] - mid_y

    # 齐次坐标下的两条直线
    a1, b1 = py - qy_, qx_ - px
    c1 = px * qy_ - qx_ * py
    a2, b2 = ry - sy, sx - rx
    c2 = rx * sy - sx * ry

    w = a1 * b2 - a2 * b1
    if w == 0.0 or not math.isfinite(w):
        return None
    x = (b1 * c2 - b2 * c1) / w
    y = (a2 * c1 - a1 * c2) / w
    if not (math.isfinite(x) and math.isfinite(y)):
        return None
    return (x + mid_x, y + mid_y)


def _seg_intersection(offset0, offset1):
    """两条**线段**的交 —— ``(交点或 None, 是否真穿越)``。

    ⚠️ **与 :func:`_intersection` 的分工必须分清,这两个不是一件事。**
    :func:`_intersection` 回答"两条**直线**交在哪",**不看线段范围**;这个函数
    回答"这两条**线段**到底交不交",对应 GEOS ``LineIntersector`` 的
    ``hasIntersection()`` / ``isProper()``。``OffsetSegmentGenerator`` 里凡是
    拿偏移线段去求交的地方(``addInsideTurn`` / ``addMitreJoin``)走的都是后者。

    直接拿直线交点当交点是错的,而且错得很隐蔽 —— 交点仍然是个像样的坐标,
    曲线仍然是闭合的,只是绕过了一个本该补"闭合段"的角。实测:
    ``POLYGON ((0 0, 10 10, 10 0, 0 10, 0 0))``(领结)在 ``distance=5`` 时,
    角 ``(0, 0)`` 的两条偏移线段 ``((5,10),(5,0))`` 与
    ``((-3.5355,3.5355),(6.4645,13.5355))`` **根本不相交**(它们的**直线**交在
    ``(5, 12.0711)``,``y`` 超出前者的 ``[0, 10]``)。照直线交点走会跳过闭合段,
    丢掉整个左半边,面积 179.24 而 GEOS 是 239.85。
    ``distance=1`` 时同样的角直线交点恰好落在线段内,两边一致 —— 所以这个 bug
    只在"偏移量相对边长够大"时露头。

    返回值第二项给 ``addMitreJoin`` 用:GEOS 那里要求 ``hasIntersection() &&
    isProper()``,交点落在任一线段的**端点**上都不算数。
    """
    kind, pt, _pt2 = seg_seg_classify(offset0[0], offset0[1],
                                      offset1[0], offset1[1])
    if kind == SEG_NONE:
        return None, False
    proper = pt not in (offset0[0], offset0[1], offset1[0], offset1[1])
    if proper:
        # 真穿越 —— 交点仍由 :func:`_intersection` 算(它与 GEOS 的
        # ``CGAlgorithmsDD`` 同为"平移到核包围盒中点"的条件化做法),
        # 不用 ``seg_seg_classify`` 内部的参数式解,免得同一件事两套浮点。
        p = _intersection(offset0[0], offset0[1], offset1[0], offset1[1])
        if p is not None:
            return p, True
    # 端点相触 / 共线重叠 —— GEOS 的 ``getIntersection(0)`` 就是取端点
    return pt, False


def _intersection_line_segment(line1, line2, s1, s2):
    """``Intersection.lineSegment`` —— 直线(过 line1/line2)与**线段** s1-s2 的交。"""
    o1 = orient(line1[0], line1[1], line2[0], line2[1], s1[0], s1[1])
    if o1 == 0.0:
        return s1
    o2 = orient(line1[0], line1[1], line2[0], line2[1], s2[0], s2[1])
    if o2 == 0.0:
        return s2
    if (o1 > 0.0 and o2 > 0.0) or (o1 < 0.0 and o2 < 0.0):
        return None
    p = _intersection(line1, line2, s1, s2)
    if p is not None:
        return p
    # 平行 —— 取离直线更近的那个端点(Intersection.lineSegment 的兜底)
    d1 = _point_line_perp(s1, line1, line2)
    d2 = _point_line_perp(s2, line1, line2)
    return s1 if d1 < d2 else s2


def _point_line_perp(p, a, b) -> float:
    """``Distance.pointToLinePerpendicular`` —— 点到**直线**的垂距。"""
    dx = b[0] - a[0]
    dy = b[1] - a[1]
    length = math.hypot(dx, dy)
    if length == 0.0:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    return abs((p[0] - a[0]) * dy - (p[1] - a[1]) * dx) / length


def _point_string_distance(p, pts) -> float:
    """``Distance.pointToSegmentString`` —— 点到折线的最短距离。

    ``pts`` 含闭合点(与 JTS 的坐标数组一致),闭合点让"回到起点"那一段
    自动被算进去。
    """
    if not pts:
        return float('inf')
    if len(pts) == 1:
        return math.hypot(p[0] - pts[0][0], p[1] - pts[0][1])
    best = float('inf')
    for i in range(len(pts) - 1):
        a = pts[i]
        b = pts[i + 1]
        d = _point_seg_distance(p[0], p[1], a[0], a[1], b[0], b[1])
        if d < best:
            best = d
    return best


# ----------------------------------------------------------------------------
# 偏移点表(OffsetSegmentString.java)
# ----------------------------------------------------------------------------
class _OffsetSegmentString:
    """偏移曲线的顶点表。

    ``addPt`` 干两件事:精度模型取整(本库默认是浮点精度模型 = **恒等**,
    只有降级重试时才会给出一个 scale),以及**最小顶点距剔除**。后者不是
    优化 —— 圆角上相邻两点可能重合,留着会让节点化产出零长线段。
    """

    __slots__ = ('pts', 'scale', 'min_vertex_distance')

    def __init__(self, scale: Optional[float], min_vertex_distance: float) -> None:
        self.pts: List[Tuple[float, float]] = []
        self.scale = scale
        self.min_vertex_distance = min_vertex_distance

    def add_pt(self, pt: Tuple[float, float]) -> None:
        if self.scale is not None:
            pt = (round(pt[0] * self.scale) / self.scale,
                  round(pt[1] * self.scale) / self.scale)
        pts = self.pts
        if pts:
            last = pts[-1]
            if math.hypot(pt[0] - last[0], pt[1] - last[1]) < self.min_vertex_distance:
                return
        pts.append(pt)

    def add_pts(self, pts: Sequence[Tuple[float, float]], forward: bool) -> None:
        if forward:
            for p in pts:
                self.add_pt(p)
        else:
            for p in reversed(pts):
                self.add_pt(p)

    def close_ring(self) -> None:
        if not self.pts:
            return
        if self.pts[0] == self.pts[-1]:
            return
        self.pts.append(self.pts[0])

    def coordinates(self) -> List[Tuple[float, float]]:
        return self.pts


# ----------------------------------------------------------------------------
# 偏移曲线生成(OffsetSegmentGenerator.java)
# ----------------------------------------------------------------------------
def _offset_segment(s1, s2, side: int, distance: float):
    """``OffsetSegmentGenerator.computeOffsetSegment``。

    法向是 ``(-uy, +ux)``,``ux/uy`` 是**按 sideSign 缩放后的**方向向量 ——
    所以 ``side == LEFT`` 时得到的是左法向(把方向向量逆时针转 90°)。
    """
    side_sign = 1.0 if side == _LEFT else -1.0
    dx = s2[0] - s1[0]
    dy = s2[1] - s1[1]
    length = math.hypot(dx, dy)
    if length == 0.0:
        # JTS 这里会得到 NaN;上游 addNextSegment 有 s1.equals(s2) 的短路,
        # initSideSegments 则被 clean() 保证不会有重复点。兜底成零偏移。
        return (s1, s2)
    ux = side_sign * distance * dx / length
    uy = side_sign * distance * dy / length
    return ((s1[0] - uy, s1[1] + ux), (s2[0] - uy, s2[1] + ux))


class _OffsetSegmentGenerator:
    """``OffsetSegmentGenerator``。

    一个有状态的"逐段推进"机器:``s0/s1/s2`` 是输入折线上连续三个点,
    ``offset0/offset1`` 是它们各自对应的偏移线段。外部按顺序喂进折线的顶点,
    内部按"转弯是共线/凸/凹"分派,把偏移曲线(含圆角、端帽、斜接)吐进
    ``seg_list``。
    """

    def __init__(self, distance: float, quad_segs: int, cap_style: int,
                 join_style: int, mitre_limit: float, scale: Optional[float],
                 simplify_factor: float) -> None:
        self.cap_style = cap_style
        self.join_style = join_style
        self.mitre_limit = mitre_limit
        self.simplify_factor = simplify_factor

        segs = quad_segs if quad_segs >= 1 else 1
        self.fillet_angle_quantum = _PI_OVER_2 / segs

        # 非圆角接合配短闭合段会出问题,所以只在"圆角接合 + 量化够细"时
        # 才把闭合段拉长(少造交点,显著提速)。见 OffsetSegmentGenerator。
        self.closing_seg_length_factor = (
            _MAX_CLOSING_SEG_LEN_FACTOR
            if quad_segs >= 8 and join_style == JOIN_ROUND else 1)

        self.distance = abs(distance)
        self.max_curve_segment_error = \
            self.distance * (1.0 - math.cos(self.fillet_angle_quantum / 2.0))
        self.seg_list = _OffsetSegmentString(
            scale, self.distance * _CURVE_VERTEX_SNAP_DISTANCE_FACTOR)

        self.s0: Optional[Tuple[float, float]] = None
        self.s1: Optional[Tuple[float, float]] = None
        self.s2: Optional[Tuple[float, float]] = None
        self.offset0: Optional[Tuple[Any, Any]] = None
        self.offset1: Optional[Tuple[Any, Any]] = None
        self.side = _LEFT
        self.has_narrow_concave_angle = False

    # -- 外部接口 -----------------------------------------------------------
    def init_side_segments(self, s1, s2, side: int) -> None:
        self.s1 = s1
        self.s2 = s2
        self.side = side
        self.offset1 = _offset_segment(s1, s2, side, self.distance)

    def add_first_segment(self) -> None:
        self.seg_list.add_pt(self.offset1[0])

    def add_last_segment(self) -> None:
        self.seg_list.add_pt(self.offset1[1])

    def add_segments(self, pts: Sequence[Tuple[float, float]], forward: bool) -> None:
        self.seg_list.add_pts(pts, forward)

    def close_ring(self) -> None:
        self.seg_list.close_ring()

    def coordinates(self) -> List[Tuple[float, float]]:
        return self.seg_list.coordinates()

    # -- 逐段推进 -----------------------------------------------------------
    def add_next_segment(self, p, add_start_point: bool) -> None:
        self.s0 = self.s1
        self.s1 = self.s2
        self.s2 = p
        self.offset0 = _offset_segment(self.s0, self.s1, self.side, self.distance)
        self.offset1 = _offset_segment(self.s1, self.s2, self.side, self.distance)

        if self.s1 == self.s2:
            return

        o = orient(self.s0[0], self.s0[1], self.s1[0], self.s1[1], self.s2[0], self.s2[1])
        if o > 0.0:
            orientation = _COUNTERCLOCKWISE
        elif o < 0.0:
            orientation = _CLOCKWISE
        else:
            orientation = _COLLINEAR

        outside_turn = ((orientation == _CLOCKWISE and self.side == _LEFT)
                        or (orientation == _COUNTERCLOCKWISE and self.side == _RIGHT))

        if orientation == _COLLINEAR:
            self._add_collinear(add_start_point)
        elif outside_turn:
            self._add_outside_turn(orientation, add_start_point)
        else:
            self._add_inside_turn()

    def _add_collinear(self, add_start_point: bool) -> None:
        """共线。

        只有 s0→s1 与 s1→s2 **反向**(折回去的尖刺)才要补东西:圆角接合时
        绕 s1 画一整圈(方向恒为顺时针 —— 这个分支只可能出现在 LineString 上,
        面环里出现反向共线就说明自交了),斜接/倒角则直接把两个偏移端点连起来。
        同向共线什么也不用加,两条偏移线本来就接着。
        """
        # JTS 用 RobustLineIntersector 判"两条共线线段是否重叠(numInt >= 2)",
        # 等价于 s0→s1 与 s1→s2 方向相反且非零长。
        if (self.s0 == self.s1) or (self.s1 == self.s2):
            return
        d0 = (self.s1[0] - self.s0[0], self.s1[1] - self.s0[1])
        d1 = (self.s2[0] - self.s1[0], self.s2[1] - self.s1[1])
        if d0[0] * d1[0] + d0[1] * d1[1] >= 0.0:
            return                       # 同向平行 —— 忽略
        if self.join_style in (JOIN_BEVEL, JOIN_MITRE):
            if add_start_point:
                self.seg_list.add_pt(self.offset0[1])
            self.seg_list.add_pt(self.offset1[0])
        else:
            self._add_corner_fillet(self.s1, self.offset0[1], self.offset1[0],
                                    _CLOCKWISE, self.distance)

    def _add_outside_turn(self, orientation: int, add_start_point: bool) -> None:
        """凸拐:偏移线在角外侧,要补圆角/斜接/倒角。"""
        o0p1 = self.offset0[1]
        o1p0 = self.offset1[0]
        if math.hypot(o0p1[0] - o1p0[0], o0p1[1] - o1p0[1]) \
                < self.distance * _OFFSET_SEGMENT_SEPARATION_FACTOR:
            # 近似平行 —— 两个偏移端点几乎重合。**无条件取 `offset0.p1`** 当唯一
            # 角点：既少一个顶点，也躲开"斜接角在近平行时算不准"的鲁棒性问题。
            #
            # ⚠️ **这里跟 JTS master 故意不一样。** JTS 后来加了一句
            # ``segLen0 > segLen1 ? offset0.p1 : offset1.p0``（注释写
            # "use endpoint of longest segment, to reduce change in area"），
            # 而 **GEOS 3.12 / 3.13 / main 至今仍是无条件 ``offset0.p1``**。
            # 本库选择跟 GEOS 走，两条理由：
            #   1. ``OGRGeometry::Buffer`` 转发到的就是 GEOS —— 本项目的口径是
            #      "严格按 GDAL 的来"，而对拍脚本的 oracle 也是 GEOS；
            #   2. 两者在近平行处的差别有 4e-4 量级（见 ``tools/verify_buffer.py``
            #      的 ``ln_shallow``），跟错边会让对拍里多出一整类"解释得通但
            #      看不出对错"的偏差。
            # 代价：面积与 JTS master 的结果有 ~1e-16 相对差，可忽略。
            self.seg_list.add_pt(o0p1)
            return

        if self.join_style == JOIN_MITRE:
            self._add_mitre_join(self.s1, self.offset0, self.offset1, self.distance)
        elif self.join_style == JOIN_BEVEL:
            self.seg_list.add_pt(o0p1)
            self.seg_list.add_pt(o1p0)
        else:
            if add_start_point:
                self.seg_list.add_pt(o0p1)
            self._add_corner_fillet(self.s1, o0p1, o1p0, orientation, self.distance)
            self.seg_list.add_pt(o1p0)

    def _add_inside_turn(self) -> None:
        """凹拐:偏移线在角内侧,取两条偏移线段的交点即可。"""
        p, _proper = _seg_intersection(self.offset0, self.offset1)
        if p is not None:
            self.seg_list.add_pt(p)
            return

        # 没有交点 = 角太尖 / 偏移太大,两条偏移线段根本不相交。这时必须补一段
        # "闭合段",否则偏移曲线会断开。这段线**永远落在缓冲多边形内部**,
        # 所以不会出现在最终轮廓上;它的唯一作用是把曲线连起来,并且别太短
        # (太短会切穿别的段,太长的也不行)—— 于是有了 closingSegLengthFactor。
        self.has_narrow_concave_angle = True
        o0p1 = self.offset0[1]
        o1p0 = self.offset1[0]
        if math.hypot(o0p1[0] - o1p0[0], o0p1[1] - o1p0[1]) \
                < self.distance * _INSIDE_TURN_VERTEX_SNAP_DISTANCE_FACTOR:
            # 两个端点近到"本该有交点、只是浮点没算出来"的程度 —— 直接用一个
            self.seg_list.add_pt(o0p1)
            return

        self.seg_list.add_pt(o0p1)
        f = self.closing_seg_length_factor
        s1 = self.s1
        if f > 0:
            self.seg_list.add_pt(((f * o0p1[0] + s1[0]) / (f + 1),
                                  (f * o0p1[1] + s1[1]) / (f + 1)))
            self.seg_list.add_pt(((f * o1p0[0] + s1[0]) / (f + 1),
                                  (f * o1p0[1] + s1[1]) / (f + 1)))
        else:
            # 只在测试路径上会走(等价于 JTS 1.9 的老逻辑)
            self.seg_list.add_pt(s1)
        self.seg_list.add_pt(o1p0)

    # -- 角部构造 -----------------------------------------------------------
    def _add_corner_fillet(self, p, p0, p1, direction: int, radius: float) -> None:
        """绕 ``p`` 画一段从 ``p0`` 到 ``p1`` 的圆弧(``addCornerFillet``)。

        ``startAngle``/``endAngle`` 由 ``p`` 指向两个端点的方位角给出;按方向
        把 ``startAngle`` 推开一整圈,保证沿指定方向走的是**那条弧**而不是补集。
        """
        start = math.atan2(p0[1] - p[1], p0[0] - p[0])
        end = math.atan2(p1[1] - p[1], p1[0] - p[0])
        if direction == _CLOCKWISE:
            if start <= end:
                start += _PI_2
        else:
            if start >= end:
                start -= _PI_2
        self.seg_list.add_pt(p0)
        self._add_directed_fillet(p, start, end, direction, radius)
        self.seg_list.add_pt(p1)

    def _add_directed_fillet(self, p, start_angle: float, end_angle: float,
                             direction: int, radius: float) -> None:
        """按指定方向、按 ``filletAngleQuantum`` 量化,吐出一段圆弧上的点。

        ⚠️ **端点是内接的**:点在半径恰为 ``radius`` 的圆上,角度从
        ``startAngle`` 起等分。实测 GEOS 就是这样(``Point(0,0).buffer(1)``
        的圆半径 min/max 都是 1.0,64 个顶点等角距),不是"切点多边形的
        外接版"。首尾两个端点不在这里加,由调用方补。
        """
        direction_factor = -1 if direction == _CLOCKWISE else 1
        total = abs(start_angle - end_angle)
        n_segs = int(total / self.fillet_angle_quantum + 0.5)
        if n_segs < 1:
            return
        angle_inc = total / n_segs
        add = self.seg_list.add_pt
        for i in range(n_segs):
            a = start_angle + direction_factor * i * angle_inc
            add((p[0] + radius * _cos_snap(a), p[1] + radius * _sin_snap(a)))

    def _add_mitre_join(self, corner_pt, offset0, offset1, distance: float) -> None:
        """斜接角(``addMitreJoin``):优先尖角,超限再考虑倒角/限长斜接。"""
        limit = self.mitre_limit * distance
        p = _intersection(offset0[0], offset0[1], offset1[0], offset1[1])
        if p is not None and math.hypot(p[0] - corner_pt[0], p[1] - corner_pt[1]) <= limit:
            self.seg_list.add_pt(p)
            return
        # mitre_limit 很小时先试纯倒角 —— 它更远就用它
        bevel_dist = _point_seg_distance(corner_pt[0], corner_pt[1],
                                         offset0[1][0], offset0[1][1],
                                         offset1[0][0], offset1[0][1])
        if bevel_dist >= limit:
            self.seg_list.add_pt(offset0[1])
            self.seg_list.add_pt(offset1[0])
            return
        self._add_limited_mitre_join(offset0, offset1, distance, limit)

    def _add_limited_mitre_join(self, offset0, offset1, distance: float,
                                limit: float) -> None:
        """限长斜接(``addLimitedMitreJoin``):把尖角在 ``limit`` 处切平。

        这是 JTS 1.19 起才有的做法 —— 老实现是把尖角整个换成倒角,尖角不够尖的
        场合会削掉一大块面积(实测 ``mitre_limit`` 很小时肉眼可见)。限长斜接
        则在斜接方向的 ``limit`` 处下刀,两条偏移线的交点就是新的角点。
        """
        corner_pt = self.s1
        ang_interior = _angle_between_oriented(self.s0, corner_pt, self.s2)
        dir0 = math.atan2(self.s0[1] - corner_pt[1], self.s0[0] - corner_pt[0])
        dir_bisector = _normalize_angle(dir0 + ang_interior / 2.0)
        bevel_mid = (corner_pt[0] + (-limit) * _cos_snap(dir_bisector),
                     corner_pt[1] + (-limit) * _sin_snap(dir_bisector))
        dir_bevel = _normalize_angle(dir_bisector + _PI_OVER_2)
        bevel0 = (bevel_mid[0] + distance * _cos_snap(dir_bevel),
                  bevel_mid[1] + distance * _sin_snap(dir_bevel))
        bevel1 = (bevel_mid[0] + distance * _cos_snap(dir_bevel + math.pi),
                  bevel_mid[1] + distance * _sin_snap(dir_bevel + math.pi))
        i0 = _intersection_line_segment(offset0[0], offset0[1], bevel0, bevel1)
        i1 = _intersection_line_segment(offset1[0], offset1[1], bevel0, bevel1)
        if i0 is not None and i1 is not None:
            self.seg_list.add_pt(i0)
            self.seg_list.add_pt(i1)
            return
        # 角很平或者 mitre_limit 很小 —— 限长斜接切不到两条偏移线,退回倒角
        self.seg_list.add_pt(offset0[1])
        self.seg_list.add_pt(offset1[0])

    # -- 端帽 ---------------------------------------------------------------
    def add_line_end_cap(self, p0, p1) -> None:
        """在 ``p1`` 处画端帽(线段从 ``p0`` 来)``addLineEndCap``。"""
        seg = (p0, p1)
        offset_l = _offset_segment(*seg, _LEFT, self.distance)
        offset_r = _offset_segment(*seg, _RIGHT, self.distance)
        angle = math.atan2(p1[1] - p0[1], p1[0] - p0[0])

        if self.cap_style == CAP_ROUND:
            self.seg_list.add_pt(offset_l[1])
            self._add_directed_fillet(p1, angle + _PI_OVER_2, angle - _PI_OVER_2,
                                      _CLOCKWISE, self.distance)
            self.seg_list.add_pt(offset_r[1])
        elif self.cap_style == CAP_FLAT:
            self.seg_list.add_pt(offset_l[1])
            self.seg_list.add_pt(offset_r[1])
        else:
            # 方形端帽 = 两条偏移线的端点各自沿原方向外推 distance
            ox = abs(self.distance) * _cos_snap(angle)
            oy = abs(self.distance) * _sin_snap(angle)
            self.seg_list.add_pt((offset_l[1][0] + ox, offset_l[1][1] + oy))
            self.seg_list.add_pt((offset_r[1][0] + ox, offset_r[1][1] + oy))

    # -- 点 ----------------------------------------------------------------
    def create_circle(self, p) -> None:
        """绕点的**顺时针**圆(``createCircle``)。起点在 ``(p.x + d, p.y)``。"""
        self.seg_list.add_pt((p[0] + self.distance, p[1]))
        self._add_directed_fillet(p, 0.0, _PI_2, -1, self.distance)
        self.seg_list.close_ring()

    def create_square(self, p) -> None:
        """绕点的**顺时针**正方形(``createSquare``)。"""
        d = self.distance
        self.seg_list.add_pt((p[0] + d, p[1] + d))
        self.seg_list.add_pt((p[0] + d, p[1] - d))
        self.seg_list.add_pt((p[0] - d, p[1] - d))
        self.seg_list.add_pt((p[0] - d, p[1] + d))
        self.seg_list.close_ring()


# ----------------------------------------------------------------------------
# 输入折线预处理(BufferInputLineSimplifier.java)
# ----------------------------------------------------------------------------
_NUM_PTS_TO_CHECK = 10


def _simplify_input_line(input_line: List[Tuple[float, float]],
                         distance_tol: float) -> List[Tuple[float, float]]:
    """``BufferInputLineSimplifier.simplify`` —— 缓冲区专用的"削浅凹角"。

    注意它**不是** Douglas–Peucker:它只删"凹进去且很浅"的中间点,而且是一遍遍
    扫到不再变化为止。凸点一个都不动。作用是把输入折线上那些深度远小于
    ``0.01 * distance`` 的锯齿先抹平 —— 它们对最终缓冲轮廓没有影响,但会让
    偏移曲线多出一堆自交,节点化要白算。

    ⚠️ **``distance_tol`` 的符号决定删哪一侧**,不是"随便一层 abs";这一条
    本轮踩过坑,写清楚:

    * ``tol >= 0`` → ``angleOrientation = COUNTERCLOCKWISE`` → 删**逆时针(左)**
      的浅凹点;
    * ``tol < 0`` → ``angleOrientation = CLOCKWISE`` → 删**顺时针(右)**的浅凹点。

    上游原话:``this.distanceTol = Math.abs(distanceTol); angleOrientation =
    COUNTERCLOCKWISE; if (distanceTol < 0) angleOrientation = CLOCKWISE;`` ——
    取绝对值的是**字段**,判符号的是**参数**,两者不是同一个东西。(这里一度
    把它读成"取完绝对值再判符号,所以恒为逆时针、是死代码",于是把符号丢了;
    后果是 :func:`_compute_ring_buffer_curve` 给右半边传的 ``-distTol`` 失效,
    10×0.001 那种细长面的外扩曲线被削成一条来回跑的死环 —— 面积恰好差一半,
    还不一定看得出来。)

    ``isShallowSampled`` 那段**照 GEOS 走,不照 JTS**——这里 JTS 与 GEOS 的
    **实参顺序不一样**,而且后果不小::

        JTS   isShallow(p0, inputLine[i], p2)   -> pointToSegment(inputLine[i], p0, 中间点)
        GEOS  isShallow(p0, p2=中间点, inputLine[i]) -> pointToSegment(中间点, p0, inputLine[i])

    函数体两边一模一样(``pointToSegment(第2个实参, 第1个, 第3个)``),差的是**传
    进去的三项的顺序**。在 ``i == i0`` 上最明显:JTS 算"``p0`` 到线段
    ``(p0, 中间点)``"——``p0`` 是端点,恒为 0,永远通过;GEOS 算"**中间点**到
    退化线段 ``(p0, p0)``",也就是 ``|中间点 - p0|``,基本不会小于 ``tol``。
    换句话说 GEOS 这一条**严得多**。
    ⚠️ 看着像 GEOS 写错了,但它是 GEOS 的**实际行为**,而本模块以 GEOS 为参照
    (GDAL 转发 GEOS)。实测:``乡行政区划`` 的 ``POLYGON`` 在 ``d=1``、55 个源
    顶点上,按 JTS 顺序削掉 8 个浅凹点、输出 127 个顶点;按 GEOS 顺序一个都不削、
    输出 136 个 —— 逐位相同。整份语料 260 对:JTS 顺序 112 对逐位相同 / 148 对
    超差,GEOS 顺序 **250 对逐位相同 / 0 对超差**。
    (原先这里写的是"照抄 JTS、别修正",并解释了 JTS 那个"形参名 ``p2`` 收的
    其实是中间点"的观感——那句只对**形参命名**成立,实参顺序两边确实不同。)

    同理,主循环的起点也照 GEOS:**恒从 ``index = 1`` 开始**。JTS 有一行
    ``int index = isRing ? 0 : 1;``(环从 0 起),GEOS 的
    ``BufferInputLineSimplifier`` **根本没有 ``isRing`` 这个成员**,一律从 1 起。
    对环来说这只影响"数组第 1 个顶点能不能被删"以及整条窗口的相位,但既然以
    GEOS 为准,就照 1 起。
    """
    tol = abs(distance_tol)
    angle_orientation = _CLOCKWISE if distance_tol < 0 else _COUNTERCLOCKWISE
    n = len(input_line)
    deleted = [False] * n

    def next_index(i: int) -> int:
        j = i + 1
        while j < n and deleted[j]:
            j += 1
        return j

    def is_shallow(p0, p1, p2) -> bool:
        return _point_seg_distance(p1[0], p1[1], p0[0], p0[1],
                                   p2[0], p2[1]) < tol

    def is_shallow_sampled(p0, mid, i0: int, i2: int) -> bool:
        """GEOS ``isShallowSampled`` —— 比的是"**中间点**到 ``(p0, 第 i 点)``"。"""
        inc = (i2 - i0) // _NUM_PTS_TO_CHECK
        if inc <= 0:
            inc = 1
        i = i0
        while i < i2:
            if not is_shallow(p0, mid, input_line[i]):
                return False
            i += inc
        return True

    def is_deletable(i0: int, i1: int, i2: int) -> bool:
        p0, p1, p2 = input_line[i0], input_line[i1], input_line[i2]
        o = orient(p0[0], p0[1], p1[0], p1[1], p2[0], p2[1])
        orientation = _COUNTERCLOCKWISE if o > 0.0 else (_CLOCKWISE if o < 0.0 else _COLLINEAR)
        if orientation != angle_orientation:
            return False
        if not is_shallow(p0, p1, p2):
            return False
        return is_shallow_sampled(p0, p1, i0, i2)

    changed = True
    while changed:
        changed = False
        index = 1
        mid = next_index(index)
        last = next_index(mid)
        while last < n:
            middle_deleted = False
            if is_deletable(index, mid, last):
                deleted[mid] = True
                middle_deleted = True
                changed = True
            index = last if middle_deleted else mid
            mid = next_index(index)
            last = next_index(mid)

    return [p for i, p in enumerate(input_line) if not deleted[i]]


# ----------------------------------------------------------------------------
# 曲线装配(OffsetCurveBuilder.java)
# ----------------------------------------------------------------------------
class _OffsetCurveBuilder:
    """把一条输入折线/环变成一条/两条原始偏移曲线。"""

    def __init__(self, scope: '_BufferScope') -> None:
        self.scope = scope

    # -- 便捷 -------------------------------------------------------------
    def _seg_gen(self) -> _OffsetSegmentGenerator:
        s = self.scope
        return _OffsetSegmentGenerator(s.distance, s.quad_segs, s.cap_style,
                                       s.join_style, s.mitre_limit, s.scale,
                                       s.simplify_factor)

    def is_line_offset_empty(self, distance: float) -> bool:
        """``isLineOffsetEmpty`` —— 零宽的线/点缓冲是空;负宽也是空(单侧除外)。"""
        if distance == 0.0:
            return True
        if distance < 0.0 and not self.scope.single_sided:
            return True
        return False

    # -- 线 / 点 -----------------------------------------------------------
    def get_line_curve(self, input_pts, distance):
        if self.is_line_offset_empty(distance):
            return None
        pos = abs(distance)
        seg_gen = self._seg_gen()
        if len(input_pts) <= 1:
            self._compute_point_curve(input_pts[0], seg_gen)
        elif self.scope.single_sided:
            self._compute_single_sided_curve(input_pts, distance < 0.0, seg_gen)
        else:
            self._compute_line_buffer_curve(input_pts, pos, seg_gen)
        return seg_gen.coordinates()

    def _compute_point_curve(self, pt, seg_gen) -> None:
        if self.scope.cap_style == CAP_ROUND:
            seg_gen.create_circle(pt)
        elif self.scope.cap_style == CAP_SQUARE:
            seg_gen.create_square(pt)
        # CAP_FLAT:什么也不加 —— 点的平头缓冲就是空

    def _compute_line_buffer_curve(self, pts, distance: float, seg_gen) -> None:
        """一整圈:左半边 -> 端帽 -> 右半边(反着走)-> 端帽 -> 收口。

        ⚠️ 两侧**各自**先简化:``simplifyTolerance(d)`` 与 ``-simplifyTolerance(d)``
        删的是不同侧的凹角(JTS 原文如此,注释说"simplify the appropriate side")。
        """
        tol = distance * self.scope.simplify_factor

        simp1 = _simplify_input_line(pts, tol)
        n1 = len(simp1) - 1
        seg_gen.init_side_segments(simp1[0], simp1[1], _LEFT)
        for i in range(2, n1 + 1):
            seg_gen.add_next_segment(simp1[i], True)
        seg_gen.add_last_segment()
        seg_gen.add_line_end_cap(simp1[n1 - 1], simp1[n1])

        # 反着走右侧 —— 因为遍历方向反了,偏移位置仍然是 LEFT(JTS 原注释)
        simp2 = _simplify_input_line(pts, -tol)
        n2 = len(simp2) - 1
        seg_gen.init_side_segments(simp2[n2], simp2[n2 - 1], _LEFT)
        for i in range(n2 - 2, -1, -1):
            seg_gen.add_next_segment(simp2[i], True)
        seg_gen.add_last_segment()
        seg_gen.add_line_end_cap(simp2[1], simp2[0])
        seg_gen.close_ring()

    def _compute_single_sided_curve(self, pts, is_right_side: bool,
                                    seg_gen) -> None:
        """单侧缓冲:把**原线**也当成曲线的一部分,最后收口。

        这样整条曲线仍然是闭合的,可以走同一条"面深度"管线。
        """
        tol = self.scope.distance * self.scope.simplify_factor
        if is_right_side:
            seg_gen.add_segments(pts, True)
            simp2 = _simplify_input_line(pts, -tol)
            n2 = len(simp2) - 1
            seg_gen.init_side_segments(simp2[n2], simp2[n2 - 1], _LEFT)
            seg_gen.add_first_segment()
            for i in range(n2 - 2, -1, -1):
                seg_gen.add_next_segment(simp2[i], True)
        else:
            seg_gen.add_segments(pts, False)
            simp1 = _simplify_input_line(pts, tol)
            n1 = len(simp1) - 1
            seg_gen.init_side_segments(simp1[0], simp1[1], _LEFT)
            seg_gen.add_first_segment()
            for i in range(2, n1 + 1):
                seg_gen.add_next_segment(simp1[i], True)
        seg_gen.add_last_segment()
        seg_gen.close_ring()

    # -- 环 ----------------------------------------------------------------
    def get_ring_curve(self, input_pts, side: int, distance: float):
        if len(input_pts) <= 2:
            return self.get_line_curve(input_pts, distance)
        if distance == 0.0:
            return list(input_pts)
        seg_gen = self._seg_gen()
        self._compute_ring_buffer_curve(input_pts, side, distance, seg_gen)
        return seg_gen.coordinates()

    def _compute_ring_buffer_curve(self, pts, side: int, distance: float,
                                   seg_gen) -> None:
        tol = distance * self.scope.simplify_factor
        if side == _RIGHT:
            tol = -tol
        simp = _simplify_input_line(pts, tol)
        n = len(simp) - 1
        seg_gen.init_side_segments(simp[n - 1], simp[0], side)
        for i in range(1, n + 1):
            seg_gen.add_next_segment(simp[i], i != 1)
        seg_gen.close_ring()


# ----------------------------------------------------------------------------
# 共享参数(BufferParameters + 降级梯带下来的东西)
# ----------------------------------------------------------------------------
class _BufferScope:
    """一次 buffer 计算的全部参数。

    ⚠️ ``distance`` 是**带符号**的。JTS ``BufferCurveSetBuilder`` 的字段就是
    带符号的:``addPoint`` 靠 ``distance <= 0`` 判空、``addPolygon`` 靠
    ``distance < 0`` 决定侵蚀方向和左右标签互换、``isLineOffsetEmpty`` 靠
    ``distance < 0`` 判"负宽的线缓冲是空"。取绝对值这件事**只发生在
    ``OffsetSegmentGenerator`` 内部**(它自己 ``Math.abs``),以及
    ``getLineCurve`` 算 ``posDistance`` 那一处。早先在这里就 ``abs()`` 过一次,
    结果 ``buffer(-1)`` 的点/线不退化成空、面的侵蚀方向也全是反的 —— 而且
    正方形缓冲在正负距离下"看起来都像那么回事",特别能骗人。
    """

    __slots__ = ('distance', 'quad_segs', 'cap_style', 'join_style',
                 'mitre_limit', 'single_sided', 'simplify_factor', 'scale')

    def __init__(self, distance: float, quad_segs: int, cap_style: int,
                 join_style: int, mitre_limit: float, single_sided: bool,
                 simplify_factor: float = DEFAULT_SIMPLIFY_FACTOR,
                 scale: Optional[float] = None) -> None:
        self.distance = distance
        self.quad_segs = quad_segs
        self.cap_style = cap_style
        self.join_style = join_style
        self.mitre_limit = mitre_limit
        self.single_sided = single_sided
        self.simplify_factor = simplify_factor
        self.scale = scale


def _is_ring(coords: Sequence[Tuple[float, float]]) -> bool:
    """``CoordinateArrays.isRing``。"""
    return len(coords) >= 4 and coords[0] == coords[-1]


def _envelope(coords) -> Tuple[float, float, float, float]:
    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    return (min(xs), min(ys), max(xs), max(ys))


def _is_ccw_area(coords) -> bool:
    """``Orientation.isCCWArea`` —— 鞋带和为正 = 逆时针。

    ⚠️ 复用 :func:`_geometry_ops.ring_signed_area2`(唯一的鞋带实现,先平移
    再顺序累加)。这里只取符号,不影响写盘,但仍然不换写法。
    """
    return ring_signed_area2(array('d', chain.from_iterable(coords))) > 0.0


def _triangle_incentre(a, b, c):
    """``Triangle.inCentre``。"""
    la = math.hypot(b[0] - c[0], b[1] - c[1])
    lb = math.hypot(c[0] - a[0], c[1] - a[1])
    lc = math.hypot(a[0] - b[0], a[1] - b[1])
    s = la + lb + lc
    return ((la * a[0] + lb * b[0] + lc * c[0]) / s,
            (la * a[1] + lb * b[1] + lc * c[1]) / s)


def _is_triangle_eroded(coord, distance: float) -> bool:
    """``BufferCurveSetBuilder.isTriangleErodedCompletely``。"""
    centre = _triangle_incentre(coord[0], coord[1], coord[2])
    d = _point_seg_distance(centre[0], centre[1], coord[0][0], coord[0][1],
                            coord[1][0], coord[1][1])
    return d < abs(distance)


def _is_ring_fully_eroded(coord, env, is_hole: bool, buffer_distance: float) -> bool:
    """``BufferCurveSetBuilder.isRingFullyEroded`` —— 这个环会被完全蚀掉吗。

    三条判据依次收紧:顶点数不足 4 → 一定蚀光;恰好 4(三角形)→ 用内切圆
    半径判;否则只在"该被蚀的方向"上(洞配正距离 / 壳配负距离)用"包围盒最短边
    小于两倍距离"来判。少任何一条都会产出反了向的空洞(三角形那条是
    JTS 注释里点名的 "inverted triangle bug")。
    """
    if len(coord) < 4:
        return True
    if len(coord) == 4:
        return _is_triangle_eroded(coord, buffer_distance)
    erodable = (is_hole and buffer_distance > 0.0) or \
               ((not is_hole) and buffer_distance < 0.0)
    if erodable:
        min_dim = min(env[3] - env[1], env[2] - env[0])
        if 2.0 * abs(buffer_distance) > min_dim:
            return True
    return False


def _has_point_on_buffer(input_ring, distance: float, curve_ring) -> bool:
    """``BufferCurveSetBuilder.hasPointOnBuffer``。

    曲线上**有没有哪个点确实跑到了 ``0.99 * distance`` 之外**。有一点超出就说明
    这条曲线是真的在缓冲,不是翻进去的。
    """
    tol = _NEARNESS_FACTOR * abs(distance)
    n = len(curve_ring)
    if n < 2:
        return False
    for i in range(n - 1):
        v = curve_ring[i]
        if _point_string_distance(v, input_ring) > tol:
            return True
        v_next = curve_ring[i + 1]
        mid = ((v[0] + v_next[0]) / 2.0, (v[1] + v_next[1]) / 2.0)
        if _point_string_distance(mid, input_ring) > tol:
            return True
    return False


def _is_ring_curve_inverted(input_ring, distance: float, curve_ring) -> bool:
    """``BufferCurveSetBuilder.isRingCurveInverted`` —— 这条环曲线是不是翻进去了。

    只对**很小的环**做这个检查(顶点数 < 9),而且只有在"曲线顶点数没有爆炸"
    (小于输入的 4 倍)时才怀疑,最后由 ``hasPointOnBuffer`` 一票否决。三条
    一起才拦得住"把外翻的曲线当成内翻的扔掉"这类静默错误。
    """
    if distance == 0.0:
        return False
    if len(input_ring) <= 3:
        return False
    if len(input_ring) >= _MAX_INVERTED_RING_SIZE:
        return False
    if len(curve_ring) > _INVERTED_CURVE_VERTEX_FACTOR * len(input_ring):
        return False
    if _has_point_on_buffer(input_ring, distance, curve_ring):
        return False
    return True


# ----------------------------------------------------------------------------
# 曲线集组装(BufferCurveSetBuilder.java)
# ----------------------------------------------------------------------------
# 一条原始偏移曲线:(顶点表, LEFT 位置, RIGHT 位置)
_Curve = Tuple[List[Tuple[float, float]], int, int]


class _CurveSet:
    """把输入几何摊成一组带标签的原始偏移曲线。"""

    def __init__(self, scope: _BufferScope) -> None:
        self.scope = scope
        self.builder = _OffsetCurveBuilder(scope)
        self.curves: List[_Curve] = []

    def add_curve(self, coord, left_loc: int, right_loc: int) -> None:
        """``addCurve`` —— 太短的曲线直接丢。

        标签是 ``Label(0, Location.BOUNDARY, leftLoc, rightLoc)``:第一个位置
        (ON)是 BOUNDARY,后两个是左右两侧。buffer 全流程**只用到 left/right
        这两个** —— ``depthDelta`` 只读它们,``isInteriorAreaEdge`` 恒为假。
        """
        if coord is None or len(coord) < 2:
            return
        self.curves.append((list(coord), left_loc, right_loc))

    # -- 分派 --------------------------------------------------------------
    def add_point(self, pt) -> None:
        if self.scope.distance <= 0.0:
            return                       # 零宽/负宽的点缓冲是空
        curve = self.builder.get_line_curve([pt], self.scope.distance)
        self.add_curve(curve, EXTERIOR, INTERIOR)

    def add_line_string(self, coord) -> None:
        if self.builder.is_line_offset_empty(self.scope.distance):
            return
        coord = _clean(coord)
        if not coord:
            return
        if _is_ring(coord) and not self.scope.single_sided:
            self._add_linear_ring_sides(coord)
        else:
            curve = self.builder.get_line_curve(coord, self.scope.distance)
            self.add_curve(curve, EXTERIOR, INTERIOR)

    def add_polygon(self, shell, holes) -> None:
        d = self.scope.distance
        offset_distance = abs(d)
        offset_side = _RIGHT if d < 0.0 else _LEFT

        clean_shell = _clean(shell)
        env = _envelope(clean_shell) if clean_shell else None
        if d < 0.0 and env is not None and \
                _is_ring_fully_eroded(clean_shell, env, False, d):
            return
        if not clean_shell:
            return
        if d <= 0.0 and len(clean_shell) < 3:
            return
        self._add_polygon_ring_side(clean_shell, offset_distance, offset_side,
                                    EXTERIOR, INTERIOR)

        for hole in holes:
            clean_hole = _clean(hole)
            if d > 0.0 and clean_hole:
                henv = _envelope(clean_hole)
                if _is_ring_fully_eroded(clean_hole, henv, True, d):
                    continue             # 洞会被完全填平 —— 不必算
            if not clean_hole:
                continue
            # 洞的标签与壳**相反**:多边形的内部在洞的另一侧
            self._add_polygon_ring_side(
                clean_hole, offset_distance,
                _RIGHT if offset_side == _LEFT else _LEFT, INTERIOR, EXTERIOR)

    def _add_polygon_ring_side(self, coord, offset_distance: float, side: int,
                               cw_left_loc: int, cw_right_loc: int) -> None:
        if offset_distance == 0.0 and len(coord) < MINIMUM_VALID_SIZE:
            return
        left_loc = cw_left_loc
        right_loc = cw_right_loc
        if len(coord) >= MINIMUM_VALID_SIZE and _is_ccw_area(coord):
            # 环是逆时针的 —— 左右标签与侧别都要翻
            left_loc = cw_right_loc
            right_loc = cw_left_loc
            side = _RIGHT if side == _LEFT else _LEFT
        self._add_ring_side(coord, offset_distance, side, left_loc, right_loc)

    def _add_linear_ring_sides(self, coord) -> None:
        """闭合折线当成环处理:两侧各画一条(一侧是壳、一侧是洞)。

        ``isHoleComputed`` 那个判断是为 GEOS issue #1223 加的:如果"洞"那一侧
        会被完全蚀掉就不用画了,否则会留下假的洞。
        """
        d = self.scope.distance
        env = _envelope(coord)
        is_hole_computed = not _is_ring_fully_eroded(coord, env, True, d)
        is_ccw = _is_ccw_area(coord)
        if (not is_ccw) or is_hole_computed:
            self._add_ring_side(coord, d, _LEFT, EXTERIOR, INTERIOR)
        if is_ccw or is_hole_computed:
            self._add_ring_side(coord, d, _RIGHT, INTERIOR, EXTERIOR)

    def _add_ring_side(self, coord, offset_distance: float, side: int,
                       left_loc: int, right_loc: int) -> None:
        curve = self.builder.get_ring_curve(coord, side, offset_distance)
        if curve is None or _is_ring_curve_inverted(coord, offset_distance, curve):
            return
        self.add_curve(curve, left_loc, right_loc)


# ----------------------------------------------------------------------------
# 顶层
# ----------------------------------------------------------------------------
def _precision_scale_factor(max_abs: float, distance: float, digits: int) -> float:
    """``BufferOp.precisionScaleFactor`` —— 按**包围盒量级**定 10 的幂。

    降精度不是"随便小一点":要让小数位刚好够描述"缓冲盒"里的最小单位,
    ``MAX_PRECISION_DIGITS - 缓冲盒的量级位数``。照抄 JTS,免得降级梯的行为
    与上游对不上。
    """
    expand = distance if distance > 0.0 else 0.0
    buf_env_max = max_abs + 2.0 * expand
    if buf_env_max <= 0.0:
        buf_env_max = 1.0
    env_digits = int(math.log10(buf_env_max) + 1.0)
    return math.pow(10.0, digits - env_digits)


def _buffer_once(kind, parts, shells, distance, quad_segs, cap, join,
                 mitre_limit, single_sided, scale):
    """跑一遍完整管线(``BufferBuilder.buffer`` + ``BufferOp`` 的取结果部分)。

    ``scale`` 不为 ``None`` 时是降级重算 —— 精度模型会作用在**偏移曲线的顶点
    生成**上(``OffsetSegmentString.addPt`` 的 ``makePrecise``),这是 JTS
    ``bufferReducedPrecision`` 全部的作用所在。
    """
    scope = _BufferScope(distance, quad_segs, cap, join, mitre_limit,
                         single_sided, DEFAULT_SIMPLIFY_FACTOR, scale)
    cs = _CurveSet(scope)
    n_points = 0
    n_shells = 0

    if kind == 'point' or kind == 'multipoint':
        for arr in parts:
            coords = _coords(arr)
            n_points += len(coords)
            for p in coords:
                cs.add_point(p)
        n_geoms = _num_geometries(kind, parts, n_points, 0)
    elif kind == 'polyline':
        for arr in parts:
            cs.add_line_string(_coords(arr))
        n_geoms = _num_geometries(kind, parts, 0, 0)
    elif kind == 'polygon':
        groups = organize_polygons(parts, shells)
        n_shells = len(groups)
        for si, hole_idx in groups:
            cs.add_polygon(_close_ring(_clean_ring(_coords(parts[si]))),
                           [_close_ring(_clean_ring(_coords(parts[h])))
                            for h in hole_idx])
        n_geoms = _num_geometries(kind, parts, 0, n_shells)
    else:
        return None

    if not cs.curves:
        return None

    node_pts, eu, ev, edelta = _noded_edges(cs.curves)
    if not eu:
        return None

    graph = _PlanarGraph(node_pts, eu, ev, edelta)
    face_of, faces = graph.walks()
    depth = _face_depths(graph, face_of, faces)
    rings = _result_rings(graph, face_of, depth)
    if not rings:
        return None
    # 第三项是 z 数组 —— buffer 不给 node_z,所以拿到的是 None,直接丢掉。
    parts_out, shells_out, _zs = _assemble(rings, node_pts, n_geoms, distance)
    return parts_out, shells_out


def _max_abs_coord(parts) -> float:
    m = 0.0
    for arr in parts:
        for v in arr:
            v = abs(v)
            if v > m:
                m = v
    return m


def _buffer_by_zero(kind, parts, shells):
    """``BufferOp.bufferByZero`` —— 零距离缓冲。

    * **面** → 顶点去重后的自身副本(绕向统一成 Esri:壳顺时针、洞逆时针);
    * **点 / 线** → **空**多边形。零宽的线和点没有面积,这是 JTS 的显式分支,
      不是"顺手"。

    实测 GEOS 对 ``POLYGON((0 0,10 0,10 10,0 10)).buffer(0)`` 的返回是
    ``POLYGON ((0 0, 0 10, 10 10, 10 0, 0 0))`` —— 输入是逆时针,输出是顺时针,
    正是"绕向被归一化了"。
    """
    if kind != 'polygon' or not parts:
        return None
    out_parts = []
    out_shells: List[bool] = []
    for si, holes in organize_polygons(parts, shells):
        for idx, is_shell in [(si, True)] + [(hj, False) for hj in holes]:
            pts = _clean_ring(_coords(parts[idx]))
            if len(pts) < 3:
                continue
            arr = array('d', chain.from_iterable(pts))
            a2 = ring_signed_area2(arr)
            if a2 == 0.0:
                continue
            want_ccw = not is_shell
            if (a2 > 0.0) != want_ccw:
                pts = list(reversed(pts))
            out_parts.append(array('d', chain.from_iterable(pts)))
            out_shells.append(is_shell)
    if not out_parts:
        return None
    return out_parts, out_shells


def buffer_parts(kind: str, parts, shells, distance: float,
                 quad_segs: int = DEFAULT_QUADRANT_SEGMENTS,
                 cap: Any = 'round',
                 join: Any = 'round',
                 mitre_limit: float = DEFAULT_MITRE_LIMIT,
                 single_sided: bool = False):
    """缓冲一个 2D 几何,返回 ``(out_parts, out_shells)``;空结果返回 ``None``。

    这里的 ``kind`` / ``parts`` / ``shells`` 就是 :class:`Geometry` 内部的
    三项(见 ``geometry.py``);不构造 :class:`Geometry`,让调用方自己拼 ——
    因为 ``Geometry._like()`` 会**丢掉** ``_shells``,结果必须显式赋。

    精度降级重试梯(``BufferOp``):

    1. **原精度**(``bufferOriginalPrecision``)—— 绝大多数输入一次就过;
    2. **逐级降精度**(``bufferReducedPrecision``)—— ``MAX_PRECISION_DIGITS``
       从 12 循环到 0,用 :func:`_precision_scale_factor` 定 scale,把偏移曲线
       的顶点按那个精度取整后**重算**。有些退化输入在原精度下拓扑自相矛盾,
       取整之后就不矛盾了。
    3. JTS 的第三档是 ``SnapRoundingNoder(new PrecisionModel(1.0))``。
       **本库整体口径是不做 snap-rounding**(见 ``_geometry_ops`` 模块
       docstring 与 DESIGN.md),所以这一档**降级为直接放弃**,返回空结果。

    ⚠️ 诚实边界:第 3 档的缺失意味着,"只有靠 snap-rounding 才能救回来"的
    退化构型(自交极严重的输入、节点几乎重合的曲线),GEOS 会返回一个结果而
    本库返回空多边形。这不是 bug,是明确的取舍。同理,本库**不承诺与 GEOS
    逐位相同** —— 交点用的是普通双精度而非自适应 DD,偏移端点的浮点末位
    可能差一位;对拍见 ``tools/verify_buffer.py``(容差判定 + "逐位相同用例数"
    统计)。

    ⚠️ ``single_sided=True`` 作用在**面 / 闭合折线**上时,本库给的是 **JTS** 的
    结果,与 GDAL/GEOS 不同。GEOS 在那之后还有一步"把输入边界与结果边界做
    ``OverlayNG`` UNION 再 ``Polygonizer``,面多于一个就只留面积最大的"后处理
    (``BufferBuilder::buffer`` 结尾);本库不做 —— ⚠️ **2026-09-30 补注**:
    overlay 引擎后来做了(见 :mod:`~pyopenfilegdb._overlay_ops`),所以这里缺的
    是 ``Polygonizer`` 与"只留最大面"那步**丢几何**的取舍,不是 overlay。
    对**开放折线**这一步是恒等的,
    两边一致。实测(``tools/verify_buffer.py``,合成 880 对 + 语料 260 对):
    开放折线与点上的 ``single_sided`` 与 GEOS 逐位相同;闭合输入上的 44 对在
    报告里**显式列出原因与计数**后排除 —— 不是静默跳过。
    """
    if kind in ('null', 'multipatch') or not parts:
        return None
    cap = _parse_style(cap, _CAP_NAMES, 'cap', "'round' / 'flat' / 'square'")
    join = _parse_style(join, _JOIN_NAMES, 'join',
                        "'round' / 'mitre' / 'bevel'")
    if quad_segs < 1:
        quad_segs = 1
    if distance == 0.0:
        return _buffer_by_zero(kind, parts, shells)

    try:
        return _buffer_once(kind, parts, shells, distance, quad_segs, cap,
                            join, mitre_limit, single_sided, None)
    except _BufferTopologyError:
        pass

    max_abs = _max_abs_coord(parts)
    for digits in range(_MAX_PRECISION_DIGITS, -1, -1):
        scale = _precision_scale_factor(max_abs, distance, digits)
        try:
            res = _buffer_once(kind, parts, shells, distance, quad_segs, cap,
                               join, mitre_limit, single_sided, scale)
        except _BufferTopologyError:
            continue
        if res is not None:
            return res
    return None

