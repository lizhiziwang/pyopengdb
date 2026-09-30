"""overlay:`difference` / `union` / `intersection` / `symmetric_difference`。

⚠️ **这一档和谓词、``buffer`` 是同一个毛病:GDAL 那边没有 C++ 可抄。**
``OGRGeometry::Difference`` / ``Union`` / ``Intersection`` /
``SymmetricDifference``(``ogrgeometry.cpp``)在 GDAL 里**一律是一句转发给
GEOS**,GDAL 自己一行算法都没有。所以本模块的上游是 **JTS 的
``operation/overlayng/```**(GEOS 3.9 起 ``Geometry::difference`` 走的就是它;
3.12 起老的 ``operation/overlay/OverlayOp`` 已被删除)。下面每一处都标了对应的
JTS 类与方法,**没有标的一律是本库自己的取舍**。

本模块**只算,不构造** ``Geometry``(与 :mod:`._buffer_ops` 同款):返回
``(result_kind, parts, shells, lines, points)``,装配交给 ``geometry.py`` ——
那边要走 flat 形式并**显式赋 ``_shells``**(``Geometry._like()`` 不搬
``_shells``,这是本项目反复踩到的坑)。

管线(与 buffer 同构,共用 :mod:`._planar`)
------------------------------------------
::

    两个输入的边界 -> _planar._noded_edges      EdgeNodingBuilder + MCIndexNoder
                                            + IntersectionAdder + EdgeMerger
                    -> _planar._PlanarGraph     OverlayGraph
                    -> _planar._face_depths     OverlayLabeller(本库换成面级 BFS)
                    -> 结果面判据 + _planar._result_rings
                                                markInResultArea + PolygonBuilder
                    -> 线结果判据                LineBuilder
                    -> 点结果判据                IntersectionPointBuilder
                    -> 装配(面 -> 线 -> 点)

输入 A、B 各占 delta 向量的一个分量:一条曲线带的是
``(pts, (A 的那一项, B 的那一项))``,编码见 ``_planar._label_vectors``。

⚠️ **"线不切面"是设计要害。** 某个输入的"线"对那个分量的 delta 贡献恒为
``0`` —— 线不是面的边界。这不是细节:实测 ``POLYGON ∖ 穿过它的线`` 面积**原样
不变**(只是边界上多出两个节点顶点)、``POLYGON ∪ 同一条线`` 是
``GEOMETRYCOLLECTION(面, 线左残段, 线右残段)``。若把两条输入的边一视同仁当
屏障,线会把面切成两半,``difference`` 就给出两个碎多边形。

为什么敢不抄 JTS 的边级标签传播
--------------------------------
JTS ``OverlayLabeller`` 在**边**上记左右位置,靠 ``propagateAreaLocations``
绕节点星形传播、再对孤立的边用"两端点各做一次 PIP"(``labelDisconnectedEdges``
→ ``locateEdgeBothEnds``)兜底。本库改成**面级**:一次面遍历拿到所有面,每个面
一个 ``(depA, depB)``,``dep > 0`` 即内部;某条边"在不在 A 里"就是"它两侧的面
在不在 A 里"。理由与 ``DESIGN.md`` §2.23 换掉 ``SubgraphDepthLocater`` 的那条
相同:面遍历是精确的,没有 ε、没有"传播顺序"的隐含假设,而且本库已经有一套
对拍过的实现。两套做法在合法输入上语义等价(JTS 每条机制 ↔ 本库对应物见
``DESIGN.md`` §2.24 的对照表),**这条差异要显式记录,别让后人当成 bug 修**。

本库**不做**的(诚实边界,详见 ``DESIGN.md`` §2.24)
--------------------------------------------------
1. **不做 snap-rounding。** ``OverlayNGRobust`` 的降级梯:固定精度 → 浮点直算
   → ``SnappingNoder`` 5 档递增容差 → ``SnapRoundingNoder``。本库照 buffer 的
   老口径,**第一档照抄、第三档降级为放弃** —— 只有靠 snap-rounding 才救得回来
   的退化构型,GEOS 给结果而本库给空。
2. **不承诺与 GEOS 逐位相同。** 交点用普通双精度。
3. **不做 ``side location conflict`` 检测。** JTS ``propagateAreaLocations``
   发现同一侧位置自相矛盾时抛 ``TopologyException``;本库不抛,非法输入静默
   给一个数。
4. **不做塌缩(``DIM_COLLAPSE``)检测。** JTS ``OverlayLabel`` 的
   ``isBoundaryCollapse`` / ``isInteriorCollapse`` / ``isCollapseAndNotPartInterior``
   三条闸门都以"两个及以上线段端点重合"为前提,本库的节点化不做这种识别
   (在合法输入上这种边不出现),所以那三条**在本库恒为假** —— 代码里留了
   位置和说明,不是漏了。
5. **不用 numpy**(图遍历是随机访存),**不加 C 扩展入口**。
"""
from __future__ import annotations

from array import array
from itertools import chain
from typing import Any, List, Optional, Sequence, Tuple

from ._geometry_ops import (
    EXTERIOR,
    INTERIOR,
    dimension_of,
    envelope_of,
    envelope_intersects,
    organize_polygons,
    point_location,
)
from ._planar import (
    DIM_BOUNDARY,
    DIM_LINE,
    DIM_NOT_PART,
    _NAN,
    _PlanarGraph,
    _assemble,
    _face_depths,
    _line_pts_and_z,
    _noded_edges,
    _result_rings,
    _ring_pts_and_z,
)

#: 四个算子。**取值与 JTS ``OverlayNG`` 逐字相同** —— 顺序也是。
#: ⚠️ 不要重排:``test_overlay.py`` 有一批守卫靠"四个算子必须给出四个不同答案"
#: 来抓"op 抄错一格",而 ``OverlayNG.isResultOfOp`` 的形参顺序是
#: ``(opCode, loc0, loc1)``,loc0 恒为**左操作数**。
INTERSECTION = 0
UNION = 1
DIFFERENCE = 2
SYMDIFFERENCE = 3

#: 算子 -> 中文名,只用于报错信息。
_OP_NAME = {
    INTERSECTION: 'intersection',
    UNION: 'union',
    DIFFERENCE: 'difference',
    SYMDIFFERENCE: 'symmetric_difference',
}

#: 环至少要有这么多点(闭合后)才可能是合法的面边界。
_MIN_RING = 4


def is_result_of_op(op: int, loc0: int, loc1: int) -> bool:
    """``OverlayNG.isResultOfOp`` —— 四个算子只差这一张表。

    JTS 那边先把 ``BOUNDARY`` 归一到 ``INTERIOR`` 再比;本库的位置只有
    ``INTERIOR`` / ``EXTERIOR`` 两种取值,所以没有那一步(面级定位天然给出
    "在内部 / 不在内部",不产生"在边界上"这种位置)。
    """
    if op == INTERSECTION:
        return loc0 == INTERIOR and loc1 == INTERIOR
    if op == UNION:
        return loc0 == INTERIOR or loc1 == INTERIOR
    if op == DIFFERENCE:
        return loc0 == INTERIOR and loc1 != INTERIOR
    # SYMDIFFERENCE
    return (loc0 == INTERIOR) != (loc1 == INTERIOR)


def _kind_dim(kind: str) -> int:
    """输入的拓扑维(0/1/2)。"""
    return dimension_of(kind)


def _empty_result(op: int, dim_a: int, dim_b: int):
    """空结果的**定型**(``OverlayUtil.resultDimension`` + ``createEmptyResult``)。

    规则:``intersection`` 取两维的**小**者、``union`` / ``symmetric_difference``
    取**大**者、``difference`` 取**左操作数**的维;``0`` → ``POINT EMPTY``、
    ``1`` → ``LINESTRING EMPTY``、``2`` → ``POLYGON EMPTY``。

    ⚠️ 实测确认过这条必须按维度定型:相离两条线的 ``intersection`` 是
    ``LINESTRING EMPTY``、``A ⊂ B`` 的 ``difference`` 是 ``POLYGON EMPTY``、
    相离两点的 ``intersection`` 是 ``POINT EMPTY``。**一律返回空面是错的。**
    """
    if op == INTERSECTION:
        dim = min(dim_a, dim_b)
    elif op == DIFFERENCE:
        dim = dim_a
    else:
        dim = max(dim_a, dim_b)
    kind = 'point' if dim <= 0 else ('polyline' if dim == 1 else 'polygon')
    # 空结果**不带 Z** —— 实测 GEOS 对两个 3D 输入的 `A ⊂ B` 交集给的就是
    # 光秃秃的 ``POLYGON EMPTY``,没有 Z 标记。
    return kind, [], None, None, None, None


def _is_empty_result(op: int, env_a, env_b) -> bool:
    """``OverlayUtil.isEmptyResult`` —— 只做能一眼定死的早退。

    ⚠️ 这里的判据必须是**包围盒相离**而不是"谓词说不相交":它只是省一次
    节点化,判错了会静默给出错结果。所以 ``INTERSECTION`` 只判包围盒,
    ``DIFFERENCE`` 只判 A 空,``UNION`` / ``SYMDIFFERENCE`` 只判两边都空 ——
    与 JTS 逐条对应。
    """
    if op == INTERSECTION:
        return not envelope_intersects(env_a, env_b)
    if op == DIFFERENCE:
        return env_a is None
    return env_a is None and env_b is None


def _envelope_of_parts(parts) -> Optional[Tuple[float, float, float, float]]:
    if not parts:
        return None
    return envelope_of(parts)


# ----------------------------------------------------------------------------
# 面输入 -> 带标签的曲线
# ----------------------------------------------------------------------------
def _ring_entry(is_shell: bool, ccw: bool) -> Tuple[int, int]:
    """一个环的标签项 ``(左位置, 右位置)``。

    ⚠️ 与 ``_buffer_ops._CurveSet`` 的 ``(cw_left_loc, cw_right_loc)`` + CCW
    翻转**逐字同款** —— 这里刻意复用同一套定号,否则 buffer 与 overlay 出来的
    面深度符号会不一致,而两边的成环代码是同一份。

    口径:**壳顺时针、洞逆时针** 是本库(和 Esri)的存储绕向。顺时针环的
    内部在**右**(``BufferBuilder.depthDelta`` 的约定),逆时针则左右互换。
    """
    if is_shell:
        left, right = EXTERIOR, INTERIOR      # 顺时针壳:内部在右
    else:
        left, right = INTERIOR, EXTERIOR      # 顺时针洞:内部在左
    if ccw:
        left, right = right, left
    return (left, right)


def _polygon_curves(parts, shells, index: int, n_in: int, zs=None) -> List[Any]:
    """把一个**面**输入摊成 ``(pts, label[, z])`` 表,标签里只有第 ``index`` 项非 None。

    壳/洞的归属走 ``organize_polygons``(与 buffer 同一入口;``shells`` 给全了
    它只做"洞归给最小的包含它的壳",给不出时按绕向兜底)。

    ``zs`` 给了(该输入带 Z)才出三元组 —— 不给就出二元组,下游
    :func:`_planar._noded_edges` 两种都吃。
    """
    out: List[Any] = []
    for si, holes in organize_polygons(parts, shells):
        for idx, is_shell in [(si, True)] + [(h, False) for h in holes]:
            # ⚠️ 用 _planar._ring_pts_and_z 而不是 _close_ring(_clean_ring(...)):
            #    后两者会删点,删完下标就对不上 z 了。前者把"保留哪些下标"
            #    当唯一事实来源,xy 与 z 一起走。
            pts, z = _ring_pts_and_z(parts[idx], None if zs is None else zs[idx])
            if len(pts) < _MIN_RING:
                continue
            entry = _ring_entry(is_shell, _is_ccw(pts))
            label = [None] * n_in
            label[index] = entry
            out.append((pts, tuple(label), z) if zs is not None
                       else (pts, tuple(label)))
    return out


def _is_ccw(pts) -> bool:
    """闭合环是否逆时针(与 ``_buffer_ops._is_ccw_area`` 同一个判据)。"""
    a2 = 0.0
    for i in range(len(pts) - 1):
        x0, y0 = pts[i]
        x1, y1 = pts[i + 1]
        a2 += x0 * y1 - x1 * y0
    return a2 > 0.0


def _line_curves(parts, index: int, n_in: int, zs=None) -> List[Any]:
    """把一个**折线**输入摊成 ``(pts, label[, z])`` 表,**每条 part 一条曲线**。

    与 :func:`_polygon_curves` 只差两处,两处都是要害:

    * 走 ``_line_pts_and_z`` 而不是 ``_ring_pts_and_z`` —— 折线不补闭合点、
      不削尾点(见 :func:`_planar._pts_and_z`);
    * 标签项是 ``(INTERIOR,)`` 这个**一元组**,它让
      ``_planar._label_vectors`` 给出 ``delta == 0`` / ``DIM_LINE``。

    ⚠️ ``delta == 0`` **是设计要害,不是省事。** 线不是面的边界,穿过它不
    改变任何分量的深度 —— 否则"横穿面的线"会把面切成两半,``difference``
    给出两个碎多边形,而 GEOS 给的是面积原样不变的单个多边形。

    塌缩的 part(顶点少于 2 个)直接跳过,对应 JTS ``EdgeNodingBuilder.createEdges``
    里的 ``Edge.isCollapsed``。跳过之后那个输入仍然可能通过别的 part 参与,
    JTS 也只在这个输入的**全部** part 都塌缩时才把 ``hasEdges[i]`` 置假。
    """
    out: List[Any] = []
    for i, part in enumerate(parts):
        pts, z = _line_pts_and_z(part, None if zs is None else zs[i])
        if len(pts) < 2:
            continue
        label = [None] * n_in
        label[index] = (INTERIOR,)
        out.append((pts, tuple(label), z) if zs is not None
                   else (pts, tuple(label)))
    return out


def _input_curves(kind, parts, shells, index: int, zs=None) -> List[Any]:
    """一个非点输入 -> 带标签的曲线表(面走环、折线走零件)。"""
    if kind == 'polygon':
        return _polygon_curves(parts, shells, index, 2, zs)
    return _line_curves(parts, index, 2, zs)


# ----------------------------------------------------------------------------
# Z —— JTS ``ElevationModel``
# ----------------------------------------------------------------------------
class _ElevationGrid:
    """``ElevationModel`` —— 给结果里**没有 z 来源**的顶点补一个 z。

    为什么需要它:节点化只给"插值得出 z"的顶点 z(见
    :func:`_planar._add_node_z`)。一个 2D 输入的角点落在 3D 输入的外面时,
    它没有任何 z 来源 —— 而 GEOS 照样给结果标 Z 并给它一个数。那个数就是
    从这里来的。

    规则(逐条照抄 ``ElevationModel`` 的类 docstring 与 ``init``):

    * 网格覆盖**两个输入**包围盒之并,默认 3×3;某方向没有跨度
      (``cellSize <= 0``)时那个方向退化成 **1 格**(注意 ``cellSize`` 本身
      不跟着改,但因为格数是 1,后面根本不会走除法);
    * 每格的 z = 落在格内的**输入顶点** z 的平均(``ElevationCell.compute``);
      输入顶点**按输入几何的坐标序列**遍历,即**多边形环的首点会被数两次**
      (``LinearRing`` 的序列含闭合点)—— 这不是抄错,权重会影响格平均;
    * 没有任何输入顶点带 z → 整个模型不作数(``hasZValue`` 假,``populateZ``
      直接返回);
    * 定位用 ``int((v - min) / cellSize)`` 再夹到 ``[0, num-1]``,所以**格外的
      点落到最近的格**;
    * 某格一个顶点都没落进去 → 回落到**所有非空格子的平均**
      (``ElevationModel.init`` 的 ``averageZ``;GEOS ``getZ`` 那句
      ``if (cell.isNull()) return averageZ;``)。

    与 GEOS 的一处**已知差异**(不是本类的锅,记在这里免得后人当 bug 修)
    -------------------------------------------------------------------
    实测:两两相离的"3D 面 ∪ 2D 面",GEOS 给 2D 那半的顶点 z 是 **NaN**,
    而本库会补出一个数(``averageZ``)。原因是 GEOS 在
    ``HeuristicOverlay`` 里对 UNION / SYMDIFFERENCE 有一条**短路**
    (``isCombinable``:两个输入**包围盒相离**且各自只有一个非空元素 →
    ``combineReduced`` 直接把两边的元素拼起来,**根本不跑 overlay 引擎**),
    于是 ``ElevationModel`` 压根没参与。本库不走这条短路 —— 一律过完整引擎
    (短路只在完全没有交互时触发,几何结果一样,差别只落在"2D 分量那个顶点
    的 z 是补出来的数还是 NaN"上,以及顶点顺序/合并与否)。这条写进
    ``DESIGN.md`` §2.24。
    """

    __slots__ = ('extent', 'num_x', 'num_y', 'size_x', 'size_y', 'cells',
                 'has_z', '_avg')

    def __init__(self, extent, num_cell: int = 3) -> None:
        self.extent = extent                  # (minx, miny, maxx, maxy)
        self.num_x = num_cell
        self.num_y = num_cell
        self.size_x = (extent[2] - extent[0]) / num_cell
        self.size_y = (extent[3] - extent[1]) / num_cell
        if self.size_x <= 0.0:
            self.num_x = 1
        if self.size_y <= 0.0:
            self.num_y = 1
        self.cells = {}                       # (ix, iy) -> [sum, n]
        self.has_z = False
        self._avg = None                      # 惰性:所有非空格子的平均

    def add_part(self, part, zs, closed: bool) -> None:
        """把一块坐标喂进模型。``closed`` = 这是一条**面环**(首点再数一次)。"""
        n = len(part) // 2
        for k in range(n):
            self._add(part[2 * k], part[2 * k + 1],
                      _NAN if zs is None else zs[k])
        if closed and n:
            self._add(part[0], part[1], _NAN if zs is None else zs[0])

    def _add(self, x: float, y: float, z: float) -> None:
        if z != z:
            return
        self.has_z = True
        key = self._cell(x, y)
        cell = self.cells.get(key)
        if cell is None:
            self.cells[key] = [z, 1]
        else:
            cell[0] += z
            cell[1] += 1

    def _cell(self, x: float, y: float):
        ix = 0
        if self.num_x > 1:
            ix = int((x - self.extent[0]) / self.size_x)
            if ix < 0:
                ix = 0
            elif ix > self.num_x - 1:
                ix = self.num_x - 1
        iy = 0
        if self.num_y > 1:
            iy = int((y - self.extent[1]) / self.size_y)
            if iy < 0:
                iy = 0
            elif iy > self.num_y - 1:
                iy = self.num_y - 1
        return ix, iy

    def get_z(self, x: float, y: float) -> float:
        cell = self.cells.get(self._cell(x, y))
        if cell is None:
            return self.average_z()
        return cell[0] / cell[1]

    def average_z(self) -> float:
        """``ElevationModel.init`` 的 ``averageZ`` —— **非空格子**的平均。"""
        if self._avg is None:
            if self.cells:
                self._avg = sum(c[0] / c[1] for c in self.cells.values()) \
                    / len(self.cells)
            else:
                self._avg = _NAN
        return self._avg

    def populate(self, xy, zs) -> None:
        """就地补 ``zs`` 里的 NaN —— ``ElevationModel.populateZ``。

        ``xy`` 是交错坐标数组、``zs`` 是与之逐点对应的 ``array('d')``;
        只补 NaN,已有的 z 一个都不动。
        """
        if not self.has_z:
            return
        for k in range(len(zs)):
            if zs[k] != zs[k]:
                zs[k] = self.get_z(xy[2 * k], xy[2 * k + 1])


def _input_elevation_grid(kind_a, parts_a, zs_a, kind_b, parts_b, zs_b):
    """把两个**输入**喂进 :class:`_ElevationGrid`(``ElevationModel.create``)。

    没有任何输入带 Z 时返回 ``None`` —— 调用方据此整条 Z 链路关掉。
    """
    if zs_a is None and zs_b is None:
        return None
    env_a = _envelope_of_parts(parts_a)
    env_b = _envelope_of_parts(parts_b)
    if env_a is None:
        extent = env_b
    elif env_b is None:
        extent = env_a
    else:
        extent = (min(env_a[0], env_b[0]), min(env_a[1], env_b[1]),
                  max(env_a[2], env_b[2]), max(env_a[3], env_b[3]))
    if extent is None:
        return None
    grid = _ElevationGrid(extent)
    for kind, parts, zs in ((kind_a, parts_a, zs_a), (kind_b, parts_b, zs_b)):
        closed = (kind == 'polygon')
        for i, part in enumerate(parts):
            grid.add_part(part, None if zs is None else zs[i], closed)
    return grid



# ----------------------------------------------------------------------------
# 线结果判据 —— LineBuilder.isResultLine 的闸门,逐条对照
# ----------------------------------------------------------------------------
def _line_locations(e: int, edim, depth, face_of) -> Tuple[int, int]:
    """一条边对**两个输入**的"线上位置" ``locLine``(``OverlayLabel.getLineLocation``)。

    * 这一项是**边界**或**线** → ``INTERIOR``。前者是 JTS ``initBoundary``
      里写死的 ``aLocLine = INTERIOR``;后者由 ``LineBuilder.effectiveLocation``
      归一成 ``INTERIOR``(它只判"是不是 ``DIM_LINE``/``DIM_COLLAPSE``"不判
      位置,所以这里直接回 ``INTERIOR`` 是等价的)。
    * ``DIM_NOT_PART`` → 该输入对这条边的看法要靠**面级定位**:面输入看这条边
      所在的面在不在它内部(``dep > 0``);线输入没有"内部",一律 ``EXTERIOR``
      —— 这正是 JTS ``labelDisconnectedEdge`` 对非面输入做的
      ``setLocationAll(EXTERIOR)``。

    ⚠️ 这条边**不穿过**某个面输入的边界(它是 ``NOT_PART``),所以它两侧的面在
    那个分量上的深度必然相同,取哪一侧都一样(实现上取左半边那一侧)。
    """
    dims = edim[e]
    dl = depth[face_of[2 * e]]
    return ((INTERIOR if dims[0] != DIM_NOT_PART or dl[0] > 0 else EXTERIOR),
            (INTERIOR if dims[1] != DIM_NOT_PART or dl[1] > 0 else EXTERIOR))


def _boundary_touch(e: int, edim, depth, face_of) -> bool:
    """``OverlayLabel.isBoundaryTouch`` —— 两个面输入**内部在相反两侧**。

    判据:取这条边**任意一侧**的面,恰好有一个输入在那儿是内部。JTS 比的是
    前向边**右侧**、本库比左侧;对一条"两个输入都是边界"的边,两侧的内部性
    对每个输入都恰好相反,所以"同侧与否"在两种取法下等价。

    实测对应:只共一条边的两方块,那条共边上 A 的内部在一侧、E 的内部在另一侧
    → 触;**完全相同**的两个方块,共边的两侧两边同进同出 → 不触(交集就是那个
    面本身,不该再冒出一条线来)。
    """
    if edim[e][0] != DIM_BOUNDARY or edim[e][1] != DIM_BOUNDARY:
        return False
    dl = depth[face_of[2 * e]]
    return (dl[0] > 0) != (dl[1] > 0)


def _is_result_line(op: int, dims, loc_line, has_result_area: bool,
                    area_idx: int, touch: bool) -> bool:
    """``LineBuilder.isResultLine`` —— 闸门的**顺序**也是照抄的,别重排。

    顺序要紧:①那条"边界单例"短路在实测里承担了绝大多数边(面输入上的普通
    边界边)的快速否掉,③之后那几条只在"有多个输入作用在同一条边上"时才可能
    触发。
    """
    d0, d1 = dims

    # ① isBoundarySingleton:只属于一个输入、另一个连边都没有 —— 除非它进了
    #    结果面,否则不算线。实测这就是"面上的普通边界边"那一路。
    if (d0 == DIM_BOUNDARY and d1 == DIM_NOT_PART) or \
       (d1 == DIM_BOUNDARY and d0 == DIM_NOT_PART):
        return False

    # ② isBoundaryCollapse / ③ isInteriorCollapse:都以 DIM_COLLAPSE 为前提,
    #    本库不做塌缩识别(见模块 docstring 第 4 条),恒假。

    if op != INTERSECTION:
        # ④ isCollapseAndNotPartInterior:同 ②/③,恒假。
        #
        # ⑤ **落在面输入内部的线要丢** —— JTS 原话:"If there is a result
        #    area, omit line edge inside it. It is sufficient to check against
        #    the input area rather than the result area, because if line edges
        #    are present then there is only one input area, and the result area
        #    must be the same as the input area."
        #    实测对应:``POLYGON ∪ 完全落在面内部的线`` → **纯 POLYGON**(线被丢)。
        #    ⚠️ 只有非 INTERSECTION 才丢 —— 交集的答案恰恰就是这条线。
        if has_result_area and area_idx >= 0 and loc_line[area_idx] == INTERIOR:
            return False

    # ⑥ 两条**面边界相触**的边(内部在相反两侧):交集要把它收成结果线。
    #    实测对应:只共一条边的两方块,``intersection`` = ``LINESTRING`` 而
    #    不是空面 —— 那条共边没有任何一侧同时是两者的内部,面判据挑不出面来。
    #
    #    ⚠️ 这一条在本库(以及 JTS)都是**冗余**的,但**照抄不删**。理由:
    #    ``OverlayLabel.initBoundary``(已读源码)把 ``aLocLine`` **写死成
    #    INTERIOR** —— 所以"两个输入都是边界"的边在闸门⑦处拿到的就是
    #    ``(INTERIOR, INTERIOR)``,而 ``is_result_of_op(INTERSECTION, I, I)``
    #    本来就是真。换句话说:在 ``op == INTERSECTION`` 下本闸门与⑦**恒等**
    #    (前面几条闸门在 INTERSECTION 下都不会短路掉它)。删掉它测试**也不会
    #    红** —— 变异测试里那一条因此记为"等价、预期存活",不是覆盖缺口。
    if op == INTERSECTION and touch:
        return True

    # ⑦ 最后才套算子自己的布尔逻辑。
    return is_result_of_op(op, loc_line[0], loc_line[1])


# ----------------------------------------------------------------------------
# 主入口
# ----------------------------------------------------------------------------
def _result_dimension(op: int, dim_a: int, dim_b: int) -> int:
    """``OverlayUtil.resultDimension`` —— 结果(含空结果)应有的拓扑维。

    ``intersection`` 取两维的**小**者、``union`` / ``symmetric_difference``
    取**大**者、``difference`` 取**左操作数**的维。见 :func:`_empty_result`。
    """
    if op == INTERSECTION:
        return min(dim_a, dim_b)
    if op == DIFFERENCE:
        return dim_a
    return max(dim_a, dim_b)


def _assemble_result(op: int, dim_a: int, dim_b: int,
                     parts, shells, lines, points, zs):
    """三份列表 -> 结果六元组 —— ``OverlayUtil.createResultGeometry``。

    装配顺序恒为 **面 → 线 → 点**(JTS 原文 "element geometries of the result
    are always in the order A,L,P"),⚠️ 不是点→线→面。

    * 只有一份非空 → 就是那个原子几何(多个 shell / 多个 part 合成一个
      ``MULTIPOLYGON`` / ``MULTILINESTRING``,多个点合成 ``MULTIPOINT``);
    * **两份以上非空 → ``GEOMETRYCOLLECTION``**,而且这时**每个分量各占一个
      成员**(不是一维一个 ``MULTI*`` 成员)。此时 ``kind`` 回
      ``'geometrycollection'``,而三份列表**原样待在各自的槽里**(``parts`` /
      ``lines`` / ``points``)—— 调用方 ``_overlay_geometry`` 据此把每一维**再
      切成单个分量**构造子几何(面的切法见 ``geometry._polygon_groups``)。这样
      :func:`overlay_parts` 的六元组签名不用改。
    * 三份都空 → :func:`_empty_result` 按维度定型,不是 ``None``。

    ⚠️ "单维合并、多维摊开"不是随手定的,是 JTS ``OverlayUtil`` 把两份列表
    ``addAll`` 进**一个** ``geomList`` 之后交给 ``GeometryFactory.buildGeometry``
    的结果:同型 → ``Multi*``,不同型 → 逐个成员装 ``GEOMETRYCOLLECTION``。JTS
    源码里那句 ``TODO: for mixed dimension, return collection of Multigeom for
    each dimension (breaking change)`` 正说明"按维合并"是他们**没做**的改动,
    所以本库也不能做。本机 GEOS 3.13.1 实测 ``POLYGON ∪ 横穿的 LINESTRING`` =
    ``GEOMETRYCOLLECTION (POLYGON (...), LINESTRING (...), LINESTRING (...))``
    —— **三个**成员,佐证。
    """
    if not parts and not lines and not points:
        return _empty_result(op, dim_a, dim_b)
    # 不变式:**``zs is None`` ⟺ 结果不带 Z**。下面各条路都是"逐分量"拼 zs 的,
    # 会出现"三项全 None 但元组本身非 None"的情形(例如点那一档全 2D 而面那
    # 一档还有 Z 的残值)—— 在这里归一掉,调用方就不用逐条判。
    if zs is not None and zs[0] is None and zs[1] is None and zs[2] is None:
        zs = None
    n_present = (1 if parts else 0) + (1 if lines else 0) + (1 if points else 0)
    if n_present > 1:
        return 'geometrycollection', parts, shells, lines, points, zs
    if parts:
        return 'polygon', parts, shells, None, None, zs
    if lines:
        return 'polyline', None, None, lines, None, zs
    return (('multipoint' if len(points) > 1 else 'point'),
            None, None, None, points, zs)


def overlay_parts(kind_a, parts_a, shells_a, kind_b, parts_b, shells_b, op,
                  zs_a=None, zs_b=None):
    """两个几何做 overlay,返回 ``(kind, parts, shells, lines, points, zs)``。

    :param op: 四个算子之一(见模块顶部的 ``INTERSECTION`` …)。
    :param zs_a/zs_b: **逐 part** 的 z 表(``list[array('d')]``,与 ``parts``
        一一对应),不给 = 该输入没有 Z。只要任意一边给了,结果就带 Z ——
        与 GEOS 实测一致(⚠️ 不是"两边都带才有"),缺 z 的顶点由
        :class:`_ElevationGrid` 补,**补不到就是 NaN**(GEOS 也是这样)。
        ⚠️ **点 × 点**那一档是例外:Z 跟着"去重后胜出的那一份点"走,详见
        :func:`_overlay_points`。
    :returns: ``(result_kind, parts, shells, lines, points, zs)``。

        * ``result_kind`` ∈ ``'polygon'`` / ``'polyline'`` / ``'point'`` /
          ``'multipoint'`` / ``'geometrycollection'``;
        * ``parts`` 只在面那一路非空、``lines`` 只在线那一路非空(每段一条
          ``array('d')`` 的交错坐标)、``points`` 只在点那一路非空
          (``(x, y)`` 元组表)。**混合维度时三份列表同时非空**
          (``kind == 'geometrycollection'``,见 :func:`_assemble_result`);
        * ``zs`` 是 ``(part_zs, line_zs, point_zs)`` 三项,各自与上面三个表
          逐项对应;结果不带 Z 时整体是 ``None``;
        * **全空**时返回对应维度的"空几何"(parts 为空表),**不返回 ``None``**
          —— 具体定型见 :func:`_empty_result`。空结果**不带 Z**(实测 GEOS
          给的就是光秃秃的 ``POLYGON EMPTY``)。

    三路分派,**逐条对应 ``OverlayNG.getResult``**:

    ================================  ====================================
    JTS 判据                            本库
    ================================  ====================================
    ``isAllPoints()``                 :func:`_overlay_points`
    ``!isSingle() && hasPoints()``    :func:`_overlay_mixed_points`
    否则                                :func:`_overlay_linework`
    ================================  ====================================

    本库用**(0 维 / 非 0 维)的个数**表达同一件事 —— 点几何的 ``kind`` 是
    ``'point'`` / ``'multipoint'``,而空几何 ``'null'`` 在 ``OGRGeometry`` 那边
    的维也是 0,所以它跟着点那两档走(与 JTS 一致:``GeometryCollection`` 为空
    时维也是 0)。

    M **一律丢弃** —— GEOS 的 overlay 不产 M,FileGDB 的 M 与这套算法无关。
    """
    for kind in (kind_a, kind_b):
        if kind == 'multipatch':
            raise NotImplementedError(
                'overlay 不支持 multipatch 输入(它的"面"语义与这四条算子'
                '不是一回事),见 DESIGN.md §2.24。')

    dim_a = _kind_dim(kind_a)
    dim_b = _kind_dim(kind_b)

    if dim_a == 0 and dim_b == 0:
        return _overlay_points(op, kind_a, parts_a, zs_a, kind_b, parts_b, zs_b)
    if dim_a == 0 or dim_b == 0:
        return _overlay_mixed_points(op, kind_a, parts_a, shells_a, zs_a,
                                     kind_b, parts_b, shells_b, zs_b)
    return _overlay_linework(kind_a, parts_a, shells_a, kind_b, parts_b,
                             shells_b, op, zs_a, zs_b)


def _overlay_linework(kind_a, parts_a, shells_a, kind_b, parts_b, shells_b, op,
                      zs_a=None, zs_b=None):
    """**两个非点输入**的主路 —— ``OverlayNG.computeEdgeOverlay()``。

    两个输入的边界(面是环、折线是零件)一起节点化成带标签的平面图,再由
    ``PolygonBuilder`` / ``LineBuilder`` / ``IntersectionPointBuilder`` 三件
    各取所需。面的部分见模块 docstring;线与点的判据见 :func:`_is_result_line`
    与 :func:`_intersection_points`。
    """
    dim_a = _kind_dim(kind_a)
    dim_b = _kind_dim(kind_b)
    for kind in (kind_a, kind_b):
        if kind not in ('polygon', 'polyline'):
            raise NotImplementedError(
                'overlay 的这一路只处理面与折线,收到 %r,见 DESIGN.md §2.24。'
                % (kind,))

    env_a = _envelope_of_parts(parts_a)
    env_b = _envelope_of_parts(parts_b)
    if _is_empty_result(op, env_a, env_b):
        return _empty_result(op, dim_a, dim_b)

    # 1) 两个输入的边界 -> 带标签(和 z)的曲线 -> 节点化
    curves = _input_curves(kind_a, parts_a, shells_a, 0, zs_a)
    curves += _input_curves(kind_b, parts_b, shells_b, 1, zs_b)
    if not curves:
        return _empty_result(op, dim_a, dim_b)

    node_pts, eu, ev, edelta, edim, node_z = _noded_edges(curves)
    if not eu:
        return _empty_result(op, dim_a, dim_b)

    #: 结果的 Z 兜底模型(没有任何输入带 Z 时是 None)
    grid = _input_elevation_grid(kind_a, parts_a, zs_a, kind_b, parts_b, zs_b)

    graph = _PlanarGraph(node_pts, eu, ev, edelta)
    face_of, faces = graph.walks()
    #: 每个面的 (depA, depB);> 0 即"在该输入内部"
    depth = _face_depths(graph, face_of, faces)

    # 2) 结果面判据 —— 两侧各自套一遍算子
    def in_area(dv) -> bool:
        return is_result_of_op(
            op, INTERIOR if dv[0] > 0 else EXTERIOR,
            INTERIOR if dv[1] > 0 else EXTERIOR)

    def is_area_edge(dl, dr) -> bool:
        """半边要留的判据:左侧是结果面、右侧不是。"""
        return in_area(dl) and not in_area(dr)

    # 结果面边集(无向)—— 线那一路要跳过它们(``LineBuilder.markResultLines``
    # 第一句 ``if (edge.isInResultEither()) continue``)。判据与下面交给
    # ``_result_rings`` 的是**同一个函数对象**,所以两者不可能走偏。
    area_edges = set()
    for h in range(2 * graph.ne):
        if is_area_edge(depth[face_of[h]], depth[face_of[h ^ 1]]):
            area_edges.add(h >> 1)

    rings = _result_rings(graph, face_of, depth, is_area_edge)

    # 3) 线结果
    has_result_area = bool(area_edges)
    area_idx = 0 if dim_a == 2 else (1 if dim_b == 2 else -1)
    line_edges = []
    for e in range(graph.ne):
        if e in area_edges:
            continue
        loc_line = _line_locations(e, edim, depth, face_of)
        touch = _boundary_touch(e, edim, depth, face_of)
        if _is_result_line(op, edim[e], loc_line, has_result_area,
                           area_idx, touch):
            line_edges.append(e)

    # 4) 点结果 —— 只有交集会从非点输入里生出点
    #    (``IntersectionPointBuilder`` 的类注释:Intersection is the only
    #    overlay operation which can result in Points from non-Point inputs.)
    point_nodes: List[int] = []
    if op == INTERSECTION:
        point_nodes = _intersection_points(
            graph, edim, area_edges, set(line_edges))

    # 5) 装配 —— 顺序恒为 面 -> 线 -> 点
    res = _assemble(rings, node_pts, 1, 0.0,
                    node_z if grid is not None else None) if rings else None
    parts, shells, part_zs = res if res is not None else ([], [], None)
    lines = [array('d', chain.from_iterable(
        (node_pts[eu[e]], node_pts[ev[e]]))) for e in line_edges]
    line_zs = None
    if grid is not None and lines:
        line_zs = [array('d', (node_z[eu[e]], node_z[ev[e]]))
                   for e in line_edges]
    points = [node_pts[u] for u in point_nodes]
    point_zs = None
    if grid is not None and points:
        point_zs = array('d', (node_z[u] for u in point_nodes))

    # Z 兜底 —— ElevationModel.populateZ(结果整体过一遍,已有 z 的不动)
    if grid is not None:
        if part_zs:
            for xy, zz in zip(parts, part_zs):
                grid.populate(xy, zz)
        if line_zs:
            for xy, zz in zip(lines, line_zs):
                grid.populate(xy, zz)
        if point_zs is not None:
            for k, u in enumerate(point_nodes):
                if point_zs[k] != point_zs[k]:
                    point_zs[k] = grid.get_z(node_pts[u][0], node_pts[u][1])
    if grid is not None and not grid.has_z:
        grid = None                     # 输入自称有 Z 却一个真值都没有
        part_zs = line_zs = point_zs = None

    zs = (part_zs, line_zs, point_zs) if grid is not None else None
    # ⚠️ 混合维度**能**走到这里:面 × 线、面 × 面(例如 B 是"与 A 共一条边的
    #    方块" ∪ "落在 A 里的小方块"的多壳面,交集 = 小方块 + 那条共边)都会
    #    同时出两个维度。装配交给 `_assemble_result`(它才管 GC 那一档)。
    return _assemble_result(op, dim_a, dim_b, parts, shells, lines, points, zs)


def _intersection_points(graph: _PlanarGraph, edim, area_edges,
                         line_edges) -> List[int]:
    """``IntersectionPointBuilder`` —— 节点上"两边都有边、但一条都不在结果里"。

    判据(JTS ``isResultPoint``):绕节点星形扫一圈,

    * 只要有一条边**已经在结果里**(面结果或线结果)→ 这个节点不是结果点
      (该点已经被更高维的结果吃掉了);
    * 否则,只要既有属于 A 的边又有属于 B 的边 → 是结果点。

    实测两个例子正好把两边都钉住:只共一条边的两方块,共边那两个节点上都有
    "已在结果线里"的边 → **不产点**(结果是那条线本身);只共一个点的两方块,
    那个节点上四条边一条都不在结果里、A B 各有边 → **产点**。

    ⚠️ JTS ``getNodeEdges()`` 每个节点只取**一条**起始边,所以一个节点最多出
    一个点;这里同样按节点去重(节点号天然唯一)。
    """
    out: List[int] = []
    for u in range(graph.n):
        incident = {h >> 1 for h in graph.out[u]}
        if incident & area_edges or incident & line_edges:
            continue
        has_a = has_b = False
        for e in incident:
            d0, d1 = edim[e]
            if d0 != DIM_NOT_PART:
                has_a = True
            if d1 != DIM_NOT_PART:
                has_b = True
        if has_a and has_b:
            out.append(u)
    return out


# ----------------------------------------------------------------------------
# 点 × 点 —— JTS ``OverlayPoints``
# ----------------------------------------------------------------------------
def _point_coords(kind, parts, zs) -> Tuple[List[Tuple[float, float]], List[float]]:
    """点 / 多点输入 -> ``([(x, y), ...], [z, ...])`` 两张等长的表(保序、不去重)。

    ⚠️ 本库里 multipoint 是**一整块**交错坐标、单点也是一块
    (见 ``Geometry.xy_parts``),所以这里统一按"块表"走;空几何的 ``parts``
    是空表。``zs`` 与 ``parts`` 逐块对应,不给则该输入的 z 全是 NaN。
    """
    xy: List[Tuple[float, float]] = []
    zz: List[float] = []
    for i, a in enumerate(parts):
        z = None if zs is None else zs[i]
        for k in range(len(a) // 2):
            xy.append((a[2 * k], a[2 * k + 1]))
            zz.append(_NAN if z is None else z[k])
    return xy, zz


def _point_map(xy, zz) -> dict:
    """``OverlayPoints.buildPointMap`` —— 按 **XY** 去重,**首次出现的那一份说了算**。

    JTS 原文:"Only add first occurrence of a point. This provides the merging
    semantics of overlay"。键是 ``Coordinate``,它的 ``equals`` / ``hashCode``
    只看 x/y(Z 不参与),所以同一 XY 不同 Z 的两个点**算同一个点**,留下的是
    先出现的那个(**包括它的 Z**)。
    """
    m: dict = {}
    for p, z in zip(xy, zz):
        if p not in m:
            m[p] = (p[0], p[1], z)
    return m


def _overlay_points(op, kind_a, parts_a, zs_a, kind_b, parts_b, zs_b):
    """``OverlayPoints`` —— 两个输入**都是**点/多点(或空几何)。

    四个算子在两个点表上就是一串集合运算(``computeIntersection`` /
    ``computeUnion`` / ``computeDifference`` / 双向 ``computeDifference``),
    这里逐条照抄。结果点的 Z 跟着"去重后胜出的那一份"走 —— 所以
    ``POINT Z(1 2 3) ∪ POINT(1 2)`` 是带 Z 的,反过来
    ``POINT(1 2) ∪ POINT Z(1 2 3)`` 是 2D 的(JTS ``copyPoint`` 复制的就是
    map0 那一份)。这与"只要有一边带 Z 结果就带 Z"的**面/线**档不同,是
    ``OverlayPoints`` 自己的语义,照抄不折中。
    """
    xy_a, z_a = _point_coords(kind_a, parts_a, zs_a)
    xy_b, z_b = _point_coords(kind_b, parts_b, zs_b)
    map0 = _point_map(xy_a, z_a)
    map1 = _point_map(xy_b, z_b)
    out: List[Tuple[float, float, float]] = []
    if op == INTERSECTION:
        for p, v in map0.items():
            if p in map1:
                out.append(v)
    elif op == UNION:
        out.extend(map0.values())
        for p, v in map1.items():
            if p not in map0:
                out.append(v)
    elif op == DIFFERENCE:
        for p, v in map0.items():
            if p not in map1:
                out.append(v)
    else:                                       # SYMDIFFERENCE = 双向差
        for p, v in map0.items():
            if p not in map1:
                out.append(v)
        for p, v in map1.items():
            if p not in map0:
                out.append(v)
    return _point_only_result(op, 0, 0, out)


def _point_only_result(op, dim_a, dim_b, out):
    """``OverlayPoints.createResultGeometry``/``createPointResult`` 的**装配**。

    ``out`` 是 ``[(x, y, z), ...]``(已去重、保序 —— ⚠️ JTS 用的是 ``HashMap``
    / ``HashSet``,次序不定;本库保插入序,这是一处**刻意的确定性改进**,
    结果集合相同)。

    ``0`` 个点 → ``POINT EMPTY``(⚠️ 不是 ``POLYGON EMPTY``,也不是 ``None``),
    ``1`` 个 → 单个 ``Point``,``>1`` → ``MultiPoint``。
    """
    if not out:
        return _empty_result(op, dim_a, dim_b)
    points = [(x, y) for x, y, _ in out]
    point_zs = array('d', (z for _, _, z in out))
    has_z = any(z == z for z in point_zs)        # 一个真值都没有 = 当它没有 Z
    zs = (None, None, point_zs) if has_z else None
    return (('multipoint' if len(points) > 1 else 'point'),
            None, None, None, points, zs)


# ----------------------------------------------------------------------------
# 点 × 非点 —— JTS ``OverlayMixedPoints``
# ----------------------------------------------------------------------------
def _unary_union(kind, parts, shells, zs):
    """``OverlayNG.union(geom)`` —— 一元并(``UnaryUnionOp``)。

    ``OverlayMixedPoints.prepareNonPoint`` 在"非点几何要进结果"时先把它自己并
    一遍(节点化 + 溶解自重叠的环),这里对应同一个动作。

    ⚠️ 实现上是把**同一个输入**喂给既有的双输入引擎、另一侧给一个**空的折线
    输入**:它一条曲线都不产,深度向量的第二个分量恒为 0,于是"结果面"判据
    退化成"在 A 内部就行"、线结果判据退化成"属于 A 的边就要",恰好就是一元并。
    那个占位输入的维度**不能是 0**(会被分派到点那两档)、**也不能是 2**
    (会把 ``area_idx`` 指到占位输入身上),折线正好。

    空输入的早退也顺带对:``_empty_result(UNION, dim, 1)`` 取 max 就是 ``dim``,
    ``POLYGON EMPTY`` 进来、``POLYGON EMPTY`` 出去。
    """
    return overlay_parts(kind, parts, shells, 'polyline', [], None, UNION,
                         zs, None)


def _locate_nonpoint(kind, parts, shells, c) -> int:
    """点相对**非点**几何的位置(:class:`IndexedPointInAreaLocator` 的等价物)。

    ``point_location`` 对折线可能回 ``BOUNDARY``(悬端点)、对多边形可能回
    ``BOUNDARY``(落在环上)—— JTS 的 ``IndexedPointOnLineLocator`` /
    ``IndexedPointInAreaLocator`` 也是这两种口径,而 ``hasLocation`` 判的只有
    ``EXTERIOR`` 一个值(``isExterior = EXTERIOR == locator.locate(coord)``),
    两边一致。

    ⚠️ ``point_location`` 的边界判定是**精确比较**(``orient() == 0``),所以对
    "算出来的交点"不可靠;这里进来的点坐标**全部来自输入的点几何**(原样,
    没有算术),所以不受这条影响。
    """
    return point_location(c[0], c[1], kind, parts, shells)


def _overlay_mixed_points(op, kind_a, parts_a, shells_a, zs_a,
                          kind_b, parts_b, shells_b, zs_b):
    """``OverlayMixedPoints`` —— 一个输入是点/多点,另一个不是(或为空几何)。

    流程逐条照抄 ``OverlayMixedPoints.getResult``:

    1. 认准哪边是点(``geom0.getDimension() == 0`` 则点是**左**操作数);
    2. ``prepareNonPoint`` —— ``resultDim == 0`` 时**不**节点化(类注释写明的
       优化:"points are compared to the non-rounded geometry"),否则先一元并;
    3. 建定位器(面 / 线各一种),把点分成"被盖住"与"没被盖住"两组;
    4. 分派:
       * ``INTERSECTION`` → 被盖住的点;
       * ``UNION`` / ``SYMDIFFERENCE`` → **同一个输出** = 非点几何 + 没被盖住
         的点(实测 ``POLYGON ∪ 面外点`` = ``GEOMETRYCOLLECTION``,而
         ``POLYGON ∪ 面内点`` = 纯 ``POLYGON``,点被这步过滤掉);
       * ``DIFFERENCE`` → 点是左操作数就给"没被盖住的点",点是右操作数就给
         非点几何**原样**(``copyNonPoint``)。
    """
    dim_a = _kind_dim(kind_a)
    dim_b = _kind_dim(kind_b)
    result_dim = _result_dimension(op, dim_a, dim_b)

    if dim_a == 0:
        gkind, gparts, gzs = kind_a, parts_a, zs_a
        nkind, nparts, nsh, nzs = kind_b, parts_b, shells_b, zs_b
        is_point_rhs = False
    else:
        gkind, gparts, gzs = kind_b, parts_b, zs_b
        nkind, nparts, nsh, nzs = kind_a, parts_a, shells_a, zs_a
        is_point_rhs = True

    # prepareNonPoint
    if result_dim == 0:
        np_kind, np_parts, np_shells = nkind, nparts, nsh
        np_zs = nzs                           # 非点几何不进结果 → 它的 z 用不上
    else:
        uk, up, us, ul, _upt, uz = _unary_union(nkind, nparts, nsh, nzs)
        if uk in ('polygon', 'geometrycollection'):
            np_kind, np_parts, np_shells = uk, up, us
        else:
            np_kind, np_parts, np_shells = uk, ul, None
        # ⚠️ ``uz`` 是**三槽元组** ``(part_zs, line_zs, point_zs)``,不是单张 z 表
        #    —— 直接当下面的 ``part_zs`` 用会把元组塞进 ``_FlatCoords`` 的 z 槽,
        #    到渲染 WKT 时才炸成一串 bytes。要取哪一槽由 ``np_kind`` 决定。
        np_zs = None if uz is None else (uz[0] if np_kind == 'polygon'
                                         else uz[1])

    xy, zz = _point_coords(gkind, gparts, gzs)

    if op == INTERSECTION:
        out = []
        seen = set()
        for c, z in zip(xy, zz):
            if c in seen or _locate_nonpoint(np_kind, np_parts, np_shells,
                                             c) == EXTERIOR:
                continue
            seen.add(c)
            out.append((c[0], c[1], z))
        return _point_only_result(op, dim_a, dim_b, out)

    if op == DIFFERENCE and is_point_rhs:
        return _nonpoint_only(op, dim_a, dim_b, np_kind, np_parts, np_shells,
                              np_zs)

    # 剩下三支都要"没被盖住的点"
    out = []
    seen = set()
    for c, z in zip(xy, zz):
        if c in seen or _locate_nonpoint(np_kind, np_parts, np_shells, c) != EXTERIOR:
            continue
        seen.add(c)
        out.append((c[0], c[1], z))

    if op == DIFFERENCE:                     # 点是左操作数
        return _point_only_result(op, dim_a, dim_b, out)

    # UNION / SYMDIFFERENCE —— 输出相同:非点几何 + 面外的点
    if not out:
        return _nonpoint_only(op, dim_a, dim_b, np_kind, np_parts, np_shells,
                              np_zs)
    points = [(x, y) for x, y, _ in out]
    point_zs = array('d', (z for _, _, z in out))
    if not any(z == z for z in point_zs):
        point_zs = None
    p_zs, l_zs = (np_zs, None) if np_kind == 'polygon' else (None, np_zs)
    return _assemble_result(op, dim_a, dim_b,
                            np_parts if np_kind == 'polygon' else [],
                            np_shells if np_kind == 'polygon' else None,
                            np_parts if np_kind == 'polyline' else [],
                            points, (p_zs, l_zs, point_zs))


def _nonpoint_only(op, dim_a, dim_b, np_kind, np_parts, np_shells, zs):
    """非点几何**原样**进结果(``copyNonPoint`` 那一支)。

    ⚠️ 空输入要在这里兜住:非点几何是空的时候三份列表全空,
    :func:`_assemble_result` 会退回 :func:`_empty_result` 按维度定型
    (``POLYGON ∖ 点`` 里那个面若为空,给的就是 ``POLYGON EMPTY``,不是 None)。
    """
    if np_kind == 'polygon':
        return _assemble_result(op, dim_a, dim_b, np_parts, np_shells,
                                [], [], (zs, None, None) if zs is not None
                                else None)
    if np_kind == 'polyline':
        return _assemble_result(op, dim_a, dim_b, [], None, np_parts, [],
                                (None, zs, None) if zs is not None else None)
    return _empty_result(op, dim_a, dim_b)
