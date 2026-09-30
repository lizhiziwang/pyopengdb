# -*- coding: utf-8 -*-
"""几何算法:度量、构造、拓扑判定 —— **纯函数,不依赖本包任何模块**。

这个模块不定义任何类型,也不 import 本包的任何模块,只认坐标数组
(``array('d')``,交错 XY,每个 part 一块)。两条理由:

1. 它是 import 图里的**叶子**:`geometry` 在它上面建类。一旦它反过来 import
   `geometry`,环就回来了。
2. 输入是**裸坐标**,不是几何对象 —— 拓扑判定的真值表可以直接拿坐标数组喂
   进来测,不用先造一个几何对象。

依赖只有标准库 + **可选的** numpy(见下面「可选的 numpy 加速」一节;没装就
整段走纯 Python)。

⚠️ 只读 ``xy_parts``,绝不碰 ``Geometry.coordinates``
---------------------------------------------------
``coordinates`` 是把 flat 存储物化成逐点元组的**派生视图**,量级 ~19 ns/顶点
(见 DESIGN.md §2.19.5)。本模块的每个算子都是逐顶点扫描,走了它就等于把刚省下
的钱又花回去。所以这里一律收 ``List[array('d')]``。

⚠️ 数值鲁棒性:没有精确算术
-------------------------
点与线的"落在上面"判定、线段求交,用的都是普通双精度浮点。**没有 snap-rounding,
没有自适应精度的方向判定**(那是 GEOS ``RobustLineIntersector`` 的活,上千行)。
后果:两个点在量化网格上重合、某条边几乎共线这类**次 ULP 的退化构型可能判错**。
GEOS 在引入 precision model 之前有同样的毛病。这条限制同时写进 DESIGN.md §2.21
与 STATUS.md §六。

⚠️ 大坐标抵消:鞋带公式**必须先平移**(这条踩过,代价是 1 公里)
-------------------------------------------------------------
面积和质心的鞋带公式是**平移不变**的,但当坐标本身很大、几何本身很小时,
不平移会灾难性抵消。这不是理论风险,是实测出来的。

真实语料(``D:/work/2024年国土行政区划.gdb``,UTM 带号 ~3.9e7)上挑 7 条要素,
拿 ``fractions.Fraction`` 在**同样的输入**上算精确解当参照::

    写法                          面积最大相对误差   质心最大偏差
    Σ(x₁·y₂ − x₂·y₁)                  8.6e-05        1135.8 m   ← 不平移
    Σ x(i)·(y(i+1) − y(i−1))          5.2e-10           0.0 m   ← GDAL 文档的写法
    平移到首顶点再累加                 1.4e-15           0.0 m   ← 本模块

原因:不平移时每一项都是 ~1.5e15 的乘积,而它们的和只有 ~854,一次抵消掉 12 位
有效数字 —— **一条 427 m² 的地块,质心能偏出 1135 米**。平移把每一项压回几何自身
的尺度(~1e4),抵消随之消失。

所以本模块所有鞋带类累加(面积、质心、顶点平均)**一律先减首顶点**。用首顶点
而不是包围盒左下角,是因为首顶点**不用额外扫一遍**求极值,而且实测更准
(1.4e-15 vs 5.9e-15)。这也是 JTS ``Area.ofRingSigned`` 的做法。

平移还顺手简化了循环:首顶点落到原点后,闭合项 ``cross(p_{n-1}, p_0)`` 恒等于
**精确的** 0,不必再算 —— 连 ``i % n`` 一起去掉,净结果是**比不平移更快**
(2000 顶点环 ×0.73,4 顶点短环 ×0.62)。

与 GDAL 的关系:GDAL 文档里 ``OGRLinearRing::get_Area`` 是
``Σ x(i)·(y(i+1) − y(i−1))/2`` 这一形式(比不平移好 5 个数量级,但不如平移)。
我们不平移就错、平移就更准,**选了平移**;两者在同一条几何上的差异远小于 1e-6
相对量级,对拍工具按这个容差比即可。

⚠️ 还有一点:开了 numpy 的算子里,面积 / 长度 / 质心的**最低几位与纯 Python
路径不同**(成对求和 vs 顺序累加)。差在 1e-15 量级,但"逐位相同"这个保证
只对纯 Python 路径成立。见下面「可选的 numpy 加速」的纪律 2。

环的"外环 / 内环"角色
--------------------
Esri 盘上只用**绕向**区分外环(顺时针)与内环(逆时针),不记录"哪个内环属于
哪个外环"。所以按绕向还原是一种可能,但它对 **OGC 风格的 WKT 输入是错的**
(RFC/OGC 的写法恰好相反:外环逆时针)。因此本模块一律要求调用方**显式传
``shells``** —— 一个逐 part 的 ``bool`` 列表,``True`` 表示这个 part 是外环。
``geometry`` 层负责把它算出来并随几何存下来(解码时按绕向,``from_wkt`` /
``from_geojson`` 时按结构);拿不到时用 :func:`ring_roles` 的兜底规则。
"""
from __future__ import annotations

import contextlib
import math
import os
from array import array
from typing import Any, List, Optional, Sequence, Tuple

# ----------------------------------------------------------------------------
# 可选的 numpy 加速
# ----------------------------------------------------------------------------
# numpy **只加速,不是依赖**:没装就整段走纯 Python,功能一模一样;装了也只
# 在"够大"的数组上走。三条纪律,改这段代码之前先读完:
#
# 1. **每次 numpy 调用有 5~20 µs 的固定开销**,小于阈值的数组上它比纯 Python
#    **慢**。下面的阈值是实测出来的交叉点,别凭感觉改小。
#
#    实测(2000 顶点的环,UTM 量级坐标,本机 Python 3.13 + numpy 1.26;
#    ``tools/verify_numpy.py --bench`` 可复现):
#
#    ========  ==========  ==========  ==========
#    算子       纯 Python    numpy       加速比
#    ========  ==========  ==========  ==========
#    length     689 µs      27 µs       ×25
#    area       351 µs      17 µs       ×21
#    centroid   494 µs      53 µs        ×9.4
#    envelope   144 µs     148 µs        ×1.0   ← **不加速**
#    ========  ==========  ==========  ==========
#
#    (这组数是「大坐标抵消」那次平移改完**之后**重测的。平移让纯 Python 的
#    面积/质心快了一点——去掉了 ``i % n`` 和闭合项——所以加速比整体比改之前
#    小,但阈值不用动:重测交叉点面积仍在 96~128、长度仍在 40~48。)
#
#    ⚠️ **别把这张表当成"阈值都够低"的证据** —— 它只测了 N=2000 这一个点,
#    在那个尺寸上什么都快。阈值合不合适要看**交叉点附近**那一档,而面积和
#    质心的交叉点差了 1.4 倍(见下面 ``_NP_MIN_CENTROID``),所以两者不同值。
#    判据永远是"分派到 numpy 的地方必须明显快",不是"numpy 更快"。
#
#    包围盒是有意思的反例:纯 Python 的 ``min(a[0::2])`` 已经把活干在 C 层
#    (切片 + 内建 min 都不建 Python 对象),numpy 那点向量优势抵不过固定开销,
#    小数组上还慢 10 倍。**所以 ``envelope_of`` 永远走纯 Python。**
#
# 2. **结果不保证逐位相同。** numpy 的 ``sum`` 用**成对求和**(pairwise
#    summation),浮点舍入顺序与顺序累加不同。实测相对误差:长度 ~3e-14、
#    面积 ~1e-15 —— 远在"量了跟没量一样"的尺度,但确实不是逐位相等。
#    ⚠️ 所以**依赖符号或低位比特的路径永远不许走 numpy**:写路径的绕向判定
#     (``_esri_geometry._is_clockwise`` → :func:`ring_signed_area2`)必须保持
#     "单循环顺序累加",那里的近零面积环上,成对求和可能把**符号**翻过来 ——
#     **这是写盘数据的正确性问题,不是性能问题**。
#
# 3. **能差分验证。** :func:`use_numpy` 在进程内开关,``tests/test_geometry.py``
#    与 ``tools/verify_numpy.py`` 靠"同一份输入跑两遍"证明两条路径结果一致
#    (与 ``_accel.use`` 同一个套路)。
try:
    import numpy as _np
except ImportError:                 # 没装 —— 正常状态,不是错误
    _np = None

#: 真值集合之外的一律当作"不关闭"。
_NP_FALSEY = ('', '0', 'false', 'no')

if os.environ.get('PYOPENFILEGDB_NO_NUMPY', '').strip().lower() not in _NP_FALSEY:
    _np = None

#: numpy 在**导入时**是否可用(构建/安装的事实)。
HAS_NUMPY = _np is not None

#: 分派处读的开关。默认等于 :data:`HAS_NUMPY`;:func:`use_numpy` 在进程内改它。
_USE_NUMPY = _np is not None

#: 启用 numpy 的顶点数下限。低于它走纯 Python —— 见上面纪律 1 的交叉点实测。
#:
#: ⚠️ **质心的下限必须比面积高一个档,这两个不是同一个数。**
#: ``_np_centroid_ring`` 要多算两组加权和,靠两次 ``np.roll`` 拿到
#: ``(x[i+1], y[i+1])`` —— 那是两份整数组拷贝,临时数组总量是面积路径的 3 倍。
#: 实测交叉点(UTM 量级坐标,2000 顶点环 ×1e-3 以下的时间不计):
#:
#:     N        64     96    128    160    192    256    384    512
#:     area    0.99×  1.03×  1.84×  2.23×  2.50×  3.27×  5.08×  6.89×
#:     质心    0.99×  0.99×  0.71×  0.91×  1.13×  1.45×  2.20×  2.86×
#:
#: 面积在 128 已经稳赚 1.8 倍;质心**在 128 反而慢 40%**,要到 192 才勉强
#: 打平。所以质心单独给 256 —— 留出跨机器的余量,代价只是把 192~256 这段
#: 本来也只有 1.1~1.45 倍的空间让回给纯 Python。
#: (这个 bug 是 ``tools/verify_numpy.py --bench`` 抓出来的:两条路径共用
#: 一个阈值时,质心在 128 顶点这个**最常见**的尺寸上是负优化。)
_NP_MIN_AREA2 = 128
_NP_MIN_CENTROID = 256
_NP_MIN_LENGTH = 48
_NP_MIN_FINITE = 256


@contextlib.contextmanager
def use_numpy(enabled: bool = True):
    """在进程内临时开/关 numpy 路径(退出时还原)。差分测试用。

    ``enabled=True`` 而 numpy 没装时是**静默无操作** —— 调用方不用先查
    :data:`HAS_NUMPY` 再决定要不要包这一层。
    """
    global _USE_NUMPY
    saved = _USE_NUMPY
    _USE_NUMPY = bool(enabled) and HAS_NUMPY
    try:
        yield
    finally:
        _USE_NUMPY = saved


def _as_f64(a: Any) -> Any:
    """``array('d')`` -> 一维 float64 数组(**零拷贝**)。

    ``array('d')`` 是 C 连续的 double 缓冲,``np.frombuffer`` 只包一层视图、
    不复制。本模块见到的每一块坐标都是 ``array('d')``(``Geometry.xy_parts``
    的两种来源都保证这点),所以快路径上没有拷贝;给了别的类型(纯 list)才
    落到 ``asarray`` 兜底。
    """
    if isinstance(a, _np.ndarray):
        return a
    if isinstance(a, array):
        return _np.frombuffer(a, dtype=_np.float64)
    return _np.asarray(a, dtype=_np.float64)


def _np_xy(a: Any):
    """交错数组 -> ``(x, y)`` 两块**步长为 2 的视图**(不拷贝)。"""
    v = _as_f64(a)
    return v[0::2], v[1::2]


def _np_signed_area2(a: Any) -> float:
    """numpy 版鞋带和 —— 与 :func:`ring_signed_area2` 一样**先平移到首顶点**。

    ⚠️ **只给面积 / 质心用**(它们对每环取 ``abs``,不在意符号的低位)。
    绕向判定请用 :func:`ring_signed_area2` —— 理由见上面纪律 2。

    平移还有一处收益:首顶点落到原点后,闭合项 ``cross(p_{n-1}, p_0)`` 恒等于
    **精确的** 0,所以不用像原来那样单独补一项。
    """
    x, y = _np_xy(a)
    x = x - x[0]
    y = y - y[0]
    return float(_np.sum(x[:-1] * y[1:] - x[1:] * y[:-1]))


def _np_part_length(a: Any, closed: bool) -> float:
    """numpy 版 :func:`part_length`(闭合段单独补,与纯 Python 版同口径)。

    长度天然平移不变(每一项都是 ``hypot(Δx, Δy)``),所以这里**不需要**平移。
    """
    x, y = _np_xy(a)
    total = float(_np.sum(_np.hypot(_np.diff(x), _np.diff(y))))
    if closed:
        total += math.hypot(x[0] - x[-1], y[0] - y[-1])
    return total


def _np_centroid_ring(a: Any) -> Tuple[float, float, float]:
    """numpy 版 :func:`_ring_centroid_terms` —— ``(带符号面积×2, 质心 x, 质心 y)``。

    返回的质心是**绝对坐标**(内部平移到首顶点算、再平移回来),调用方可以
    直接按面积加权。

    三个量一次算完 —— 分开算会把同一次叉积乘两遍(纯 Python 版就在这里
    各算一遍,是它 569 µs 里的大头)。
    """
    x, y = _np_xy(a)
    ox = float(x[0])
    oy = float(y[0])
    x = x - ox
    y = y - oy
    xr = _np.roll(x, -1)
    yr = _np.roll(y, -1)
    cross = x * yr - xr * y
    area2 = float(_np.sum(cross))
    if area2 == 0.0:
        return 0.0, ox, oy
    return (area2,
            float(_np.sum((x + xr) * cross)) / (3.0 * area2) + ox,
            float(_np.sum((y + yr) * cross)) / (3.0 * area2) + oy)


# ----------------------------------------------------------------------------
# 分类码
# ----------------------------------------------------------------------------
#: 一个点相对某几何的位置。
#:
#: ⚠️ 这三个值**同时就是 DE-9IM 矩阵的行/列下标**(矩阵顺序是 ``I B E``),
#: 所以 ``m[角色][位置]`` 可以直接当二维下标使。早先把它们写成 ``E=0, B=1, I=2``
#: 时就踩过这个坑:整张矩阵被**镜像**了(I 和 E 对调),而"两个全等正方形"这种
#: 在对调下不变的情形看起来还是对的,特别能骗人。
INTERIOR = 0
BOUNDARY = 1
EXTERIOR = 2

#: 两条**闭**线段的关系。
SEG_NONE = 0
#: 交于一点。
SEG_POINT = 1
#: 共线且有重叠 —— 交是一条线段。
SEG_OVERLAP = 2


# ----------------------------------------------------------------------------
# 基础:方向、点在线段上、线段求交
# ----------------------------------------------------------------------------
def orient(ax: float, ay: float, bx: float, by: float,
           cx: float, cy: float) -> float:
    """叉积 ``(b - a) × (c - a)``(数值上等于两倍三角形面积)。

    ``> 0`` 表示 c 在 a→b 的左侧(数学坐标系 y 向上)、``< 0`` 右侧、
    ``== 0`` 三点共线。普通双精度,没有自适应精度 —— 见模块 docstring。
    """
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def point_on_segment(px: float, py: float, ax: float, ay: float,
                     bx: float, by: float) -> bool:
    """点是否落在**闭**线段 ab 上(含两个端点)。

    ⚠️ 叉积为 0 只说明点在**直线**上,还要再用包围盒卡一次落在**线段**上 ——
    少了这一步,直线延长线上的点会被误判成在线段上。
    """
    if orient(ax, ay, bx, by, px, py) != 0.0:
        return False
    return (min(ax, bx) <= px <= max(ax, bx)
            and min(ay, by) <= py <= max(ay, by))


def _point_at(coord: float, axis: int,
              pts: Sequence[Tuple[float, float]]) -> Tuple[float, float]:
    """在共线点集里挑出主轴坐标为 ``coord`` 的那个(用于还原重叠段端点)。"""
    for p in pts:
        if p[axis] == coord:
            return p
    return pts[0]


def _collinear_overlap(p1: Tuple[float, float], p2: Tuple[float, float],
                       q1: Tuple[float, float], q2: Tuple[float, float]):
    """四点共线时求两线段的重叠。

    投影到**主轴**(跨度大的那个坐标)上退化成一维区间求交 —— 这样既不
    除零,也不用管线段是垂直还是水平。
    """
    axis = 0 if abs(p2[0] - p1[0]) >= abs(p2[1] - p1[1]) else 1
    a1, a2 = sorted((p1[axis], p2[axis]))
    b1, b2 = sorted((q1[axis], q2[axis]))
    lo = max(a1, b1)
    hi = min(a2, b2)
    if lo > hi:
        return (SEG_NONE, None, None)
    pts = (p1, p2, q1, q2)
    if lo == hi:
        return (SEG_POINT, _point_at(lo, axis, pts), None)
    return (SEG_OVERLAP, _point_at(lo, axis, pts), _point_at(hi, axis, pts))


def _proper_intersection(p1: Tuple[float, float], p2: Tuple[float, float],
                         q1: Tuple[float, float],
                         q2: Tuple[float, float]) -> Tuple[float, float]:
    """两条非共线且**真穿越**的线段的交点(参数式求解)。"""
    x1, y1 = p1
    x2, y2 = p2
    x3, y3 = q1
    x4, y4 = q2
    den = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    t = ((x1 - x3) * (y3 - y4) - (y1 - y3) * (x3 - x4)) / den
    return (x1 + t * (x2 - x1), y1 + t * (y2 - y1))


def seg_seg_classify(p1: Tuple[float, float], p2: Tuple[float, float],
                     q1: Tuple[float, float], q2: Tuple[float, float]):
    """两条**闭**线段的交。

    :returns: ``(kind, a, b)``

        * ``SEG_NONE`` —— 不相交,``a``/``b`` 都是 ``None``;
        * ``SEG_POINT`` —— 交于一点,``a`` 是那个 ``(x, y)``;
        * ``SEG_OVERLAP`` —— 共线重叠,``a``/``b`` 是重叠段的两个端点。

    零长线段(两个端点重合)按点处理,不走共线分支。
    """
    p_deg = p1[0] == p2[0] and p1[1] == p2[1]
    q_deg = q1[0] == q2[0] and q1[1] == q2[1]
    if p_deg:
        if q_deg:
            hit = p1[0] == q1[0] and p1[1] == q1[1]
            return (SEG_POINT, p1, None) if hit else (SEG_NONE, None, None)
        ok = point_on_segment(p1[0], p1[1], q1[0], q1[1], q2[0], q2[1])
        return (SEG_POINT, p1, None) if ok else (SEG_NONE, None, None)
    if q_deg:
        ok = point_on_segment(q1[0], q1[1], p1[0], p1[1], p2[0], p2[1])
        return (SEG_POINT, q1, None) if ok else (SEG_NONE, None, None)

    d1 = orient(q1[0], q1[1], q2[0], q2[1], p1[0], p1[1])
    d2 = orient(q1[0], q1[1], q2[0], q2[1], p2[0], p2[1])
    d3 = orient(p1[0], p1[1], p2[0], p2[1], q1[0], q1[1])
    d4 = orient(p1[0], p1[1], p2[0], p2[1], q2[0], q2[1])

    if d1 == 0.0 and d2 == 0.0:
        # p 的两个端点都在 q 的直线上 —— 只可能是共线
        return _collinear_overlap(p1, p2, q1, q2)

    # 真穿越:两条线段**内部**各交一次。四个 d 必须都非零,否则退化成
    # "端点在另一条线段上",那要按下面的端点分支处理。
    if (d1 != 0.0 and d2 != 0.0 and d3 != 0.0 and d4 != 0.0
            and (d1 > 0.0) != (d2 > 0.0)
            and (d3 > 0.0) != (d4 > 0.0)):
        return (SEG_POINT, _proper_intersection(p1, p2, q1, q2), None)

    for pt in (p1, p2):
        if point_on_segment(pt[0], pt[1], q1[0], q1[1], q2[0], q2[1]):
            return (SEG_POINT, pt, None)
    for pt in (q1, q2):
        if point_on_segment(pt[0], pt[1], p1[0], p1[1], p2[0], p2[1]):
            return (SEG_POINT, pt, None)
    return (SEG_NONE, None, None)


# ----------------------------------------------------------------------------
# 基础:点相对环 / 面的位置
# ----------------------------------------------------------------------------
def point_in_ring(px: float, py: float, a: Any) -> int:
    """点相对**单个环**的位置(``EXTERIOR`` / ``BOUNDARY`` / ``INTERIOR``)。

    ``a`` 是交错的 ``array('d')``,**内存里的环不闭合**(闭合点由解码器削掉),
    所以这里用 ``i % n`` 收尾,把"末点回到首点"那一段补上。

    ⚠️ 边界判定必须**先做**。射线交叉数法对落在环上的点给的是不确定的答案
    (取决于射线怎么擦过顶点),而 OGC 规定"在环上"一律算边界。这是 DE-9IM
    里 I 与 B 的分界,也是最容易写错的一处。
    """
    n = len(a) // 2
    if n == 0:
        return EXTERIOR
    if n == 1:
        return BOUNDARY if (px == a[0] and py == a[1]) else EXTERIOR
    inside = False
    x1 = a[0]
    y1 = a[1]
    for i in range(1, n + 1):
        j = i % n
        x2 = a[2 * j]
        y2 = a[2 * j + 1]
        if orient(x1, y1, x2, y2, px, py) == 0.0:
            if (min(x1, x2) <= px <= max(x1, x2)
                    and min(y1, y2) <= py <= max(y1, y2)):
                return BOUNDARY
        # 半开区间规则 (y1 > py) != (y2 > py):顶点只被数一次
        elif (y1 > py) != (y2 > py):
            if px < x1 + (py - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
        x1 = x2
        y1 = y2
    return INTERIOR if inside else EXTERIOR


def ring_roles(parts: Sequence[Any],
               shells: Optional[Sequence[bool]] = None) -> List[bool]:
    """逐 part 的"是不是外环"。

    ``shells`` 由调用方给出(解码时按 Esri 绕向、``from_wkt`` 时按结构),这是
    **唯一可靠**的来源。给不出来时的兜底规则是 **part 0 是外环、其余是内环**
    —— 也就是 OGR ``OGRPolygon`` 的模型,对"手工拼出来的几何"是最合理的猜测。

    只判"是不是外环",不判"这个洞属于哪个外环" —— 后者见
    :func:`organize_polygons`。
    """
    if shells is not None and len(shells) == len(parts):
        return list(shells)
    return [i == 0 for i in range(len(parts))]


def _ring_representative(a: Any) -> Optional[Tuple[float, float]]:
    """环上取一个用于"包含判定"的代表点(先试首顶点,不行再取内部点)。"""
    n = len(a) // 2
    if n == 0:
        return None
    return (a[0], a[1])


def organize_polygons(parts: Sequence[Any],
                      shells: Optional[Sequence[bool]] = None
                      ) -> List[Tuple[int, List[int]]]:
    """把平铺的环组装成 ``[(外环下标, [洞下标, ...]), ...]``。

    **这是 GDAL ``OGRGeometryFactory::organizePolygons()`` 的对应物**
    (实现在 ``ogr/ogrgeometryfactory.cpp``,约 1015 行起)。Esri 盘上把一个面的
    所有环平铺在一起、**不记录"哪个洞属于哪个外环"**,GDAL 读 OpenFileGDB 时
    就是调它把环组装成 ``OGRPolygon`` / ``OGRMultiPolygon`` 的,受
    ``OGR_ORGANIZE_POLYGONS`` 这个配置项控制(默认 ``ONLY_CCW``)。

    ``ONLY_CCW`` 是**绕向捷径**:按约定"外环顺时针、内环逆时针"直接分壳与洞,
    不做几何包含测试 —— shapefile 驱动默认走它,OpenFileGDB 也默认走它。
    绕向不符合约定的数据上这条捷径会**组装错**(GDAL issue #1369 就是 FileGDB
    里非约定绕向的数据被渲染出了错误的拓扑),所以 GDAL 后来加了回退:捷径
    组不出自洽结果时改用 ``DEFAULT`` 策略,也就是**几何包含测试**。这里合成了
    两者:

    1. 角色优先用调用方给的 ``shells``(解码时按绕向、``from_wkt`` 时按结构);
       没给才退回绕向捷径 ``ring_signed_area2 < 0``(顺时针 = 外环,Esri/GDAL
       的约定,与 ISO 19107 / OGC 恰好相反);
    2. 每个洞找**包含它的最小外环**当归属(GDAL/JTS 都是取最小的那个,因为
       嵌套时最小的才是直接父级);
    3. 一个洞若**不被任何外环包含**,说明它的绕向多半是反的 —— 按 GDAL 的做法
       把它**改当成外环**(GDAL 那边是把环的坐标顺序反过来,这里只改角色,
       不动坐标)。

    对良构数据(洞在外环内、洞之间不相交)结果与 GDAL 一致。
    """
    n = len(parts)
    if not n:
        return []
    if shells is not None and len(shells) == n:
        is_shell = list(shells)
    else:
        # ONLY_CCW 捷径:顺时针(带符号面积为负)= 外环
        is_shell = [ring_signed_area2(a) < 0.0 for a in parts]
        if not any(is_shell):
            # 一个顺时针环都没有 —— 全是逆时针。这是 OGC 风格的写法(外环逆时针),
            # 按位置约定兜底:part 0 是外环。见 ring_roles 的说明。
            is_shell = [i == 0 for i in range(n)]

    groups: List[List[Any]] = []
    index_of: List[int] = [-1] * n
    for i in range(n):
        if is_shell[i]:
            index_of[i] = len(groups)
            groups.append([i, []])

    areas = [abs(ring_signed_area2(a)) for a in parts]
    for i in range(n):
        if is_shell[i]:
            continue
        rep = _ring_representative(parts[i])
        best = -1
        best_area = 0.0
        if rep is not None:
            for sj in range(n):
                if not is_shell[sj]:
                    continue
                if point_in_ring(rep[0], rep[1], parts[sj]) == EXTERIOR:
                    continue
                if best < 0 or areas[sj] < best_area:
                    best = sj
                    best_area = areas[sj]
        if best < 0:
            is_shell[i] = True                # 绕向反了 —— 改当外环(GDAL 的回退)
            index_of[i] = len(groups)
            groups.append([i, []])
        else:
            groups[index_of[best]][1].append(i)
    return [(g[0], g[1]) for g in groups]


def point_in_polygon(px: float, py: float, parts: Sequence[Any],
                     shells: Optional[Sequence[bool]] = None,
                     groups: Optional[Sequence[Any]] = None) -> int:
    """点相对**整个面**的位置(含洞,含多个外环)。

    判定顺序(顺序本身就是语义):落在**任何一个**环上 → 边界;否则落在某个
    外环里、且不在**该外环自己的**洞里 → 内部;否则 → 外部。

    ⚠️ 这里必须知道外环/内环的角色,而且要按 :func:`organize_polygons` 的
    **归属**关系判 —— 不能简化成"不在任何洞里就算内部":多壳面里 A 壳的洞
    可能正好落在 B 壳内部,那个位置该算 B 的内部(A 的洞管不着 B)。

    :param groups: 预先算好的 :func:`organize_polygons` 结果。**一次遍历里判很多
        个点时务必传它** —— ``relate()`` 每个探针都调一次本函数,不传的话
        ``O(环数²)`` 的组装会被每个探针重算一遍。
    """
    for a in parts:
        if point_in_ring(px, py, a) == BOUNDARY:
            return BOUNDARY
    if groups is None:
        groups = organize_polygons(parts, shells)
    for shell_idx, holes in groups:
        if point_in_ring(px, py, parts[shell_idx]) != INTERIOR:
            continue
        for h in holes:
            if point_in_ring(px, py, parts[h]) == INTERIOR:
                break
        else:
            return INTERIOR
    return EXTERIOR


# ----------------------------------------------------------------------------
# 基础:计数与包围盒
# ----------------------------------------------------------------------------
def point_count_of(parts: Sequence[Any]) -> int:
    """所有 part 的顶点数之和(不逐点,只读每个数组的长度)。"""
    return sum(len(a) for a in parts) // 2


def part_count_of(parts: Sequence[Any]) -> int:
    return len(parts)


def envelope_of(parts: Sequence[Any]) -> Optional[Tuple[float, float, float, float]]:
    """精确 XY 包围盒 ``(xmin, ymin, xmax, ymax)``;没有顶点时 ``None``。

    用 ``min(a[0::2]) / max(a[1::2])`` 而不是 Python 逐点循环:切片是 C 层的,
    之后 ``min``/``max`` 也在 C 层走。实测 **18.1 ns/元素**,而 Python 逐点循环
    是 36.3 ns/元素(2× 差距,见 DESIGN.md §2.21)。
    """
    xmin = ymin = math.inf
    xmax = ymax = -math.inf
    for a in parts:
        if not a:
            continue
        x0 = min(a[0::2])
        x1 = max(a[0::2])
        y0 = min(a[1::2])
        y1 = max(a[1::2])
        if x0 < xmin:
            xmin = x0
        if x1 > xmax:
            xmax = x1
        if y0 < ymin:
            ymin = y0
        if y1 > ymax:
            ymax = y1
    if xmin > xmax:
        return None
    return (xmin, ymin, xmax, ymax)


def envelope_intersects(a: Optional[Tuple[float, float, float, float]],
                        b: Optional[Tuple[float, float, float, float]]) -> bool:
    """两个包围盒是否相交(闭区间)。``None`` 视作空,恒为 ``False``。"""
    if a is None or b is None:
        return False
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


# ----------------------------------------------------------------------------
# 环:带符号面积、周长
# ----------------------------------------------------------------------------
def ring_signed_area2(a: Any) -> float:
    """环的**带符号面积的两倍** ``Σ(x_i·y_{i+1} − x_{i+1}·y_i)``。

    ⚠️ **先平移到首顶点再累加** —— 这不是优化,是正确性。理由见模块开头
    「大坐标抵消」一节:真实投影坐标(UTM 带号 ~3.9e7)下不平移会丢到 1e-4
    相对精度,质心能偏出一公里。平移之后误差回到浮点 epsilon 量级。

    附带两个好处:

    * **闭合项恒等于 0,不用算。** 平移后首顶点就是 ``(0.0, 0.0)``,最后一段
      ``cross(p_{n-1}, p_0) = x_{n-1}·0 − 0·y_{n-1} = 0`` —— 是**精确的** 0,
      不是约等于。所以循环只跑 ``range(1, n)``,``i % n`` 也一并去掉。
    * 实测因此**比不平移还快**(2000 顶点环 ×0.73,4 顶点短环 ×0.62)。

    ⚠️ 仍然**必须保持"单循环顺序累加"**这个形态:``_esri_geometry._is_clockwise``
    (写路径的绕向判定)依赖它的符号,换成 ``sum(map(mul, ...))`` 那种两项各自
    求和再相减的写法会改变浮点舍入顺序 —— 近零面积的环上符号可能翻,那是写盘
    数据的正确性问题。平移**不改变累加顺序**,而且显著削弱抵消,所以平移后
    符号判定比平移前**更**可靠。

    自环(顶点数 < 3)返回 0.0。
    """
    n = len(a) // 2
    if n < 3:
        return 0.0
    ox = a[0]
    oy = a[1]
    total = 0.0
    x1 = 0.0
    y1 = 0.0
    for i in range(1, n):
        j = 2 * i
        x2 = a[j] - ox
        y2 = a[j + 1] - oy
        total += x1 * y2 - x2 * y1
        x1 = x2
        y1 = y2
    return total


def _signed_area2(a: Any) -> float:
    """鞋带和 —— **给面积 / 质心用的分派版**(大环走 numpy,小环走纯 Python)。

    ⚠️ 只有这两个消费者。绕向判定(:func:`ring_roles`、写路径的
    ``_esri_geometry._is_clockwise``)一律用 :func:`ring_signed_area2` 的
    顺序累加版:那里在意的是**符号**在近零面积环上会不会翻,而成对求和恰好
    改的就是低位。这条分工不要合并。
    """
    if _USE_NUMPY and len(a) // 2 >= _NP_MIN_AREA2:
        return _np_signed_area2(a)
    return ring_signed_area2(a)


def ring_area(a: Any) -> float:
    """环面积的绝对值。"""
    return abs(ring_signed_area2(a)) * 0.5


def part_length(a: Any, closed: bool) -> float:
    """一块数组的长度。

    :param closed: ``True`` 时把"末点回到首点"那一段也算上(面环、multipatch);
        ``False`` 时不算(折线)。⚠️ 内存里的环不闭合,所以"闭合的那一段"必须
        在这里补 —— 少了它,周长会少一段。

    实测纯 Python ~244 ns/顶点(``math.hypot`` 的调用开销占大头);顶点数达到
    :data:`_NP_MIN_LENGTH` 后改走 numpy,大环上快 ~40 倍(见模块开头的实测表)。
    没有走 ``sum(map(hypot, ...))`` 的写法:那样虽然快,但要先切出四条临时数组,
    而这里 ``polyline`` 的 part 常常只有几个点,临时数组的分配比省下的循环还贵。
    """
    n = len(a) // 2
    if n < 2:
        return 0.0
    if _USE_NUMPY and n >= _NP_MIN_LENGTH:
        return _np_part_length(a, closed)
    total = 0.0
    x1 = a[0]
    y1 = a[1]
    last = n if closed else n - 1
    for i in range(1, last + 1):
        j = i % n
        x2 = a[2 * j]
        y2 = a[2 * j + 1]
        total += math.hypot(x2 - x1, y2 - y1)
        x1 = x2
        y1 = y2
    return total


def ring_is_closed(a: Any) -> bool:
    """交错数组的首末 XY 是否重合。"""
    n = len(a)
    return n >= 4 and a[0] == a[n - 2] and a[1] == a[n - 1]


# ----------------------------------------------------------------------------
# 度量:面积 / 长度 / 质心
# ----------------------------------------------------------------------------
def area_of(kind: str, parts: Sequence[Any],
            shells: Optional[Sequence[bool]] = None) -> float:
    """面积。非面几何恒为 ``0.0``(与 ``OGRGeometry::get_Area`` 一致)。

    算的是 **外环面积之和 减 内环面积之和**,逐环取 ``abs`` 再按角色带符号相加::

        area = Σ|壳环| − Σ|洞环|

    ⚠️ 这里踩过一个坑,记下来:早先写成"**逐 part 取 abs 再相加**"(理由是
    "对环方向写反的数据更宽容"),结果对"外环 100、洞 4"的正方形算出 **104**
    而不是 96 —— 洞被加了进去。取 abs 是为了不受绕向影响,但**减号不能丢**,
    而"哪个环是洞"只能由 roles 提供。所以 :func:`ring_roles` 是这里的必需输入。

    ⚠️ 与 OGR 的模型差异:OGR 的 ``OGRPolygon`` 靠**环在列表里的位置**区分
    外环与内环(第 0 个是外环);Esri 的盘上布局把多壳面的环平铺在一起、只用
    绕向区分。我们走后者(roles),所以多壳面也是对的。方向写反的数据上
    GDAL 会加出负数甚至互相抵消,我们按角色带符号相加更稳。

    Z / M 不参与(GDAL 也只算 XY)。
    """
    if kind != 'polygon':
        return 0.0
    roles = ring_roles(parts, shells)
    total = 0.0
    for a, is_shell in zip(parts, roles):
        a_abs = abs(_signed_area2(a)) * 0.5
        total += a_abs if is_shell else -a_abs
    return total


def length_of(kind: str, parts: Sequence[Any]) -> float:
    """长度。面/ multipatch 返回**周长**,折线返回长度,点/multipoint 返回 ``0.0``。

    对应 ``OGRGeometry::get_Length``(面走 ``OGRCurvePolygon`` 是各环周长之和)。
    multipatch 在 OGR 里没有对应物 —— 这里按"一串闭合环"处理,是本库的选择。
    """
    if kind == 'polyline':
        return sum(part_length(a, False) for a in parts)
    if kind in ('polygon', 'multipatch'):
        return sum(part_length(a, True) for a in parts)
    return 0.0


def _vertex_average(parts: Sequence[Any]) -> Optional[Tuple[float, float]]:
    """所有顶点的算术平均。

    同样**先平移到首顶点**:897 个 3.9e7 量级的坐标直接累加,部分和涨到
    3.5e10,末位是 4e-6 —— 加上几百次随机游走,结果能差出 1e-4。平移到首顶点
    后部分和只有几何自身的尺度,误差回到 epsilon 量级。
    """
    ox = oy = 0.0
    have = False
    for a in parts:
        if len(a) >= 2:
            ox = a[0]
            oy = a[1]
            have = True
            break
    if not have:
        return None
    n = 0
    sx = 0.0
    sy = 0.0
    for a in parts:
        for i in range(0, len(a), 2):
            sx += a[i] - ox
            sy += a[i + 1] - oy
            n += 1
    if not n:
        return None
    return (sx / n + ox, sy / n + oy)


def _ring_centroid_terms(a: Any) -> Tuple[float, float, float]:
    """纯 Python 版 :func:`_np_centroid_ring` —— ``(带符号面积×2, 质心 x, 质心 y)``。

    质心是**绝对坐标**;内部与 :func:`ring_signed_area2` 用同一套平移(先减
    首顶点),闭合项同样因为首顶点落到原点而恒为 0,不用算。
    """
    n = len(a) // 2
    ox = a[0]
    oy = a[1]
    area2 = 0.0
    rx = 0.0
    ry = 0.0
    x1 = 0.0
    y1 = 0.0
    for i in range(1, n):
        j = 2 * i
        x2 = a[j] - ox
        y2 = a[j + 1] - oy
        cross = x1 * y2 - x2 * y1
        area2 += cross
        rx += (x1 + x2) * cross
        ry += (y1 + y2) * cross
        x1 = x2
        y1 = y2
    if area2 == 0.0:
        return 0.0, ox, oy
    return (area2, rx / (3.0 * area2) + ox, ry / (3.0 * area2) + oy)


def centroid_of(kind: str, parts: Sequence[Any],
                shells: Optional[Sequence[bool]] = None
                ) -> Optional[Tuple[float, float]]:
    """质心的 ``(x, y)``;空几何返回 ``None``。

    两种语义,按 GDAL 分:

    * **面** —— 按面积的加权质心(鞋带质心公式),权重是每个环面积的绝对值、
      **洞取负号**(与 :func:`area_of` 同一套符号规则);总权重 ≤ 0 时退化成顶点
      平均(GDAL ``OGRPolygon::Centroid`` 同样在面积为 0 时退回基类行为)。
    * **点 / multipoint / 折线** —— **顶点算术平均**,这是 GDAL 基类
      ``OGRGeometry::Centroid`` 与 ``OGRSimpleCurve::Centroid`` 的行为。

    ⚠️ 与 GEOS 的分歧:GEOS 的线质心是**按长度加权**的,GDAL 是顶点平均。
    我们跟 GDAL。

    ⚠️ 另一个刻意的分歧:返回 ``(x, y)`` **元组**,不是像 ``OGR_G_Centroid``
    那样填一个点几何 —— 一个坐标不值得为它建一个对象。
    """
    if kind != 'polygon':
        return _vertex_average(parts)

    total_area = 0.0
    cx = 0.0
    cy = 0.0
    roles = ring_roles(parts, shells)
    use_np = _USE_NUMPY
    for a, is_shell in zip(parts, roles):
        n = len(a) // 2
        if n < 3:
            continue
        if use_np and n >= _NP_MIN_CENTROID:
            area2, rc_x, rc_y = _np_centroid_ring(a)
        else:
            area2, rc_x, rc_y = _ring_centroid_terms(a)
        if area2 == 0.0:
            continue
        # 权重 = 该环面积的**绝对值**,洞取负 —— 与 area_of 同一套符号规则,
        # 少了这个负号,偏心洞会把质心往错误方向拽。
        w = abs(area2) * 0.5
        if not is_shell:
            w = -w
        total_area += w
        cx += w * rc_x
        cy += w * rc_y
    if total_area <= 0.0:
        return _vertex_average(parts)
    return (cx / total_area, cy / total_area)


# ----------------------------------------------------------------------------
# 构造:凸包
# ----------------------------------------------------------------------------
def _hull_of_points(pts: List[Tuple[float, float]]
                    ) -> List[Tuple[float, float]]:
    """Andrew 单调链。返回逆时针的**不闭合**点列(可能退化到 1 / 2 个点)。"""
    pts = sorted(set(pts))
    if len(pts) <= 2:
        return pts

    def half(points: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
        out: List[Tuple[float, float]] = []
        for p in points:
            while len(out) >= 2 and orient(out[-2][0], out[-2][1],
                                          out[-1][0], out[-1][1],
                                          p[0], p[1]) <= 0.0:
                out.pop()
            out.append(p)
        return out

    lower = half(pts)
    upper = half(pts[::-1])
    return lower[:-1] + upper[:-1]


def convex_hull_xy(parts: Sequence[Any]) -> List[Tuple[float, float]]:
    """所有 part 的所有顶点(XY)的凸包顶点表(逆时针,**不闭合**)。

    退化情形与 GEOS 一致:0 个点 → 空表;1 个点(或全部重合)→ 1 个点;
    全部共线 → 2 个端点。洞被丢弃(凸包本来就没有洞)。
    """
    pts: List[Tuple[float, float]] = []
    for a in parts:
        for i in range(0, len(a), 2):
            pts.append((a[i], a[i + 1]))
    if not pts:
        return []
    return _hull_of_points(pts)


# ----------------------------------------------------------------------------
# 构造:Douglas–Peucker 简化
# ----------------------------------------------------------------------------
def _dp_keep(xy: Any, tolerance: float) -> List[int]:
    """Douglas–Peucker:返回**保留下来的顶点下标**(升序,首尾必留)。

    迭代实现(显式栈),避免长折线递归爆栈。容差判定用点到**线段**的垂距,
    不是点到直线 —— 少了这一步,折线拐回去的那一截会被误判成"在线上"。
    """
    n = len(xy) // 2
    if n < 3:
        return list(range(n))
    keep = [False] * n
    keep[0] = True
    keep[n - 1] = True
    tol2 = tolerance * tolerance
    stack = [(0, n - 1)]
    while stack:
        i, k = stack.pop()
        if k <= i + 1:
            continue
        ax = xy[2 * i]
        ay = xy[2 * i + 1]
        bx = xy[2 * k]
        by = xy[2 * k + 1]
        dx = bx - ax
        dy = by - ay
        seg2 = dx * dx + dy * dy
        best = -1.0
        bestj = -1
        for j in range(i + 1, k):
            px = xy[2 * j]
            py = xy[2 * j + 1]
            if seg2 > 0.0:
                t = ((px - ax) * dx + (py - ay) * dy) / seg2
                if t < 0.0:
                    t = 0.0
                elif t > 1.0:
                    t = 1.0
                ex = ax + t * dx - px
                ey = ay + t * dy - py
            else:
                ex = ax - px
                ey = ay - py
            d2 = ex * ex + ey * ey
            if d2 > best:
                best = d2
                bestj = j
        if best > tol2 and bestj > 0:
            keep[bestj] = True
            stack.append((i, bestj))
            stack.append((bestj, k))
    return [i for i in range(n) if keep[i]]


def simplify_parts(parts: Sequence[Any], tolerance: float,
                   min_points: int = 0):
    """逐 part 的 Douglas–Peucker。

    :param min_points: 简化后**少于**这个顶点数的 part 整体放弃简化
        (保守策略:宁可留着毛刺,也不产出一个非法的环)。

        ⚠️ 面环传 **3**,不是 4:内存里的环**不存闭合点**(闭合段由
        ``part_length(closed=True)`` / 导出时补),所以三角形环就是 3 个顶点。
        传 4 会让所有三角形环都不被简化。

    :returns: ``(新的 parts, 每个 part 保留的原始下标)``
    """
    out: List[Any] = []
    kept: List[List[int]] = []
    for a in parts:
        idx = _dp_keep(a, tolerance)
        if min_points and len(idx) < min_points:
            idx = list(range(len(a) // 2))
        kept.append(idx)
        nxt = array('d')
        for j in idx:
            nxt.append(a[2 * j])
            nxt.append(a[2 * j + 1])
        out.append(nxt)
    return out, kept


# ----------------------------------------------------------------------------
# 构造:segmentize
# ----------------------------------------------------------------------------
def segmentize_xy(a: Any, max_len: float):
    """把一块坐标数组里超长的线段等分插入中间点。

    返回 ``(新数组, 新顶点对应的"来源"下标对)`` —— 第二项给 Z/M 插值用:
    每个新点记 ``(原段起点下标, 段内参数 t)``,Z/M 按 ``z1 + t*(z2-z1)`` 插。
    对应 ``OGRLineString::segmentize``(GDAL 的 C++ 实现直接可照抄)。
    """
    n = len(a) // 2
    out = array('d')
    src: List[Tuple[int, float]] = []
    if n == 0:
        return out, src
    out.append(a[0])
    out.append(a[1])
    src.append((0, 0.0))
    for i in range(n - 1):
        x1 = a[2 * i]
        y1 = a[2 * i + 1]
        x2 = a[2 * i + 2]
        y2 = a[2 * i + 3]
        d = math.hypot(x2 - x1, y2 - y1)
        steps = int(math.ceil(d / max_len - 1e-12)) if max_len > 0.0 else 1
        if steps < 1:
            steps = 1
        for k in range(1, steps + 1):
            t = k / steps
            out.append(x1 + t * (x2 - x1))
            out.append(y1 + t * (y2 - y1))
            src.append((i, t))
    return out, src


def interpolate_scalars(vals: Any, src: Sequence[Tuple[int, float]]):
    """按 :func:`segmentize_xy` 给出的来源表把 Z 或 M 插值出来。"""
    if vals is None:
        return None
    out = array('d')
    for i, t in src:
        if t == 0.0:
            out.append(vals[i])
        else:
            v1 = vals[i]
            v2 = vals[i + 1] if i + 1 < len(vals) else vals[i]
            out.append(v1 + t * (v2 - v1))
    return out


# ----------------------------------------------------------------------------
# 有效性 / 单纯性(部分实现)
# ----------------------------------------------------------------------------
def _segments_of(a: Any):
    """一块坐标数组的所有线段 ``(起点, 终点)``(含闭合环的回首段由调用方决定)。"""
    n = len(a) // 2
    for i in range(n - 1):
        yield ((a[2 * i], a[2 * i + 1]), (a[2 * i + 2], a[2 * i + 3]))


def _ring_segments(a: Any):
    """环的线段(含"末点回首点"那一段)。"""
    n = len(a) // 2
    for i in range(n):
        j = (i + 1) % n
        yield ((a[2 * i], a[2 * i + 1]), (a[2 * j], a[2 * j + 1]))


def ring_self_intersects(a: Any) -> bool:
    """环是否自交(相邻段共享端点不算)。

    ``O(n²)`` 逐段两两判定 —— 对单条要素的环是够的,但**大环会明显慢**
    (一条 100k 顶点的环是 5·10⁹ 次比较)。这是刻意的取舍:没有扫描线,
    也不做线段包围盒索引,理由写在 DESIGN §2.21。
    """
    segs = list(_ring_segments(a))
    n = len(segs)
    if n < 3:
        return False
    for i in range(n):
        p1, p2 = segs[i]
        for j in range(i + 1, n):
            if j == i + 1 or (i == 0 and j == n - 1):
                continue                       # 相邻段,共享端点是正常的
            q1, q2 = segs[j]
            kind, pt, _ = seg_seg_classify(p1, p2, q1, q2)
            if kind == SEG_OVERLAP:
                return True
            if kind == SEG_POINT:
                # 交在"公共端点"上(自环、重复点)也算自交
                return True
    return False


def _all_finite(a: Any) -> bool:
    """整块坐标是否都是有限值(NaN / ±Inf → ``False``)。

    大块走 numpy 的 ``isfinite``:实测纯 Python 每元素 ~50 ns,numpy 便宜到
    可以忽略 —— 但固定开销在,所以小数组还是走循环。
    """
    if _USE_NUMPY and len(a) >= _NP_MIN_FINITE:
        return bool(_np.isfinite(_as_f64(a)).all())
    for v in a:
        if v != v or v == math.inf or v == -math.inf:
            return False
    return True


def is_valid_of(kind: str, parts: Sequence[Any],
                shells: Optional[Sequence[bool]] = None) -> bool:
    """**部分实现**的几何有效性检查。

    只做这几条(其余一律不查,调用方不要以为 ``True`` 就等于 OGC 有效):

    1. 所有 XY 都是有限值(NaN / Inf → 无效);
    2. 顶点数下限:point 要有 1 个点、polyline 的每个 part ≥ 2 点、
       polygon 的每个环 ≥ 3 个**不同**顶点;
    3. 每个环自交检测(``O(n²)``,见 :func:`ring_self_intersects`);
    4. 内环必须落在**某个外环**之内(取环上一个顶点做代表点)。

    **没做**:多个外环互相重叠、环退化成零面积、洞与洞相交、multipoint 顶点重合、
    自相切的环、以及任何 multipatch 的有效性。要 OGC 完整有效性得上 GEOS
    (``OGR_G_IsValid`` 就是转手给它的),本库做不到。

    multipatch 返回 ``False``(它连环语义都没有,不做猜测)。
    """
    if kind == 'null':
        return True
    if kind == 'multipatch':
        return False
    for a in parts:
        if not _all_finite(a):
            return False

    if kind == 'point':
        return point_count_of(parts) == 1
    if kind == 'multipoint':
        return point_count_of(parts) >= 1
    if kind == 'polyline':
        return all(len(a) // 2 >= 2 for a in parts) if parts else True

    # polygon
    if not parts:
        return True
    for a in parts:
        # 环至少 3 个不同顶点(首尾不重复,所以就是 n >= 3)
        if len(a) // 2 < 3:
            return False
        if ring_self_intersects(a):
            return False
    roles = ring_roles(parts, shells)
    for a, is_shell in zip(parts, roles):
        if is_shell or not a:
            continue
        # 内环取一个顶点当代表点,必须落在某个外环内部(允许贴边)
        px = a[0]
        py = a[1]
        for b, b_shell in zip(parts, roles):
            if not b_shell or not b:
                continue
            if point_in_ring(px, py, b) in (INTERIOR, BOUNDARY):
                break
        else:
            return False
    return True


def is_simple_of(kind: str, parts: Sequence[Any]) -> bool:
    """**部分实现**的单纯性检查。

    * ``point`` —— 恒为 ``True``;
    * ``multipoint`` —— 没有重合顶点才单纯;
    * ``polyline`` —— 每个 part 不自交(与环同样的 ``O(n²)`` 判定);
    * ``polygon`` —— **恒为 ``True``**(OGC 规定面天然单纯,GEOS 也这么答);
    * ``multipatch`` —— 恒为 ``False``。

    **没做**:part 与 part 之间的相交判定、折线经过自身端点之外顶点的情形。
    """
    if kind in ('point', 'polygon', 'null'):
        return True
    if kind == 'multipatch':
        return False
    if kind == 'multipoint':
        seen = set()
        for a in parts:
            for i in range(0, len(a), 2):
                p = (a[i], a[i + 1])
                if p in seen:
                    return False
                seen.add(p)
        return True
    for a in parts:
        if _open_self_intersects(a):
            return False
    return True


def _open_self_intersects(a: Any) -> bool:
    """折线(不闭合)自交判定:同样的两两线段法,但没有"回首段"。"""
    segs = list(_segments_of(a))
    n = len(segs)
    for i in range(n):
        p1, p2 = segs[i]
        for j in range(i + 1, n):
            q1, q2 = segs[j]
            kind, _, _ = seg_seg_classify(p1, p2, q1, q2)
            if kind == SEG_NONE:
                continue
            if j == i + 1:
                # 相邻段只在共享端点相交是正常的;重叠就不正常了
                if kind == SEG_OVERLAP:
                    return True
                continue
            return True
    return False


# ----------------------------------------------------------------------------
# 距离
# ----------------------------------------------------------------------------
def _point_seg_distance(px: float, py: float, ax: float, ay: float,
                        bx: float, by: float) -> float:
    """点到**线段**(闭区间)的距离。投影参数夹到 [0, 1] —— 少了这一步量的是
    点到**直线**的距离,线段延长线外的点会被算近。"""
    dx = bx - ax
    dy = by - ay
    seg2 = dx * dx + dy * dy
    if seg2 <= 0.0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / seg2
    if t < 0.0:
        t = 0.0
    elif t > 1.0:
        t = 1.0
    return math.hypot(ax + t * dx - px, ay + t * dy - py)


def seg_seg_distance(p1: Tuple[float, float], p2: Tuple[float, float],
                     q1: Tuple[float, float], q2: Tuple[float, float]) -> float:
    """两条**闭**线段之间的距离(相交则为 0.0)。"""
    if seg_seg_classify(p1, p2, q1, q2)[0] != SEG_NONE:
        return 0.0
    # 不相交 → 最小值必在某个端点到另一条线段的垂距上
    return min(_point_seg_distance(p1[0], p1[1], q1[0], q1[1], q2[0], q2[1]),
               _point_seg_distance(p2[0], p2[1], q1[0], q1[1], q2[0], q2[1]),
               _point_seg_distance(q1[0], q1[1], p1[0], p1[1], p2[0], p2[1]),
               _point_seg_distance(q2[0], q2[1], p1[0], p1[1], p2[0], p2[1]))


def as_segments(kind: str, parts: Sequence[Any]
                ) -> List[Tuple[float, float, float, float]]:
    """把几何摊成一串线段 ``(x1, y1, x2, y2)``。

    * ``point`` / ``multipoint`` —— 每个顶点变成一条**零长线段**(起点 == 终点),
      这样距离那一套不用为 0 维单独写分支;
    * ``polyline`` —— 每个 part 的相邻顶点;
    * ``polygon`` / ``multipatch`` —— 每个环的相邻顶点**含闭合段**。

    ⚠️ 这是 ``distance()`` 与 ``relate()`` 的共同入口,两边都会 ``O(n·m)``。
    """
    out: List[Tuple[float, float, float, float]] = []
    if kind in ('point', 'multipoint'):
        for a in parts:
            for i in range(0, len(a), 2):
                out.append((a[i], a[i + 1], a[i], a[i + 1]))
        return out
    closed = kind in ('polygon', 'multipatch')
    for a in parts:
        n = len(a) // 2
        last = n if closed else n - 1
        for i in range(last):
            j = (i + 1) % n
            out.append((a[2 * i], a[2 * i + 1], a[2 * j], a[2 * j + 1]))
    return out


def distance_of(ka: str, pa: Sequence[Any],
                kb: str, pb: Sequence[Any]) -> Optional[float]:
    """两个几何之间的最近距离(XY 平面);任一为空返回 ``None``。

    对应 ``OGR_G_Distance``(GEOS 也是线段基的,不是近似)。先做线段包围盒剪枝,
    再逐**线段对**取最小 —— ``O(n·m)``,大几何之间会明显慢,不做空间索引
    (见 DESIGN §2.21 的已知限制)。
    """
    sa = as_segments(ka, pa)
    sb = as_segments(kb, pb)
    if not sa or not sb:
        return None
    best = math.inf
    for x1, y1, x2, y2 in sa:
        lox = x1 if x1 < x2 else x2
        hix = x2 if x1 < x2 else x1
        loy = y1 if y1 < y2 else y2
        hiy = y2 if y1 < y2 else y1
        for u1, v1, u2, v2 in sb:
            # 包围盒间距当剪枝上界:盒距已 >= best 的一对不可能更近
            dx = max(lox - (u1 if u1 > u2 else u2),
                     (u1 if u1 < u2 else u2) - hix, 0.0)
            dy = max(loy - (v1 if v1 > v2 else v2),
                     (v1 if v1 < v2 else v2) - hiy, 0.0)
            if dx * dx + dy * dy >= best * best:
                continue
            d = seg_seg_distance((x1, y1), (x2, y2), (u1, v1), (u2, v2))
            if d < best:
                best = d
                if best == 0.0:
                    return 0.0
    return best


# ----------------------------------------------------------------------------
# DE-9IM:元素分解与点位分类
# ----------------------------------------------------------------------------
def boundary_endpoints(parts: Sequence[Any]) -> set:
    """折线的边界端点集。

    OGC 规则:一条 **MultiLineString** 的边界是"出现**奇数次**的端点"。
    所以两条首尾相接的折线,接缝处那个点出现两次 → 算**内部**,不是边界。
    闭合的部分(首点 == 末点)完全不贡献边界。
    """
    counts = {}
    for a in parts:
        n = len(a) // 2
        if n == 0:
            continue
        if n >= 2 and a[0] == a[-2] and a[1] == a[-1]:
            continue                        # 闭合线:边界为空
        for k in (0, n - 1):
            p = (a[2 * k], a[2 * k + 1])
            counts[p] = counts.get(p, 0) + 1
    return {p for p, c in counts.items() if c % 2 == 1}


def point_location(px: float, py: float, kind: str, parts: Sequence[Any],
                   shells: Optional[Sequence[bool]] = None,
                   groups: Optional[Sequence[Any]] = None,
                   boundary: Optional[set] = None) -> int:
    """点相对几何的位置(INTERIOR / BOUNDARY / EXTERIOR)。

    三种几何各一套规则:

    * **点 / 多点** —— 命中某个顶点就是内部;点几何**没有边界**(OGC 规定边界为空);
    * **折线** —— 落在**某条线段的相对内部**(不含两端)就是内部;否则若它是
      边界端点算边界,其余顶点算内部(出现偶数次);都不在就是外部。
      这两条**有优先级**:一个点即使又是某条线的端点,只要它落在另一条线的
      中间,它就是内部点 —— 所以判定顺序不能反;
    * **面** —— 交给 :func:`point_in_polygon`(那里也是先判边界)。

    ⚠️ **这个函数只对"逐位精确落在几何上的点"可靠。** 它判边界用的是
    ``orient(...) == 0`` 精确比较,所以对**算出来的交点**(坐标不可精确表示)
    会答错 —— 那种情形要先用 :func:`on_geometry_role`。`relate_of` 里就是这么做的。

    ``shells`` / ``groups`` 只对面有意义,且 ``groups`` 是给批量判点用的缓存
    (见 :func:`point_in_polygon`)。``boundary`` 只对折线有意义,不传就就地算
    一遍 —— 逐点循环里一定要传,它的代价是 O(顶点数)。
    """
    if kind in ('point', 'multipoint'):
        for a in parts:
            for i in range(0, len(a), 2):
                if a[i] == px and a[i + 1] == py:
                    return INTERIOR
        return EXTERIOR
    if kind == 'polygon':
        return point_in_polygon(px, py, parts, shells, groups)
    if kind != 'polyline':
        return EXTERIOR

    if boundary is None:
        boundary = boundary_endpoints(parts)
    at_vertex = False
    for a in parts:
        n = len(a) // 2
        for i in range(n - 1):
            x1 = a[2 * i]
            y1 = a[2 * i + 1]
            x2 = a[2 * i + 2]
            y2 = a[2 * i + 3]
            if not point_on_segment(px, py, x1, y1, x2, y2):
                continue
            if (px != x1 or py != y1) and (px != x2 or py != y2):
                return INTERIOR             # 落在某段的相对内部 —— 优先级最高
            at_vertex = True
    if at_vertex:
        return BOUNDARY if (px, py) in boundary else INTERIOR
    return EXTERIOR


def _elements(kind: str, parts: Sequence[Any]
              ) -> Tuple[List[Tuple[float, float, int]],
                         List[Tuple[float, float, float, float, int, int, int]]]:
    """把几何拆成拓扑元素 ``(0 维元素, 1 维元素)``。

    * 0 维元素 ``(x, y, role)`` —— 点几何的所有顶点、折线的所有顶点、面的所有顶点;
    * 1 维元素 ``(x1, y1, x2, y2, seg_role, role_start, role_end)`` ——
      折线的线段(``seg_role=INTERIOR``)、面的环边(**``seg_role=BOUNDARY``**,
      面的边界就是环)。

    ⚠️ 线段要**同时**带上"段内部"和"两个端点"的角色,不能只带一个。折线的一个
    端点(比如 ``LINESTRING(0 0, 10 0)`` 的 ``(0,0)``)属于**边界**,而这条线段的
    内部属于**内部** —— 拿 ``seg_role`` 去标端点,那个点就会被算成内部点,
    "点落在线的端点上"会错算成 ``II=0`` 而正确的答案是 ``IB=0``、``II=F``。
    面的环边没有这个区分(整条环边连端点都是边界)。

    面的 **2 维内部**不在这里:它没法用有限个低维元素表示,由
    :func:`interior_point` 的探针单独处理(见 :func:`relate_of` 第 3 块)。
    """
    zero: List[Tuple[float, float, int]] = []
    one: List[Tuple[float, float, float, float, int, int, int]] = []
    if kind in ('point', 'multipoint'):
        for a in parts:
            for i in range(0, len(a), 2):
                zero.append((a[i], a[i + 1], INTERIOR))
        return zero, one
    if kind == 'polygon':
        for a in parts:
            n = len(a) // 2
            for i in range(n):
                j = (i + 1) % n
                one.append((a[2 * i], a[2 * i + 1], a[2 * j], a[2 * j + 1],
                            BOUNDARY, BOUNDARY, BOUNDARY))
                zero.append((a[2 * i], a[2 * i + 1], BOUNDARY))
        return zero, one
    if kind != 'polyline':
        return zero, one
    boundary = boundary_endpoints(parts)
    for a in parts:
        n = len(a) // 2
        roles = [BOUNDARY if (a[2 * i], a[2 * i + 1]) in boundary else INTERIOR
                 for i in range(n)]
        for i in range(n):
            zero.append((a[2 * i], a[2 * i + 1], roles[i]))
        for i in range(n - 1):
            one.append((a[2 * i], a[2 * i + 1],
                        a[2 * i + 2], a[2 * i + 3],
                        INTERIOR, roles[i], roles[i + 1]))
    return zero, one


def interior_point(parts: Sequence[Any],
                   shells: Optional[Sequence[bool]] = None
                   ) -> Optional[Tuple[float, float]]:
    """面的**内部**取一个点;取不到返回 ``None``。

    用**扫描线法**构造,不是质心、也不是顶点平均 —— 后两者在非凸面上可能落在
    **外面**(U 形面的质心在豁口里,带巨大洞的面的质心在洞里),而这里要的是
    "保证在内部"。

    步骤:取顶点 y 序列里**最宽**的那个间隔的中点当扫描线高度 —— 这保证扫描线
    不穿过任何顶点(穿顶点是交叉数法最容易翻车的地方);求它与所有环边的交点,
    排序后按偶奇规则取**最宽**的内部区间,取其中点。洞被偶奇规则天然扣掉。
    """
    # XY 交错:x 在偶数下标, y 在奇数下标 —— 所以步长是 2、取 i+1
    ys = sorted({a[i + 1] for a in parts for i in range(0, len(a), 2)})
    if len(ys) < 2:
        return None
    span = 0.0
    y = None
    for k in range(len(ys) - 1):
        gap = ys[k + 1] - ys[k]
        if gap > span:
            span = gap
            y = (ys[k] + ys[k + 1]) * 0.5
    if y is None:
        return None

    xs: List[float] = []
    for a in parts:
        n = len(a) // 2
        for i in range(n):
            j = (i + 1) % n
            y1 = a[2 * i + 1]
            y2 = a[2 * j + 1]
            if (y1 > y) != (y2 > y):
                x1 = a[2 * i]
                x2 = a[2 * j]
                xs.append(x1 + (y - y1) * (x2 - x1) / (y2 - y1))
    if len(xs) < 2:
        return None
    xs.sort()
    best = None
    best_w = 0.0
    for k in range(0, len(xs) - 1, 2):
        w = xs[k + 1] - xs[k]
        if w > best_w:
            best_w = w
            best = ((xs[k] + xs[k + 1]) * 0.5, y)

    # 良构面上构造出来的点必然是内部点。退化输入上兜个底:探针错了整张矩阵
    # 都会错,所以宁可退回一个"不一定在内部"的近似点,也不要给错误依据。
    if best is not None and point_in_polygon(best[0], best[1],
                                           parts, shells) == INTERIOR:
        return best
    return _vertex_average(parts)


def _split_parameters(p1: Tuple[float, float], p2: Tuple[float, float],
                      obstacles: Sequence[Any]):
    """线段 ``p1p2`` 上所有"该切开"的参数 ``t``。

    :returns: ``(ts, on_obstacle)``

        ``ts`` 是 ``[(t, hit), ...]``,按 ``t`` 升序去重;``t`` 必含 0.0 与 1.0。
        ``hit`` 说明**这个切点是怎么来的**:

        * ``None`` —— 它是 ``p1p2`` 自己的端点,或者是对方的一个**顶点**。
          这两种坐标都是逐位精确的字面量,后面拿 :func:`point_on_segment`
          判位置没问题;
        * ``(x, y)`` —— 它是与对方一条线段**求交**得到的交点。

        ``on_obstacle`` 是长度 ``len(ts) - 1`` 的 ``bool`` 列表:第 ``k`` 段
        (``ts[k]`` 到 ``ts[k+1]`` 之间)是否**与对方某条线段共线重叠**。

    ⚠️ **``hit`` 和 ``on_obstacle`` 都是为了同一件事:不许拿浮点去问"这个点在不在
    线上"。** 两个真 bug 的根因都是它(见 :func:`relate_of`):
    ``hit`` 治交点,``on_obstacle`` 治中点。

    ``obstacles`` 的每一项既可以是点 ``(x, y)``(长度 2),也可以是线段
    ``(x1, y1, x2, y2)``(长度 4)。

    为什么要切:一条线段被对方边界交过之后,**每个子段的内部**要么整体在对方
    内部、要么整体在对方边界上、要么整体在外部,不存在"一半在里一半在外"。
    于是"取子段中点分类"是**精确**的,不是抽样 —— 误差只剩"交点算得准不准"。
    """
    x1, y1 = p1
    x2, y2 = p2
    dx = x2 - x1
    dy = y2 - y1
    len2 = dx * dx + dy * dy
    lo_x = x1 if x1 < x2 else x2
    hi_x = x2 if x1 < x2 else x1
    lo_y = y1 if y1 < y2 else y2
    hi_y = y2 if y1 < y2 else y1

    ts: List[Any] = [(0.0, None), (1.0, None)]
    #: 与障碍线段共线重叠的 t 区间。**这些子段"在对方身上"是组合事实,不用算。**
    spans: List[Tuple[float, float]] = []

    def param(px: float, py: float) -> float:
        if len2 <= 0.0:
            return 0.0
        t = ((px - x1) * dx + (py - y1) * dy) / len2
        if t <= 0.0:
            return 0.0
        if t >= 1.0:
            return 1.0
        return t

    for ob in obstacles:
        if len(ob) == 2:
            px, py = ob
            if (lo_x <= px <= hi_x and lo_y <= py <= hi_y
                    and point_on_segment(px, py, x1, y1, x2, y2)):
                ts.append((param(px, py), None))
            continue
        u1, v1, u2, v2 = ob
        # 线段包围盒快速排除 —— O(n·m) 里唯一省得下大头的地方
        if (u1 if u1 < u2 else u2) > hi_x or (u1 if u1 > u2 else u2) < lo_x:
            continue
        if (v1 if v1 < v2 else v2) > hi_y or (v1 if v1 > v2 else v2) < lo_y:
            continue
        kind, a, b = seg_seg_classify(p1, p2, (u1, v1), (u2, v2))
        if kind == SEG_POINT:
            ts.append((param(a[0], a[1]), a))
        elif kind == SEG_OVERLAP:
            ta = param(a[0], a[1])
            tb = param(b[0], b[1])
            ts.append((ta, a))
            ts.append((tb, b))
            if ta < tb:
                spans.append((ta, tb))
            elif tb < ta:
                spans.append((tb, ta))
    ts.sort(key=lambda it: it[0])
    out = [ts[0]]
    for t, hit in ts[1:]:
        if t > out[-1][0]:
            out.append((t, hit))
        elif t == out[-1][0] and out[-1][1] is None and hit is not None:
            # 同一个 t 上既来了个顶点障碍、又来了个线段交点 —— **交点那条更值
            # 钱**:它带着"这一点按构造就在对方身上"的信息,顶点那条只给了个 t。
            # 取交点,别把它丢了。
            out[-1] = (t, hit)

    # 子段 k 是否落在某个重叠区间里。判**中点**而不是判端点:重叠区间的两个
    # 端点也是通过 param() 出来的、必然在 out 里,所以"区间整个落进去"等价于
    # "中点落进去",而中点比较没有端点归属的歧义。
    on_obstacle = []
    for k in range(len(out) - 1):
        mid_t = (out[k][0] + out[k + 1][0]) * 0.5
        on_obstacle.append(any(lo <= mid_t <= hi for lo, hi in spans))
    return out, on_obstacle


def shared_segment_role(kind: str) -> int:
    """**与对方一条线段共线重叠**的那段子线段,整体算是对方的哪一部分。

    第 2 块里"子段落在对方哪儿"平常是取中点问 :func:`point_location`;但共线
    重叠的子段**按构造就在对方身上**,而它的中点算出来一般不在对方线上(见
    :func:`relate_of` 的两条说明),所以必须按几何类型直接定:

    * **面** —— 边界就是环,子段落在环上 → ``BOUNDARY``;
    * **折线** —— 子段是 1 维的,而折线的边界只是一组**点**,1 维的东西不可能
      被塞进一个 0 维集合里,所以它一定在相对内部 → ``INTERIOR``。

    (点 / 多点没有 1 维元素,走不到这里。)
    """
    return BOUNDARY if kind == 'polygon' else INTERIOR


def on_geometry_role(px: float, py: float, kind: str,
                     boundary: Optional[set] = None) -> int:
    """已知点 ``(px, py)`` **就在** ``kind`` 这个几何上时,它的角色。

    这是给"与对方线段求交得来的交点"用的兜底。那种点的坐标不是精确可表示的,
    再拿浮点叉积去问一遍只会得到错的答案 —— **这就是 :func:`relate_of` 第 2 块
    里那个 bug**。既然按构造它一定在对方上,角色就只有那么几种可能,直接数:

    * **面** —— 边界就是环,所以一定是 ``BOUNDARY``(面的内部是 2 维的,一个
      点不可能落在它里面 —— 这跟"点在环上"本来就是同一件事);
    * **折线** —— 是边界端点(出现**奇数次**的端点)就 ``BOUNDARY``,否则
      ``INTERIOR``;
    * **点 / 多点** —— OGC 规定点几何的边界为空,一律 ``INTERIOR``。

    ``boundary`` 是 :func:`boundary_endpoints` 的结果,只对折线有意义。
    """
    if kind == 'polygon':
        return BOUNDARY
    if kind == 'polyline':
        return BOUNDARY if (px, py) in (boundary or ()) else INTERIOR
    return INTERIOR


def relate_of(ka: str, pa: Sequence[Any], sa: Optional[Sequence[bool]],
              kb: str, pb: Sequence[Any],
              sb: Optional[Sequence[bool]]) -> str:
    """DE-9IM 矩阵:9 个字符,取值 ``F`` / ``0`` / ``1`` / ``2``。

    格子顺序 ``II IB IE / BI BB BE / EI EB EE``(A 的 I/B/E 是行,B 的是列)。

    三块:

    1. **0 维元素**直接分类:每个顶点判它在对方哪儿,对应格子记维度 0。
    2. **1 维元素切分后分类**:把 A 的每条线段用 B 的**所有**元素(点和线段)
       切开,每个子段取中点判位置 —— 子段内部位置恒定,所以这是精确的。
       子段记维度 1,子段端点(含交点本身)记维度 0。两边的 1 维元素各做一遍。
    3. **2 维内部(面)**:面的内部没法用有限个低维元素表示,靠
       :func:`interior_point` 的构造性内部点,加"环子段落在对方内部/外部 ⇒
       环内侧紧邻的那块内部也在对方内部/外部"这条推断来定 ``II`` / ``IE`` / ``EI``。

    ``EE`` 恒为 2(两个几何都有界)。

    ⚠️ 唯一误差来源是第 2、3 块里**交点的浮点求解**:交点算偏了,切分位置就偏,
    子段中点可能落到错误的一侧。没有 snap-rounding,次 ULP 的退化构型
    (几乎共线的边、量化网格上重合的点)可能判错。见模块 docstring。

    任一为空 → ``'FFFFFFFF2'``(与 JTS/GEOS 的 ``RelateOp`` 一致)。
    """
    if not pa or not pb:
        return 'FFFFFFFF2'

    zero_a, one_a = _elements(ka, pa)
    zero_b, one_b = _elements(kb, pb)

    # m[i][j]:i 是 A 侧角色,j 是 B 侧角色;值 = 维度,-1 表示 F
    m = [[-1, -1, -1], [-1, -1, -1], [-1, -1, -1]]

    def bump(i: int, j: int, dim: int) -> None:
        if m[i][j] < dim:
            m[i][j] = dim

    # 环的组装(哪个洞属于哪个外环)只算一次 —— relate 要对成百上千个探针点
    # 判位置,每次重算一遍 organize_polygons 是 O(环数²) 的浪费。
    groups_a = organize_polygons(pa, sa) if ka == 'polygon' else None
    groups_b = organize_polygons(pb, sb) if kb == 'polygon' else None

    # 1 维块要按"交点在对方上的角色"兜底,这个集合只算一次 —— 折线的
    # boundary_endpoints 是 O(顶点数),放进逐点循环里等于每次多扫一遍全几何。
    boundary_a = boundary_endpoints(pa) if ka == 'polyline' else None
    boundary_b = boundary_endpoints(pb) if kb == 'polyline' else None

    def loc_a(x: float, y: float) -> int:
        return point_location(x, y, ka, pa, sa, groups_a, boundary_a)

    def loc_b(x: float, y: float) -> int:
        return point_location(x, y, kb, pb, sb, groups_b, boundary_b)

    def split_role(loc: int, x: float, y: float, hit: Any, kind: str,
                   boundary: Optional[set]) -> int:
        """切点的 B 侧(A 侧由调用方转置)角色。

        正常情况下问 :func:`point_location` 就完了。**但它答不对交点的位置**:
        交点 ``(x, y)`` 是与对方线段求出来的,**不是**精确可表示的浮点数,把它
        代进 ``orient(...) == 0`` 得到的是 2.8e-14 而不是 0 —— 于是"在线上"被判
        成"在外部",``II`` / ``IB`` / ``BI`` 整格塌成 ``F``。

        实测的最小复现::

            A = LINESTRING(-0.0000003 0.0000004, 10.0000002 9.9999998)
            B = LINESTRING(0 10, 10 0)
            本库 FF1FF0102   GEOS 0F1FF0102

        ⚠️ 这**不是**次 ULP 的退化构型,是任何交点不可精确表示的正常情形 ——
        整数坐标的合成用例碰不到,所以它躲过了 147 个用例;是
        ``tools/verify_topology.py`` 在**真** GEOS 上抓出来的。

        兜底很直接:``hit`` 带着交点坐标,而按构造这个点**就在**对方上,所以
        不需要再问浮点 —— :func:`on_geometry_role` 直接把它数出来。
        """
        if loc == EXTERIOR and hit is not None:
            return on_geometry_role(hit[0], hit[1], kind, boundary)
        return loc

    # --- 1. 0 维元素 ---
    for x, y, role in zero_a:
        bump(role, loc_b(x, y), 0)
    for x, y, role in zero_b:
        bump(loc_a(x, y), role, 0)

    # --- 2. 1 维元素:切分 + 分类(两个方向各一遍) ---
    # 每个子段的分类结果留下来给第 3 块复用 —— 那里要按同样的切分再看一遍子段
    # 落在哪儿,重算一遍等于把 relate 在面上的成本翻一倍。
    splits_a: List[Any] = []
    seg_locs_a: List[Any] = []
    obstacles_b: List[Any] = [(x, y) for x, y, _ in zero_b]
    obstacles_b.extend((o[0], o[1], o[2], o[3]) for o in one_b)
    for x1, y1, x2, y2, seg_role, role_a, role_b in one_a:
        ts, on_ob = _split_parameters((x1, y1), (x2, y2), obstacles_b)
        splits_a.append(ts)
        last = len(ts) - 1
        locs = []
        for k, (t, hit) in enumerate(ts):
            # 0 维:切分点自己的角色 —— 两端的原顶点用 role_a/role_b,
            # 中间的交点落在段的相对内部,用 seg_role(见 _elements 的警示)
            role_pt = role_a if k == 0 else (role_b if k == last else seg_role)
            px = x1 + t * (x2 - x1)
            py = y1 + t * (y2 - y1)
            bump(role_pt, split_role(loc_b(px, py), px, py, hit, kb,
                                     boundary_b), 0)
            if k < last:
                if on_ob[k]:
                    # 这一段与 B 的一条线段共线重叠 —— **它在 B 上是组合事实**,
                    # 它的中点算出来一般不在 B 的线上,不能拿去问 point_location。
                    loc = shared_segment_role(kb)
                else:
                    mid = (t + ts[k + 1][0]) * 0.5
                    loc = loc_b(x1 + mid * (x2 - x1), y1 + mid * (y2 - y1))
                bump(seg_role, loc, 1)
                locs.append(loc)
            else:
                locs.append(None)
        seg_locs_a.append(locs)

    splits_b: List[Any] = []
    seg_locs_b: List[Any] = []
    obstacles_a: List[Any] = [(x, y) for x, y, _ in zero_a]
    obstacles_a.extend((o[0], o[1], o[2], o[3]) for o in one_a)
    for x1, y1, x2, y2, seg_role, role_a, role_b in one_b:
        ts, on_ob = _split_parameters((x1, y1), (x2, y2), obstacles_a)
        splits_b.append(ts)
        last = len(ts) - 1
        locs = []
        for k, (t, hit) in enumerate(ts):
            role_pt = role_a if k == 0 else (role_b if k == last else seg_role)
            px = x1 + t * (x2 - x1)
            py = y1 + t * (y2 - y1)
            bump(split_role(loc_a(px, py), px, py, hit, ka,
                            boundary_a), role_pt, 0)
            if k < last:
                if on_ob[k]:
                    loc = shared_segment_role(ka)
                else:
                    mid = (t + ts[k + 1][0]) * 0.5
                    loc = loc_a(x1 + mid * (x2 - x1), y1 + mid * (y2 - y1))
                bump(loc, seg_role, 1)
                locs.append(loc)
            else:
                locs.append(None)
        seg_locs_b.append(locs)

    # --- 3. 2 维内部 ---
    # ⚠️ 维度上限由**低维那一侧**决定:交集的维度不可能超过任一操作数。所以往
    # II 里写 2 必须**两侧都是面**;往 IE / EI 里写 2 只要求"2 维的那一侧"是面
    # (另一侧是 I/B/E 都行,因为外部/边界不限制维度)。
    # 少了这个判据,"一条线穿过一个面"会算出 II=2(实际只能是 1)。
    both_2d = ka == 'polygon' and kb == 'polygon'
    if ka == 'polygon':
        ip = interior_point(pa, sa)
        if ip is not None:
            loc = loc_b(ip[0], ip[1])
            if loc == EXTERIOR:
                # 面内部有落在对方之外的部分 → IE 是 2 维(那一侧是 2 维的)
                bump(INTERIOR, EXTERIOR, 2)
            elif loc == INTERIOR and both_2d:
                bump(INTERIOR, INTERIOR, 2)
        # 环子段落在对方内部/外部 ⇒ 环内侧紧邻的那块**内部**也在对方内部/外部
        for locs in seg_locs_a:
            for loc in locs:
                if loc == EXTERIOR:
                    bump(INTERIOR, EXTERIOR, 2)
                elif loc == INTERIOR and both_2d:
                    bump(INTERIOR, INTERIOR, 2)
    if kb == 'polygon':
        ip = interior_point(pb, sb)
        if ip is not None:
            loc = loc_a(ip[0], ip[1])
            if loc == EXTERIOR:
                bump(EXTERIOR, INTERIOR, 2)
            elif loc == INTERIOR and both_2d:
                bump(INTERIOR, INTERIOR, 2)
        for locs in seg_locs_b:
            for loc in locs:
                if loc == EXTERIOR:
                    bump(EXTERIOR, INTERIOR, 2)
                elif loc == INTERIOR and both_2d:
                    bump(INTERIOR, INTERIOR, 2)

    m[EXTERIOR][EXTERIOR] = 2
    return ''.join('F' if v < 0 else str(v) for row in m for v in row)


# ----------------------------------------------------------------------------
# 谓词:全部由 DE-9IM 矩阵派生
# ----------------------------------------------------------------------------
#: 维度,对应 ``OGRGeometry::getDimension``。
def dimension_of(kind: str) -> int:
    """拓扑维:点/多点 ``0``、折线 ``1``、面/multipatch ``2``、null ``0``。

    对应 ``OGRGeometry::getDimension``(``wkbNone`` 走基类返回 0;multipatch 按
    ``wkbPolyhedralSurface`` 算 2)。
    """
    if kind == 'polyline':
        return 1
    if kind in ('polygon', 'multipatch'):
        return 2
    return 0


def transpose_de9im(m: str) -> str:
    """矩阵转置 —— 也就是 ``relate(A, B)`` 与 ``relate(B, A)`` 的关系。

    这是最省事的一条自检:两个方向的矩阵必须互为转置,拿它测实现比背真值表可靠。
    """
    return ''.join(m[i] for i in (0, 3, 6, 1, 4, 7, 2, 5, 8))


def matches_de9im(m: str, pattern: str) -> bool:
    """矩阵是否匹配一个 DE-9IM 模式。

    模式里 ``T`` 匹配 0/1/2,``F`` 匹配 F,``*`` 匹配任意。
    """
    for a, b in zip(m, pattern):
        if b == '*':
            continue
        if b == 'T':
            if a == 'F':
                return False
        elif a != b:
            return False
    return True


#: 单模式的谓词。值里的每个模式任一命中即为 True。
#: 语义取自 OGC Simple Features / DE-9IM,与 GEOS(以及转手给它的 GDAL)一致。
_DE9IM_PATTERNS = {
    # 不相交 —— 两个几何的任意部分都不碰
    'disjoint': ('FF*FF****',),
    # 包含:内点相交,且 B 的边界与内部都不落在 A 的外部
    'contains': ('T*****FF*',),
    # 被包含:contains 换方向
    'within': ('T*F**F***',),
    # 覆盖:包含的弱化版 —— A 的内部**或边界**与 B 的内部相交,
    # 且 B 不落在 A 的外部。所以是四种情形的并
    'covers': ('T*****FF*', '*T****FF*', '***T**FF*', '****T*FF*'),
    # 相接:至少一处相交,且两者内部**不相交**
    'touches': ('FT*******', 'F**T*****', 'F***T****'),
    # 拓扑等价
    'equals': ('T*F**FFF*',),
}


def predicate_de9im(name: str, m: str, dim_a: int, dim_b: int) -> bool:
    """由一个 DE-9IM 矩阵算出谓词结果。

    ``crosses`` / ``overlaps`` / ``covered_by`` 不是查表能解决的,单独处理:

    * ``crosses`` —— 按维度分三种模式(低维穿高维 / 高维被穿 / 同维只有线能穿);
    * ``overlaps`` —— 点/面用一种模式(6、7 格都要 T),线用另一种(要求 II=1);
    * ``covered_by(a, b)`` —— 就是 ``covers(b, a)``:把矩阵转置了再查表。
    """
    if name == 'covered_by':
        return predicate_de9im('covers', transpose_de9im(m), dim_b, dim_a)
    if name == 'intersects':
        return not matches_de9im(m, 'FF*FF****')
    if name == 'crosses':
        # 维度不同:低维那个的内部要同时碰到对方的内部和外部
        if dim_a < dim_b:
            return matches_de9im(m, 'T*T******')
        if dim_a > dim_b:
            return matches_de9im(m, 'T*****T**')
        # 同维:只有两条线能"交叉"(面交叉就是重叠,点的交叉不可能),
        # 模式要求 II 是 0 维
        return dim_a == 1 and matches_de9im(m, '0********')
    if name == 'overlaps':
        if dim_a != dim_b:
            return False
        if dim_a == 1:                      # 线:交集必须是一段(1 维)
            return matches_de9im(m, '1*T***T**')
        return matches_de9im(m, 'T*T***T**')   # 点 / 面
    patterns = _DE9IM_PATTERNS.get(name)
    if patterns is None:
        raise ValueError(f'未知的 DE-9IM 谓词:{name}')
    return any(matches_de9im(m, p) for p in patterns)
