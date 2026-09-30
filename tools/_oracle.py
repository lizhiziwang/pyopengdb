#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""在 ``tools/`` 下共用的"权威实现"适配层。

⚠️ **shapely / osgeo 只允许出现在 ``tools/`` 里。** 这个文件就是把那条规矩
收在一个地方:别的脚本(``verify_topology.py`` / ``verify_buffer.py``)从这里
import,自己一行都不碰 shapely / osgeo。``pyopenfilegdb`` 包的 import 图不含
它们,``tests/`` 不依赖它们,``pyproject.toml`` 里没有它们。

两个 oracle,按可得性自动挑(**都不是依赖**)
--------------------------------------------
======================  =========================================  =============
oracle                  提供什么                                   本机
======================  =========================================  =============
**shapely(GEOS)**       ``relate`` 矩阵 + 全部 10 个谓词 + 全世界  ✅ 2.1.2 / GEOS 3.13.1
                        最完整的 ``buffer`` 参数矩阵
**GDAL/``osgeo``**      7 个谓词 + 结构 ``Equals``;``Buffer``     ✅ 3.13.3
                        只有 ``(distance, quadsegs)`` 两个参数
(都没有)                 —                                          skip,exit 0
======================  =========================================  =============

**优先 shapely** 的理由:它就是 GEOS 本体。谓词那一档 ``relate`` 直接给 DE-9IM
矩阵(那是地基);buffer 那一档更要紧 —— **GDAL 的 ``OGRGeometry::Buffer`` 本来就
是转发给 GEOS 的**,只有 shapely 能给出 ``cap_style`` / ``join_style`` /
``mitre_limit`` / ``single_sided`` 的完整对照。GDAL 绑定的 ``Buffer`` 只收
``(distance, quadsegs)``,参数一多就没法比 —— 那就**显式标成未支持**,不是静默跳过。

GDAL 那条路上的三处已知差异(照 ``verify_topology.py`` 的老话)
--------------------------------------------------------------
* **没有 ``relate``** —— 比不了矩阵;
* **没有 ``Covers`` / ``CoveredBy``**;
* **``Equals`` 是结构比较**(对应本库的 ``exactly_equals()``),不是 ``equals()``。
"""
from __future__ import annotations

from typing import Any, List, Optional, Sequence, Tuple


def _ring_coords(ring) -> List[Tuple[float, float]]:
    """shapely 的 ring -> ``(x, y)`` 表,**削掉尾部闭合点**(与本库内存表示一致)。"""
    pts = [(float(c[0]), float(c[1])) for c in ring.coords]
    while len(pts) > 1 and pts[0] == pts[-1]:
        pts.pop()
    return pts


#: overlay 的四个算子名 —— 本库的方法名与 oracle 的调用名在这里对齐。
OVERLAY_OPS = ('intersection', 'union', 'difference', 'symmetric_difference')


def _member_stats(s, g):
    """结果几何 -> ``{dim, area, length, empty, type, members}``。

    ``members`` 是**顶层成员**的 ``[(类型名, 面积, 线长, 顶点数), ...]``,按**列表
    顺序**保留。这一项是比"结构"用的:``GC(面, 线, 线)`` 与
    ``GC(面, MULTILINESTRING)`` 在**集合意义下完全相同**(面积、线长、对称差都是
    0),但成员表长度 3 与 2 —— 只比集合会把这类差异全放过。⚠️ 本库的
    ``polygon`` 带多个壳 ≡ 别的库的 ``MultiPolygon``,所以成员表里两者都可以
    出现,不对齐类型名,只比"几个成员、各自多大"。
    """
    gt = g.geom_type
    if gt in ('GeometryCollection', 'MultiPolygon', 'MultiLineString',
              'MultiPoint'):
        kids = list(g.geoms)
    else:
        kids = [g]
    return {
        'dim': int(s.get_dimensions(g)),
        'area': float(g.area),
        'length': float(g.length),
        'empty': bool(g.is_empty),
        'type': gt,
        'members': [(k.geom_type, float(k.area), float(k.length),
                     int(s.get_num_coordinates(k))) for k in kids],
    }


class ShapelyOracle:
    """GEOS 本体。`relate` 直接给 DE-9IM 矩阵,10 个谓词全在,buffer 参数全在。

    ⚠️ shapely **不是 GDAL 相关库**(它是 GEOS 的绑定),但仍只在 ``tools/``
    里出现,库、`tests/`、`pyproject.toml` 都不依赖它。
    """

    name = 'shapely/GEOS'
    supports_relate = True
    #: 本库方法名 -> 该 oracle 的函数。**没有的项就是"未暴露"**。
    unsupported = ()
    structural_equals = None
    #: buffer 的完整参数矩阵能不能比(``cap`` / ``join`` / ``mitre_limit`` /
    #: ``single_sided``)。GDAL 那个是 ``False``。
    supports_buffer_params = True
    #: overlay 的四个算子能不能比。
    supports_overlay = True
    #: 有没有 :meth:`hausdorff`(GDAL 的绑定没暴露)。没有的话
    #: ``verify_overlay`` 会**明说**自己退回了哪条更松的腿。
    has_hausdorff = True
    #: 这个 oracle 的 ``GEOMETRYCOLLECTION`` 面积是不是真的。GEOS 是(它是各子
    #: 几何之和);GDAL 的 ``OGRGeometryCollection::get_Area()`` **返回 0**。
    gc_area_ok = True

    def __init__(self, shapely):
        self._s = shapely

    def describe(self):
        s = self._s
        return (f'shapely {s.__version__} / GEOS '
                f'{getattr(s, "geos_version_string", "?")}')

    def make(self, wkt):
        return self._s.from_wkt(wkt)

    def relate(self, a, b):
        return self._s.relate(a, b)

    def pred(self, name, a, b):
        return bool(getattr(self._s, name)(a, b))

    # -- overlay -----------------------------------------------------------
    def overlay(self, wkt_a, wkt_b, op):
        """``op`` ∈ :data:`OVERLAY_OPS` —— 返回**结果的 WKT**;不支持回 ``None``。

        两个输入收 WKT、结果也回 WKT,是为了让"本库算出来的结果"能原样喂回来
        (比"本库的结果与 oracle 的结果是不是同一个集合"全靠这一步,而两边都
        只要一种序列化形式)。
        """
        assert op in OVERLAY_OPS, op
        f = getattr(self._s, op)
        return f(self._s.from_wkt(wkt_a), self._s.from_wkt(wkt_b)).wkt

    def stats(self, geom):
        """几何(**或 WKT 字符串**)→ :func:`_member_stats` 的摘要。

        纯 oracle 侧算的 —— 不碰本库的谓词,免得判定变成自证。收 WKT 字符串
        是为了让"本库算出来的结果"能原样喂进来比。
        """
        if isinstance(geom, str):
            geom = self._s.from_wkt(geom)
        return _member_stats(self._s, geom)

    def xor_stats(self, wkt_a, wkt_b):
        """``A △ B`` 的摘要(WKT 进、摘要出)—— 判定"两个结果集合同不同"用它。"""
        wkt = self.overlay(wkt_a, wkt_b, 'symmetric_difference')
        return None if wkt is None else self.stats(wkt)

    def hausdorff(self, wkt_a, wkt_b):
        """两个几何的 **Hausdorff 距离**(离散版:各顶点到对方几何的距离取最大)。

        ``verify_overlay`` 的"同一集合"判据有**三条腿**,这条是第三腿,管的是
        另外两条都抓不住的差异 —— 顶点被挪开、但面积和线长都变不了的那种(点
        结果最典型:挪一个点,面积恒 0、线长恒 0)。

        ⚠️ **不能用"对称差的线长"当这条腿。** 最小复现:同一个环把一个顶点的 y
        动 **1 ulp**(5.68e-14),两个环的面积差 5.03e-14、**周长差 6.75e-14**,
        而 GEOS 自己给出的 ``symmetric_difference`` 是一个**周长 4.75** 的退化
        环 —— 它是 GEOS 对近重合输入自身的数值不稳,不是几何差。所以线长那条腿
        一律取"**两个结果各自的总长之差**",不取对称差的线长。工具里
        :data:`verify_overlay._XOR_LENGTH_ARTIFACT` 有同样一段话。

        离散 Hausdorff 够用,不必 densify:面/线结果的边界是直线段,顶点全在
        tol 之内 + 面积也对得上 ⇒ 中间段能差到哪儿去;真要有一块差异,面积那条腿
        会先红。空几何:两边都空 → ``0.0``;一边空一边不空 → ``inf``(必然红,
        这种情况面积那条腿本来也会拦)。
        """
        s, inf = self._s, float('inf')
        a, b = s.from_wkt(wkt_a), s.from_wkt(wkt_b)
        if a.is_empty or b.is_empty:
            return 0.0 if (a.is_empty and b.is_empty) else inf
        return float(s.hausdorff_distance(a, b))

    # -- buffer ------------------------------------------------------------
    def buffer_parts(self, wkt, distance, quad_segs=8, cap='round',
                     join='round', mitre_limit=5.0, single_sided=False):
        """返回 ``[(shell, [hole, ...]), ...]``;空结果返回 ``[]``;不支持返回 ``None``。

        ⚠️ 必须**显式传 ``quad_segs``**:``shapely.buffer``(模块函数)默认是
        ``8``,而 ``BaseGeometry.buffer``(实例方法,``Point(0,0).buffer(1)`` 走的
        那条)默认是 ``16``。不传就等着顶点数差一倍还没人发现。
        """
        s = self._s
        g = s.from_wkt(wkt)
        r = s.buffer(g, distance, quad_segs=quad_segs, cap_style=cap,
                     join_style=join, mitre_limit=mitre_limit,
                     single_sided=single_sided)
        if r.is_empty:
            return []
        geoms = list(r.geoms) if r.geom_type == 'MultiPolygon' else [r]
        out = []
        for poly in geoms:
            if poly.geom_type != 'Polygon' or poly.is_empty:
                continue
            out.append((_ring_coords(poly.exterior),
                        [_ring_coords(x) for x in poly.interiors]))
        return out


def _gdal_stats(g):
    """:func:`_member_stats` 的 GDAL 版。字段同名同义,类型名用 GDAL 自己的大写名。

    ⚠️ ``area`` 在"结果是 GC"时**是假的(0)** —— ``gc_area_ok = False`` 就是
    为它设的,调用方要据此排除,不能当成通过。
    """
    from osgeo import ogr
    n = g.GetGeometryCount()
    kids = [g.GetGeometryRef(i) for i in range(n)] if n else [g]
    return {
        'dim': int(g.GetDimension()),
        'area': float(g.GetArea()),
        'length': float(g.Length()),
        'empty': bool(g.IsEmpty()),
        'type': str(g.GetGeometryName()),
        'members': [(str(k.GetGeometryName()), float(k.GetArea()),
                     float(k.Length()), int(k.GetPointCount()))
                    for k in kids],
    }


class GdalOracle:
    """GDAL 的 Python 绑定。比 shapely 弱一档,只作后备。

    ⚠️ 三处与 shapely 不同,必须知道:
    * **没有 `relate`** —— 比不了矩阵(这正是当年"假绿"的来源);
    * **没有 `Covers` / `CoveredBy`**;
    * **`Equals` 是结构比较**,对应本库的 `exactly_equals()`,**不是** `equals()`
      (实测:`POLYGON((0 0,10 0,10 10,0 10,0 0))` vs 同地多一个共线顶点的版本,
      GDAL `Equals` 给 False,GEOS `equals` 给 True)。

    另外 **`Buffer` 只收 ``(distance, quadsegs)``** —— ``cap`` / ``join`` /
    ``mitre_limit`` / ``single_sided`` 一概比不了,``buffer_parts`` 返回 ``None``,
    由调用方显式列进"未支持"。
    """

    name = 'GDAL/GEOS'
    supports_relate = False
    unsupported = ('relate', 'covers', 'covered_by', 'equals')
    structural_equals = 'exactly_equals'
    supports_buffer_params = False
    supports_overlay = True
    #: ⚠️ GDAL 的 Python 绑定**没有暴露 Hausdorff 距离**(C 层的
    #: ``OGRGeometry`` 里也没有;GEOS 有 ``GEOSHausdorffDistance``,GDAL 没转出来)。
    #: 所以这个 oracle 的"同一集合"判据会**退回**更松的那条腿,并在输出里明说。
    has_hausdorff = False
    #: ⚠️ ``OGRGeometryCollection::get_Area()`` **返回 0**(GDAL 就是这么写的),
    #: 而 GEOS 的同名方法返回各子几何之和。所以这个 oracle 一比到"结果是 GC"的
    #: 用例,面积判定就是假的 —— 由 ``tools/verify_overlay.py`` 显式排除并报出
    #: 原因,不许静默当成通过。
    gc_area_ok = False

    #: 本库方法名 -> GDAL 的**方法名**(大小写不同,逐个点出来,不用 .capitalize()
    #: 猜 —— `covered_by` 猜出来是 `Covered_by`,根本不存在)。
    _METHOD = {
        'intersects': 'Intersects',
        'disjoint': 'Disjoint',
        'contains': 'Contains',
        'within': 'Within',
        'touches': 'Touches',
        'crosses': 'Crosses',
        'overlaps': 'Overlaps',
        'exactly_equals': 'Equals',
    }

    def __init__(self, ogr):
        self._ogr = ogr

    def describe(self):
        try:
            from osgeo import gdal
            return f'GDAL {gdal.VersionInfo()}'
        except Exception:                                   # noqa: BLE001
            return 'GDAL(版本未知)'

    def make(self, wkt):
        return self._ogr.CreateGeometryFromWkt(wkt)

    def relate(self, a, b):
        return None                                          # 未暴露

    def pred(self, name, a, b):
        m = self._METHOD.get(name)
        if m is None:
            return None                                      # 未暴露
        return bool(getattr(a, m)(b))

    # -- overlay -----------------------------------------------------------
    #: 本库的方法名 -> GDAL 的**方法名**。⚠️ GDAL 这边 ``Union`` /
    #: ``Intersection`` / ``Difference`` / ``SymmetricDifference`` 也是转发给
    #: GEOS 的(与 ``ogrgeometry.cpp`` 的 ``OGRGeometry::Difference`` 一样,
    #: 一行转发),所以它与 shapely oracle 同源 —— 但绑定弱一档,失败时回 ``None``。
    _OVERLAY_METHOD = {
        'intersection': 'Intersection',
        'union': 'Union',
        'difference': 'Difference',
        'symmetric_difference': 'SymmetricDifference',
    }

    def overlay(self, wkt_a, wkt_b, op):
        assert op in OVERLAY_OPS, op
        m = self._OVERLAY_METHOD[op]
        a = self.make(wkt_a)
        b = self.make(wkt_b)
        if a is None or b is None:
            return None
        try:
            r = getattr(a, m)(b)
        except Exception:                                    # noqa: BLE001
            return None
        return None if r is None else r.ExportToWkt()

    def stats(self, geom):
        """几何(**或 WKT 字符串**)→ :func:`_member_stats` 的 GDAL 版。"""
        g = self.make(geom) if isinstance(geom, str) else geom
        return None if g is None else _gdal_stats(g)

    def xor_stats(self, wkt_a, wkt_b):
        wkt = self.overlay(wkt_a, wkt_b, 'symmetric_difference')
        return None if wkt is None else self.stats(wkt)

    def hausdorff(self, wkt_a, wkt_b):
        """GDAL 没暴露 Hausdorff(见 :attr:`has_hausdorff`)→ ``None``。"""
        return None

    # -- buffer ------------------------------------------------------------
    def buffer_parts(self, wkt, distance, quad_segs=8, cap='round',
                     join='round', mitre_limit=5.0, single_sided=False):
        """``None`` = 这个 oracle 比不了 buffer 的参数矩阵(见类 docstring)。"""
        return None


def pick_oracle(force=None):
    """挑一个 oracle;装不上就返回 ``None``(调用方负责 skip)。"""
    if force in (None, 'auto', 'shapely'):
        try:
            import shapely
            return ShapelyOracle(shapely)
        except ImportError:
            if force == 'shapely':
                raise
    if force in (None, 'auto', 'gdal'):
        try:
            from osgeo import ogr
            return GdalOracle(ogr)
        except ImportError:
            if force == 'gdal':
                raise
    return None
