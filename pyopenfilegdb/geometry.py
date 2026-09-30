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
from ._buffer_ops import buffer_parts
from ._overlay_ops import (
    DIFFERENCE,
    INTERSECTION,
    SYMDIFFERENCE,
    UNION,
    overlay_parts,
)
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

    ``geometrycollection`` **没有坐标** —— 四种坐标属性一律抛 ``TypeError``,
    子几何走 :meth:`geometry_at` / :meth:`geometries`。它是本库唯一一个
    **不是 Esri 类型**的几何(FileGDB 的 ``ShapeType`` 里没有这一档),
    只作为 overlay 的结果出现,**写不进 .gdb** —— 见
    :meth:`geometry_collection` 与 ``DESIGN.md §2.24``。

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
                 _shells: Optional[List[bool]] = None,
                 _geoms: Optional[Sequence['Geometry']] = None) -> None:
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
        #: ``GEOMETRYCOLLECTION`` 的子几何。**只有** ``shape_type ==
        #: GEOMETRYCOLLECTION`` 时非 ``None`` —— 这个类型不是 Esri 的,
        #: 盘上读不出来,只能由 overlay 或 :meth:`geometry_collection` 造出来。
        #: 用构造器而不是直接调 ``__init__``,是为了走那一层的检查。
        self._geoms = None if _geoms is None else list(_geoms)
        #: :meth:`_groups` 的缓存(环组装结果,见 ``_geometry_ops.organize_polygons``)
        self._groups = None
        #: :meth:`envelope` 的缓存。包围盒是**精确值**,算一次就够,
        #: 而拓扑判定每个谓词都要用它做快速排除。
        self._env = _UNSET

    # -- 构造 ---------------------------------------------------------------
    @classmethod
    def geometry_collection(cls, geoms: Sequence['Geometry']) -> 'Geometry':
        """造一个 ``GEOMETRYCOLLECTION``。

        ⚠️ **本库唯一会用到的"不是 Esri 类型"的几何**,所以有三条硬规矩:

        1. **它写不进 .gdb。** FileGDB 的 ``ShapeType`` 里没有这一档,
           :func:`~pyopenfilegdb._esri_geometry.encode_geometry` 会明确报错
           (不是悄悄写个 NULL 出去)。
        2. **坐标属性一律抛 ``TypeError``** —— :attr:`coordinates` /
           :attr:`xy_parts` / :attr:`z_parts` / :attr:`m_parts`。一个 GC 的
           "坐标"没有单一含义(OGR 对 GC 也不给坐标),拼一份出来只会让调用方
           把它当成单一几何读。
        3. 子几何**原样保留** —— 空的子几何不丢,嵌套的 GC 不摊平。要摊平自己
           做(overlay 的输入摊平是分开的一步,见 ``_overlay_ops``)。

        :param geoms: 子几何序列,每一项必须是 :class:`Geometry`。
        """
        kids: List['Geometry'] = []
        for i, g in enumerate(geoms):
            if not isinstance(g, Geometry):
                raise TypeError(
                    f'geometry_collection() 第 {i + 1} 个子几何不是 Geometry,'
                    f'得到 {type(g).__name__}')
            kids.append(g)
        # ``has_z`` / ``has_m`` 取"任一子几何有"(与"至少一边带 Z 就带 Z"的
        # overlay 口径一致)。⚠️ 这两个标志**不写进 WKT 的类型名后缀** ——
        # 见 ``_esri_geometry.to_wkt`` 里为什么。
        return cls(C.ShapeType.GEOMETRYCOLLECTION, None,
                   any(g.has_z for g in kids), any(g.has_m for g in kids),
                   _geoms=kids)

    @property
    def geometry_count(self) -> int:
        """子几何个数;非 ``GEOMETRYCOLLECTION`` 抛 ``TypeError``。

        ⚠️ 故意不返回 0 —— 一个面"有 0 个子几何"和"它根本装不下子几何"是
        两件事,静默返回 0 会让 ``for i in range(g.geometry_count)`` 这种
        写法在面上得到一个空循环而不是一个错误。
        """
        self._require_collection('geometry_count')
        return len(self._geoms or ())

    def geometry_at(self, index: int) -> 'Geometry':
        """第 ``index`` 个子几何(支持负数下标);非 GC 抛 ``TypeError``。"""
        self._require_collection('geometry_at')
        try:
            return (self._geoms or ())[index]
        except IndexError:
            raise IndexError(
                f'子几何下标 {index} 越界(共 {self.geometry_count} 个)'
            ) from None

    def geometries(self) -> List['Geometry']:
        """子几何的**浅拷贝**列表;非 GC 抛 ``TypeError``。

        给的是新列表,原地 ``append`` 不会改到这个几何(子几何对象本身还是
        同一批 —— 几何是按值用的,不该有"改一个动全身"的语义)。
        """
        self._require_collection('geometries')
        return list(self._geoms or ())

    def _require_collection(self, op: str) -> None:
        if self.shape_type != C.ShapeType.GEOMETRYCOLLECTION:
            raise TypeError(
                f'{op}() 只对 GEOMETRYCOLLECTION 有意义,而这是一个 '
                f'{self.kind}')

    def _no_coords(self, name: str) -> None:
        """GC 的坐标入口一律挡在这里,消息里说清"为什么不给"。

        见 :meth:`geometry_collection` 第 2 条:GC 的坐标没有单一含义,
        拼一份出来比报错危险得多。
        """
        if self.shape_type == C.ShapeType.GEOMETRYCOLLECTION:
            raise TypeError(
                f'GEOMETRYCOLLECTION 没有 {name} —— 一个几何集合的坐标没有'
                f'单一含义(OGR 对 GEOMETRYCOLLECTION 也不给坐标),'
                f'拼出来会被当成单一几何读。请逐个取 geometry_at(i).{name},'
                f'或先用 geometries() 拿到子几何列表。')

    # -- 坐标 ---------------------------------------------------------------
    @property
    def coordinates(self) -> Any:
        """公开的元组形态;**第一次访问时才物化**,之后缓存。

        ⚠️ 物化要在 Python 里建逐点的 ``float`` / ``tuple``,量级是
        ~19 ns/顶点(整层 ~1.7 s)。只需要坐标数值的话用 :attr:`xy_parts`。

        ⚠️ ``GEOMETRYCOLLECTION`` 抛 ``TypeError``,见 :meth:`_no_coords`。
        """
        self._no_coords('coordinates')
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

        ⚠️ ``GEOMETRYCOLLECTION`` 抛 ``TypeError``,见 :meth:`_no_coords`。
        """
        self._no_coords('xy_parts')
        f = self._flat
        if f is not None:
            return f.xy
        return [_xy_of_tuples(g) for g in _tuple_part_groups(self)]

    @property
    def z_parts(self) -> Optional[List[Any]]:
        """与 :attr:`xy_parts` 逐块对应的 Z 数组;没有 Z 时是 ``None``。"""
        self._no_coords('z_parts')
        if not self.has_z:
            return None
        f = self._flat
        if f is not None and f.zs is not None:
            return f.zs
        return _scalar_parts_from_tuples(self, 2)

    @property
    def m_parts(self) -> Optional[List[Any]]:
        """与 :attr:`xy_parts` 逐块对应的 M 数组;没有 M 时是 ``None``。"""
        self._no_coords('m_parts')
        if not self.has_m:
            return None
        f = self._flat
        if f is not None and f.ms is not None:
            return f.ms
        return _scalar_parts_from_tuples(self, 3 if self.has_z else 2)

    # -- 杂项 ---------------------------------------------------------------
    @property
    def kind(self) -> str:
        """point / polyline / polygon / multipoint / multipatch /
        geometrycollection / null。"""
        return C.ShapeType.base_kind(self.shape_type)

    @property
    def is_empty(self) -> bool:
        # 注意**不要**写 self.coordinates is None —— 那会把 flat 几何整层
        # 物化一遍,只为判一个空。
        if self.shape_type == C.ShapeType.GEOMETRYCOLLECTION:
            # 空 GC = **每个**子几何都空(与 OGC / GEOS 一致)。空列表也是空。
            # 递归深度只取决于输入的嵌套深度,不是顶点数。
            return not self._geoms or all(g.is_empty for g in self._geoms)
        return self.shape_type == C.ShapeType.NULL or (
            self._coords is None and self._flat is None)

    # -- 描述 ---------------------------------------------------------------
    @property
    def dimension(self) -> int:
        """拓扑维:点/多点 ``0``、折线 ``1``、面/multipatch ``2``、null ``0``。

        对应 ``OGRGeometry::getDimension``(``wkbNone`` 走基类返回 0;
        multipatch 按 ``wkbPolyhedralSurface`` 算 2)。

        ``GEOMETRYCOLLECTION`` 取**子几何里最大的那一维**(OGC 的定义,
        GEOS 也是这么答的);空 GC 是 ``0``。**不物化坐标。**
        """
        if self.shape_type == C.ShapeType.GEOMETRYCOLLECTION:
            return max((g.dimension for g in self._geoms or ()), default=0)
        return dimension_of(self.kind)

    @property
    def point_count(self) -> int:
        """**所有 part** 的顶点数之和。**不物化坐标。**

        ⚠️ 与 OGR 分歧:``OGRPolygon::getNumPoints`` 只数外环。这里给的是
        "这条几何到底有多大",那才是调用方要问的。要单个数某一环就自己数
        ``len(self.xy_parts[i]) // 2``。

        GC 是各子几何之和。
        """
        if self.shape_type == C.ShapeType.GEOMETRYCOLLECTION:
            return sum(g.point_count for g in self._geoms or ())
        return point_count_of(self.xy_parts)

    @property
    def part_count(self) -> int:
        """part 个数。**不物化坐标。** multipoint 恒为 1(point 也是 1)。

        ⚠️ GC 给的是**子几何个数**(不是子几何的 part 之和)—— 与
        :attr:`geometry_count` 同义。要顶点 / part 的总量请用
        :attr:`point_count`。
        """
        if self.shape_type == C.ShapeType.GEOMETRYCOLLECTION:
            return len(self._geoms or ())
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

        两种情形都要求恰好**一个** part。``GEOMETRYCOLLECTION`` 恒 ``False``
        (绕向本来就是"一个环"的属性)。
        """
        if self.shape_type == C.ShapeType.GEOMETRYCOLLECTION:
            return False
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
        逐环问请自己取 ``xy_parts[i]``)。``GEOMETRYCOLLECTION`` 同样 ``False``。
        """
        if self.shape_type == C.ShapeType.GEOMETRYCOLLECTION:
            return False
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
        if kind == 'geometrycollection':
            # GC **没有坐标**,"换一批坐标"这个口子对它没有意义。全库唯一会
            # 走到这里的是"空几何原样复制"(``coordinates_system.transform_geometry``
            # 的空分支 —— 非空 GC 在那之前就被逐子几何那条路接走了)。
            #
            # ⚠️ 这里**不能**让它掉到下面 ``if not parts:`` 那一档:那条会造出
            # ``_geoms=None`` 的空 GC,``is_empty`` 虽然还是 True,但
            # ``geometry_count`` 会从"n 个空子几何"变成 0,``exactly_equals``
            # 也跟着变 —— 结构被悄悄丢了。子几何列表原样抄一遍就没有这个问题
            # (子几何都是空的,本来也无需转换)。
            return Geometry.geometry_collection(list(self._geoms or ()))
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

        ``GEOMETRYCOLLECTION`` 取各子几何包围盒的**并**(全部为空时为
        ``None``)。这也是 :meth:`intersects` 那一步包围盒快排能对 GC 生效的
        前提。
        """
        if self._env is _UNSET:
            if self.shape_type == C.ShapeType.GEOMETRYCOLLECTION:
                boxes = [e for e in (g.envelope() for g in self._geoms or ())
                         if e is not None]
                self._env = None if not boxes else (
                    min(b[0] for b in boxes), min(b[1] for b in boxes),
                    max(b[2] for b in boxes), max(b[3] for b in boxes))
            else:
                self._env = envelope_of(self.xy_parts)
        return self._env

    def area(self) -> float:
        """面积;非面几何恒为 ``0.0``(与 ``OGRGeometry::get_Area`` 一致)。

        面是 **外环面积之和 减 内环面积之和**(不是逐环取绝对值相加 —— 那样
        会把洞加进去)。Z / M 不参与。**不物化坐标。**

        ⚠️ ``GEOMETRYCOLLECTION`` 给的是**子几何面积之和**,即 **GEOS**
        的口径。这里**与 GDAL 分岔**:``OGRGeometryCollection::get_Area()``
        直接返回 **0**(它没有覆写基类),GDAL 的 GC 面积永远是 0。本库跟
        GEOS —— 一个"面 + 线"的集合返回那个面的面积才有意义,返回 0 是
        把"没实现"伪装成"面积为零"。对拍时以 GEOS 为准(见
        ``DESIGN.md §2.24``)。
        """
        if self.shape_type == C.ShapeType.GEOMETRYCOLLECTION:
            return sum(g.area() for g in self._geoms or ())
        return area_of(self.kind, self.xy_parts, self._roles())

    def length(self) -> float:
        """长度:折线是各段之和,面是**周长**(各环之和),点/多点 ``0.0``。

        对应 ``OGR_G_Length``(面走 ``OGRCurvePolygon::get_Length``,即周长
        而不是"边界的长度之和"以外的东西)。Z / M 不参与。**不物化坐标。**

        ``GEOMETRYCOLLECTION`` 是各子几何长度之和(同 :meth:`area`,跟
        GEOS 不跟 GDAL)。
        """
        if self.shape_type == C.ShapeType.GEOMETRYCOLLECTION:
            return sum(g.length() for g in self._geoms or ())
        return length_of(self.kind, self.xy_parts)

    def centroid(self) -> Optional[Tuple[float, float]]:
        """质心 ``(x, y)``;空几何返回 ``None``。

        * **面** —— 按面积加权(洞的权重取负),总权重 ≤ 0 时退化成顶点平均;
        * **点 / 多点 / 折线** —— **顶点算术平均**。

        后一条刻意跟 GDAL(``OGRGeometry::Centroid`` 与 ``OGRSimpleCurve::
        Centroid`` 都是顶点平均),与 GEOS 不同 —— GEOS 的线质心按**长度**加权。

        ⚠️ 返回**元组**,不像 ``OGR_G_Centroid`` 那样再去填一个点几何:
        一个坐标不值得为它建一个对象。

        ⚠️ ``GEOMETRYCOLLECTION`` **不支持**(``NotImplementedError``):
        GEOS 的 GC 质心要按**最高维分量**加权(先取最高维的子几何再各自加权),
        那一套(``Centroid``)本库没抄,凭"把子几何质心算术平均"猜会给出
        语义不同的数 —— 宁可明说。
        """
        self._require_not_collection('centroid')
        return centroid_of(self.kind, self.xy_parts, self._roles())

    def _require_not_collection(self, op: str) -> None:
        """GC 上"没有对应语义"的那几档统一挡在这里。

        ⚠️ 这是**明确的拒绝**,不是"还没做":每一条都在消息里给出理由。
        静默返回一个假的数(比如面积 0、质心取平均)比报错糟得多 ——
        调用方分不出"算出来是 0"和"没算"。
        """
        if self.shape_type == C.ShapeType.GEOMETRYCOLLECTION:
            raise NotImplementedError(
                f'{op}() 不支持 GEOMETRYCOLLECTION:{self._collection_reason(op)}'
                f'见 DESIGN.md §2.24。')

    @staticmethod
    def _collection_reason(op: str) -> str:
        return {
            'centroid': 'GEOS 的 GC 质心要按最高维分量加权(先取最高维的子几何'
                        '再各自加权),那一套没抄;把子几何质心算术平均会得到'
                        '语义不同的数。请自己按维取子几何再求质心。',
            'distance': 'GC 的距离要跨维度比较(点到线、线到面、面到面)并对'
                        '所有子几何取最小,还得先判"整体是否相交";本库的'
                        'distance 只支持同维度的一对几何。请逐个子几何算再取'
                        '最小。',
            'convex_hull': 'GC 的凸包要先把所有子几何的顶点合起来再求包,'
                           '本库的 convex_hull 走的是单几何的顶点序列。'
                           '请自己合并 geometry_at(i).xy_parts 的顶点。',
            'simplify': 'GC 的简化要逐子几何做,而且子几何的维度不同、'
                        '容差语义也不同(线的垂距 vs 面的环)。'
                        '请逐个子几何调 simplify()。',
            'segmentize': 'GC 的加密要逐子几何做,而且断开 / 闭合的规则不同'
                          '(面的环要连末点回首点那段一起切)。'
                          '请逐个子几何调 segmentize()。',
            'buffer': '把多个子几何的偏移曲线喂进同一个曲线集'
                      '(JTS BufferCurveSetBuilder.addCollection)本库没实现。'
                      '请逐个子几何 buffer 再 union。',
        }.get(op, '这个运算在几何集合上没有单一语义。')

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
        self._require_not_collection('distance')
        other._require_not_collection('distance')
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
        self._require_not_collection('convex_hull')
        pts = convex_hull_xy(self.xy_parts)
        if not pts:
            return Geometry()
        if len(pts) == 1:
            return Geometry(C.ShapeType.POINT, pts[0])
        if len(pts) == 2:
            return Geometry(C.ShapeType.POLYLINE, ([0], list(pts)))
        pts.reverse()                   # 逆时针 -> Esri 的顺时针
        return Geometry(C.ShapeType.POLYGON, ([0], list(pts)))

    def buffer(self, distance: float, quad_segs: int = 8,
               cap: Any = 'round', join: Any = 'round',
               mitre_limit: float = 5.0,
               single_sided: bool = False) -> 'Geometry':
        """缓冲区(``buffer`` / ``OGR_G_Buffer``)。结果恒为 2D 的 ``POLYGON``。

        ⚠️ **这一档不是"严格按 GDAL 实现"的。** 全库其余部分都能在 GDAL 的
        C++ 里找到逐字对照,唯独 ``OGRGeometry::Buffer`` 是一句转发 —— GDAL 把
        整个算法交给 GEOS,自己一行都没有。所以本方法按 **GEOS 的上游 JTS**
        逐段复刻(`operation/buffer/*`),每一处都在 ``_buffer_ops`` 的注释里
        标了对应的 JTS 类与方法。细节、以及**以 GEOS 为准**的那几处分岔,
        见 DESIGN.md §2.23。和谓词那一档是同一个毛病:GDAL 那边没有东西可抄。

        参数
        ----
        ``distance``
            缓冲宽度。**可以为负**(侵蚀);``0`` 走 JTS 的 ``bufferByZero``:
            面返回去重后的自身副本,点/线返回**空**。
        ``quad_segs``
            四分之一圆的等分段数,默认 ``8``(JTS ``QUADRANT_SEGMENTS``,也是
            ``shapely.buffer`` 模块函数的默认值)。圆角/圆帽的顶点数 = ``4 *
            quad_segs``。⚠️ ``shapely.geometry.Point(0,0).buffer(1)`` 的实例方法
            默认是 **16**,对拍时要显式传参,不然顶点数差一倍。
        ``cap`` / ``join``
            ``'round'`` / ``'flat'`` / ``'square'`` 与 ``'round'`` / ``'mitre'``
            / ``'bevel'``;也接受整数 ``1/2/3``(GEOS 的 ``cap_style`` /
            ``join_style``),以及别名 ``'butt'`` / ``'miter'``。
        ``mitre_limit``
            斜接角的长度上限,单位是 ``distance`` 的倍数(默认 ``5.0``)。
            超过就改画限长斜接(不是简单退化成倒角 —— 那是 JTS 1.19 之前的
            老做法,尖角不够尖时会削掉一大块面积)。
        ``single_sided``
            只在一侧生成缓冲(``distance`` 的符号决定哪一侧:正为左、负为右)。
            ⚠️ OGR 的 ``OGR_G_Buffer`` 没有这个开关,是 GEOS 的扩展。
            ⚠️ **闭合输入(面 / 闭合折线)上与 GDAL/GEOS 不同**:GEOS 在单侧管线
            之后还有一步"把输入边界与结果边界 overlay 求并,再 polygonize、
            只留最大的面"的后处理,本库按 **JTS** 原样返回单侧缓冲。开放折线
            上两边一致。见下面「诚实边界」第 5 条。

        口径
        ----
        * **恒为 2D**,Z / M 一律丢掉(GEOS 的 buffer 也不产 Z),结果恒
          ``has_z = has_m = False``;与 :meth:`convex_hull` 同口径。
        * 结果的环是 **Esri 约定:外环顺时针、内环逆时针**,与全库一致。
        * ``multipatch`` 输入抛 ``NotImplementedError``(理由同谓词一档)。
        * ``buffer(0)`` 之外的**空结果**是"空 POLYGON",不是 ``None``。

        诚实边界
        --------
        1. **不做 snap-rounding。** JTS ``BufferOp`` 的最后一级降级重试用
           ``SnapRoundingNoder``,本库整体口径是不做(见 ``_geometry_ops``
           模块 docstring)。前两级(原精度 + 逐级降精度重算)**照抄**,第三级
           **降级为直接放弃** —— 于是"只有靠 snap-rounding 才能救回来"的退化
           构型,GEOS 会给结果而本库给空多边形。
        2. **不承诺与 GEOS 逐位相同。** 交点用普通双精度而非自适应精算术,
           偏移端点的浮点末位可能差一位。对拍见 ``tools/verify_buffer.py``
           (容差判定 + "逐位相同用例数"统计),不靠嘴说。
        3. 节点等同是**精确坐标**比较,三线共点可能产生极短边 ——
           这是 JTS 同款行为,不是本库偷懒。
        4. 性能无保证:noder 是 O((n+k) log n) 级扫描但常数不小。
        5. **``single_sided`` 在闭合输入上跟 JTS,不跟 GEOS。** 理由见参数表那
           一条;实测 10×10 方框 ``d=1, single_sided=True``:GEOS 给 100(方框
           本身那块面),本库给 143.12(方框外那圈单侧带)。两者都"对",是两套
           语义 —— 本库不做那步后处理,因为它需要 overlay 引擎 + Polygonizer。
           ⚠️ **2026-09-30 补注**:overlay 那一档后来做了(见 :meth:`difference`
           等),这一步却**仍旧不做** —— 还差一个 ``Polygonizer``
           (JTS ``operation/polygonize``),而且"面多于一个时只留最大面"是
           **丢几何**的取舍,不是无副作用的后处理。详见 ``DESIGN.md`` §2.23 第 5 条。
           ``tools/verify_buffer.py`` 把这类对**显式列出原因并计数**排除。
        """
        self._require_no_multipatch('buffer')
        self._require_not_collection('buffer')
        res = buffer_parts(
            self.kind, self.xy_parts, self._shells, float(distance),
            int(quad_segs), cap, join, float(mitre_limit), bool(single_sided))
        if res is None:
            return Geometry(C.ShapeType.POLYGON, None)
        parts, shells = res
        return Geometry(C.ShapeType.POLYGON, None, False, False,
                        _flat=_FlatCoords(parts, None, None),
                        _shells=list(shells))

    # -- overlay ------------------------------------------------------------
    def _overlay(self, other: Any, op: int, name: str) -> 'Geometry':
        """四个 overlay 算子的**共用**入口:入参检查 → 分派 → 装配。

        Z 的两条规则(与 GEOS 逐条实测对过):

        * **只要有一边带 Z,结果就带 Z** —— ⚠️ 不是"两边都带才有";
        * 每个输出顶点的 Z = 节点化时各条**经过该点的源线段**插值出来的 Z 的
          平均(NaN 不参与);一个 z 来源都没有的顶点,由 ``_ElevationGrid``
          (JTS ``ElevationModel``:3×3 网格取格内输入顶点 z 的平均)兜底,
          **兜不到就是 NaN**(GEOS 也是这样)。

        M **一律丢弃** —— GEOS 的 overlay 不产 M,FileGDB 的 M 与这套算法无关。

        ``GEOMETRYCOLLECTION`` 输入
        ---------------------------
        含 GC 的输入**不走** ``OverlayNG``(GEOS 的 ``HeuristicOverlay`` 把它
        转给 ``StructuredCollection``),本库按"摊平成齐次部分"处理 ——
        逐条见 :func:`_flatten_overlay_input`。⚠️ 这与 GEOS 在**维度混杂**的
        GC 上不是一回事,那一档本库明说 ``NotImplementedError``。
        """
        other = self._require_geometry(other)
        self._require_no_multipatch(name)
        other._require_no_multipatch(name)
        a = _flatten_overlay_input(self, name)
        b = _flatten_overlay_input(other, name)
        res = overlay_parts(
            a.kind, a.xy_parts, a._shells,
            b.kind, b.xy_parts, b._shells, op,
            a.z_parts if a.has_z else None,
            b.z_parts if b.has_z else None)
        return _overlay_geometry(res)

    def difference(self, other: 'Geometry') -> 'Geometry':
        """差集 ``A ∖ B``(``OGRGeometry::Difference`` / ``OGR_G_Difference``)。

        ⚠️ **这一档不是"严格按 GDAL 实现"的。** 与谓词、:meth:`buffer` 同一个
        毛病:``OGRGeometry::Difference`` 在 GDAL 里**是一句转发给 GEOS 的话**,
        GDAL 自己一行算法都没有。本库按 **GEOS 的上游 JTS**
        (``operation/overlayng/``)复刻,每一处都在 ``_overlay_ops`` 的注释里
        标了对应的 JTS 类与方法。细节、以及本库**刻意不做**的那几档,见
        DESIGN.md §2.24。

        口径
        ----
        * **Z 保留、M 丢弃。** 只要有一边带 Z,结果就带 Z(⚠️ 不是"两边都带
          才有"),新节点的 Z 按 :meth:`_overlay` 记的那两条规则来;M 一律丢
          —— GEOS 的 overlay 也不产 M。⚠️ 与 :meth:`buffer` / :meth:`convex_hull`
          那两档**不同**:它们的"恒为 2D"照旧,overlay 这一档是带 Z 的。
        * 结果环是 **Esri 约定:外壳顺时针、洞逆时针**,与全库一致。
        * **空结果按维度定型,不是 ``None``。** 规则照 JTS ``OverlayUtil`` 的
          ``resultDimension``:``intersection`` 取两输入维度的小者、
          ``union`` / ``symmetric_difference`` 取大者、``difference`` 取 A 的
          维;再按 0 → ``POINT EMPTY``、1 → ``LINESTRING EMPTY``、2 →
          ``POLYGON EMPTY`` 定型。所以 ``A ⊂ B`` 时 ``A.difference(B)`` 是
          ``POLYGON EMPTY``。⚠️ 本段**只收面 × 面的输入**,所以此刻四个算子的
          空**都**是 ``POLYGON EMPTY``;线和点那一档(见 DESIGN.md §2.24)接上
          之后才会出现 ``LINESTRING EMPTY`` / ``POINT EMPTY``。
        * ``multipatch`` 输入抛 ``NotImplementedError``(两个几何都查)。

        诚实边界
        --------
        1. **不做 snap-rounding。** JTS ``OverlayNGRobust`` 的降级梯(固定精度
           → 浮点直算 → ``SnappingNoder`` 5 档递增容差 → ``SnapRoundingNoder``)
           本库照 :meth:`buffer` 的老口径只走**第一档 + 简化重试**,最后一档
           降级为放弃。于是"只有靠 snap-rounding 才救得回来"的退化构型,
           GEOS 给结果而本库给空。对拍见 ``tools/verify_overlay.py``。
        2. **不承诺与 GEOS 逐位相同**:交点用普通双精度,末位可能差一位。
        3. **不做 ``side location conflict`` 检测**(JTS 那边会抛
           ``TopologyException``);非法输入静默给一个数。
        4. **共线顶点会被保留。** ``POLYGON ∖ 横穿它的线`` 的面积**原样不变**,
           但边界上会多出两个节点顶点 —— 所以"结果与输入逐位相同"**不能**当
           断言口径,要断言面积 / 长度 / 拓扑等价。
        """
        return self._overlay(other, DIFFERENCE, 'difference')

    def union(self, other: 'Geometry') -> 'Geometry':
        """并集 ``A ∪ B``(``OGRGeometry::Union`` / ``OGR_G_Union``)。

        ⚠️ 与 :meth:`difference` 同一条口径:GDAL 那边是一句转发给 GEOS 的话,
        本库按 JTS ``operation/overlayng/`` 复刻,见 DESIGN.md §2.24。

        要点:

        * **结果可以是低维的,甚至是混合维度的。** 实测只共**一条边**的两方块
          并起来是**一个** ``POLYGON``(面积 200,那条共边不是结果);只共
          **一个点**的两方块并起来是 ``MULTIPOLYGON``(**两个**壳,不合并)。
        * **空结果按维度定型**:``union`` 取两输入维度的**大**者。
        * 其余口径(2D / Esri 绕向 / multipatch 报错 / 不做 snap-rounding)
          同 :meth:`difference`。
        """
        return self._overlay(other, UNION, 'union')

    def intersection(self, other: 'Geometry') -> 'Geometry':
        """交集 ``A ∩ B``(``OGRGeometry::Intersection`` / ``OGR_G_Intersection``)。

        ⚠️ 与 :meth:`difference` 同一条口径(GDAL 转发 GEOS,本库按 JTS 复刻)。

        要点:

        * **面 × 面的交集可以降维。** 实测只共**一条边**的两方块,交集是那条
          ``LINESTRING``(**不是空面**);只共**一个点**的两方块,交集是那个
          ``POINT``。这不是特例补丁,是 JTS ``LineBuilder`` /
          ``IntersectionPointBuilder`` 两条独立出口的必然结果 —— JTS 只在
          ``intersection`` 上开这两条路(其余三个算子不会从非点输入里生出点)。
        * **空结果按维度定型**:``intersection`` 取两输入维度的**小**者
          (面 ∩ 面 = 空面、线 ∩ 线 = 空线、点 ∩ 点 = 空点)。
        """
        return self._overlay(other, INTERSECTION, 'intersection')

    def symmetric_difference(self, other: 'Geometry') -> 'Geometry':
        """对称差 ``A △ B``(``OGRGeometry::SymmetricDifference``)。

        即"两块地加起来,但不算重合那块" —— ``(A ∖ B) ∪ (B ∖ A)``。

        ⚠️ 名字是 OGR ``SymmetricDifference`` 的 snake_case(与本库
        ``exactly_equals ← OGRGeometry::Equals`` 同例);要 shapely 那个短名
        用 :meth:`sym_difference`,是**同一个方法**。

        口径同 :meth:`union`(取两输入维度的**大**者定型空结果;GDAL 那边转发
        GEOS,本库按 JTS ``operation/overlayng/`` 复刻)。

        ⚠️ 实测一个反直觉处:**只共一条边**的两方块,对称差是**一个**面积 200 的
        ``POLYGON``(那条共边两边都算"结果面内部",于是消失),而 **只共一个点**
        的两方块是 ``MULTIPOLYGON``(两个壳)—— 和 :meth:`union` 同款。
        """
        return self._overlay(other, SYMDIFFERENCE, 'symmetric_difference')

    def sym_difference(self, other: 'Geometry') -> 'Geometry':
        """``symmetric_difference`` 的**别名**(shapely 用的短名)。

        直接转发,不是第二份实现 —— 两个名字调的是同一个东西。
        """
        return self._overlay(other, SYMDIFFERENCE, 'sym_difference')

    def simplify(self, tolerance: float,
                 preserve_topology: bool = False) -> 'Geometry':
        """Douglas–Peucker 简化,**逐 part** 做,首尾点必留。

        容差是点到**线段**的垂距上限(不是点到直线)—— 少了这一步,折线
        拐回去的那一截会被误判成"在线上"而整段删掉。Z / M 跟着保留点走,
        ``shape_type`` / ``has_z`` / ``has_m`` 都不变。

        ⚠️ **不支持拓扑保持。** ``preserve_topology=True`` 抛
        ``NotImplementedError``:GEOS 的拓扑保持简化要求结果不自交、且与其它
        几何的关系不变,那要靠**鲁棒性保证**(snap-rounding 那一档,本库整体
        不做)。⚠️ overlay 引擎**本库已经有了**(:meth:`difference` 等),缺的
        不是它 —— 是"保证不自交"的那层鲁棒性。参数留着是为了对齐
        ``OGR_G_SimplifyPreserveTopology`` 的调用形态 —— "存在但明确拒绝",
        而不是静默忽略。

        ⚠️ 面的环简化后若不足 **3** 个顶点,该环**整体不简化** —— 宁可留着
        毛刺,也不产出一个非法的环。(内存里的环不存闭合点,所以下限是 3
        而不是 4。)
        """
        if preserve_topology:
            raise NotImplementedError(
                'simplify() 不支持拓扑保持:GEOS 的 preserveTopology 需要'
                '鲁棒性(snap-rounding 那一档)保证结果不自交,本库没有。'
                '⚠️ 不是缺 overlay —— overlay 已经有了(difference() 等)。请用'
                'simplify(tolerance) 的非拓扑保持版本,或用 is_valid() '
                '自行检查结果。见 DESIGN.md §2.21。')
        self._require_no_multipatch('simplify')
        self._require_not_collection('simplify')
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
        self._require_not_collection('segmentize')
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

        ``GEOMETRYCOLLECTION`` = **全部子几何都有效**。⚠️ OGC 还要求 GC 的
        子几何两两内部互斥,那一条**不查**(与本库 ``is_valid`` 一贯的
        "部分实现"口径一致,清单见 ``_geometry_ops.is_valid_of``)。
        """
        if self.shape_type == C.ShapeType.GEOMETRYCOLLECTION:
            return all(g.is_valid() for g in self._geoms or ())
        return is_valid_of(self.kind, self.xy_parts, self._roles())

    def is_simple(self) -> bool:
        """**部分实现**的单纯性检查。

        点恒 ``True``;多点不重合才单纯;折线的每个 part 不自交;面恒 ``True``
        (OGC 规定面天然单纯,GEOS 也这么答);multipatch 恒 ``False``。

        **没做**:part 与 part 之间的相交。同样是 ``O(n²)``,同样做成方法。

        ``GEOMETRYCOLLECTION`` = **全部子几何都单纯**(同 ``is_valid`` 的口径)。
        """
        if self.shape_type == C.ShapeType.GEOMETRYCOLLECTION:
            return all(g.is_simple() for g in self._geoms or ())
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

        ``GEOMETRYCOLLECTION`` 的矩阵 = **逐子几何算,再逐格取维度最大的那一
        格**(``F`` < ``0`` < ``1`` < ``2``)。这是 OGC / GEOS 的口径;见
        :func:`_relate_collection` 里对"为什么这么取"与"哪里是近似"的说明。
        """
        other = self._require_geometry(other)
        self._require_no_multipatch('relate')
        other._require_no_multipatch('relate')
        if self.is_empty or other.is_empty:
            return 'FFFFFFFF2'
        return self._de9im(other)

    def _de9im(self, other: 'Geometry') -> str:
        """两个**非空**几何的 DE-9IM 矩阵 —— :meth:`relate` 与 :meth:`_predicate`
        的公共落点。

        单独拎出来是因为 GC 那一档要在这里分叉:分叉写两遍的话,漏一处就会
        出现"``relate()`` 对而 ``intersects()`` 错"这种最难查的不一致。
        """
        if (self.shape_type == C.ShapeType.GEOMETRYCOLLECTION
                or other.shape_type == C.ShapeType.GEOMETRYCOLLECTION):
            return _relate_collection(self, other)
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
            # ⚠️ GC 的 ``envelope()`` 是子几何包围盒的**并**,所以这一句对 GC
            # 依然保守正确:并集不相交 ⇒ 任何一对子几何都不相交 ⇒ 矩阵必空。
            return False
        m = self._de9im(other)
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
        if self.shape_type == C.ShapeType.GEOMETRYCOLLECTION:
            # ⚠️ 放在 is_empty 判断**之前**:两个空的 GC 结构可以不同
            # (``GC()`` vs ``GC(POLYGON EMPTY)``),先判空会把它们算成相等。
            # 对应 ``OGRGeometryCollection::Equals`` —— 它也是逐子几何比的。
            kids_a = self._geoms or []
            kids_b = other._geoms or []
            if len(kids_a) != len(kids_b):
                return False
            return all(a.exactly_equals(b)
                       for a, b in zip(kids_a, kids_b))
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
        """从 OGC WKT 构造。支持 POINT / LINESTRING / POLYGON / MULTIPOINT /
        MULTIPOLYGON 以及 ``GEOMETRYCOLLECTION``(可嵌套)。"""
        from ._esri_geometry import from_wkt
        return from_wkt(wkt, has_z=has_z, has_m=has_m)

    # -- 坐标变换 -----------------------------------------------------------
    def to_crs(self, dst: Any, src: Any = None, *, transformer: Any = None) -> 'Geometry':
        """把一个几何从 ``src`` 坐标系转到 ``dst``,返回**新几何**。

        :param dst: 目标坐标系 —— EPSG 号(``4326``)、``'EPSG:4527'``、
            WKT 串、``pyproj.CRS`` 或 :class:`GdbSpatialRef`。
        :param src: 源坐标系。**几何自己不知道自己是什么坐标系**,所以这个
            必须给,除非给了现成的 ``transformer``。要素几何可以从
            ``layer.spatial_ref`` 拿。
        :param transformer: 现成的 ``pyproj.Transformer``(批量转换时给,省掉
            重复解析 CRS 与建管道)。
        :returns: 新 :class:`Geometry`;``shape_type`` / ``has_z`` / ``has_m``
            与原几何一致。
        :raises ImportError: 没装 pyproj。**它是可选依赖**,见
            :mod:`pyopenfilegdb.coordinates_system`。

        ⚠️ **不改 ``self``** —— 与 ``OGRGeometry::Transform()`` 的就地语义
        相反。理由是要素把解码后的几何**缓存在 ``feat.geometry`` 上**,就地
        改会静默污染那个缓存(盘上的没变,再取一次却是改过的)。

        ⚠️ 转换前会做一次**量级体检**(源是地理坐标系却给出 ±180 以外的坐标、
        或反之),不通过时发
        :class:`~pyopenfilegdb.coordinates_system.CoordTransformWarning`。
        这条体检是必需的:pyproj 的 ``errcheck`` **拦不住域外点**,实测它
        会安静地返回一个有限但完全错误的数。要关掉这个告警用
        ``warnings.simplefilter('ignore', CoordTransformWarning)``,要
        逐次关掉就直接调
        :func:`~pyopenfilegdb.coordinates_system.transform_geometry` 的
        ``check=False``。

        >>> g = Geometry.from_wkt('POINT(39394370.79 3179362.10)')   # 4527
        >>> g.to_crs(4326, src=4527).wkt()                          # doctest: +SKIP
        'POINT (115.91881971302487 28.725838734163123)'
        """
        from .coordinates_system import transform_geometry
        # ``transform_geometry`` 的形参序也是 ``(geom, dst, src)`` —— 目标在
        # 前、源在后,与本方法一致,直接正着传。⚠️ 这两个名字一旦对调,pyproj
        # 不会报错,只会把转换方向反过来(投影坐标当经纬度),症状是一堆
        # ``inf`` —— 2026-09-30 实测过一次,断言之前一路安静。
        return transform_geometry(self, dst, src, transformer=transformer)

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, Geometry):
            return NotImplemented
        if (self.shape_type != other.shape_type
                or self.has_z != other.has_z
                or self.has_m != other.has_m):
            return False
        if self.shape_type == C.ShapeType.GEOMETRYCOLLECTION:
            # ``coordinates`` 对 GC 抛 TypeError,所以这一档必须单独走。
            # 比的是子几何**序列**(顺序有意义),与 ``exactly_equals`` 同口径。
            return (self._geoms or []) == (other._geoms or [])
        return self.coordinates == other.coordinates

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (f'Geometry(shape_type={self.shape_type!r}, '
                f'has_z={self.has_z!r}, has_m={self.has_m!r})')


#: DE-9IM 单个格子的**维度序** —— 元素集为空是 ``F``,否则是它的拓扑维。
#: "取最大"就是在这个序上取,不是按字符的 ASCII 序(``'F' > '2'``,按字符取
#: 会把最空的答案挑出来)。
_DE9IM_RANK = {'F': -1, '0': 0, '1': 1, '2': 2}

#: 空几何对的 DE-9IM —— 与 JTS / GEOS 的 ``RelateOp`` 一致。
_DE9IM_EMPTY = 'FFFFFFFF2'


def _flatten_collection(geom: 'Geometry', out: List['Geometry']) -> None:
    """把 GC **递归**摊平成"不是 GC 的几何"列表;非 GC 就直接放进去。

    **空的子几何直接丢掉。** 一个空几何对 9 个格子的贡献是 ``FFFFFFFF2``,
    在任何一格上都赢不了非空的那对(``EE`` 是 ``2`` 对 ``2``,平手),所以丢掉
    结果不变;留着反而要多防一手"空几何进 ``relate_of`` 会不会炸"。

    ``multipatch`` 子几何在这里就挡掉:顶层 :meth:`Geometry.relate` 的
    ``_require_no_multipatch`` 只看得到 GC 自己那个 ``kind``
    (``'geometrycollection'``),看不到里面装了什么。
    """
    if geom.shape_type == C.ShapeType.GEOMETRYCOLLECTION:
        for kid in (geom._geoms or ()):
            _flatten_collection(kid, out)
        return
    geom._require_no_multipatch('relate')
    if geom.is_empty:
        return
    out.append(geom)


def _relate_collection(a: 'Geometry', b: 'Geometry') -> str:
    """``GEOMETRYCOLLECTION`` 的 DE-9IM:逐子几何算,再**逐格取维度最大的那格**。

    为什么是"取最大":一个格子的含义是"self 的这一部分与 other 的那一部分在
    哪个维度上相交",而 GC 的"这一部分"是**所有**子几何拼起来的集合 —— 所以
    答案是"至少有一对子几何给出这个维度",也就是逐格取最大
    (``F`` < ``0`` < ``1`` < ``2``)。⚠️ 这个序**不是**字符的 ASCII 序
    (``'F' > '2'``),按字符取会把最空的答案挑出来。

    ⚠️ **内部 / 边界那六格在"逐部分的分解"这个口径下是精确的。**
    ``int(GC) = ⋃ int(child)`` 且 ``bdry(GC) = ⋃ bdry(child)``(合法 GC 的
    定义),而"并集的维数 = 维数的最大值",所以 ``II`` / ``IB`` / ``BI`` /
    ``BB`` / ``IE`` / ``BE`` 逐格取最大与**这个口径**完全一致。

    ⚠️ 说"这个口径"是**必须的**:OGC 的 DE-9IM 对**混合维度**的集合本来就没
    把内部/边界定义到"逐点唯一"——一个点可以既是"那个 POINT 子几何的内部"、
    又是"那条 LINESTRING 子几何的边界"。两个口径都自称 OGC:

    * **本库**:"并集的内部 = 各子几何内部的并" —— 所以上面六格取最大是对的。
    * **GEOS 3.13.1 实测**:点定位实际是"**维度最高的那个子几何说了算**"。
      实测 ``GC(LINESTRING(5 5,15 15), POINT(5 5)).relate(LINESTRING(5 5,15 15))``
      给 ``1FFF0FFF2``(``IB = F``),本库给 ``10FF0FFF2``(``IB = 0``):
      ``(5 5)`` 是那条线的起点(线的边界),又是那个 POINT 子几何的全部;
      按本库口径它在 ``int(GC)`` 里,按 GEOS 口径它由**那条线**(维度更高)定成
      ``BOUNDARY``,于是不在。

    也就是说:**混合维度 GC 的 DE-9IM 没有唯一真值可抄。** 本库选第一个口径,
    理由是可证明、可解释(六格精确 + 外部是上界 + 转置一致,下面三条都是结构性
    的),而"最高维优先"那条要在**逐点**层面干预,与本库"逐格聚合"的结构打架。
    这条差异在 ``DESIGN.md §2.24`` 里有对应条目。

    ⚠️ 顺带:**别拿 GEOS 的 GC ``relate`` 当 oracle 去对拍 E 格**——它自己就
    不自洽。实测 ``GC(POLYGON((0 0,1 0,1 1,0 0)), POINT(1 2)).relate(
    POLYGON((0 0,1 0,1 1,0 0)))`` 给 ``2F0F1F212``,即 ``EI = 2`` / ``EB = 1``;
    可是那个 POLYGON 就是 GC 自己的一个子几何(``T ⊆ GC``),``ext(GC) ∩ cl(T)``
    按定义**必须**是空集,两格都只能是 ``F``。本库给 ``2F0F1FFF2``(两格都是
    ``F``),方向是对的。所以对拍只统计"一致对数",不把分歧都记成本库的错。

    ⚠️ **``EI`` / ``EB`` 需要额外收紧一格。** 外部**不是**并集 ——
    ``ext(GC) = ⋂ ext(child)``,逐格取最大会**高估**(它算的是
    ``⋃ ext(child)``,那是补集的**下**界)。补一条**可靠**的收紧:

        ``ext(child_i) ∩ X = ∅`` ⟹ ``X ⊆ child_i ⊆ GC`` ⟹ ``ext(GC) ∩ X = ∅``

    所以"任何一个子几何的对应格是 ``F``"就说明整个 GC 那一格也是 ``F``。
    先逐"对方元素"取列最大、再套这条收紧,最后才是行最大。

    实测(GEOS 3.13.1):``GC(POLYGON((0 0,1 0,1 1)), POINT(1 2))`` 对
    ``POINT(1 2)`` 的真实矩阵是 ``0F2FF1FF2``(那个点被算在 GC 的内部,
    ``EI`` 与 ``EB`` 都是 ``F``),而不带收紧的朴素最大给 ``0F2FF10F2``
    —— ``EI`` 被那个三角形子几何的"外部"顶成了 ``0``。收紧之后对上。
    这不是孤例:96 组(12 个子几何组合 × 8 个对手)里朴素最大只对上
    **53** 组,收紧后 **82** 组。

    对不上的 14 组(**逐格核对过**)分两类,只有第一类是本节的收紧要管的
    —— 那 11 组**只差在 ``EI`` / ``EB``** 两格:

    * **GEOS 把外部算大**(大多数):``GC`` 对 ``T`` 那个例子就是 —— 子几何自己
      盖住了对方,GEOS 仍报 ``EI = 2``。上面那条"别当 oracle"说的就是这类。
    * **本库把外部算大**(联手盖住那个残余):例如
      ``GC(POLYGON((0 0,1 0,1 1)), LINESTRING(5 5,15 15))`` 对
      ``LINESTRING(0 0,10 10)``:后者的两个端点 ``(0 0)`` 落在三角形里、
      ``(10 10)`` 落在线段上,**联手**盖住了,真值 ``EB = F``,本库给 ``0``
      (GEOS 对)。没有任何一个子几何单独盖住,收紧钩不到。

    ⚠️ 另外 3 组非 E 格分歧(``IB`` / ``II``)与外部无关,是上面那个"混合维度
    口径"的问题,方向相反(GEOS 给 ``F``、本库给 ``0``),按"本库口径"定死。

    ⚠️ **仍然可能高估的残余情形**:对方的内部被**多个**子几何**联手**盖住、
    而没有任何一个子几何单独盖住它。这时每个 ``EI_ij`` 都不是 ``F``,收紧
    触发不了,取最大仍是上界。要精确就得把子几何先 union 成一个几何再算,而
    混合维度的并集要靠 ``OverlayNG``(本库只有面 × 面那一档,见
    ``_overlay_ops``)—— 所以这一档**不做**。残余误差的方向是**单向**的:
    收紧后的值落在 ``[真值, 朴素最大]`` 之间,即**只会把外部格子算大**
    (真值是 ``F`` 时报成非 ``F``),**绝不会把非空的格子报成 ``F``**。
    "真值是 ``F`` 报成非 ``F``"会让 ``contains`` / ``equals`` 一侧偏保守,
    但不会漏掉真实的相交。对拍见 ``tools/verify_overlay.py``。

    ⚠️ 转置一致性是**结构性**成立的:转置是 9 格的置换,而"取最大 + 上述收紧"
    在每一格上都是独立的,两者可交换 —— 所以 ``gc.relate(x)`` 与
    ``x.relate(gc)`` 必互为转置,不管嵌套多深。这一点有测试守着。
    """
    left: List['Geometry'] = []
    right: List['Geometry'] = []
    _flatten_collection(a, left)
    _flatten_collection(b, right)
    if not left or not right:
        return _DE9IM_EMPTY

    mats = [[relate_of(x.kind, x.xy_parts, x._roles(),
                       y.kind, y.xy_parts, y._roles()) for y in right]
            for x in left]
    n_left = len(left)
    n_right = len(right)
    cells = [max((mats[i][j][k] for i in range(n_left)
                  for j in range(n_right)), key=_DE9IM_RANK.__getitem__)
             for k in range(9)]

    def _tighten(values: List[str], index: int) -> str:
        """一组同一格子的取值:有 ``F`` 就取 ``F``,否则取维度最大。"""
        if 'F' in values:
            return 'F'
        return max(values, key=_DE9IM_RANK.__getitem__)

    def _col_tighten(index: int) -> str:
        """``EI`` / ``EB``:对 other 的**每个**子几何收一列(self 侧所有子几何),

        用"任何一个 self 子几何在这一格是 ``F``"来收紧,再对列取最大。
        """
        return max((_tighten([mats[i][j][index] for i in range(n_left)], index)
                    for j in range(n_right)), key=_DE9IM_RANK.__getitem__)

    def _row_tighten(index: int) -> str:
        """``IE`` / ``BE``:对 self 的**每个**子几何收一行(other 侧所有子几何)。"""
        return max((_tighten([mats[i][j][index] for j in range(n_right)], index)
                    for i in range(n_left)), key=_DE9IM_RANK.__getitem__)

    # ``EI`` / ``EB``:外部那一圈在 self 是 GC 时会被高估,逐列收紧。
    # ``IE`` / ``BE``:同一个问题换到 other 是 GC 的方向,逐行收紧。
    # 两边都不是 GC 时这两步是**恒等变换**(只有一列 / 一行),所以无条件套。
    cells[6] = _col_tighten(6)
    cells[7] = _col_tighten(7)
    cells[2] = _row_tighten(2)
    cells[5] = _row_tighten(5)
    return ''.join(cells)


#: overlay 结果 —— ``kind`` -> ``ShapeType``。
_OVERLAY_SHAPE = {
    'polygon': C.ShapeType.POLYGON,
    'polyline': C.ShapeType.POLYLINE,
    'point': C.ShapeType.POINT,
    'multipoint': C.ShapeType.MULTIPOINT,
}


def _flatten_overlay_input(geom: Geometry, op: str) -> Geometry:
    """把 ``GEOMETRYCOLLECTION`` 输入摊平成一个**齐次**几何(非 GC 原样返回)。

    为什么不能直接算:``overlay_parts`` 的输入是
    ``(kind, parts, shells)`` 三件套 —— 它假定"一个几何只有一种维度"。
    GC 打破了这个假定,而 GEOS 那边也**正是**因为这一点才不走 ``OverlayNG``:
    ``HeuristicOverlay`` 见到维度混杂的输入就转给 ``StructuredCollection``
    (先按维度各自 ``union``、再分别算、再拼)。

    本库只做其中**能精确对应**的那一半:

    * 子几何(递归摊平、跳过空几何)**维度一致** —— 全点 / 全线 / 全面
      → 拼成一个 ``multipoint`` / ``polyline`` / ``polygon``。面的多个壳直接
      并成一个多壳面几何(与 ``StructuredCollection`` 先 union 的语义一致:
      壳的绕向已经由 ``_ring_entry`` 的带符号深度表达了内外);
    * 子几何**维度混杂** → ``NotImplementedError``。
      ``StructuredCollection`` 那一套(逐维度算完再拼)本库没复刻 ——
      宁可明说,也不静默丢掉一个维度。
    * 子几何里有 ``multipatch`` → 照旧 ``NotImplementedError``
      (走 :meth:`Geometry._require_no_multipatch` 的原消息)。
    * GC 里**全是空几何** → 摊平成一个 ``NULL`` 几何(空输入),后续早退
      逻辑与"传了一个空几何"完全同路。
    """
    if geom.kind != 'geometrycollection':
        geom._require_no_multipatch(op)
        return geom

    kids: List[Geometry] = []
    stack: List[Geometry] = list(geom.geometries())
    while stack:
        g = stack.pop(0)
        if g.kind == 'geometrycollection':
            stack = list(g.geometries()) + stack
            continue
        g._require_no_multipatch(op)
        if g.kind == 'null' or g.is_empty:
            continue
        kids.append(g)
    if not kids:
        return Geometry(C.ShapeType.NULL, None)

    dims = sorted({dimension_of(k.kind) for k in kids})
    if len(dims) > 1:
        raise NotImplementedError(
            f'{op}() 的这个 GEOMETRYCOLLECTION 输入混了多个维度(拓扑维 '
            + ' / '.join(str(d) for d in dims)
            + '):GEOS 会把它交给 HeuristicOverlay 的 StructuredCollection '
            '逐维度算完再拼,本库没有复刻那一套。请先自己按维度拆开'
            '(GEOS 的做法也是分别算),或逐个子几何调 ' + op + '()。'
            '见 DESIGN.md §2.24。')
    dim = dims[0]

    has_z = any(k.has_z for k in kids)
    parts: List[Any] = []
    zs: Optional[List[Any]] = [] if has_z else None
    shells: List[bool] = []
    for k in kids:
        kp = k.xy_parts
        kz = k.z_parts if has_z else None
        roles = k._roles() if dim == 2 else None
        for i, a in enumerate(kp):
            parts.append(a)
            if has_z:
                zs.append(kz[i] if kz is not None
                          else array('d', [float('nan')] * (len(a) // 2)))
            if dim == 2:
                shells.append(True if roles is None else roles[i])

    if dim == 0:
        # multipoint 在 flat 形式里是**一整块**交错坐标(见 xy_parts 的
        # docstring),所以这里要把所有子几何的块接成一块。
        block = array('d', chain.from_iterable(parts))
        if len(block) == 2:
            x, y = block[0], block[1]
            coords: Any = (x, y, zs[0][0]) if has_z else (x, y)
            return Geometry(C.ShapeType.POINT, coords, has_z, False)
        blk_z = None
        if has_z:
            blk_z = [array('d', chain.from_iterable(zs))]
        return Geometry(C.ShapeType.MULTIPOINT, None, has_z, False,
                        _flat=_FlatCoords([block], blk_z, None))
    if dim == 1:
        return Geometry(C.ShapeType.POLYLINE, None, has_z, False,
                        _flat=_FlatCoords(parts, zs, None))
    return Geometry(C.ShapeType.POLYGON, None, has_z, False,
                    _flat=_FlatCoords(parts, zs, None),
                    _shells=shells)


def _only_zs(zs: Any, idx: int) -> Any:
    """从 overlay 三槽的 ``zs`` 里取出第 ``idx`` 槽 —— **全 None 就回 None**。

    ``_overlay_geometry`` 的六元组里 ``zs`` 是 ``(part_zs, line_zs, point_zs)``。
    装 ``GEOMETRYCOLLECTION`` 要逐分量递归,而每一维只能拿自己那一槽:把整条
    三元的元组传下去的话,"另一个分量有 Z、这一维没有"会被读成"这一维有 Z",
    越界取 z 直接炸。
    """
    if zs is None or zs[idx] is None:
        return None
    return tuple(zs[idx] if t == idx else None for t in range(3))


def _polygon_groups(parts, shells, part_zs):
    """把 ``parts`` / ``shells`` 切成"**每个外环 + 它的洞** = 一组"。

    为什么需要切
    ------------
    ``OverlayUtil.createResultGeometry``(JTS,已读源码)把结果分量**平铺**成
    一个列表再交给 ``GeometryFactory.buildGeometry``::

        // element geometries of the result are always in the order A,L,P
        if (resultPolyList != null) geomList.addAll(resultPolyList);
        if (resultLineList != null) geomList.addAll(resultLineList);
        if (resultPointList != null) geomList.addAll(resultPointList);
        // build the most specific geometry possible
        return geometryFactory.buildGeometry(geomList);

    而 ``buildGeometry`` 的规则是"同型就并成 Multi*,**不同型就逐个分量原样装进
    ``GEOMETRYCOLLECTION``**"。所以混维度时结果是 ``GC(面1, 面2, 线)``,而**不是**
    ``GC(MULTIPOLYGON, 线)`` —— 本机 GEOS 3.13.1 实测:

    * ``POLYGON ∪ 横穿的 LINESTRING`` → ``GEOMETRYCOLLECTION (POLYGON (...),
      LINESTRING (-5 5,0 5), LINESTRING (10 5,15 5))`` —— 两个线段是**两个成员**,
      不是一条 ``MULTILINESTRING``。JTS 源码里那句 ``TODO: for mixed dimension,
      return collection of Multigeom for each dimension (breaking change)`` 正
      说明"按维合并"是**他们没做的**改动。

    本库的 ``_FlatCoords`` 把多个环平铺在一张表里(``_shells`` 标角色),一个
    "``MULTIPOLYGON``"是**一个** ``Geometry``。要多产出几个分量就得在这里切开。

    ⚠️ 分组靠的是 ``_planar._assemble`` 的输出顺序 —— 那里是
    ``for si, holes in groups:`` 逐个"先壳后洞"地 append 的,所以 ``shells``
    里每个 ``True`` 就开启一组。**不是**靠 ``organize_polygons``:它只回
    ``[(外环下标, [洞下标])]``,从不重排输入。往 ``_assemble`` 里插东西之前先回去
    确认它仍然成组。

    :return: ``[(parts_i, shells_i, zs_i), ...]``;``part_zs is None`` 时
        ``zs_i`` 也是 ``None``。
    """
    if not parts:
        return []
    out = []
    for i, is_shell in enumerate(shells):
        # 组的开头:壳,或者(理论上到不了)第一个环。真到了说明组序被破坏,
        # 与其把环静默丢掉,不如让它自成一"组"。
        if is_shell or not out:
            out.append(([], [], [] if part_zs is not None else None))
        grp = out[-1]
        grp[0].append(parts[i])
        grp[1].append(bool(is_shell))
        if part_zs is not None:
            grp[2].append(part_zs[i])
    return out


def _overlay_geometry(res: Sequence[Any]) -> Geometry:
    """把 ``_overlay_ops.overlay_parts`` 的六元组装成一个 ``Geometry``。

    ⚠️ 与 :meth:`Geometry.buffer` 同款:**走 flat 形式并显式赋 ``_shells``**。
    ``Geometry._like()`` **不搬** ``_shells`` / ``_groups`` / ``_env``,所以只要
    想保留"哪个环是外环"这件事,就必须自己传进去 —— 这是本项目反复踩到的坑,
    ``test_overlay.py`` 有一条专门的守卫盯着。

    ``zs`` 那一项是 ``(part_zs, line_zs, point_zs)``;非 ``None`` 时 ``has_z``
    置真。z 可能含 **NaN** —— 那是"这个顶点确实没有 z 来源"(GEOS 同款),
    不是缺数据,别把它当成错误抹掉。

    ``kind == 'geometrycollection'`` 时三份列表**同时**非空(见
    ``_overlay_ops._assemble_result`` 的 ``n_present > 1``),而且**每个分量单独
    成成员** —— 顺序照 ``OverlayUtil.createResultGeometry`` 的原文 **面 → 线 →
    点**,但不按维合并成 ``MULTIPOLYGON`` / ``MULTILINESTRING`` / ``MULTIPOINT``。
    这是 ``GeometryFactory.buildGeometry`` 在"分量不同型"时的行为,也是 GEOS
    3.13.1 的实测输出;理由与切法见 :func:`_polygon_groups`。

    ⚠️ 单维那几条路**照旧合并**(``MULTIPOLYGON`` / ``MULTILINESTRING`` /
    ``MULTIPOINT``)—— 那不是例外,正是 ``buildGeometry`` 在"分量同型"时的另一半
    行为:本库的"一个 ``polygon`` 带多个壳"就等于 JTS 的 ``MultiPolygon``。
    """
    kind, parts, shells, lines, points, zs = res
    if kind == 'geometrycollection':
        # 逐分量递归。⚠️ 每一次都只把**这一维**的 z 传下去 —— 三槽里其余两项
        # 是别的分量的(或者是 None),照传会把"这个分量没有 Z"误判成"有 Z",
        # 于是一个 2D 的点跑到 `point_zs[0]` 上炸掉。全 None 就回 None
        # (不带 Z),这样 `has_z` 也就自然为假。
        part_zs, line_zs, point_zs = zs if zs is not None else (None, None, None)
        kids: List[Geometry] = []
        # 面:每个壳(+ 它的洞)一个成员。
        for grp_parts, grp_shells, grp_zs in _polygon_groups(
                parts, shells, part_zs):
            kids.append(_overlay_geometry(
                ('polygon', grp_parts, grp_shells, None, None,
                 _only_zs((grp_zs, None, None), 0))))
        # 线:每个 part 一个成员。
        for i in range(len(lines)):
            one_zs = None if line_zs is None else [line_zs[i]]
            kids.append(_overlay_geometry(
                ('polyline', None, None, [lines[i]], None,
                 _only_zs((None, one_zs, None), 1))))
        # 点:每个点一个成员(混维度时不做 MultiPoint 合并)。
        for i in range(len(points)):
            one_zs = None if point_zs is None else [point_zs[i]]
            kids.append(_overlay_geometry(
                ('point', None, None, None, [points[i]],
                 _only_zs((None, None, one_zs), 2))))
        return Geometry.geometry_collection(kids)
    st = _OVERLAY_SHAPE[kind]
    part_zs, line_zs, point_zs = zs if zs is not None else (None, None, None)
    has_z = zs is not None
    if kind == 'polygon':
        if not parts:
            return Geometry(st, None)
        return Geometry(st, None, has_z, False,
                        _flat=_FlatCoords(parts, part_zs, None),
                        _shells=list(shells))
    if kind == 'polyline':
        if not lines:
            return Geometry(st, None)
        return Geometry(st, None, has_z, False,
                        _flat=_FlatCoords(lines, line_zs, None))
    if kind == 'multipoint':
        if not points:
            return Geometry(st, None)
        # multipoint 在 flat 形式里是**一整块**交错坐标(见 xy_parts 的 docstring)
        return Geometry(st, None, has_z, False,
                        _flat=_FlatCoords(
                            [array('d', chain.from_iterable(points))],
                            None if point_zs is None else [point_zs], None))
    if not points:
        return Geometry(st, None)
    # 单点:coordinates 是**裸元组**(见 _like 的 point 分支),带 Z 时是三元组
    return Geometry(st, points[0] + ((point_zs[0],) if has_z else ()),
                    has_z, False)


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
