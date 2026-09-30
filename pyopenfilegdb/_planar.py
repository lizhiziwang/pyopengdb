"""平面图机器 —— 节点化 / 半边图 / 面遍历 / 面深度 / 走环。

这个模块是 :mod:`~pyopenfilegdb._buffer_ops`(缓冲区)与
:mod:`~pyopenfilegdb._overlay_ops`(overlay:`union` / `difference` /
`intersection` / `symmetric_difference`)**共用**的那一层。

放在这里的都是**与输入几何无关**的东西:把一组带标签的曲线节点化、去重合并,
搭成半边平面图,遍历出所有面,算出每个面的深度,再把"结果面"的边界走成环。
两边的差别只在于"曲线怎么带标签"与"哪些面算结果面",那两条各自留在自己的模块里。

依赖关系是 DAG:``_geometry_ops`` ← ``_planar``;本模块**不 import 本包其他任何
模块**(所以谁都能站在它上面,也不会出现环)。

对照的 JTS/GEOS 源码(GEOS 的 ``operation/overlayng/`` 与
``operation/buffer/`` 都源自 JTS):

* ``_noded_edges``        —— ``EdgeNodingBuilder`` + ``MCIndexNoder`` +
  ``IntersectionAdder`` + ``EdgeMerger`` 的等价物;
* ``_PlanarGraph``        —— ``OverlayGraph`` / ``PlanarGraph``;
* ``_face_depths``        —— ``OverlayLabeller`` 的位置标签(本库换成面级 BFS,
  理由见 ``DESIGN.md`` §2.23 / §2.24);
* ``_result_rings``       —— ``OverlayLabeller.markInResultArea`` +
  ``PolygonBuilder.linkResultAreaEdgesMax``;
* ``_assemble``           —— ``PolygonBuilder`` + ``OverlayEdgeRing`` 的壳洞归属。

⚠️ **本模块不做 snap-rounding**(见 ``_geometry_ops`` 模块 docstring 与
``DESIGN.md``):节点等同用精确坐标,交点用普通双精度,不承诺与 GEOS 逐位相同。

delta 是**向量,不是标量**
-----------------------
这个模块只认"N 个输入"的通用形态:一条曲线的标签有 **N 项**(每个输入一项),
合并出来的 ``edelta`` / 面深度都是长度 N 的向量 —— 第 k 个分量只统计**第 k 个
输入的边界**。``_buffer_ops`` 是 N = 1 的特例(它用一个薄包装把标量包成
单元素向量再摊回去,见那边顶部);``_overlay_ops`` 是 N = 2。向量化的理由不是
"将来可能更多输入",而是 overlay 必须分开跟踪"边在 A 里的深度"与"在 B 里的
深度"才能套四个算子各不相同的判据。

⚠️ **一个输入的"线"对 delta 的贡献恒为 0。** 线不是面的边界,穿过它不改变任何
分量的深度 —— 否则线会把面切成两半,``difference`` 会给出两个碎多边形。见
:func:`_label_vectors` 与 ``_overlay_ops`` 的模块 docstring。
"""
from __future__ import annotations

import math
from array import array
from itertools import chain
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ._geometry_ops import (
    EXTERIOR,
    INTERIOR,
    SEG_NONE,
    SEG_OVERLAP,
    organize_polygons,
    ring_signed_area2,
    seg_seg_classify,
)

_PI_2 = 2.0 * math.pi

#: "这个顶点没有 z"。**故意用 NaN 而不是 None** —— JTS/GEOS 的
#: ``ElevationModel`` 全程用 ``Double.isNaN`` 判空,而且它要参与算术
#:(``z_a + t * (z_b - z_a)``):传 NaN 进来会自己传染出去,正是我们要的语义。
_NAN = float('nan')

#: 标签里的一项 —— 这条边相对某个输入的角色。与 JTS ``OverlayLabel`` 的
#: ``DIM_*`` 同义(``DIM_COLLAPSE`` 本库**不产生**:不做塌缩检测,见
#: ``_overlay_ops`` 的诚实边界)。
DIM_NOT_PART = 0
DIM_LINE = 1
DIM_BOUNDARY = 2


# ----------------------------------------------------------------------------
# 通用小工具
# ----------------------------------------------------------------------------
def _clean(coords: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """``CoordinateArrays.removeRepeatedOrInvalidPoints``。

    去掉非法(NaN/Inf)与**相邻重复**的点。注意它**不做首尾环绕去重** ——
    JTS 也不做。
    """
    out: List[Tuple[float, float]] = []
    for c in coords:
        if not (math.isfinite(c[0]) and math.isfinite(c[1])):
            continue
        if out and out[-1] == c:
            continue
        out.append(c)
    return out


def _coords(arr) -> List[Tuple[float, float]]:
    """交错数组 -> ``(x, y)`` 表。"""
    return [(arr[i], arr[i + 1]) for i in range(0, len(arr), 2)]


def _close_ring(pts: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """补上闭合点 —— JTS 的 ``LinearRing.getCoordinates()`` 是**含**闭合点的。"""
    if pts and pts[0] != pts[-1]:
        return pts + [pts[0]]
    return pts


def _clean_ring(pts: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
    """去重复/非法点,并削掉尾部闭合点(本库内存里的环不闭合)。"""
    pts = _clean(pts)
    while len(pts) > 1 and pts[0] == pts[-1]:
        pts.pop()
    return pts


def _pts_and_z(part, zs, close_ring: bool):
    """一块交错坐标的 ``(pts, z)``:``pts`` 与 ``_clean`` 逐位同源,``z`` 跟着
    **同一张下标表**走。

    为什么不能用 ``_clean`` / ``_clean_ring`` 再自己配一遍 z:那两个函数会**删点**
    (非法点、相邻重复点、尾部闭合点),删了之后下标就对不上了。所以这里
    把"保留哪些原始下标"抽出来当唯一事实来源,xy 和 z 都照它取。

    ``zs`` 为 ``None``(该输入没有 Z)时返回的 z 全是 NaN —— 由
    :func:`_add_node_z` 忽略,整条链路的行为与 Z 不存在时完全一致。

    ⚠️ ``close_ring`` 的两个分支**只差尾部**,而且**必须**分开:面输入是环,
    要削掉尾部的闭合点再补回来(与 ``_close_ring`` 逐位同源);折线**不能**走
    那一支 —— 一条首尾重合的折线(合法输入,Esri 里很常见)里"末点回到首点"
    那一段是**真实存在的边**,把末点削掉会把这条边抹掉。
    """
    keep: List[int] = []
    n = len(part) // 2
    last = None
    for k in range(n):
        p = (part[2 * k], part[2 * k + 1])
        if not (math.isfinite(p[0]) and math.isfinite(p[1])):
            continue
        if p == last:
            continue
        keep.append(k)
        last = p
    if close_ring:
        while len(keep) > 1 and (part[2 * keep[0]], part[2 * keep[0] + 1]) == \
                (part[2 * keep[-1]], part[2 * keep[-1] + 1]):
            keep.pop()
    pts = [(part[2 * k], part[2 * k + 1]) for k in keep]
    z = [_NAN if zs is None else zs[k] for k in keep]
    if close_ring and pts and pts[0] != pts[-1]:   # 补闭合点(_close_ring 同款判据)
        pts.append(pts[0])
        z.append(z[0])
    return pts, z


def _ring_pts_and_z(part, zs):
    """一个**环**的 ``(pts, z)``(尾部闭合点补齐)。见 :func:`_pts_and_z`。"""
    return _pts_and_z(part, zs, True)


def _line_pts_and_z(part, zs):
    """一条**折线**的 ``(pts, z)``(**不**补闭合点、**不**削尾点)。

    见 :func:`_pts_and_z` —— overlay 的线输入走这一支
    (``_overlay_ops._line_curves``)。
    """
    return _pts_and_z(part, zs, False)



def _num_geometries(kind: str, parts, n_points: int, n_shells: int) -> int:
    """输入的 ``OGRGeometry::getNumGeometries()`` —— 只用于 ``keepLargestArea``。"""
    if kind == 'point':
        return 1
    if kind == 'multipoint':
        return n_points
    if kind == 'polyline':
        return len(parts)
    if kind == 'polygon':
        return n_shells
    return 1


# ----------------------------------------------------------------------------
# 标签 -> depthDelta
# ----------------------------------------------------------------------------
class PlanarTopologyError(Exception):
    """平面图的拓扑自相矛盾(面深度对不上)。

    对应 JTS 的 ``TopologyException``。**它不是内部错误**,而是降级重试梯的
    触发信号 —— ``buffer_parts`` 靠它决定"要不要降精度重算",overlay 侧同理。
    """


def _depth_delta(left_loc: int, right_loc: int) -> int:
    """这条边两边的"层数差"。

    对应 ``BufferBuilder.depthDelta`` 与 ``EdgeNodingBuilder.computeDepthDelta``
    —— 两侧的 JTS 名字不同,公式是同一个。

    只有"一侧内部、一侧外部"才是真正的边界(±1);两侧都是外部 / 都是内部 /
    有一侧是边界 —— 差都是 0,也就是**这条边不影响面深度,不产生轮廓**。
    这个 0 不是"没用",而是关键:两条**走向相同、标签相反**的偏移曲线合到
    一起时,+1 和 −1 加出 0,那条边就自动从结果里消失了 —— 这就是
    "一条折线的两侧偏移互相抵消"背后的机制,JTS 那边叫 ``DIM_COLLAPSE``。

    ⚠️ 别把"标签相反"说成"方向相反" —— 实测(``tests/test_buffer.py`` 的
    ``test_merged_duplicate_edge_has_zero_delta`` 守着):走向**相反**的两条
    曲线合起来是 **±2**,不是 0;要出 0 必须**走向相同**。区别在于
    ``insertUniqueEdge`` 只在方向相反时才 ``flip()`` 标签。
    """
    if left_loc == INTERIOR and right_loc == EXTERIOR:
        return 1
    if left_loc == EXTERIOR and right_loc == INTERIOR:
        return -1
    return 0


# ----------------------------------------------------------------------------
# 节点化 —— 本库没有,全新写(语义对齐 MCIndexNoder + IntersectionAdder)
# ----------------------------------------------------------------------------
def _label_vectors(label) -> Tuple[Tuple[int, ...], Tuple[int, ...]]:
    """一条曲线的标签 -> ``(delta_vec, dim_vec)``,长度都等于输入个数。

    标签的编码(**每一项一个输入**,按输入顺序):

    * ``None``   —— 这条边**不属于**该输入:delta ``0``、``DIM_NOT_PART``;
    * ``(loc,)`` —— 该输入是**线**,这条边在线上:delta **恒为 0**、
      ``DIM_LINE``;
    * ``(l, r)`` —— 该输入是**面**,这条边是它的边界:delta 由
      :func:`_depth_delta` 定、``DIM_BOUNDARY``。

    ``dim`` 与 ``delta`` 必须分开记:一条"两侧同一位置"的边界边(退化边)
    delta 是 0,但它仍然是 ``DIM_BOUNDARY`` —— 而线输入 delta 也是 0。两者
    在 ``LineBuilder.isResultLine`` 那一串闸门里判然不同(``isBoundarySingleton``
    看的就是"另一侧是不是 ``NOT_PART``"),所以不能拿 delta 反推 dim。

    ⚠️ **线那一项 delta 恒为 0 是设计要害。** 线不是面的边界,穿过它不改变
    任何分量的深度。若把两条输入的边一视同仁地当屏障,穿过面的线会把面切成
    两半,``difference`` 就给出两个碎多边形(实测 GEOS 给的是面积原样不变
    的单个多边形)。详见 ``_overlay_ops`` 的模块 docstring。
    """
    dl: List[int] = []
    dm: List[int] = []
    for e in label:
        if e is None:
            dl.append(0)
            dm.append(DIM_NOT_PART)
        elif len(e) == 1:
            dl.append(0)
            dm.append(DIM_LINE)
        else:
            dl.append(_depth_delta(e[0], e[1]))
            dm.append(DIM_BOUNDARY)
    return tuple(dl), tuple(dm)


def _add_split(extra: list, i: int, a, b, p) -> None:
    """把一个交点记到第 ``i`` 条段上(端点不算,内部点才算)。"""
    if p is None or p == a or p == b:
        return
    lst = extra[i]
    if lst is None:
        extra[i] = [p]
    elif p not in lst:
        lst.append(p)


def _add_node_z(zacc: Dict[int, List[float]], u: int, a, b, za: float, zb: float,
                p) -> None:
    """记下"节点 ``u`` 从**这一条源线段**那里拿到的 z"。

    这是 JTS ``LineIntersector.computeZ`` 的等价物:交点的 z = 两条线段各自
    在交点处**线性插值**出来的 z 的平均(NaN 不参与)。本库把"平均"推迟到
    最后统一做(见 :func:`_noded_edges` 的收尾),所以这里只负责**记一条贡献**。

    ⚠️ 一条**源线段**只贡献一次,**不管它在节点化里被切成了几段** —— 实测
    "3D 与 3D 不等高"那组数据就是这么定的:``POLYGON Z(全 5)`` 与
    ``POLYGON Z(5,5,15,15 处为 7,7,10,10)`` 求交,节点 ``(5,10)`` 上 A 的边被
    切成两段的**一侧**只算一次,A 给 5、B 给 8.5 → 6.75。若按"子段"计数会得到
    6.17,与 GEOS 对不上。

    线段自己没 z(2D 输入)时什么都不记 —— 于是结果里那个顶点要么由另一侧的
    3D 线段给值,要么留 NaN 交给 ``_overlay_ops._ElevationGrid`` 兜底。
    """
    if za != za and zb != zb:
        return
    dx = b[0] - a[0]
    dy = b[1] - a[1]
    den = dx * dx + dy * dy
    if den <= 0.0:
        z = za if za == za else zb
    else:
        t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / den
        z = za + t * (zb - za)
    if z != z:
        return
    lst = zacc.get(u)
    if lst is None:
        zacc[u] = [z]
    else:
        lst.append(z)


def _noded_edges(curves):
    """把带标签的原始曲线摊成"节点化 + 去重合并"之后的边表。

    :param curves: ``(pts, label)`` 或 ``(pts, label, zs)`` 三元组表,``label`` 的
        编码见 :func:`_label_vectors`;``zs`` 是**与 ``pts`` 逐点对应**的 z
        列表(不给 = 这条曲线没有 z)。
    :returns: ``(node_pts, eu, ev, edelta, edim, node_z)``;``node_pts`` 是
        ``(x, y)`` 元组表,``eu[e] -> ev[e]`` 是第 e 条边的**规范方向**(首次
        插入时的方向),``edelta[e]`` 是沿这个方向的**逐输入** depthDelta
        累计值(``list[int]``),``edim[e]`` 是逐输入的边角色(``list[int]``,
        取值见 ``DIM_*``),``node_z`` 是逐节点的 z(``list[float]``,
        没有 z 来源的节点是 NaN)。

    三个要点:

    * **交点坐标只算一次、两边共用同一个元组。** 这不是省事 —— JTS
      ``IntersectionAdder`` 传给两条 ``NodedSegmentString`` 的是**同一个
      ``Coordinate`` 对象**。若两边各算一次,末位差一位就会生成两个节点和一条
      极短边,面遍历立刻错乱。这是整个节点化里唯一"必须这么做"的地方。
    * **节点等同用精确 ``(x, y)`` 元组做 key。** JTS 的 ``TreeMap`` +
      ``Coordinate.compareTo`` 就是精确比较。三线共点但两两交点末位不同 → 图上
      就是两个节点 + 一条极短边,**JTS/GEOS 也是这样**。这里**不加容差** ——
      加了就和参照实现对不上,而且容差不解决拓扑问题,只把问题挪到别处。
    * 去重按**无向**节点对,方向相反的入边先把 delta 取反再累加
      (``insertUniqueEdge`` + ``Label.flip``)。**dim 不跟着翻**(它是角色不是
      符号),合并时逐项**取大** —— 与 JTS ``Edge.merge`` 的 ``dim = max`` 一致。
    """
    if not curves:
        return [], [], [], [], [], []
    # delta / dim 逐曲线算一次(同一曲线上的每段都一样)
    vecs = [_label_vectors(c[1]) for c in curves]
    nv = len(vecs[0][0])            # 输入个数 —— 取 delta 那一半的长度

    # 1) 摊成线段(z 跟着**段**走:每段记两端点的 z,曲线没 z 就是 NaN)
    segs: List[Tuple[Any, Any, int]] = []
    seg_z: List[Tuple[float, float]] = []
    for ci, curve in enumerate(curves):
        pts = curve[0]
        zs = curve[2] if len(curve) > 2 else None
        for i in range(len(pts) - 1):
            a = pts[i]
            b = pts[i + 1]
            if a == b:
                continue            # computeNodedEdges:端点相同的 2 点子串丢掉
            segs.append((a, b, ci))
            if zs is None:
                seg_z.append((_NAN, _NAN))
            else:
                seg_z.append((zs[i], zs[i + 1]))
    n = len(segs)
    if n == 0:
        return [], [], [], [], [], []

    # 2) 找所有交点(按 x 区间排序 + 活动集扫描,不做全对全)
    min_x = [min(s[0][0], s[1][0]) for s in segs]
    max_x = [max(s[0][0], s[1][0]) for s in segs]
    min_y = [min(s[0][1], s[1][1]) for s in segs]
    max_y = [max(s[0][1], s[1][1]) for s in segs]
    extra: List[Optional[List[Any]]] = [None] * n
    active: List[int] = []
    for i in sorted(range(n), key=lambda k: min_x[k]):
        xi = min_x[i]
        if len(active) > 256:
            # 惰性压实 —— 活动集是"min_x 已扫过、max_x 还没扫过"的段
            active = [j for j in active if max_x[j] >= xi]
        ai, bi, _ = segs[i]
        ay_lo = min_y[i]
        ay_hi = max_y[i]
        for j in active:
            if max_x[j] < xi or min_x[j] > max_x[i]:
                continue
            if min_y[j] > ay_hi or max_y[j] < ay_lo:
                continue
            aj, bj, _ = segs[j]
            kind, pa, pb = seg_seg_classify(ai, bi, aj, bj)
            if kind == SEG_NONE:
                continue
            _add_split(extra, i, ai, bi, pa)
            _add_split(extra, j, aj, bj, pa)
            if kind == SEG_OVERLAP:
                _add_split(extra, i, ai, bi, pb)
                _add_split(extra, j, aj, bj, pb)
        active.append(i)

    # 3) 按参数序把交点插回去,并逐段插入边表
    node_id: Dict[Tuple[float, float], int] = {}
    eu: List[int] = []
    ev: List[int] = []
    edelta: List[List[int]] = []
    edim: List[List[int]] = []
    edge_index: Dict[Tuple[int, int], int] = {}
    #: 节点 -> 各条"经过它的**源线段**"插值出来的 z(见 :func:`_add_node_z`)
    zacc: Dict[int, List[float]] = {}

    def nid(p) -> int:
        i = node_id.get(p)
        if i is None:
            i = len(node_id)
            node_id[p] = i
        return i

    for i in range(n):
        a, b, ci = segs[i]
        za, zb = seg_z[i]
        chain = [a]
        pts_i = extra[i]
        if pts_i:
            dx = b[0] - a[0]
            dy = b[1] - a[1]
            den = dx * dx + dy * dy
            if den > 0.0:
                pts_i.sort(key=lambda p, _a=a, _dx=dx, _dy=dy, _d=den:
                           ((p[0] - _a[0]) * _dx + (p[1] - _a[1]) * _dy) / _d)
            for p in pts_i:
                if p != chain[-1]:
                    chain.append(p)
        if chain[-1] != b:
            chain.append(b)

        for p in chain:                 # z:链上**每一个**节点都从这条线段拿一份
            _add_node_z(zacc, nid(p), a, b, za, zb, p)

        dv, dm = vecs[ci]
        for k in range(len(chain) - 1):
            p = chain[k]
            q = chain[k + 1]
            if p == q:
                continue
            u = nid(p)
            v = nid(q)
            key = (u, v) if u < v else (v, u)
            e = edge_index.get(key)
            if e is None:
                edge_index[key] = len(eu)
                eu.append(u)
                ev.append(v)
                edelta.append(list(dv))
                edim.append(list(dm))
            else:
                de = edelta[e]
                em = edim[e]
                if eu[e] == u:
                    for t in range(nv):
                        de[t] += dv[t]
                else:
                    for t in range(nv):     # 方向相反 —— Label.flip 等价于取反
                        de[t] -= dv[t]
                for t in range(nv):
                    if dm[t] > em[t]:
                        em[t] = dm[t]

    node_pts = [None] * len(node_id)
    for p, i in node_id.items():
        node_pts[i] = p
    # 每个节点的 z = 它收到的那几条贡献的**平均**(NaN 不参与);
    # 一条都没收到就是 NaN,交给 _overlay_ops._ElevationGrid 兜底。
    # 这里正是 JTS ``LineIntersector.computeZ`` 那句 "average of the two
    # interpolated z values" 的落点 —— 只不过本库是在节点上汇总,
    # 所以"两条线段"自然推广成"所有经过它的源线段"。
    node_z = [_NAN] * len(node_pts)
    for u, vals in zacc.items():
        node_z[u] = sum(vals) / len(vals)
    return node_pts, eu, ev, edelta, edim, node_z


# ----------------------------------------------------------------------------
# 平面图 + 面深度
# ----------------------------------------------------------------------------
class _PlanarGraph:
    """有向边 - 面 的关联结构。

    * **半边**:``2*e`` 是 ``eu[e] -> ev[e]``,``2*e+1`` 是反向。``h ^ 1`` 就是
      反向半边(这是本项目里 ``^1`` 的第一次正经用途:它比 ``+1/-1`` 判断少一次
      分支,而且天然满足对合)。
    * **角度序**:每个节点的出边按极角升序排。节点化之后同一个节点不会有两条
      **同向**出边(较短的会被较长的截断),所以顺序是良定义的。
    * **面遍历**:``next(h) = rev(h) 在该节点的角度序里的顺时针前任``。
      实测这条规则走出来的面,**面在左**,有界面是逆时针(带符号面积为正)、
      无界面是顺时针 —— 这与 Esri 的绕向约定恰好一致(见模块 docstring)。

    ``edelta[e]`` / ``hdelta[h]`` 都是**逐输入的向量**(``list[int]``),
    长度 = 输入个数。``hdelta`` 里正向那一条**别名** ``edelta[e]`` 本身
    (不复制),所以**谁都不许就地改 delta** —— 取反一律新建列表。
    """

    def __init__(self, node_pts, eu, ev, edelta) -> None:
        self.pts = node_pts
        self.n = len(node_pts)
        self.ne = len(eu)
        self.eu = eu
        self.ev = ev
        self.edelta = edelta
        self.nv = len(edelta[0]) if edelta else 0

        m = 2 * self.ne
        origin = [0] * m
        target = [0] * m
        hdelta = [None] * m
        hang = [0.0] * m
        for e in range(self.ne):
            u = eu[e]
            v = ev[e]
            h = 2 * e
            origin[h] = u
            target[h] = v
            origin[h + 1] = v
            target[h + 1] = u
            d = edelta[e]
            hdelta[h] = d
            hdelta[h + 1] = [-x for x in d]
            pu = node_pts[u]
            pv = node_pts[v]
            a = math.atan2(pv[1] - pu[1], pv[0] - pu[0])
            hang[h] = a
            hang[h + 1] = a - math.pi if a > 0.0 else a + math.pi
        self.origin = origin
        self.target = target
        self.hdelta = hdelta
        self.hang = hang

        out: List[List[int]] = [[] for _ in range(self.n)]
        for h in range(m):
            out[origin[h]].append(h)
        for u in range(self.n):
            out[u].sort(key=hang.__getitem__)
        self.out = out
        pos = [0] * m
        for u in range(self.n):
            for k, h in enumerate(out[u]):
                pos[h] = k
        self.hpos = pos

    def walks(self):
        """面遍历。:returns: ``(face_of, faces)``。

        ``face_of[h]`` = 半边 h **左侧**的面号(每条半边恰好属于一个面);
        ``faces[f]`` 是该面走一圈得到的半边序列。
        """
        m = 2 * self.ne
        face_of = [-1] * m
        faces: List[List[int]] = []
        out = self.out
        hpos = self.hpos
        target = self.target
        for h0 in range(m):
            if face_of[h0] >= 0:
                continue
            fid = len(faces)
            walk = []
            h = h0
            while face_of[h] < 0:
                face_of[h] = fid
                walk.append(h)
                u = target[h]
                k = len(out[u])
                h = out[u][(hpos[h ^ 1] - 1) % k]
            faces.append(walk)
        return face_of, faces


def _ray_depth(q, ang: float, node_pts, eu, ev, edelta) -> List[int]:
    """从 ``q`` 沿方位角 ``ang`` 打一条射线,累加穿过的边的 depthDelta **向量**。

    这就是"面深度 = 从无穷远走到该处所穿过的所有边的 depthDelta 之和"的
    直接实现 —— 也是 JTS ``SubgraphDepthLocater.getDepth`` 想算的那个量,
    只是那边用"最右点向左打射线"来定位、这里用面遍历来定位。返回长度 =
    输入个数的 ``list[int]``(空图返回 ``[]``)。

    **调用方必须保证射线不穿过任何顶点**(见 :func:`_pick_ray`),于是:

    * ``t > 0`` 与 ``0 < u < 1`` 全是**严格**比较,不需要任何容差;
    * 射线与某条边**共线**的情况不可能出现(那要求边的两个端点都落在射线的
      直线上,而这两个方向都已被 :func:`_pick_ray` 排除)。
    """
    nv = len(edelta[0]) if edelta else 0
    dx = math.cos(ang)
    dy = math.sin(ang)
    qx, qy = q
    acc = [0] * nv
    for e in range(len(eu)):
        pa = node_pts[eu[e]]
        pb = node_pts[ev[e]]
        ex = pb[0] - pa[0]
        ey = pb[1] - pa[1]
        den = dx * ey - dy * ex
        if den == 0.0:
            continue
        fx = pa[0] - qx
        fy = pa[1] - qy
        t = (fx * ey - fy * ex) / den
        if t <= 0.0:
            continue
        u = (fx * dy - fy * dx) / den
        if u <= 0.0 or u >= 1.0:
            continue
        # 沿射线往前走跨到哪一侧:den < 0 表示射线方向在边方向的左侧(叉积变号)
        d = edelta[e]
        if den < 0.0:
            for t2 in range(nv):
                acc[t2] -= d[t2]
        else:
            for t2 in range(nv):
                acc[t2] += d[t2]
    return acc


def _pick_ray(q, lo: float, span: float, node_pts) -> float:
    """在开扇区 ``(lo, lo + span)`` 里挑一个**不指向任何顶点**的方位角。

    这是本模块替代 JTS "最右点 + ε 偏移" 的关键一步:与其把射线挪一个 ε 再看
    运气,不如把"哪些方向会出事"全部枚举出来(指向任一顶点的方向,及其反向),
    然后在剩下的空隙里取中点。**空隙一定存在**(有限个方向不可能填满一段区间),
    而且离两端至少有一半间距,浮点舍入吃不掉它。
    """
    forbidden: List[float] = []
    qx, qy = q
    for p in node_pts:
        if p == q:
            continue
        a = math.atan2(p[1] - qy, p[0] - qx)
        for b in (a, a + math.pi):
            c = math.fmod(b - lo, _PI_2)
            if c < 0.0:
                c += _PI_2
            if 0.0 < c < span:
                forbidden.append(c)
    forbidden.sort()
    best = span / 2.0
    best_gap = -1.0
    prev = 0.0
    for c in forbidden:
        gap = c - prev
        if gap > best_gap:
            best_gap = gap
            best = (prev + c) / 2.0
        prev = c
    gap = span - prev
    if gap > best_gap:
        best = (prev + span) / 2.0
    return lo + best


def _components(n: int, eu, ev) -> List[int]:
    """并查集求节点连通分量(无向)。"""
    parent = list(range(n))

    def find(x: int) -> int:
        r = x
        while parent[r] != r:
            r = parent[r]
        while parent[x] != r:
            parent[x], x = r, parent[x]
        return r

    for e in range(len(eu)):
        a = find(eu[e])
        b = find(ev[e])
        if a != b:
            parent[a] = b
    return [find(i) for i in range(n)]


def _face_depths(g: _PlanarGraph, face_of, faces) -> List[List[int]]:
    """每个面的深度 —— 逐输入的**向量**(``list[int]``,长度 = 输入个数)。

    每个**连通分量**先用一次精确射线投射定它的基准面深度,再在对偶图上 BFS
    传播 ``depth(左面) = depth(右面) + depthDelta``。跨分量的基准深度由射线
    投射**直接给出**(射线会穿过别的分量的边并累加),所以分量之间不需要排序、
    也不需要知道"谁在谁里面"。

    传播中若出现"同一个面被赋了两个不同的深度",说明图本身自相矛盾(典型是
    delta ≠ 0 的桥边)—— 抛 :class:`PlanarTopologyError`,与 JTS
    ``DirectedEdge.setDepth`` 抛 ``TopologyException`` 同一个语义。

    ⚠️ 深度**只累计属于某输入的边**(那个分量非零的边)—— 线输入的边 delta
    恒为 0,所以"线在不在这个面里"根本不影响这个面在**面输入**上的深度。
    """
    m = 2 * g.ne
    nv = g.nv
    adj: List[List[Tuple[int, List[int]]]] = [[] for _ in range(len(faces))]
    for h in range(m):
        left = face_of[h]
        right = face_of[h ^ 1]
        d = g.hdelta[h]
        adj[left].append((right, [-x for x in d]))   # depth[right] = depth[left] - d
        adj[right].append((left, d))                 # depth[left]  = depth[right] + d

    comp = _components(g.n, g.eu, g.ev)
    by_comp: Dict[int, List[int]] = {}
    for f, walk in enumerate(faces):
        by_comp.setdefault(comp[g.origin[walk[0]]], []).append(f)

    depth: List[Optional[List[int]]] = [None] * len(faces)
    for fs in by_comp.values():
        f0 = fs[0]
        h = faces[f0][0]
        u = g.origin[h]
        lo = g.hang[h]
        k = len(g.out[u])
        if k == 1:
            span = _PI_2                 # 悬边:整个圆都是这个面
        else:
            span = (g.hang[g.out[u][(g.hpos[h] + 1) % k]] - lo) % _PI_2
            if span == 0.0:
                span = _PI_2
        ang = _pick_ray(g.pts[u], lo, span, g.pts)
        depth[f0] = _ray_depth(g.pts[u], ang, g.pts, g.eu, g.ev, g.edelta)

        stack = [f0]
        while stack:
            f = stack.pop()
            df = depth[f]
            for gf, delta in adj[f]:
                nd = [df[t] + delta[t] for t in range(nv)]
                if depth[gf] is None:
                    depth[gf] = nd
                    stack.append(gf)
                elif depth[gf] != nd:
                    raise PlanarTopologyError(
                        '平面图的面深度自相矛盾(第 %d 面 %r vs %r)'
                        % (gf, depth[gf], nd))
    zero = [0] * nv
    return [zero if d is None else d for d in depth]


# ----------------------------------------------------------------------------
# 结果边 -> 环
# ----------------------------------------------------------------------------
def _split_repeated(ring: List[int]) -> List[List[int]]:
    """把一个自接触(夹点)的环拆成**最小环** —— ``PolygonBuilder.buildMinimalRings``。

    JTS 那边是两步走:``MaximalEdgeRing.linkResultAreaMaxRingAtNode`` 先把结果边
    在一个节点上按角度序**最大地**串起来(所以夹点处的两个瓣会被串进同一个环),
    ``PolygonBuilder.buildMinimalRings`` 再拆成最小环。本库的走环规则
    (:func:`_result_rings`)走出来的正是那个**最大环**,所以这里补上第二步。

    ⚠️ **不拆的后果是实打实的错**,不是洁癖:只共一个点的两方块做 ``union``,
    最大环是 ``POLYGON ((0 10, 10 10, 10 20, 20 20, 20 10, 10 10, 10 0, 0 0, 0 10))``
    —— 一个**重复了 ``(10 10)``** 的自接触环,面积虽然对,拓扑是错的;
    GEOS 给的是 ``MULTIPOLYGON``(两个壳)。这条是 ``DESIGN.md`` §6.2 点名的"牙齿"。

    算法:按走环顺序过一遍顶点,**栈 + 顶点->栈位**表。碰到一个已经在栈里的顶点,
    说明从上次出现到现在走完了一个内嵌的瓣 —— 把它切出来(含夹点本身,不含重复的
    那一个),把栈回退到夹点处继续。走完之后栈里剩的就是最外层那一圈。
    两瓣的例:``[10,20,21,22,23,10,0,1,2]`` → ``[[10,20,21,22,23], [10,0,1,2]]``。

    切出来的子环是**开环**(不重复首点,首尾由调用方隐含闭合),与
    ``ring_signed_area2`` 的口径一致 —— 它靠"平移到首点"消掉闭合项。

    顺带一个好处:如果某个瓣其实该是**洞**(绕向相反),切出来之后它的有向面积
    为负,``_assemble`` 自然把它当内环交给 ``organize_polygons`` —— 拆分不会
    把洞的特性弄丢。
    """
    out: List[List[int]] = []
    stack: List[int] = []
    pos_of: dict = {}
    for i in ring:
        j = pos_of.get(i)
        if j is not None:
            loop = stack[j:]
            for k in loop[1:]:
                del pos_of[k]
            del stack[j + 1:]
            if len(loop) >= 3:
                out.append(loop)
        else:
            pos_of[i] = len(stack)
            stack.append(i)
    if len(stack) >= 3:
        out.append(stack)
    return out


def _result_rings(g: _PlanarGraph, face_of, depth, is_result) -> List[List[int]]:
    """留结果边并走成环(``BufferSubgraph.findResultEdges`` + 有向边成环)。

    ``is_result(depth_left, depth_right)`` **由调用方给** —— 它拿到一条边**两侧**
    面的深度向量,回一个布尔。留半边 ``h`` 的判据是"``h`` 左侧是结果面、右侧
    不是",于是每条结果边的两个方向里恰好留一个。

    * buffer 的判据是"内部在右侧"的相反方向(``dl[0] >= 1 and dr[0] <= 0``);
      本模块统一用"内部在左",因为"内部在左"走出来的环是**数学正向**,而本库
      (和 Esri、和 GEOS)要的是外环顺时针 —— 最后统一翻一次,比在两条不同的
      朝向约定之间来回切换可靠。
    * overlay 的判据是"两侧各自套一遍四个算子的结果面谓词"(见
      ``_overlay_ops``)。

    走法还是那条规则:下一条 = ``rev(h)`` 在角度序里的顺时针前任,**但只在结果
    边里找**。这正是"沿区域边界走一圈"的标准做法,面上自接触(夹点)时两个瓣
    会被串进**同一个**最大环 —— 出口前用 :func:`_split_repeated` 拆成最小环。
    """
    sel = []
    for h in range(2 * g.ne):
        if is_result(depth[face_of[h]], depth[face_of[h ^ 1]]):
            sel.append(h)
    if not sel:
        return []
    sel_set = set(sel)

    prev_s: List[Optional[List[Optional[int]]]] = [None] * g.n
    for u in range(g.n):
        out = g.out[u]
        k = len(out)
        sel_at = [idx for idx in range(k) if out[idx] in sel_set]
        if not sel_at:
            continue
        # ``arr[j]`` = "``out[j]`` 在角度序里的**顺时针前任结果边**"。角度序按
        # 逆时针排 ⇒ 顺时针就是下标递减 ⇒ 取 j 之前最近的那条结果边(越过 0 绕回
        # 下标最大的那条)。这与 `walks()` 的 ``out[u][(hpos[h^1] - 1) % k]`` 同向。
        #
        # ⚠️ 方向**必须**是"顺时针前任",不能反过来取"逆时针后继"。判据
        # ``is_area_edge`` 留的是"结果面在**左**"的半边;在"两个结果瓣只共一个
        # 节点"的节点上两条半边都留,两个方向给出**不同**的配对:
        #   * 逆时针后继 = 贴外圈走 —— 会把两个瓣串成"外圈当外壳、另一个面当洞"
        #     的环。面积照样对,几何却**非法**(洞的边界在 2 个点上碰到外壳,
        #     GEOS 判 ``Interior is disconnected``)。共边重叠的两方块做
        #     ``symmetric_difference`` 就是这么踩的。
        #   * 顺时针前任 = 每个结果面各走各的边界 ⇒ 环天然是简单环。
        arr: List[Optional[int]] = [None] * k
        cur = sel_at[-1]
        for j in range(k):
            arr[j] = cur
            if out[j] in sel_set:
                cur = j
        prev_s[u] = arr

    rings: List[List[int]] = []
    seen = set()
    for h0 in sel:
        if h0 in seen:
            continue
        ring: List[int] = []
        h = h0
        while h not in seen:
            seen.add(h)
            ring.append(g.origin[h])
            u = g.target[h]
            arr = prev_s[u]
            if arr is None:
                break
            h = g.out[u][arr[g.hpos[h ^ 1]]]
        if len(ring) >= 3:
            # 自接触(夹点)的环在这里拆成最小环 —— 见 _split_repeated。
            # 长度不足 3 的碎片直接丢(围不出面,也不可能是合法的线结果,
            # 线结果走的是另一路)。
            for piece in _split_repeated(ring):
                rings.append(piece)
    return rings


# ----------------------------------------------------------------------------
# 成环 -> esri parts(PolygonBuilder + 绕向)
# ----------------------------------------------------------------------------
def _keep_largest_group(groups, rings, areas) -> list:
    """``BufferBuilder.keepLargestArea`` —— 单输入却出多个多边形时只留最大的。

    这是**伪影启发式**,不是几何运算:``buffer(1)`` 作用在单个几何上,理论上
    只该产出一个多边形;真出多个,多半是输入自交导致曲线翻面产生了碎块。GEOS
    的做法是直接扔掉小的。
    """
    best = 0
    best_area = -1.0
    for gi, (si, _holes) in enumerate(groups):
        a = abs(areas[si])
        if a > best_area:
            best_area = a
            best = gi
    return [groups[best]]


def _assemble(rings: List[List[int]], pts, n_geoms: int, distance: float,
              node_z=None):
    """环 -> ``(parts, shells, zs)``,Esri 绕向(外环顺时针)。

    ``node_z`` 给了的话(只能是长度 = 节点数的列表),同时按**逐位相同**的
    顶点顺序产出 z 数组 —— 关键是它要跟着 ``reversed(coords)`` 一起翻,
    否则 z 与顶点错位,而且错得**静默**。没给时第三项是 ``None``,调用方
    完全不用管这一档(``_buffer_ops`` 就是)。

    ⚠️ 不用 ``_geometry_ops.organize_polygons`` 的**绕向兜底**:那条兜底
    (``ring_signed_area2 < 0`` = 外环)是给"Esri 流式顺序"准备的,而这里的环是
    按面遍历顺序出来的,没有那个保证。所以外壳/内环**由本函数显式判定并显式
    传给** ``organize_polygons``。一旦 ``shells`` 给全了,它就只做"洞归给最小的
    包含它的壳"这一件正确的事,两种用法互为补充而不是重叠。
    """
    polys: List[Tuple[List[int], float, bool]] = []
    for r in rings:
        if len(r) < 3:
            continue
        coords = [pts[i] for i in r]
        arr = array('d', chain.from_iterable(coords))
        a2 = ring_signed_area2(arr)
        if a2 == 0.0:
            continue                     # 零面积环围不出面
        polys.append((r, a2, a2 > 0.0))
    if not polys:
        return None

    parts = [array('d', chain.from_iterable(pts[i] for i in r)) for r, _a, _s in polys]
    shells = [s for _r, _a, s in polys]
    groups = organize_polygons(parts, shells)

    if distance > 0.0 and n_geoms == 1 and len(groups) > 1:
        groups = _keep_largest_group(groups, polys, [a for _r, a, _s in polys])

    out_parts = []
    out_shells: List[bool] = []
    out_zs: Optional[List[Any]] = [] if node_z is not None else None
    for si, holes in groups:
        for idx, is_shell in [(si, True)] + [(hj, False) for hj in holes]:
            # 翻成 Esri 绕向:数学正向(逆时针)的外环变成顺时针,洞反之
            ring = polys[idx][0]
            coords = [pts[i] for i in ring]
            out_parts.append(array('d', chain.from_iterable(reversed(coords))))
            out_shells.append(is_shell)
            if out_zs is not None:
                # ⚠️ 与上面那一行**同一个** reversed(ring) —— 两处一旦走偏,
                #    z 就与顶点错位,而且面积/长度这些断言全都看不出来。
                out_zs.append(array('d', (node_z[i] for i in reversed(ring))))
    if not out_parts:
        return None
    return out_parts, out_shells, out_zs
