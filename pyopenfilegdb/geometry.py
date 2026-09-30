# -*- coding: utf-8 -*-
"""几何对象 :class:`Geometry` —— 存储、空间计算、几何文本导出。

这个模块从 ``_datatypes.py`` 里分出来,理由有两条:

1. **体积**:``Geometry`` 原来只有 9 个成员(存储与格式),加上空间计算和拓扑
   判定后要几百行,再塞在"数据类型 + 要素 + 异常 + 时间辅助"那个杂货铺里不合适。
2. **依赖方向**。``_datatypes.py`` 现在被 ``_esri_geometry`` 反过来依赖
   (两边互相咬);把几何类摘出来后依赖图成了**严格的 DAG**::

       _constants ← _geometry_ops        (叶子,只认 array('d'),不 import 本包)
       _constants ← geometry → _geometry_ops
       _datatypes → geometry
       _esri_geometry → geometry, _datatypes

   所以 ``geometry`` 只管 ``_constants``(拿 ``ShapeType``)与 ``_geometry_ops``,
   **不依赖 ``_datatypes``**。几何类的构造**一个异常都不抛**,这也是它敢这么切的
   前提。

还是只有一个具体类,没有 OGR 式的 ``OGRPoint`` / ``OGRLineString`` / ``OGRPolygon``
子类树 —— 靠 ``shape_type`` 分流,与盘上的存储形态一一对应。

⚠️ 性能上的硬规矩
----------------
本模块**所有**空间计算只读 :attr:`Geometry.xy_parts`(每个 part 一块交错的
``array('d')``),**绝不碰** :attr:`Geometry.coordinates`。后者是把 flat 存储
物化成逐点元组的派生视图,值 ~19 ns/顶点(见 DESIGN.md §2.19.5);空间计算是
逐顶点扫描,走它就等于把换 flat 容器省下来的钱原样花回去,而且是在遍历循环里花。

⚠️ 数值鲁棒性
------------
拓扑判定没有精确算术、没有 snap-rounding,次 ULP 的退化构型可能判错;``is_valid``
只是**部分实现**。两条限制的完整说法见 :mod:`._geometry_ops` 的 docstring 与
DESIGN.md §2.21。谓词这一档 GDAL **没有纯 C++ 实现可抄** ——
``OGRGeometry::Intersects`` 等全部转手给 GEOS(没编 GEOS 时直接报错返回 FALSE),
所以本模块的目标是**对齐 OGC DE-9IM 的语义**,与 GDAL 有 GEOS 时的结果一致。
"""
from __future__ import annotations

from array import array
from itertools import chain
from typing import Any, List, Optional, Sequence, Tuple

from . import _constants as C
from ._geometry_ops import (
    area_of,
    centroid_of,
    convex_hull_xy,
    dimension_of,
    distance_of,
    envelope_of,
    envelope_intersects,
    interpolate_scalars,
    is_simple_of,
    is_valid_of,
    length_of,
    organize_polygons,
    point_count_of,
    predicate_de9im,
    relate_of,
    ring_roles,
    ring_signed_area2,
    segmentize_xy,
    simplify_parts,
)

#: "还没算过"的哨兵。用它而不是 ``None``,是因为 ``None`` 在好几个缓存位点上
#: 都是**合法结果**(空几何的包围盒就是 ``None``)。``_datatypes`` 也从这儿
#: 引它,全库只用一个哨兵对象。
_UNSET = object()

# ----------------------------------------------------------------------------
# 几何
# ----------------------------------------------------------------------------
class _FlatCoords:
    """几何坐标的 **flat 后备存储**:免掉逐点建 Python 对象。

    为什么要有它
    ------------
    一条 2,118 个顶点的多边形,元组表示要建 2,118 个 ``tuple`` 加 4,236 个
    ``float``。实测整层 89,935,418 个 varint:varint 循环本身只要 0.33 s,
    把这些数包成 Python 对象要 **1.7 s**。GDAL 的 ``OGRLineString`` 是一条
    ``double*``,这笔钱一分不付 —— 所以这是**换坐标容器**,不是算法优化
    (见 DESIGN.md §2.19.5)。

    形态
    ----
    ``xy`` / ``zs`` / ``ms`` 都是 **每个 part 一块数组** 的列表,逐块一一
    对应(多边形的每个环一块;multipoint 只有一块,没有 part 之分):

    * ``xy[i]``:``array('d')``,``(x0, y0, x1, y1, ...)`` —— **交错**存放;
    * ``zs[i]`` / ``ms[i]``:``array('d')``,每点一个值 —— **不**交错。
      整个几何没有 Z / M 时对应项是 ``None``。

    交错存(XY 混在一个数组里)而不是分成 xs/ys 两串,是因为磁盘上的 varint
    流本来就是 x、y 交替来的,交错写下去不用二次搬运。
    """

    __slots__ = ('xy', 'zs', 'ms')

    def __init__(self, xy: List[Any], zs: Optional[List[Any]] = None,
                 ms: Optional[List[Any]] = None) -> None:
        self.xy = xy
        self.zs = zs
        self.ms = ms

    # -- 还原成公开的元组形态 ------------------------------------------------
    def _part_tuples(self, i: int) -> List[Tuple[float, ...]]:
        """第 i 块的 ``[(x, y[, z][, m]), ...]``。"""
        xy = self.xy[i]
        n = len(xy) // 2
        rng = range(n)
        zs = None if self.zs is None else self.zs[i]
        ms = None if self.ms is None else self.ms[i]
        if zs is None:
            if ms is None:
                return [(xy[2 * j], xy[2 * j + 1]) for j in rng]
            return [(xy[2 * j], xy[2 * j + 1], ms[j]) for j in rng]
        if ms is None:
            return [(xy[2 * j], xy[2 * j + 1], zs[j]) for j in rng]
        return [(xy[2 * j], xy[2 * j + 1], zs[j], ms[j]) for j in rng]

    def as_multipoint_tuples(self) -> List[Tuple[float, ...]]:
        """multipoint 的 ``coordinates``:一个扁的元组列表。"""
        return self._part_tuples(0)

    def as_part_tuples(self) -> Tuple[List[int], List[Tuple[float, ...]]]:
        """polyline / polygon 的 ``coordinates``:``(parts, points)``。

        ``parts`` 是每个 part 首点在**拼接后**点序里的下标,与 flat 存储里
        "每块一个数组"的分块边界等价 —— 这里现算,不常驻。
        """
        starts: List[int] = []
        out: List[Tuple[float, ...]] = []
        k = 0
        for i in range(len(self.xy)):
            starts.append(k)
            pts = self._part_tuples(i)
            out.extend(pts)
            k += len(pts)
        return starts, out


class Geometry:
    """几何对象。

    ``coordinates`` 的形态随 :attr:`kind` 变化::

        point      : (x, y) 或 (x, y, z) / (x, y, m) / (x, y, z, m)
        multipoint : [(x, y, ...), ...]
        polyline   : (parts, points)
        polygon    : (parts, points)

    其中 ``parts`` 是 ``List[int]``(每个 part 起始点在 ``points`` 中的下标),
    ``points`` 是 ``List[Tuple[float, ...]]``。多边形沿用 Esri/OGC 的环语义:
    外环顺时针、内环逆时针。

    ⚠️ **环的首尾点不重复。** ArcGIS 写到盘上的环是闭合的(末尾重复一次
    首点),这个"重复的闭合点"由 `_esri_geometry` 在解码末尾削掉、编码开头
    补回,外面看到的一律是不闭合的环 —— 与 :meth:`from_wkt`、:meth:`wkt`
    的表示一致,``POLYGON`` 的顶点数就是环的真实顶点数。

    ``coordinates`` 是**惰性**的
    ---------------------------
    解码器落进来的是 :class:`_FlatCoords`(连续 double 缓冲),**不建逐点
    的元组**。第一次读 :attr:`coordinates` 时才物化成上面那种形态并缓存 ——
    老代码一行不用改,只是"要元组才付元组的钱"。整批用坐标的话走
    :attr:`xy_parts` / :attr:`z_parts` / :attr:`m_parts`,那是零拷贝的
    ``array('d')``,见 DESIGN.md §2.19.5。
    """

    def __init__(self, shape_type: int = C.ShapeType.NULL,
                 coordinates: Any = None, has_z: bool = False,
                 has_m: bool = False, _flat: Optional[_FlatCoords] = None,
                 _shells: Optional[List[bool]] = None) -> None:
        self.shape_type = shape_type
        self.has_z = has_z
        self.has_m = has_m
        self._flat = _flat
        #: flat 存储时是"还没物化"的哨兵 ``None``(flat 几何的 coordinates
        #: 不可能是 None,所以这个标记没有歧义);否则就是调用方给的值。
        self._coords = None if _flat is not None else coordinates
        #: 逐 part 的"是不是外环"。**显式给出来的角色优先级最高** ——
        #: ``from_wkt`` 从结构上知道哪个环是外环,直接记下来;解码器给不出
        #: (Esri 只存绕向、不存归属),那时它是 ``None``,由 :meth:`_roles`
        #: 按绕向还原,正是 GDAL ``OGR_ORGANIZE_POLYGONS`` 默认的 ``ONLY_CCW``。
        self._shells = _shells
        #: :meth:`_groups` 的缓存(环组装结果,见 ``_geometry_ops.organize_polygons``)
        self._groups = None
        #: :meth:`envelope` 的缓存。包围盒是**精确值**,算一次就够,
        #: 而拓扑判定每个谓词都要用它做快速排除。
        self._env = _UNSET

    # -- 坐标 ---------------------------------------------------------------
    @property
    def coordinates(self) -> Any:
        """公开的元组形态;**第一次访问时才物化**,之后缓存。

        ⚠️ 物化要在 Python 里建逐点的 ``float`` / ``tuple``,量级是
        ~19 ns/顶点(整层 ~1.7 s)。只需要坐标数值的话用 :attr:`xy_parts`。
        """
        if self._flat is not None and self._coords is None:
            f = self._flat
            kind = self.kind
            if kind == 'multipoint':
                self._coords = f.as_multipoint_tuples()
            elif kind in ('polyline', 'polygon'):
                self._coords = f.as_part_tuples()
            else:                       # pragma: no cover - 不会走到
                self._coords = f.as_multipoint_tuples()
        return self._coords

    @property
    def xy_parts(self) -> List[Any]:
        """每个 part 一块交错的 ``array('d')``(零拷贝)。

        多边形 / 折线是一个环一块,``(x0, y0, x1, y1, ...)``;multipoint 是
        一整块。要 x 序列用 ``arr[0::2]``、要 y 序列用 ``arr[1::2]``。

        point 几何**没有** part 这一层(它就一个点,本来也不存在物化成本),
        这里返回一个单元素列表,内容就是那个点。
        """
        f = self._flat
        if f is not None:
            return f.xy
        return [_xy_of_tuples(g) for g in _tuple_part_groups(self)]

    @property
    def z_parts(self) -> Optional[List[Any]]:
        """与 :attr:`xy_parts` 逐块对应的 Z 数组;没有 Z 时是 ``None``。"""
        if not self.has_z:
            return None
        f = self._flat
        if f is not None and f.zs is not None:
            return f.zs
        return _scalar_parts_from_tuples(self, 2)

    @property
    def m_parts(self) -> Optional[List[Any]]:
        """与 :attr:`xy_parts` 逐块对应的 M 数组;没有 M 时是 ``None``。"""
        if not self.has_m:
            return None
        f = self._flat
        if f is not None and f.ms is not None:
            return f.ms
        return _scalar_parts_from_tuples(self, 3 if self.has_z else 2)

    # -- 杂项 ---------------------------------------------------------------
    @property
    def kind(self) -> str:
        """point / polyline / polygon / multipoint / null。"""
        return C.ShapeType.base_kind(self.shape_type)

    @property
    def is_empty(self) -> bool:
        # 注意**不要**写 self.coordinates is None —— 那会把 flat 几何整层
        # 物化一遍,只为判一个空。
        return self.shape_type == C.ShapeType.NULL or (
            self._coords is None and self._flat is None)

    # -- 描述 ---------------------------------------------------------------
    @property
    def dimension(self) -> int:
        """拓扑维:点/多点 ``0``、折线 ``1``、面/multipatch ``2``、null ``0``。

        对应 ``OGRGeometry::getDimension``(``wkbNone`` 走基类返回 0;
        multipatch 按 ``wkbPolyhedralSurface`` 算 2)。**不物化坐标。**
        """
        return dimension_of(self.kind)

    @property
    def point_count(self) -> int:
        """**所有 part** 的顶点数之和。**不物化坐标。**

        ⚠️ 与 OGR 分歧:``OGRPolygon::getNumPoints`` 只数外环。这里给的是
        "这条几何到底有多大",那才是调用方要问的。要单个数某一环就自己数
        ``len(self.xy_parts[i]) // 2``。
        """
        return point_count_of(self.xy_parts)

    @property
    def part_count(self) -> int:
        """part 个数。**不物化坐标。** multipoint 恒为 1(point 也是 1)。"""
        return len(self.xy_parts)

    @property
    def is_ring(self) -> bool:
        """这条几何是不是**一个闭合环**。

        判据按两种存储形态分开,这里有个**差一陷阱**必须说清:

        * ``polyline`` —— 照 ``OGRLinearRing::IsRing()``,即
          ``IsClosed() && getNumPoints() >= 4``。本库的折线**保留**闭合点,
          所以与 OGR 逐字对应,顶点数下限就是 **4**。
        * ``polygon`` —— 内存里的环**不存闭合点**(闭合段由导出/编码时补),
          所以一个"三角形环"存的是 3 个顶点,顶点数下限是 **3**。
          换成"不同顶点数"的说法就是"≥ 3 个不同顶点"。

        两种情形都要求恰好**一个** part。
        """
        parts = self.xy_parts
        if len(parts) != 1:
            return False
        a = parts[0]
        n = len(a) // 2
        kind = self.kind
        if kind == 'polyline':
            return n >= 4 and a[0] == a[n * 2 - 2] and a[1] == a[n * 2 - 1]
        if kind == 'polygon':
            return n >= 3
        return False

    @property
    def is_clockwise(self) -> bool:
        """第一个环的绕向是不是**顺时针**。

        用的是本库既有的绕向约定(数学坐标系 y 向上,**带符号面积为负 =
        顺时针**,见 ``_esri_geometry._is_clockwise``)—— 这也是 Esri 的约定
        (外环顺时针),恰好与 ISO 19107 / OGC 相反。

        与 OGR ``OGRLinearRing::isClockwise()`` 的对应:它对顶点数不足的环返回
        ``FALSE``,这里同样 —— 折线要 ≥ 4 个顶点、面的环要 ≥ 3 个
        (不存闭合点)。多 part 几何返回 ``False``(绕向本来就是"一个环"的属性,
        逐环问请自己取 ``xy_parts[i]``)。
        """
        parts = self.xy_parts
        if len(parts) != 1:
            return False
        a = parts[0]
        n = len(a) // 2
        if self.kind == 'polyline':
            if n < 4:
                return False
        elif self.kind == 'polygon':
            if n < 3:
                return False
        else:
            return False
        return ring_signed_area2(a) < 0.0

    # -- 环的角色与组装(内部) ----------------------------------------------
    def _roles(self) -> Optional[List[bool]]:
        """逐 part 的"是不是外环";非面返回 ``None``。

        显式给过的角色(``from_wkt`` 的路)优先,否则按绕向还原 ——
        即 GDAL ``OGR_ORGANIZE_POLYGONS`` 的默认策略 ``ONLY_CCW``。
        """
        if self.kind != 'polygon':
            return None
        return ring_roles(self.xy_parts, self._shells)

    def _ring_groups(self) -> Optional[List[Any]]:
        """``[(外环下标, [洞下标...]), ...]``,算一次就缓存;非面返回 ``None``。

        缓存是必须的:``relate()`` 每个探针点都要按归属判位置,不缓存的话
        ``O(环数²)`` 的包含测试会被重算上千遍。
        """
        if self.kind != 'polygon':
            return None
        if self._groups is None:
            self._groups = organize_polygons(self.xy_parts, self._shells)
        return self._groups

    @staticmethod
    def _require_geometry(other: Any) -> 'Geometry':
        """拓扑 / 距离运算的入参检查。"""
        if not isinstance(other, Geometry):
            raise TypeError(
                f'只接受另一个 Geometry,得到 {type(other).__name__}')
        return other

    def _require_no_multipatch(self, op: str) -> None:
        """multipatch 在谓词这一档一律拒绝,且要给得出理由。"""
        if self.kind == 'multipatch':
            raise NotImplementedError(
                f'{op}() 不支持 multipatch:FileGDB 里的 multipatch 是 '
                'triangle strip / triangle fan 的流式编码,与 OGR / DE-9IM 的'
                '"环"模型对不上,硬换算过去会得到一条语义已经变了的几何,'
                '错得看不出来。本库对 multipatch 只支持描述与度量'
                '(envelope / area / length / centroid)。见 DESIGN.md §2.21。')

    def _like(self, parts: Sequence[Any], zs: Any, ms: Any) -> 'Geometry':
        """用新造出来的坐标装一个**同类型、同 Z/M 标记**的新几何。

        ``zs`` / ``ms`` 与 ``parts`` 逐块对应(``None`` 表示本几何没有 Z / M)。
        """
        kind = self.kind
        if kind == 'null':
            return Geometry(self.shape_type, None, self.has_z, self.has_m)
        if kind == 'point':
            # 点的 coordinates 是一个**裸元组**;走 _FlatCoords 会被
            # :meth:`coordinates` 当成 multipoint 物化成"元组的列表"。而且点
            # 只有 1 个顶点,手工装没有成本。
            a = parts[0] if parts else None
            if not a:
                return Geometry(self.shape_type, None, self.has_z, self.has_m)
            pt = (a[0], a[1])
            if self.has_z and zs is not None:
                pt += (zs[0][0],)
            if self.has_m and ms is not None:
                pt += (ms[0][0],)
            return Geometry(self.shape_type, pt, self.has_z, self.has_m)
        if not parts:
            return Geometry(self.shape_type, None, self.has_z, self.has_m)
        return Geometry(self.shape_type, None, self.has_z, self.has_m,
                        _flat=_FlatCoords(parts, zs, ms))

    # -- 度量 ---------------------------------------------------------------
    def envelope(self) -> Optional[Tuple[float, float, float, float]]:
        """**精确**包围盒 ``(xmin, ymin, xmax, ymax)``;空几何返回 ``None``。

        ⚠️ 和 ``_LazyGeometry.envelope()`` 不是一回事,别混:那个读的是盘上
        **已放宽一个量化步长**的上界,不用解码顶点就能拿到,用来做 bbox 预筛;
        这个是**解码后逐顶点扫出来的精确值**。要判"这条要素是否落在某个范围
        里",用那个;要报一个真值,用这个。

        ⚠️ 与 OGR 分歧:``OGRGeometry::getEnvelope`` 填的是
        ``(MinX, MaxX, MinY, MaxY)``,顺序不是这个。本库全库统一
        ``(xmin, ymin, xmax, ymax)``。

        精确值算一次就缓存 —— 拓扑判定的每个谓词都要拿它做快速排除。
        **不物化坐标。**
        """
        if self._env is _UNSET:
            self._env = envelope_of(self.xy_parts)
        return self._env

    def area(self) -> float:
        """面积;非面几何恒为 ``0.0``(与 ``OGRGeometry::get_Area`` 一致)。

        面是 **外环面积之和 减 内环面积之和**(不是逐环取绝对值相加 —— 那样
        会把洞加进去)。Z / M 不参与。**不物化坐标。**
        """
        return area_of(self.kind, self.xy_parts, self._roles())

    def length(self) -> float:
        """长度:折线是各段之和,面是**周长**(各环之和),点/多点 ``0.0``。

        对应 ``OGR_G_Length``(面走 ``OGRCurvePolygon::get_Length``,即周长
        而不是"边界的长度之和"以外的东西)。Z / M 不参与。**不物化坐标。**
        """
        return length_of(self.kind, self.xy_parts)

    def centroid(self) -> Optional[Tuple[float, float]]:
        """质心 ``(x, y)``;空几何返回 ``None``。

        * **面** —— 按面积加权(洞的权重取负),总权重 ≤ 0 时退化成顶点平均;
        * **点 / 多点 / 折线** —— **顶点算术平均**。

        后一条刻意跟 GDAL(``OGRGeometry::Centroid`` 与 ``OGRSimpleCurve::
        Centroid`` 都是顶点平均),与 GEOS 不同 —— GEOS 的线质心按**长度**加权。

        ⚠️ 返回**元组**,不像 ``OGR_G_Centroid`` 那样再去填一个点几何:
        一个坐标不值得为它建一个对象。
        """
        return centroid_of(self.kind, self.xy_parts, self._roles())

    def distance(self, other: 'Geometry') -> Optional[float]:
        """到 ``other`` 的最近距离(XY 平面);任一为空返回 ``None``。

        对应 ``OGR_G_Distance``(GEOS 也是按线段算的,不是近似值)。

        ⚠️ **必须先判相交。** 一个点落在面里、或一个面整个套在另一个面里时,
        "线段到线段的最近距离"是正数,但正确答案是 ``0.0``(GEOS / GDAL 都
        返回 0)。所以本方法依赖 :meth:`intersects`,也就**不支持 multipatch**
        (见 DESIGN.md §2.21)。

        ⚠️ ``O(n·m)`` 的线段对枚举,只做了线段包围盒剪枝,没有空间索引 ——
        两个万级顶点的几何互相测距会明显慢。
        """
        other = self._require_geometry(other)
        self._require_no_multipatch('distance')
        other._require_no_multipatch('distance')
        if self.is_empty or other.is_empty:
            return None
        if self.intersects(other):
            return 0.0
        return distance_of(self.kind, self.xy_parts,
                           other.kind, other.xy_parts)

    # -- 构造型(返回新的 Geometry) -----------------------------------------
    def convex_hull(self) -> 'Geometry':
        """凸包。**恒为 2D**(Z / M 丢掉),**恒丢洞**(凸包本来就没有洞)。

        退化情形与 GEOS 一致:一个点都没有 → 空几何;只有 1 个点(或全部
        重合)→ ``POINT``;全部共线 → ``LINESTRING``;否则 → 单环 ``POLYGON``。

        结果的环按本库(以及 Esri)的约定是**外环顺时针**,而
        ``_geometry_ops.convex_hull_xy`` 内部给的是逆时针,所以这里翻一下。
        """
        self._require_no_multipatch('convex_hull')
        pts = convex_hull_xy(self.xy_parts)
        if not pts:
            return Geometry()
        if len(pts) == 1:
            return Geometry(C.ShapeType.POINT, pts[0])
        if len(pts) == 2:
            return Geometry(C.ShapeType.POLYLINE, ([0], list(pts)))
        pts.reverse()                   # 逆时针 -> Esri 的顺时针
        return Geometry(C.ShapeType.POLYGON, ([0], list(pts)))

    def simplify(self, tolerance: float,
                 preserve_topology: bool = False) -> 'Geometry':
        """Douglas–Peucker 简化,**逐 part** 做,首尾点必留。

        容差是点到**线段**的垂距上限(不是点到直线)—— 少了这一步,折线
        拐回去的那一截会被误判成"在线上"而整段删掉。Z / M 跟着保留点走,
        ``shape_type`` / ``has_z`` / ``has_m`` 都不变。

        ⚠️ **不支持拓扑保持。** ``preserve_topology=True`` 抛
        ``NotImplementedError``:GEOS 的拓扑保持简化要求结果不自交、且与其它
        几何的关系不变,那需要一整套 overlay 与鲁棒性,本库没有。参数留着
        是为了对齐 ``OGR_G_SimplifyPreserveTopology`` 的调用形态 ——
        "存在但明确拒绝",而不是静默忽略。

        ⚠️ 面的环简化后若不足 **3** 个顶点,该环**整体不简化** —— 宁可留着
        毛刺,也不产出一个非法的环。(内存里的环不存闭合点,所以下限是 3
        而不是 4。)
        """
        if preserve_topology:
            raise NotImplementedError(
                'simplify() 不支持拓扑保持:GEOS 的 preserveTopology 需要'
                '整套 overlay 与鲁棒性保证,本库没有。请用'
                'simplify(tolerance) 的非拓扑保持版本,或用 is_valid() '
                '自行检查结果。见 DESIGN.md §2.21。')
        self._require_no_multipatch('simplify')
        kind = self.kind
        if kind in ('point', 'multipoint', 'null'):
            return self._like(self.xy_parts, self.z_parts, self.m_parts)
        parts, kept = simplify_parts(self.xy_parts, tolerance,
                                     3 if kind == 'polygon' else 0)
        zs = self.z_parts
        ms = self.m_parts
        return self._like(parts,
                          None if zs is None else _gather(zs, kept),
                          None if ms is None else _gather(ms, kept))

    def segmentize(self, max_len: float) -> 'Geometry':
        """把长于 ``max_len`` 的线段等分,插入中间点。

        ``max_len <= 0`` 返回副本。对应 ``OGRGeometry::segmentize`` /
        ``OGRLineString::segmentize`` —— 这是 GDAL 里少数**纯 C++ 可逐字
        照抄**的算法之一(其余的谓词全在 GEOS 里)。

        ⚠️ 面的环要连"末点回首点"那一段一起切。内存里的环不存闭合点,而盘上
        是闭合的;这里临时补上闭合点、切完再削掉,所以结果不会多出一个重复
        顶点,也不需要调用方自己补。

        point / multipoint 没有线段,原样返回。
        """
        self._require_no_multipatch('segmentize')
        kind = self.kind
        if kind in ('point', 'multipoint', 'null') or max_len <= 0.0:
            return self._like(self.xy_parts, self.z_parts, self.m_parts)
        closed = kind == 'polygon'
        zs = self.z_parts
        ms = self.m_parts
        new_parts: List[Any] = []
        new_zs: Optional[List[Any]] = None if zs is None else []
        new_ms: Optional[List[Any]] = None if ms is None else []
        for i, a in enumerate(self.xy_parts):
            na, nz, nm = _segmentize_part(
                a, None if zs is None else zs[i],
                None if ms is None else ms[i], max_len, closed)
            new_parts.append(na)
            if new_zs is not None:
                new_zs.append(nz)
            if new_ms is not None:
                new_ms.append(nm)
        return self._like(new_parts, new_zs, new_ms)

    # -- 有效性 / 单纯性(**部分实现**) ---------------------------------------
    def is_valid(self) -> bool:
        """**部分实现**的几何有效性检查 —— 不要以为 ``True`` 就等于 OGC 有效。

        做了的:所有坐标是有限值;顶点数满足类型下限;每个环不自交(单个环
        两两线段判定,``O(n²)``);内环确实落在某个外环之内。multipatch 恒
        ``False``(它连环语义都没有,不做猜测)。

        **没做的**:多个外环互相重叠、环退化成零面积、洞与洞相交、multipoint
        顶点重合、自相切的环、以及任何 multipatch 的有效性。要 OGC 完整有效性
        得上 GEOS(``OGR_G_IsValid`` 就是转手给它的),本库做不到。逐条清单见
        ``_geometry_ops.is_valid_of`` 的 docstring。

        做成方法而不是 property,是因为它是 ``O(n²)`` 的 —— 属性取值不该藏
        一个平方级的开销。
        """
        return is_valid_of(self.kind, self.xy_parts, self._roles())

    def is_simple(self) -> bool:
        """**部分实现**的单纯性检查。

        点恒 ``True``;多点不重合才单纯;折线的每个 part 不自交;面恒 ``True``
        (OGC 规定面天然单纯,GEOS 也这么答);multipatch 恒 ``False``。

        **没做**:part 与 part 之间的相交。同样是 ``O(n²)``,同样做成方法。
        """
        return is_simple_of(self.kind, self.xy_parts)

    # -- 拓扑判定 -----------------------------------------------------------
    def relate(self, other: 'Geometry') -> str:
        """DE-9IM 矩阵:9 个字符,取值 ``F`` / ``0`` / ``1`` / ``2``。

        格子顺序 ``II IB IE / BI BB BE / EI EB EE``(``I`` = 内部、``B`` =
        边界、``E`` = 外部;**行是 self,列是 other**)。任一为空返回
        ``'FFFFFFFF2'``(与 JTS / GEOS 的 ``RelateOp`` 一致)。

        九条谓词全部由这张矩阵派生 —— 把它公开,是因为判错时能一眼看出错在
        哪一格;自己按矩阵写谓词的人也不用重算一遍。对应
        ``OGRGeometry::relate``。

        ⚠️ **这一档 GDAL 没有纯 C++ 实现可抄。** ``OGRGeometry::Relate`` 与
        ``Intersects`` / ``Contains`` / ``Touches`` / … 在 ``ogrgeometry.cpp``
        里全部委托给 GEOS,没编 GEOS 时 GDAL 直接报 ``CPLE_NotSupported`` 并
        返回 ``FALSE``。所以本库的口径是**对齐 OGC Simple Features 的 DE-9IM
        语义**(= GEOS 算出来的东西 = GDAL 带 GEOS 时返回的东西),实现是自己
        的。鲁棒性上的取舍见模块 docstring 与 DESIGN.md §2.21。
        """
        other = self._require_geometry(other)
        self._require_no_multipatch('relate')
        other._require_no_multipatch('relate')
        if self.is_empty or other.is_empty:
            return 'FFFFFFFF2'
        return relate_of(self.kind, self.xy_parts, self._roles(),
                         other.kind, other.xy_parts, other._roles())

    def _predicate(self, name: str, other: Any) -> bool:
        """九条谓词的公共入口:空值口径、包围盒快排、矩阵、模式匹配。"""
        other = self._require_geometry(other)
        self._require_no_multipatch(name)
        other._require_no_multipatch(name)
        a_empty = self.is_empty
        b_empty = other.is_empty
        if a_empty or b_empty:
            # 空几何的口径与 GEOS 一致:除了"不相交"恒真、"两边都空"才算
            # 等价,其余一律 False。注意"空 vs 非空"也是 disjoint —— 这是
            # DE-9IM 定义直接推出来的,不是特例。
            if name == 'disjoint':
                return True
            if name == 'equals':
                return a_empty and b_empty
            return False
        if name == 'intersects' and not envelope_intersects(
                self.envelope(), other.envelope()):
            # GDAL 在转手 GEOS 之前做的**唯一**一件事就是这句包围盒排除
            # (``ogrgeometry.cpp`` 的 ``OGRGeometry::Intersects``)。实际调用
            # 里不相交的要素对占绝大多数,这一句省掉的就是整个 relate。
            return False
        m = relate_of(self.kind, self.xy_parts, self._roles(),
                      other.kind, other.xy_parts, other._roles())
        return predicate_de9im(name, m, self.dimension, other.dimension)

    def intersects(self, other: 'Geometry') -> bool:
        """两者是否至少有一个公共点(内部或边界都算)。即 ``not disjoint``。"""
        return self._predicate('intersects', other)

    def disjoint(self, other: 'Geometry') -> bool:
        """两者的内部与边界**都不相交**(矩阵 ``FF*FF****``)。"""
        return self._predicate('disjoint', other)

    def contains(self, other: 'Geometry') -> bool:
        """``other`` 是否完全落在 self 之内(矩阵 ``T*****FF*``)。

        ⚠️ 是**严格**包含:``other`` 只碰到 self 的边界、或 ``other`` 整个贴在
        self 边界上,都不算 ``contains`` —— 那是 :meth:`covers`。一个几何不
        包含自己(``contains`` 非自反),但 ``covers`` 是。
        """
        return self._predicate('contains', other)

    def within(self, other: 'Geometry') -> bool:
        """self 是否完全落在 ``other`` 之内。等价于 ``other.contains(self)``。"""
        return self._predicate('within', other)

    def covers(self, other: 'Geometry') -> bool:
        """``contains`` 的弱化版:允许 ``other`` 整个贴在 self 的边界上。

        四种情形取并(矩阵 ``T*****FF*`` / ``*T****FF*`` / ``***T**FF*`` /
        ``****T*FF*``)—— 前两格("self 的内部**或边界**与 other 的内部
        相交")是它比 ``contains`` 松的那一点。
        """
        return self._predicate('covers', other)

    def covered_by(self, other: 'Geometry') -> bool:
        """self 是否被 ``other`` 覆盖。等价于 ``other.covers(self)``。"""
        return self._predicate('covered_by', other)

    def touches(self, other: 'Geometry') -> bool:
        """相接:至少有一处公共点,但两者的**内部不相交**。

        点相接、线在端点相接、两个面共一条边都算;两个面只共一个角点也算。
        对称谓词。
        """
        return self._predicate('touches', other)

    def crosses(self, other: 'Geometry') -> bool:
        """交叉:交集维度**低于**两者中较低的那一维,且双方内部都参与。

        线穿线、线穿面是典型;点不能交叉任何东西。⚠️ 两个面**不算**交叉
        (它们的交集是面) —— 那是 :meth:`overlaps`。对称谓词。
        """
        return self._predicate('crosses', other)

    def overlaps(self, other: 'Geometry') -> bool:
        """部分重叠:交集与两者**同维**,且谁都不包含谁。

        面的部分交叠、线的部分共线交叠、多点有公共点且各有独有点都算。
        ⚠️ 与 :meth:`crosses` 的分工:交集维度**同维**是 overlaps,更**低**
        才是 crosses。对称谓词。
        """
        return self._predicate('overlaps', other)

    def equals(self, other: 'Geometry') -> bool:
        """**拓扑等价**(OGC ``ST_Equals``,矩阵 ``T*F**FFF*``)。

        两个几何占同一块空间就算等价,**不看顶点怎么摆**::

            A.equals(B)          # 顶点不同、形状相同 -> True
            A.exactly_equals(B)  # 结构比较 -> False

        ⚠️ 这与 GDAL 的 ``OGRGeometry::Equals`` **不是一回事** —— 那个是
        结构比较(``ST_OrderingEquals``),对应本库的 :meth:`exactly_equals`。
        GDAL 里真正的空间等价是 ``OGR_G_Equals`` 之外的 GEOS 路径。两个名字
        分开就是为了不让人搞混。对称谓词。
        """
        return self._predicate('equals', other)

    def exactly_equals(self, other: 'Geometry') -> bool:
        """**结构**比较 —— 对应 GDAL 的 ``OGRGeometry::Equals``。

        先比 ``shape_type`` / ``has_z`` / ``has_m``,再比 part 数、每个 part 的
        顶点数,最后逐点比坐标(XY,带 Z 时连 Z),**顺序必须完全一致**。

        GDAL 的文档把这条写得很明确 —— 它实现的是 SQL/MM 的
        ``ST_OrderingEquals()``:

            "The comparison is done in a structural way, that is to say that
            the geometry types must be identical, as well as the number and
            ordering of sub-geometries and vertices."

        也就是"WKT / WKB 表示相同",**不是**空间等价 —— 空间等价是
        :meth:`equals`(``ST_Equals``)。所以环的**起点旋转、整体反向、外环与
        洞的次序**在这里**都算不等**::

            A = Geometry.from_wkt('POLYGON((0 0,10 0,10 10,0 10,0 0))')
            # 在顶边上多插一个顶点,占的还是同一块地
            B = Geometry.from_wkt('POLYGON((0 0,10 0,10 10,5 10,0 10,0 0))')
            A.equals(B)             # True  —— 拓扑等价
            A.exactly_equals(B)     # False —— 顶点数不同,结构不同
            A.exactly_equals(A.convex_hull())   # False —— 顶点集相同但顺序变了

        ⚠️ 与 :meth:`__eq__` 的关系:``__eq__`` 比的是 ``shape_type`` + Z/M
        标记 + ``coordinates`` 元组相等,基本是同一件事,但它**会物化坐标**。
        ``exactly_equals`` 是为对齐 OGR 的 ``Equals`` 单独给的;``__eq__``
        继续服务"同一个几何解两遍是否一致"这类内部对拍。

        ⚠️ 与 GDAL 一致的两点:类型(含 M 标记)必须相同;但逐点比较时
        **M 的数值不参与**(GDAL 的说明是 "sub-geometry equality tests ignore
        the M dimension")。
        """
        other = self._require_geometry(other)
        if (self.shape_type != other.shape_type
                or self.has_z != other.has_z
                or self.has_m != other.has_m):
            return False
        if self.is_empty or other.is_empty:
            return self.is_empty and other.is_empty
        pa = self.xy_parts
        pb = other.xy_parts
        if len(pa) != len(pb):
            return False
        za = self.z_parts if self.has_z else None
        zb = other.z_parts if other.has_z else None
        for i in range(len(pa)):
            a = pa[i]
            b = pb[i]
            # ``array('d')`` 的 ``==`` 是逐元素的 C 层比较,不用自己写循环
            if len(a) != len(b) or a != b:
                return False
            if za is not None and za[i] != zb[i]:
                return False
        return True

    # -- GeoJSON ------------------------------------------------------------
    @property
    def __geo_interface__(self) -> Any:
        """``__geo_interface__`` 协议 —— geopandas / folium / shapely 认这个。

        返回 GeoJSON 的 ``geometry`` 对象(dict)。口径(环必须闭合、不按
        RFC 7946 重绕、Z 写在第三个分量、M 丢弃、multipatch 抛
        ``NotImplementedError``)见 ``_esri_geometry.to_geo_dict``。
        """
        from ._esri_geometry import to_geo_dict
        return to_geo_dict(self)

    def to_geojson(self) -> str:
        """几何对象的 GeoJSON 串(不含 Feature 外壳)。"""
        from ._esri_geometry import to_geojson
        return to_geojson(self)

    def wkt(self) -> str:
        """转成 OGC WKT,便于人读/调试。"""
        from ._esri_geometry import to_wkt
        return to_wkt(self)

    @classmethod
    def from_wkt(cls, wkt: str, has_z: bool = False, has_m: bool = False) -> 'Geometry':
        """从 OGC WKT 构造。仅支持 POINT/LINESTRING/POLYGON/MULTIPOINT。"""
        from ._esri_geometry import from_wkt
        return from_wkt(wkt, has_z=has_z, has_m=has_m)

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, Geometry):
            return NotImplemented
        return (self.shape_type == other.shape_type
                and self.has_z == other.has_z
                and self.has_m == other.has_m
                and self.coordinates == other.coordinates)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (f'Geometry(shape_type={self.shape_type!r}, '
                f'has_z={self.has_z!r}, has_m={self.has_m!r})')


def _tuple_part_groups(geom: Geometry) -> List[List[Tuple[float, ...]]]:
    """把非 flat 几何的 ``coordinates`` 拆成"每个 part 一个点列表"。

    只服务 :attr:`Geometry.xy_parts` 等访问器在**没有 flat 存储**时的
    兜底(point 几何,或者调用方自己用元组手工构造的几何)。解码器出来的
    几何永远走 flat,不会到这儿。
    """
    kind = geom.kind
    coords = geom.coordinates
    if kind == 'point':
        return [[coords] if coords else []]
    if kind == 'multipoint':
        return [list(coords or [])]
    if coords is None:
        # NULL 几何,或者没有解码坐标的 multipatch。返回空表而不是让下面那
        # 个解包炸掉 —— is_empty / envelope / point_count 都要走这条路径。
        return []
    starts, points = coords
    n = len(starts)
    return [points[starts[i]:(starts[i + 1] if i + 1 < n else len(points))]
            for i in range(n)]


def _scalar_parts_from_tuples(geom: Geometry, idx: int) -> List[Any]:
    """非 flat 几何:抽出每个点的第 ``idx`` 个分量,按 part 拼成 ``array('d')``。"""
    return [array('d', (p[idx] for p in grp))
            for grp in _tuple_part_groups(geom)]


def _xy_of_tuples(grp: Sequence[Tuple[float, ...]]) -> Any:
    """把一组的 ``[(x, y[, z][, m]), ...]`` 压成交错的 ``array('d')``。

    ⚠️ **只取前两个分量。** 直接 ``chain.from_iterable`` 会把 Z / M 也拼进去:
    2D 的几何看着没事(元组里本来就只有 x、y),带 Z 的会**整体错位** ——
    实测 ``LINESTRING Z(0 0 0, 4 0 4)`` 会被读成 3 个点 ``(0,0) (0,4) (0,4)``。
    解码器出来的几何永远走 flat,所以这条路径只有 ``from_wkt`` 和手工构造的
    几何会走,但错起来是静默的。
    """
    pts = list(grp)
    if not pts:
        return array('d')
    if len(pts[0]) == 2:
        return array('d', chain.from_iterable(pts))
    out = array('d')
    for p in pts:
        out.append(p[0])
        out.append(p[1])
    return out


def _gather(seqs: Sequence[Any], kept: Sequence[Sequence[int]]) -> List[Any]:
    """按"保留下标表"从每个 part 的数组里挑值,拼成新的 ``array('d')``。

    Z / M 跟 XY 走的时候用 —— ``simplify_parts`` 只回传 XY 和下标表,
    标量得照同一张表自己挑。
    """
    return [array('d', (a[j] for j in idx)) for a, idx in zip(seqs, kept)]


def _segmentize_part(a: Sequence[float], z: Any, m: Any, max_len: float,
                     closed: bool):
    """切一块坐标数组,返回 ``(xy, z, m)`` 三个新数组(``z`` / ``m`` 可为 None)。

    ``closed`` 表示这是一条**面环**:内存里的环不存闭合点(闭合段由导出时
    补),但"末点回首点"那一段是真实存在的边,segmentize 必须切到它。

    做法是临时补上闭合点 -> 按闭合折线切 -> 把补出来的那个末点削掉。
    ⚠️ 削掉的那一个不能改成"精确的首点"再留用:``segmentize_xy`` 的末点是
    ``x1 + 1.0 * (x2 - x1)`` 算出来的,浮点上不保证与 ``x2`` 逐位相等,留着
    会让环多出一个"几乎重合"的顶点,面积和周长都跟着偏一点。
    """
    if closed and len(a) >= 4:
        a2 = array('d', a)
        a2.append(a[0])
        a2.append(a[1])
        z2 = None if z is None else array('d', list(z) + [z[0]])
        m2 = None if m is None else array('d', list(m) + [m[0]])
        na, src = segmentize_xy(a2, max_len)
        nz = interpolate_scalars(z2, src)
        nm = interpolate_scalars(m2, src)
        del na[-2:]
        if nz is not None:
            del nz[-1:]
        if nm is not None:
            del nm[-1:]
        return na, nz, nm
    na, src = segmentize_xy(a, max_len)
    return na, interpolate_scalars(z, src), interpolate_scalars(m, src)
