# -*- coding: utf-8 -*-
"""overlay 的**线与点输入**(外加 ``GEOMETRYCOLLECTION`` 输入)。

跑法::

    /d/zsh/app/py_3.13.1/python -m unittest tests.test_overlay_line_point -v

为什么单独一个文件
------------------
``test_overlay.py`` 管的是**面 × 面**那一路(``PolygonBuilder`` + 面级 BFS),
它的出口判据是"面 × 面能降维 + 装配顺序"。线 / 点 / 混合维度是 D 段才接上的
三件**另外的**机器 —— JTS ``LineBuilder`` / ``OverlayPoints`` /
``OverlayMixedPoints`` —— 真值来源、守卫、边界条件都自成一套,塞进那个文件
只会让两边都难读。

算法出处
--------
GDAL 的 ``OGRGeometry::Difference / Union / Intersection / SymmetricDifference``
**一律是一句转发给 GEOS**,GDAL 自己一行算法都没有;上游是 JTS
``operation/overlayng/``。三件机器的对应关系:

===================  ======================================================
本文件这一节          对应的 JTS 类
===================  ======================================================
折线输入              ``LineBuilder.isResultLine`` 的闸门梯(闸门在
                      ``_overlay_ops._is_result_line``,顺序照抄)
点 × 点               ``OverlayPoints``
点 × 非点             ``OverlayMixedPoints``
GC 输入               ``HeuristicOverlay`` 的 ``StructuredCollection``
                      (**只做了能精确对应的那一半**,见 §五)
===================  ======================================================

真值从哪来
----------
* 每一条都是对**几何定义手算**的闭式解,不看实现 —— 抓的是"实现和理解一起错"。
* 全部用本机 **GEOS 3.13.1**(shapely 2.1.2)实测核对过一遍,连"结果是
  ``LineString`` 还是 ``MultiLineString`` / ``Point`` 还是 ``MultiPoint``"
  这一层都逐条对上;对拍脚本在 ``tools/verify_overlay.py``。本文件**不 import
  shapely / osgeo**(那是 ``tools/`` 的特权)。

⚠️ 两条与本库口径有关的坑(``test_overlay.py`` 里也各写了一遍,这里再点一次):

* **空结果的 WKT 一律是 ``'GEOMETRYCOLLECTION EMPTY'``**(``_esri_geometry.py``
  的既有行为)。所以"空结果定型"只能靠 ``kind`` / ``dimension`` 断言 ——
  ``LINESTRING EMPTY`` 与 ``POINT EMPTY`` 在本库**看着一模一样**。
* **``MULTILINESTRING`` / ``MULTIPOLYGON`` 都不是独立的 ``kind``** —— 它们是
  ``kind == 'polyline'`` / ``'polygon'`` 且有多个 part / 多个壳。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pyopenfilegdb.geometry import Geometry                      # noqa: E402

#: 四个算子,统一用 ``getattr`` 取 —— 免得手打方法名打错还测不出。
_OPS = ('intersection', 'union', 'difference', 'symmetric_difference')


def _g(wkt):
    return Geometry.from_wkt(wkt)


def _sq(x0, y0, x1, y1):
    return _g('POLYGON((%g %g,%g %g,%g %g,%g %g,%g %g))'
              % (x0, y0, x1, y0, x1, y1, x0, y1, x0, y0))


#: 本文件反复用到的 10×10 方块(面积 100)。
BOX = _sq(0, 0, 10, 10)
#: 横穿 ``BOX`` 的线(两端都在面外,交点 (0 5) 与 (10 5) 都落在环边的**内部**)。
CROSS = _g('LINESTRING(-5 5,15 5)')
#: 完全落在 ``BOX`` 内部的线。
INSIDE = _g('LINESTRING(2 5,8 5)')
#: 完全在 ``BOX`` 外面的线。
OUTSIDE = _g('LINESTRING(20 20,30 30)')


class _Assert(unittest.TestCase):

    def assertArea(self, got, want, msg=''):
        delta = 1e-9 * max(1.0, abs(want))
        if abs(got - want) > delta:
            self.fail('%s面积 %.15g,期望 %.15g(差 %.3g)'
                      % (msg and msg + ': ', got, want, got - want))

    def assertLength(self, got, want, msg=''):
        delta = 1e-9 * max(1.0, abs(want))
        if abs(got - want) > delta:
            self.fail('%s长度 %.15g,期望 %.15g(差 %.3g)'
                      % (msg and msg + ': ', got, want, got - want))

    def assertEmpty(self, geom, msg=''):
        if not geom.is_empty:
            self.fail('%s期望空几何,实际 %s' % (msg and msg + ': ', geom.wkt()))

    def assertKindDim(self, geom, kind, dim, msg=''):
        self.assertEqual(geom.kind, kind, '%s: kind' % (msg or 'geometry'))
        self.assertEqual(geom.dimension, dim, '%s: dimension' % (msg or 'geometry'))


# ----------------------------------------------------------------------------
# 一、面 × 线(计划 §6.1 的线那几行)
# ----------------------------------------------------------------------------
class TestPolygonVsLine(_Assert):
    """面与折线的四种位置关系 × 四个算子。期望值全部对照 GEOS 3.13.1 实测。"""

    def test_crossing_intersection_is_the_inside_segment(self):
        """横穿的线,交集 = 面内那一段(⚠️ 只此一个算子会把线留成结果)。"""
        r = BOX.intersection(CROSS)
        self.assertKindDim(r, 'polyline', 1)
        self.assertEqual(len(r.xy_parts), 1)
        self.assertLength(r.length(), 10.0)

    def test_crossing_difference_keeps_the_area(self):
        """面 ∖ 横穿的线 = **面积原样不变**的多边形(实测 GEOS 同款)。

        ⚠️ 这是本设计最容易写错的一处:线**不是面的边界**,它的 delta 分量
        恒为 0,所以它不会把面切成两半。若把两条输入的边一视同仁当屏障,
        这里会给出两个 50 面积的多边形 —— 本用例就是为那条 bug 设的。
        """
        r = BOX.difference(CROSS)
        self.assertKindDim(r, 'polygon', 2)
        self.assertArea(r.area(), 100.0)
        self.assertEqual(len(r.xy_parts), 1, '必须只有一个环,不能切成两半')

    def test_crossing_difference_adds_collinear_vertices(self):
        """⚠️ 面积不变,但边界上**多出两个共线顶点** ``(0 5)`` / ``(10 5)``。

        所以"结果与输入逐位相同"**不能**当断言口径(与 buffer 那一档同一个
        坑),断言要落在面积 / 拓扑上。这里明确把"多出来的顶点"也钉住。
        """
        r = BOX.difference(CROSS)
        part = r.xy_parts[0]
        pts = {(part[2 * i], part[2 * i + 1]) for i in range(len(part) // 2)}
        self.assertIn((0.0, 5.0), pts)
        self.assertIn((10.0, 5.0), pts)
        self.assertNotIn((0.0, 0.0), {(-1.0, -1.0)})     # 只是别比错对象
        self.assertEqual(len(pts), 6, '原本 4 个角 + 2 个共线节点')

    def test_crossing_union_is_a_collection(self):
        """并集 = GC(整块面, 左侧残段, 右侧残段)—— 实测的降维出口。

        ⚠️ 是**三个**成员,两条线段各自算一个,不是"一个面 + 一条
        MULTILINESTRING"。这条照的是 ``GeometryFactory.buildGeometry``:
        分量不同型时**逐个原样**装进 ``GEOMETRYCOLLECTION``。理由与出处见
        ``geometry._polygon_groups`` 的 docstring。
        """
        r = BOX.union(CROSS)
        self.assertKindDim(r, 'geometrycollection', 2)
        self.assertEqual(r.geometry_count, 3)
        self.assertKindDim(r.geometry_at(0), 'polygon', 2)
        self.assertKindDim(r.geometry_at(1), 'polyline', 1)
        self.assertKindDim(r.geometry_at(2), 'polyline', 1)
        self.assertArea(r.area(), 100.0)
        self.assertLength(r.geometry_at(0).length(), 40.0, '面的周长')
        self.assertLength(r.geometry_at(1).length()
                          + r.geometry_at(2).length(), 10.0, '面外两段各 5')

    def test_crossing_symmetric_difference(self):
        """对称差与并集在"线有一半在面外"时输出相同(面内那一段被丢掉)。

        ⚠️ 这两个算子在**面 × 线**这一档**永远**相同:线在面内那一段被面盖住,
        两个算子都丢;面外那段两个算子都留。这不是抄错,是集合运算的必然 ——
        所以"四算子必须给出四个答案"那条守卫**不能**用面×线这一对(见
        ``TestGuards.test_four_ops_give_four_answers``)。
        """
        r = BOX.symmetric_difference(CROSS)
        self.assertKindDim(r, 'geometrycollection', 2)
        self.assertEqual(r.geometry_count, 3)
        self.assertArea(r.area(), 100.0)
        line_len = sum(r.geometry_at(i).length()
                       for i in range(r.geometry_count)
                       if r.geometry_at(i).kind == 'polyline')
        self.assertLength(line_len, 10.0, '面外两段各 5')
        u = BOX.union(CROSS)
        self.assertEqual(r.wkt(), u.wkt(), '面×线这一档 union 与 symdiff 必须同款')

    def test_inside_line_intersection(self):
        r = BOX.intersection(INSIDE)
        self.assertKindDim(r, 'polyline', 1)
        self.assertLength(r.length(), 6.0)

    def test_inside_line_is_dropped_from_union_and_symdiff(self):
        """``POLYGON ∪ 完全落在内部的线`` = **纯 POLYGON**(线被丢)。

        对应 ``LineBuilder.isResultLine`` 的闸门⑤:有结果面时,落在面输入
        内部的线边一律不取 —— 只有 ``intersection`` 例外。这条**不许按实测
        反猜**,是照抄 JTS 源码来的。
        """
        for op in ('union', 'symmetric_difference', 'difference'):
            r = getattr(BOX, op)(INSIDE)
            self.assertKindDim(r, 'polygon', 2, op)
            self.assertArea(r.area(), 100.0, op)

    def test_outside_line(self):
        self.assertEmpty(BOX.intersection(OUTSIDE))
        r = BOX.union(OUTSIDE)
        self.assertKindDim(r, 'geometrycollection', 2)
        self.assertEqual(r.geometry_count, 2)
        self.assertArea(r.geometry_at(0).area(), 100.0)
        # ⚠️ ``length()`` 在 GC 上是**各子几何之和**,面那一份贡献的是周长 40
        # —— 要量线就得单独取那一个成员。
        self.assertLength(r.geometry_at(1).length(), 10 * 2 ** 0.5)

    def test_line_minus_polygon(self):
        """``LINESTRING ∖ POLYGON`` = 面外两段,长度 = 线在面外的总长。"""
        r = CROSS.difference(BOX)
        self.assertKindDim(r, 'polyline', 1)
        self.assertEqual(len(r.xy_parts), 2, '两段')
        self.assertLength(r.length(), 10.0)
        self.assertLength(CROSS.length(), 20.0)

    def test_line_intersection_is_commutative(self):
        a = BOX.intersection(CROSS)
        b = CROSS.intersection(BOX)
        self.assertEqual(a.wkt(), b.wkt())
        self.assertEqual(a.kind, b.kind)


# ----------------------------------------------------------------------------
# 二、线 × 线
# ----------------------------------------------------------------------------
class TestLineVsLine(_Assert):
    """两个折线输入 —— 交点走 ``IntersectionPointBuilder``,共线段走
    ``LineBuilder``(两条输入的"线"标签在合并时 delta 都是 0)。"""

    def test_crossing_gives_a_point(self):
        """交叉 → ``POINT``(⚠️ 不是空,也不是线)。"""
        r = _g('LINESTRING(0 0,10 0)').intersection(_g('LINESTRING(5 -5,5 5)'))
        self.assertKindDim(r, 'point', 0)
        self.assertEqual(r.coordinates, (5.0, 0.0))

    def test_touching_at_endpoints_gives_a_point(self):
        r = _g('LINESTRING(0 0,10 0)').intersection(_g('LINESTRING(10 0,20 0)'))
        self.assertKindDim(r, 'point', 0)
        self.assertEqual(r.coordinates, (10.0, 0.0))

    def test_collinear_overlap_gives_the_overlap(self):
        r = _g('LINESTRING(0 0,10 0)').intersection(_g('LINESTRING(5 0,15 0)'))
        self.assertKindDim(r, 'polyline', 1)
        self.assertEqual(len(r.xy_parts), 1, '共线段是一条,不是两条')
        self.assertLength(r.length(), 5.0)

    def test_disjoint_gives_an_empty_line(self):
        """相离的两条线 → 空 —— ⚠️ 定型是 ``LINESTRING``(1 维),不是面。"""
        r = _g('LINESTRING(0 0,1 0)').intersection(_g('LINESTRING(0 5,1 5)'))
        self.assertEmpty(r)
        self.assertKindDim(r, 'polyline', 1)

    def test_difference_cuts_at_the_crossing(self):
        r = _g('LINESTRING(0 0,10 0)').difference(_g('LINESTRING(5 0,15 0)'))
        self.assertKindDim(r, 'polyline', 1)
        self.assertLength(r.length(), 5.0)

    def test_union_nodes_both_lines(self):
        """并集要把两条线在交点处**节点化**(4 段,长度 = 两者之和)。"""
        a = _g('LINESTRING(0 0,10 0)')
        b = _g('LINESTRING(5 -5,5 5)')
        r = a.union(b)
        self.assertKindDim(r, 'polyline', 1)
        self.assertEqual(len(r.xy_parts), 4)
        self.assertLength(r.length(), a.length() + b.length())


# ----------------------------------------------------------------------------
# 三、点 × 点(JTS ``OverlayPoints``)
# ----------------------------------------------------------------------------
class TestPointVsPoint(_Assert):

    def test_same_point(self):
        p = _g('POINT(1 2)')
        self.assertEqual(p.intersection(p).coordinates, (1.0, 2.0))
        self.assertEqual(p.union(p).coordinates, (1.0, 2.0))
        self.assertEmpty(p.difference(p))
        self.assertEmpty(p.symmetric_difference(p))

    def test_empty_result_is_a_point(self):
        """⚠️ 相离两点的交集是 ``POINT EMPTY`` —— 按 ``resultDimension`` 定型,
        不是 ``POLYGON EMPTY``,也不是 ``None``。"""
        r = _g('POINT(1 2)').intersection(_g('POINT(3 4)'))
        self.assertEmpty(r)
        self.assertKindDim(r, 'point', 0)

    def test_union_and_symdiff_are_multipoints(self):
        a, b = _g('POINT(1 2)'), _g('POINT(3 4)')
        for op in ('union', 'symmetric_difference'):
            r = getattr(a, op)(b)
            self.assertKindDim(r, 'multipoint', 0, op)
            self.assertEqual(r.point_count, 2)

    def test_first_occurrence_wins_on_duplicates(self):
        """同一 XY 重复出现只留一份(``buildPointMap`` 的合并语义)。"""
        r = _g('MULTIPOINT(1 2,1 2,3 4)').union(_g('MULTIPOINT(3 4,5 6)'))
        self.assertKindDim(r, 'multipoint', 0)
        self.assertEqual(r.point_count, 3)

    def test_multipoint_intersection(self):
        r = _g('MULTIPOINT(1 2,3 4)').intersection(_g('MULTIPOINT(3 4,5 6)'))
        self.assertKindDim(r, 'point', 0, '只剩一个点 → 是 Point 不是 MultiPoint')
        self.assertEqual(r.coordinates, (3.0, 4.0))

    def test_first_occurrence_wins_including_its_z(self):
        """``OverlayPoints.copyPoint`` 抄的是**胜出那一份的**坐标序列 ——
        XY 相同时 Z 跟着走,不是"XY 取自 A、Z 另算"。

        实测 GEOS 3.13.1:``POINT Z(5 5 3) ∩ POINT(5 5)`` → ``POINT Z(5 5 3)``,
        反过来是 ``POINT (5 5)``。⚠️ 这两条**不一样**是刻意的 —— 交集抄的是
        ``map0``(= A 侧)那一份。专治"坐标和 Z 分两处拼"。
        """
        pz, p2 = _g('POINT Z(5 5 3)'), _g('POINT(5 5)')
        r = pz.intersection(p2)
        self.assertTrue(r.has_z, 'A 侧带 Z,结果就该带 Z')
        self.assertEqual(r.coordinates, (5.0, 5.0, 3.0))
        r2 = p2.intersection(pz)
        self.assertFalse(r2.has_z, 'A 侧是 2D,结果就不该有 Z')
        self.assertEqual(r2.coordinates, (5.0, 5.0))
        # 并集同理:先收 A 的,再收 B 里"A 没有的"。
        u = pz.union(_g('POINT(5 5)'))
        self.assertTrue(u.has_z)
        self.assertEqual(u.coordinates, (5.0, 5.0, 3.0))

    def test_first_occurrence_wins_within_one_multipoint(self):
        """⚠️ 去重是**在一个点表内部**做的,所以同一 XY 的不同 Z 也要先到先得。

        实测 GEOS 3.13.1:``MULTIPOINT Z(1 2 5,1 2 9) ∩ POINT Z(1 2 7)`` →
        ``POINT Z (1 2 5)``(不是 9,也不是 7)。专治"先收进 dict 再覆盖" ——
        那样留下的是**最后一个**。
        """
        a = _g('MULTIPOINT Z(1 2 5,1 2 9)')
        self.assertEqual(a.point_count, 2, '输入本身两个点都还在')
        for op in ('intersection', 'union'):
            r = getattr(a, op)(_g('POINT Z(1 2 7)'))
            self.assertKindDim(r, 'point', 0, op)
            self.assertEqual(r.coordinates, (1.0, 2.0, 5.0), op)

    def test_multipoint_symmetric_difference(self):
        r = _g('MULTIPOINT(1 2,3 4)').symmetric_difference(
            _g('MULTIPOINT(3 4,5 6)'))
        self.assertKindDim(r, 'multipoint', 0)
        self.assertEqual(r.point_count, 2)


# ----------------------------------------------------------------------------
# 四、点 × 非点(JTS ``OverlayMixedPoints``)
# ----------------------------------------------------------------------------
class TestPointVsPolygon(_Assert):

    def test_union_with_an_inside_point_is_just_the_polygon(self):
        r = BOX.union(_g('POINT(5 5)'))
        self.assertKindDim(r, 'polygon', 2)
        self.assertArea(r.area(), 100.0)

    def test_union_with_an_outside_point_is_a_collection(self):
        r = BOX.union(_g('POINT(99 99)'))
        self.assertKindDim(r, 'geometrycollection', 2)
        self.assertEqual(r.geometry_count, 2)
        self.assertKindDim(r.geometry_at(1), 'point', 0)

    def test_intersection_needs_containment(self):
        self.assertEqual(BOX.intersection(_g('POINT(5 5)')).coordinates,
                         (5.0, 5.0))
        r = BOX.intersection(_g('POINT(99 99)'))
        self.assertEmpty(r)
        self.assertKindDim(r, 'point', 0, '空结果按 min(2,0) 定成 0 维')

    def test_point_on_the_boundary_counts_as_covered(self):
        """边界上的点算"被盖住"(``IndexedPointInAreaLocator`` 的 ``BOUNDARY``
        也不是 ``EXTERIOR``)。"""
        on_edge = _g('POINT(0 5)')
        self.assertEqual(BOX.intersection(on_edge).coordinates, (0.0, 5.0))
        self.assertKindDim(BOX.union(on_edge), 'polygon', 2)

    def test_difference_both_directions(self):
        """``面 ∖ 点`` 是**面原样**(点改不了面的环);``点 ∖ 面`` 才看点在哪。"""
        self.assertKindDim(BOX.difference(_g('POINT(5 5)')), 'polygon', 2)
        self.assertArea(BOX.difference(_g('POINT(99 99)')).area(), 100.0)
        self.assertEqual(_g('POINT(99 99)').difference(BOX).coordinates,
                         (99.0, 99.0))
        self.assertEmpty(_g('POINT(5 5)').difference(BOX))

    def test_union_and_symdiff_agree_on_points(self):
        """``OverlayMixedPoints`` 的类注释:UNION 与 SYMDIFFERENCE **输出相同**。"""
        a = BOX.symmetric_difference(_g('POINT(99 99)'))
        b = BOX.union(_g('POINT(99 99)'))
        self.assertEqual(a.kind, b.kind)
        self.assertArea(a.area(), b.area())

    def test_duplicate_outside_points_collapse(self):
        """重复的坐标只留一份(``HashSet<Coordinate>`` 去重),但留下的各占一个
        成员 —— 混维度时点**不**并成 ``MULTIPOINT``。"""
        r = BOX.union(_g('MULTIPOINT(99 99,99 99,98 98)'))
        self.assertKindDim(r, 'geometrycollection', 2)
        self.assertEqual(r.geometry_count, 3, '面 + 两个去重后的点')
        self.assertKindDim(r.geometry_at(1), 'point', 0)
        self.assertKindDim(r.geometry_at(2), 'point', 0)
        pts = {(r.geometry_at(1).coordinates, r.geometry_at(2).coordinates)}
        self.assertEqual(pts, {((99.0, 99.0), (98.0, 98.0))})

    def test_multi_shell_polygon_splits_into_separate_members(self):
        """⚠️ 面那一维有**两个**壳时,GC 里是**两个** ``POLYGON`` 成员,不是一个
        带两个壳的 ``MULTIPOLYGON`` 成员 —— 实测 GEOS 3.13.1::

            MULTIPOLYGON(两个方块).union(POINT(99 99))
              -> GEOMETRYCOLLECTION (POLYGON (...), POLYGON (...), POINT (99 99))

        专治"按维合并成 MULTI* 再装 GC"。切法见 ``geometry._polygon_groups``。
        """
        mp = _g('MULTIPOLYGON(((0 0,10 0,10 10,0 10,0 0)),'
                '((20 20,30 20,30 30,20 30,20 20)))')
        r = mp.union(_g('POINT(99 99)'))
        self.assertKindDim(r, 'geometrycollection', 2)
        self.assertEqual(r.geometry_count, 3)
        self.assertKindDim(r.geometry_at(0), 'polygon', 2)
        self.assertKindDim(r.geometry_at(1), 'polygon', 2)
        self.assertKindDim(r.geometry_at(2), 'point', 0)
        for i in (0, 1):
            self.assertEqual(len(r.geometry_at(i).xy_parts), 1, '一个壳一个环')
            self.assertArea(r.geometry_at(i).area(), 100.0)
        # 单个壳时还是"一个成员"(不是 MultiPolygon,本库里本来就是 polygon)。
        self.assertEqual(BOX.union(_g('POINT(99 99)')).geometry_count, 2)

    def test_multi_shell_polygon_with_a_line_splits_both(self):
        """两个维度都多分量:``GC(面, 面, 线, 线)`` —— 实测 4 个成员。"""
        mp = _g('MULTIPOLYGON(((0 0,10 0,10 10,0 10,0 0)),'
                '((20 30,30 30,30 20,20 20,20 30)))')
        r = mp.union(CROSS)
        self.assertKindDim(r, 'geometrycollection', 2)
        self.assertEqual(r.geometry_count, 4)
        for i in range(4):
            self.assertKindDim(r.geometry_at(i),
                               'polygon' if i < 2 else 'polyline',
                               2 if i < 2 else 1)
        self.assertArea(r.area(), 200.0)

    def test_hole_grouping_survives_the_split(self):
        """⚠️ 切开时**洞必须跟着它的壳走**。铸一个"带洞的方块 + 面外的线"的
        GC,断言面那一份仍是一个带洞的面 —— 洞丢了会多出 4 的面积。"""
        holed = _g('POLYGON((0 0,10 0,10 10,0 10,0 0),'
                   '(4 4,4 6,6 6,6 4,4 4))')
        r = holed.union(_g('LINESTRING(20 20,30 30)'))
        self.assertKindDim(r, 'geometrycollection', 2)
        poly = r.geometry_at(0)
        self.assertKindDim(poly, 'polygon', 2)
        self.assertArea(poly.area(), 96.0, '洞没跟着壳走的话这里会是 100')
        self.assertEqual(len(poly.xy_parts), 2, '一个壳 + 一个洞')
        self.assertEqual(list(poly._shells), [True, False])


class TestPointVsLine(_Assert):

    def test_point_on_the_line(self):
        line = _g('LINESTRING(0 0,10 0)')
        self.assertEqual(line.intersection(_g('POINT(5 0)')).coordinates,
                         (5.0, 0.0))
        self.assertEmpty(line.intersection(_g('POINT(5 5)')))

    def test_point_off_the_line_is_kept_by_union(self):
        r = _g('LINESTRING(0 0,10 0)').union(_g('POINT(5 5)'))
        # ⚠️ GC 的 dimension 取子几何**最大**的那个 —— 线(1)+ 点(0) → 1,不是 2。
        self.assertKindDim(r, 'geometrycollection', 1)
        self.assertKindDim(r.geometry_at(0), 'polyline', 1)
        self.assertKindDim(r.geometry_at(1), 'point', 0)

    def test_endpoint_counts_as_covered(self):
        """线的端点:JTS ``IndexedPointOnLineLocator`` 只要落在**任一线段**上就算
        内部(不分端点),所以端点上的点是被盖住的。"""
        line = _g('LINESTRING(0 0,10 0)')
        self.assertEqual(line.intersection(_g('POINT(10 0)')).coordinates,
                         (10.0, 0.0))


# ----------------------------------------------------------------------------
# 五、``GEOMETRYCOLLECTION`` 输入
# ----------------------------------------------------------------------------
class TestCollectionInput(_Assert):
    """GC 输入**不走** OverlayNG(GEOS 的 ``HeuristicOverlay`` 转给
    ``StructuredCollection``)。本库只做"摊平成齐次部分"那一半 —— 见
    ``geometry._flatten_overlay_input``。"""

    def test_homogeneous_polygon_collection(self):
        gc = _g('GEOMETRYCOLLECTION (POLYGON((0 0,5 0,5 5,0 5,0 0)), '
                'POLYGON((20 20,25 20,25 25,20 25,20 20)))')
        r = gc.intersection(_sq(0, -5, 30, 30))
        self.assertKindDim(r, 'polygon', 2)
        self.assertArea(r.area(), 50.0)
        self.assertEqual(sum(1 for s in r._shells if s), 2, '两个壳')

    def test_homogeneous_line_collection(self):
        gc = _g('GEOMETRYCOLLECTION (LINESTRING(0 0,10 0), '
                'LINESTRING(10 0,10 10))')
        r = gc.intersection(_g('LINESTRING(5 -5,5 5)'))
        self.assertKindDim(r, 'point', 0)
        self.assertEqual(r.coordinates, (5.0, 0.0))

    def test_homogeneous_point_collection(self):
        gc = _g('GEOMETRYCOLLECTION (POINT(1 2), POINT(3 4))')
        r = gc.union(_g('POINT(5 6)'))
        self.assertKindDim(r, 'multipoint', 0)
        self.assertEqual(r.point_count, 3)

    def test_nested_and_empty_children_are_flattened(self):
        gc = _g('GEOMETRYCOLLECTION (GEOMETRYCOLLECTION (POINT(1 2)), '
                'POINT EMPTY)')
        r = gc.union(_g('POINT(3 4)'))
        self.assertEqual(r.point_count, 2)

    def test_collection_of_only_empties_behaves_like_an_empty_input(self):
        gc = _g('GEOMETRYCOLLECTION (POLYGON EMPTY, POINT EMPTY)')
        r = gc.union(_sq(0, 0, 1, 1))
        self.assertKindDim(r, 'polygon', 2)
        self.assertArea(r.area(), 1.0)

    def test_mixed_dimension_collection_is_refused(self):
        """维度混杂的 GC → ``NotImplementedError``,消息里点得出维度。

        ⚠️ 这是**刻意的缺口**:GEOS 会把它交给 ``StructuredCollection``
        逐维度算完再拼,本库没复刻。宁可明说,也不静默丢掉一个维度。
        """
        gc = _g('GEOMETRYCOLLECTION (POLYGON((0 0,1 0,1 1,0 1,0 0)), '
                'LINESTRING(0 0,1 1))')
        with self.assertRaises(NotImplementedError) as ctx:
            gc.union(_sq(0, 0, 1, 1))
        msg = str(ctx.exception)
        self.assertIn('GEOMETRYCOLLECTION', msg)
        self.assertIn('StructuredCollection', msg)
        # 两个几何都要查 —— 只看 self 会漏掉"右面是 GC"这一半。
        with self.assertRaises(NotImplementedError):
            _sq(0, 0, 1, 1).union(gc)

    def test_collection_input_does_not_break_the_single_kind_result(self):
        """摊平之后走的是**普通**那三路,结果类型不该多出 GC 这一层。"""
        gc = _g('GEOMETRYCOLLECTION (POLYGON((0 0,5 0,5 5,0 5,0 0)))')
        r = gc.intersection(_sq(1, 1, 3, 3))
        self.assertKindDim(r, 'polygon', 2)
        self.assertArea(r.area(), 4.0)


# ----------------------------------------------------------------------------
# 六、"有牙齿"的守卫
# ----------------------------------------------------------------------------
class TestGuards(_Assert):

    def test_four_ops_give_four_answers(self):
        """同一对输入,四个算子必须给出**四个不同**的答案 ——
        专治"op 参数被忽略 / 抄错一格"。

        ⚠️ 这一对刻意选**面 × 面**(重叠 5×5 的两个方块),因为四个算子在
        这一档给出四个不同的面积(25 / 175 / 75 / 150),而结果 kind 全是
        ``polygon`` —— 唯一的区分信号就是数,守卫因此最紧。**面 × 线**那一对
        用不了:那两个输入下 ``union`` 与 ``symmetric_difference`` 在数学上
        **必然**同款(见 ``TestPolygonVsLine.test_crossing_symmetric_difference``)。
        """
        a, b = _sq(0, 0, 10, 10), _sq(5, 5, 15, 15)
        areas = {}
        for op in _OPS:
            r = getattr(a, op)(b)
            self.assertKindDim(r, 'polygon', 2, op)
            areas[op] = round(r.area(), 9)
        self.assertEqual(len(set(areas.values())), 4,
                         '四个算子给出了重复面积:%r' % (areas,))
        self.assertEqual(areas, {'intersection': 25.0, 'union': 175.0,
                                 'difference': 75.0,
                                 'symmetric_difference': 150.0})

    def test_four_ops_give_four_answers_on_lines_and_points(self):
        """线 / 点那一档另取一对,同样要求四个答案互不相同。

        ``LINESTRING(0 0,10 0)`` 与 ``LINESTRING(5 0,15 0)`` 共线重叠:
        交集 = 共线段(长 5)、并集 = 整条(长 15)、``A∖B`` = 左段(长 5)、
        ``A△B`` = 两头两段(长 10)。

        ⚠️ 签名里**必须**带包围盒:交集与差集的 ``(kind, part 数, 长度)``
        **完全一样**(都是"一条长 5 的 LINESTRING"),差别只在**位置** ——
        实测 GEOS 也是 ``LINESTRING (5 0, 10 0)`` 与 ``LINESTRING (0 0, 5 0)``。
        只用长度当签名的话这条守卫会把一个正确实现误判成红的。
        """
        a, b = _g('LINESTRING(0 0,10 0)'), _g('LINESTRING(5 0,15 0)')

        def sig(g):
            return (g.kind, len(g.xy_parts), round(g.length(), 9), g.envelope())

        seen = {op: sig(getattr(a, op)(b)) for op in _OPS}
        self.assertEqual(len(set(seen.values())), 4, '重复答案:%r' % (seen,))
        self.assertEqual(seen['intersection'], ('polyline', 1, 5.0,
                                                (5.0, 0.0, 10.0, 0.0)))
        self.assertEqual(seen['union'], ('polyline', 3, 15.0,
                                         (0.0, 0.0, 15.0, 0.0)))
        self.assertEqual(seen['difference'], ('polyline', 1, 5.0,
                                              (0.0, 0.0, 5.0, 0.0)))
        self.assertEqual(seen['symmetric_difference'], ('polyline', 2, 10.0,
                                                        (0.0, 0.0, 15.0, 0.0)))

    def test_four_ops_on_points_give_four_answers(self):
        a, b = _g('POINT(1 2)'), _g('POINT(3 4)')
        got = {}
        for op in _OPS:
            r = getattr(a, op)(b)
            got[op] = (r.kind, r.is_empty, r.point_count)
        self.assertEqual(got['intersection'], ('point', True, 0))
        self.assertEqual(got['union'], ('multipoint', False, 2))
        self.assertEqual(got['difference'], ('point', False, 1))
        self.assertEqual(got['symmetric_difference'], ('multipoint', False, 2))
        # ⚠️ 并集与对称差在这一档同为 MULTIPOINT 两个点 —— 与面×线那个
        # union == symdiff 同性质,不是 bug。区分它们要靠**相同**的两个点:
        self.assertEqual(_g('POINT(1 2)').union(_g('POINT(1 2)')).point_count, 1)
        self.assertTrue(_g('POINT(1 2)').symmetric_difference(
            _g('POINT(1 2)')).is_empty)

    def test_difference_is_not_commutative(self):
        a = BOX.difference(CROSS)
        b = CROSS.difference(BOX)
        self.assertNotEqual(a.kind, b.kind, 'A∖B 是面、B∖A 是线')

    def test_union_and_intersection_are_commutative(self):
        self.assertEqual(BOX.union(CROSS).wkt(), CROSS.union(BOX).wkt())
        self.assertEqual(BOX.intersection(CROSS).wkt(),
                         CROSS.intersection(BOX).wkt())

    def test_a_subset_b_difference_is_empty(self):
        """⚠️ 专治"逐边看标签"那个朴素做法 —— ``A ⊂ B`` 时 A 的每条边在局部
        看都是"A 内部在左",朴素判据会把 A 的边界全留下。"""
        small = _sq(2, 2, 4, 4)
        r = small.difference(BOX)
        self.assertEmpty(r)
        self.assertKindDim(r, 'polygon', 2)
        self.assertArea(small.symmetric_difference(BOX).area(), 96.0)

    def test_empty_result_kind_follows_the_dimension(self):
        """空结果的**维度**必须对:面 ∖ 面 → 2、线 ∩ 线 → 1、点 ∩ 点 → 0。"""
        cases = [
            (_sq(2, 2, 4, 4), BOX, 'difference', 'polygon', 2),
            (_g('LINESTRING(0 0,1 0)'), _g('LINESTRING(0 5,1 5)'),
             'intersection', 'polyline', 1),
            (_g('POINT(1 2)'), _g('POINT(3 4)'), 'intersection', 'point', 0),
        ]
        for a, b, op, kind, dim in cases:
            self.assertKindDim(getattr(a, op)(b), kind, dim, op)

    def test_result_shells_are_populated(self):
        """⚠️ 守卫 ``Geometry._like()`` 丢 ``_shells`` 那个老坑(本项目第 6 次)。

        结果只要往 flat 形式装配就必须**显式**把 ``_shells`` 传进去;这里连
        "面里带线 / 带点"那几条路一起守 —— 混合维度的 GC 分支最容易漏。
        """
        for r in (BOX.difference(CROSS), BOX.union(CROSS).geometry_at(0),
                  BOX.difference(_g('POINT(5 5)'))):
            self.assertKindDim(r, 'polygon', 2)
            self.assertTrue(r._shells, '_shells 被丢了')
            self.assertEqual(len(r._shells), len(r.xy_parts))

    def test_collection_children_keep_their_own_shells(self):
        r = BOX.union(CROSS)
        self.assertEqual(r.kind, 'geometrycollection')
        poly = r.geometry_at(0)
        self.assertTrue(poly._shells, 'GC 子几何的 _shells 被丢了')

    def test_z_survives_into_lines_and_points(self):
        """只要有一边带 Z,结果就带 Z —— 线与点那两档同样成立。"""
        pz = _g('POLYGON Z((0 0 5,10 0 5,10 10 5,0 10 5,0 0 5))')
        r = pz.intersection(CROSS)
        self.assertTrue(r.has_z)
        self.assertKindDim(r, 'polyline', 1)
        g = pz.union(_g('POINT(99 99)'))
        self.assertTrue(g.has_z, 'GC 整体带 Z')
        self.assertTrue(g.geometry_at(0).has_z, '面那一份带 Z')
        self.assertFalse(g.geometry_at(1).has_z, '2D 的点那一份**不该**凭空长 Z')

    def test_m_is_dropped(self):
        r = _g('LINESTRING M(0 5 1,10 5 2)').intersection(BOX)
        self.assertFalse(r.has_m)

    def test_bounding_box_within_the_inputs(self):
        """四个算子的结果包围盒都必须落在**两个输入包围盒的并**之内。

        不变量,不需要真值:overlay 的任何结果都是输入的"子集"(在集合意义
        下),所以包围盒不可能越界。用一个封闭的方格 + 一条横穿线来测 ——
        结果里有超出 ``BOX`` 但仍在 ``CROSS`` 里的部分(面外那两段线),
        所以要拿"并"比,不能拿 ``BOX`` 比。
        """
        env_a, env_b = BOX.envelope(), CROSS.envelope()
        lo_x = min(env_a[0], env_b[0])
        lo_y = min(env_a[1], env_b[1])
        hi_x = max(env_a[2], env_b[2])
        hi_y = max(env_a[3], env_b[3])
        for op in _OPS:
            r = getattr(BOX, op)(CROSS)
            self.assertFalse(r.is_empty, op)
            env = r.envelope()
            self.assertIsNotNone(env, op)
            self.assertGreaterEqual(env[0], lo_x - 1e-9, op)
            self.assertGreaterEqual(env[1], lo_y - 1e-9, op)
            self.assertLessEqual(env[2], hi_x + 1e-9, op)
            self.assertLessEqual(env[3], hi_y + 1e-9, op)
        # 并集确实用满了两个输入的 x 跨度(左端来自 CROSS,右端也来自 CROSS)。
        self.assertEqual(BOX.union(CROSS).envelope(), (-5.0, 0.0, 15.0, 10.0))


# ----------------------------------------------------------------------------
# 七、不支持的输入必须**明说**
# ----------------------------------------------------------------------------
class TestRefusals(_Assert):

    def test_multipatch_raises(self):
        """multipatch 与谓词 / buffer 同口径:**两个几何都要查**。"""
        from pyopenfilegdb import _constants as C
        box = _sq(0, 0, 10, 10)
        patch = _sq(20, 20, 30, 30)
        patch.shape_type = C.ShapeType.MULTIPATCH
        for op in _OPS:
            with self.assertRaises(NotImplementedError):
                getattr(box, op)(patch)
            with self.assertRaises(NotImplementedError):
                getattr(patch, op)(box)

    def test_multipatch_inside_a_collection_raises(self):
        """GC 里的 multipatch 也得挡住 —— 不然它会一路漏到 ``xy_parts``,
        在那边报一个看不懂的 ``TypeError``。"""
        from pyopenfilegdb import _constants as C
        patch = _sq(20, 20, 30, 30)
        patch.shape_type = C.ShapeType.MULTIPATCH
        gc = Geometry.geometry_collection([patch])
        with self.assertRaises(NotImplementedError):
            _sq(0, 0, 10, 10).union(gc)


if __name__ == '__main__':
    unittest.main()
