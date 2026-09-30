# -*- coding: utf-8 -*-
"""``Geometry`` 的四个 overlay 算子 —— 手推真值、有牙齿的守卫、Z 与空结果口径。

跑法::

    /d/zsh/app/py_3.13.1/python -m unittest tests.test_overlay -v

本文件**不需要样例数据**(全部几何在内存里构造),也**不 import shapely /
osgeo** —— 对拍 GEOS 的活在 ``tools/verify_overlay.py`` 里,只有 ``tools/``
才允许碰它们(见 README「与 GDAL 的关系」)。

真值从哪来
----------
* 每一条面积 / 长度都是**对着几何定义手算**的闭式解(下表的 ``|A∪B|`` 之类),
  **不看实现**。这是本文件存在的理由:它抓的是"实现和理解一起错",比只测不变量可靠。
* 全部用本机 **GEOS 3.13.1**(shapely 2.1.2)实测核对过一遍,写在 ``tools/`` 那个
  对拍脚本里;这里的期望值两边同源。
* ⚠️ **``kind`` 只有四个值**:``polygon`` / ``polyline`` / ``point`` / ``multipoint``。
  ``MULTIPOLYGON`` 在本库**不是**独立的 ``kind`` —— 它是 ``kind == 'polygon'``
  且 ``_shells`` 有**多个** ``True``(见 ``_esri_geometry.py`` 的 WKT 渲染)。
  所以"结果是 ``MULTIPOLYGON``"这条断言要落在 ``_shells`` 上,不是 ``kind`` 上。
* ⚠️ **空结果的 WKT 一律是 ``'GEOMETRYCOLLECTION EMPTY'``**(``_esri_geometry.py``
  的既有行为,不是本轮引入的),所以**空结果的类型只能靠 ``kind`` / ``dimension``
  断言,不能靠 WKT**。本文件专门有一节守这条。

算法出处
--------
GDAL 的 ``OGRGeometry::Difference / Union / Intersection / SymmetricDifference``
在 ``ogrgeometry.cpp`` 里**一律是一句转发给 GEOS**,GDAL 自己一行算法都没有。
所以这里照抄的是 **JTS ``operation/overlayng/``**(GEOS 3.9 起走的就是它,老的
``operation/overlay/OverlayOp`` 在 GEOS 3.12.0 已删)。细节见 ``_overlay_ops.py``
的模块 docstring 与 ``DESIGN.md`` §2.24。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pyopenfilegdb import _constants as C                        # noqa: E402
from pyopenfilegdb.geometry import Geometry                      # noqa: E402

#: 四个算子,统一用 ``getattr`` 取 —— 免得手打方法名打错还测不出。
_OPS = ('intersection', 'union', 'difference', 'symmetric_difference')


def _sq(x0, y0, x1, y1):
    """轴对齐方块。**顶点顺序统一为逆时针**(Esri 要的是顺时针,这里无所谓 ——
    ``from_wkt`` 会把绕向归一化)。"""
    return Geometry.from_wkt(
        'POLYGON((%g %g,%g %g,%g %g,%g %g,%g %g))'
        % (x0, y0, x1, y0, x1, y1, x0, y1, x0, y0))


def _canonical_rings(geom):
    """把结果的每个环**规范化成"从字典序最小的顶点起算"**,再返回可比较的元组。

    ⚠️ 为什么不能直接比 WKT:**GEOS 自己也不满足"交换后逐位相同"** ——
    实测(3.13.1)

        a.union(b) -> POLYGON ((10 0, 0 0, 0 10, 5 10, 5 15, 15 15, 15 5, 10 5, 10 0))
        b.union(a) -> POLYGON ((5 15, 15 15, 15 5, 10 5, 10 0, 0 0, 0 10, 5 10, 5 15))

    两个**是同一个环**,只是起点转了半圈。所以可交换律的正确口径是
    "环相同(允许循环移位)",不是"WKT 逐字节相同"。断言后者会去追一个
    连 oracle 都没有的性质。
    """
    out = []
    for part in geom.xy_parts:
        pts = [(part[2 * i], part[2 * i + 1]) for i in range(len(part) // 2)]
        # 去掉闭合点(``_FlatCoords`` 的环是**开的**;保险起见两种都收)。
        if len(pts) > 1 and pts[0] == pts[-1]:
            pts.pop()
        if not pts:
            continue
        k = min(range(len(pts)), key=lambda i: pts[i])
        out.append(tuple(pts[k:] + pts[:k]))
    return tuple(sorted(out))



def _shell_count(geom):
    """``MULTIPOLYGON`` 的"多边形个数" —— 本库里就是 ``_shells`` 里 ``True`` 的个数。"""
    shells = geom._shells
    if shells is None:
        return 0
    return sum(1 for s in shells if s)


class _OverlayAssert(unittest.TestCase):
    """面积断言统一走这里 —— 容差随量级走,不写死死数。"""

    def assertArea(self, got, want, msg=''):
        delta = 1e-9 * max(1.0, abs(want))
        if abs(got - want) > delta:
            self.fail('%s面积 %.15g,期望 %.15g(差 %.3g,容差 %.3g)'
                      % (msg and msg + ': ', got, want, got - want, delta))

    def assertEmpty(self, geom, msg=''):
        if not geom.is_empty:
            self.fail('%s期望空几何,实际 %s(面积 %.15g)'
                      % (msg and msg + ': ', geom.wkt(), geom.area()))

    def assertKindDim(self, geom, kind, dim, msg=''):
        self.assertEqual(geom.kind, kind, '%s: kind' % (msg or 'geometry'))
        self.assertEqual(geom.dimension, dim, '%s: dimension' % (msg or 'geometry'))


# ----------------------------------------------------------------------------
# 一、面 × 面手推真值表(计划 §6.1 前七行)
# ----------------------------------------------------------------------------
class TestAreaPairs(_OverlayAssert):
    """七种位置关系 × 四个算子。每条都是对着定义手算的。"""

    def test_overlap_5x5(self):
        """``[0,10]²`` 与 ``[5,15]²`` 重叠的是一块 5×5。

        ``|A| = |B| = 100``,``|A∩B| = 25`` ⇒
        ``|A∪B| = 175``、``|A∖B| = |B∖A| = 75``、``|A△B| = 150``。
        """
        a, b = _sq(0, 0, 10, 10), _sq(5, 5, 15, 15)
        self.assertArea(a.intersection(b).area(), 25.0, 'intersection')
        self.assertArea(a.union(b).area(), 175.0, 'union')
        self.assertArea(a.difference(b).area(), 75.0, 'difference')
        self.assertArea(a.symmetric_difference(b).area(), 150.0, 'symdiff')

    def test_overlap_identities(self):
        """容斥恒等式 —— 四个算子的答案必须彼此自洽,不然至少有一个错了。"""
        a, b = _sq(0, 0, 10, 10), _sq(5, 5, 15, 15)
        inter = a.intersection(b).area()
        union = a.union(b).area()
        self.assertArea(union + inter, a.area() + b.area(), '|A∪B| + |A∩B|')
        self.assertArea(a.symmetric_difference(b).area(), union - inter, '|A△B|')
        self.assertArea(a.difference(b).area() + inter, a.area(), '|A∖B| + |A∩B|')

    def test_disjoint(self):
        """相离:交集是**空面**(不是 ``None``),并集/对称差是两块,
        差集原样是 A。"""
        a, b = _sq(0, 0, 10, 10), _sq(20, 0, 30, 10)
        inter = a.intersection(b)
        self.assertEmpty(inter, '相离的 intersection')
        self.assertKindDim(inter, 'polygon', 2, '相离的 intersection')
        for name in ('union', 'symmetric_difference'):
            r = getattr(a, name)(b)
            self.assertArea(r.area(), 200.0, name)
            self.assertEqual(_shell_count(r), 2, '%s 应该是两块' % name)
        self.assertArea(a.difference(b).area(), 100.0, 'difference')

    def test_a_inside_b(self):
        """``A ⊂ B``:差集必须为空 —— 这是**对着朴素做法的一颗牙**,见下。"""
        a, b = _sq(2, 2, 4, 4), _sq(0, 0, 10, 10)
        self.assertEmpty(a.difference(b), 'A ⊂ B 的 difference')
        self.assertKindDim(a.difference(b), 'polygon', 2, 'A ⊂ B 的 difference')
        self.assertArea(a.union(b).area(), 100.0, 'union')
        self.assertArea(a.intersection(b).area(), 4.0, 'intersection')
        self.assertEqual(_shell_count(a.intersection(b)), 1)
        self.assertArea(a.symmetric_difference(b).area(), 96.0, 'symdiff')

    def test_identical(self):
        """完全相同:差集与对称差都空,并集与交集是自身。

        ⚠️ 断言落在**面积 + 单壳**上,不落在 WKT 上 —— 共线顶点会被保留
        (见模块级说明与 ``DESIGN.md §2.24``),逐位相同不是本库的承诺。
        """
        a, b = _sq(0, 0, 10, 10), _sq(0, 0, 10, 10)
        for name in ('difference', 'symmetric_difference'):
            self.assertEmpty(getattr(a, name)(b), '相同的 %s' % name)
        for name in ('union', 'intersection'):
            r = getattr(a, name)(b)
            self.assertArea(r.area(), 100.0, name)
            self.assertEqual(_shell_count(r), 1, name)

    def test_edge_sharing_intersection_is_a_line(self):
        """只共一条边 ⇒ **交集是线**,不是空面。

        没有任何一个面的内部是"两边都算内部"的,所以面判据一个都挑不出来;
        GEOS 靠的是"这条边同时是两个输入的边界"(JTS ``isBoundaryBoth``),
        把它当"两边都算 INTERIOR"再套 ``isResultOfOp``,于是进了**线结果列表**。
        这条专治"只做面列表"的半成品实现。
        """
        a, b = _sq(0, 0, 10, 10), _sq(10, 0, 20, 10)
        inter = a.intersection(b)
        self.assertKindDim(inter, 'polyline', 1, '共边的 intersection')
        self.assertArea(inter.length(), 10.0, '共边的 intersection 长度')
        self.assertEqual(inter.point_count, 2)

    def test_edge_sharing_other_ops(self):
        """共边的并集是**一个** 200 的方块(不是两块),差集原样是 A。"""
        a, b = _sq(0, 0, 10, 10), _sq(10, 0, 20, 10)
        u = a.union(b)
        self.assertArea(u.area(), 200.0, '共边的 union')
        self.assertEqual(_shell_count(u), 1, '共边的 union 应该合成一块')
        self.assertArea(a.difference(b).area(), 100.0, '共边的 difference')
        s = a.symmetric_difference(b)
        self.assertArea(s.area(), 200.0, '共边的 symdiff')
        self.assertEqual(_shell_count(s), 1, '共边的 symdiff 应该合成一块')

    def test_point_touching(self):
        """只共一个点 ⇒ 交集是**点**,并集/对称差是**两块**。

        共点**不合并** —— 两个面各留一个壳。这条专治 ``_assemble`` 把共点面合一。
        """
        a, b = _sq(0, 0, 10, 10), _sq(10, 10, 20, 20)
        inter = a.intersection(b)
        self.assertKindDim(inter, 'point', 0, '共点的 intersection')
        self.assertFalse(inter.is_empty)
        self.assertEqual(inter.point_count, 1)
        for name in ('union', 'symmetric_difference'):
            r = getattr(a, name)(b)
            self.assertArea(r.area(), 200.0, name)
            self.assertEqual(_shell_count(r), 2, '%s 应该留两块' % name)
        self.assertArea(a.difference(b).area(), 100.0, '共点的 difference')

    def test_overlapping_squares_symdiff_is_two_lobes(self):
        """共边重叠(交集是**面**)的两方块,对称差是**两个 L 形**,不是一个带洞的面。

        专治走环方向搞反:``_planar._result_rings`` 在"两个结果瓣只共一个节点"的
        节点上,若取"逆时针后继"而不是"顺时针前任",就会贴着外圈走,把结果装配成
        ``POLYGON(并集外圈, 交集方块当洞)`` —— 面积一样是 150,几何却**非法**
        (洞的边界在 ``(5 10)`` / ``(10 5)`` 两处碰到外壳,GEOS 判
        ``Interior is disconnected``),而正确结果是两块 L 形。
        """
        a, b = _sq(0, 0, 10, 10), _sq(5, 5, 15, 15)
        r = a.symmetric_difference(b)
        self.assertArea(r.area(), 150.0, '对称差')
        self.assertEqual(_shell_count(r), 2, '两个 L 形,不是"外圈 + 洞"')
        self.assertEqual(r._shells, [True, True], '两块都是壳,没有洞')
        self.assertEqual(r.part_count, 2)
        for i in range(2):
            self.assertEqual(len(r.xy_parts[i]) // 2, 6, '每块 L 形 6 个顶点')

    def test_hole_is_filled_by_a_plug(self):
        """带 2×2 洞的 10×10 方块,并上恰好盖住洞的小方块 ⇒ 洞被填平,面积 100。"""
        hole = Geometry.from_wkt(
            'POLYGON((0 0,10 0,10 10,0 10,0 0),(4 4,6 4,6 6,4 6,4 4))')
        plug = _sq(4, 4, 6, 6)
        self.assertArea(hole.area(), 96.0, '带洞方块自身')
        u = hole.union(plug)
        self.assertArea(u.area(), 100.0, '填洞后的 union')
        self.assertEqual(_shell_count(u), 1, '洞填平后应当只剩一个壳')
        self.assertFalse(any(s is False for s in u._shells), '不该再有洞')

    def test_hole_intersection_and_difference(self):
        """洞**不算**面的内部:交集为空面积,差集仍是原来那个带洞的方块。"""
        hole = Geometry.from_wkt(
            'POLYGON((0 0,10 0,10 10,0 10,0 0),(4 4,6 4,6 6,4 6,4 4))')
        plug = _sq(4, 4, 6, 6)
        self.assertArea(hole.intersection(plug).area(), 0.0, '洞 ∩ 塞子')
        d = hole.difference(plug)
        self.assertArea(d.area(), 96.0, '洞 ∖ 塞子')
        self.assertEqual(_shell_count(d), 1)
        self.assertIn(False, d._shells, '差集里那个洞应该还在')


# ----------------------------------------------------------------------------
# 二、"有牙齿"的守卫(计划 §6.2 前四条)
# ----------------------------------------------------------------------------
class TestGuards(_OverlayAssert):
    """每条守卫都对应一种具体的写错方式 —— 注释里点名是哪种。"""

    def test_four_ops_give_four_answers(self):
        """四个算子在同一对输入上必须给出**四个不同**的面积。

        专治 ``op`` 参数被忽略 / 判据表抄错一格(比如 UNION 与 SYMDIFFERENCE
        共用一行)。这里选的输入让四个面积两两不等:25 / 175 / 75 / 150。
        """
        a, b = _sq(0, 0, 10, 10), _sq(5, 5, 15, 15)
        areas = [getattr(a, op)(b).area() for op in _OPS]
        self.assertEqual(len(set(round(v, 9) for v in areas)), 4,
                         '四个算子的面积撞车了: %r' % (areas,))

    def test_difference_is_not_commutative(self):
        """``A∖B`` 与 ``B∖A`` 是两个不同的东西 —— 专治实参传反。

        ⚠️ 这对输入要挑**面积不等**的:A = ``[0,10]²``(100)、
        B = ``[5,5]×[15,20]``(10×15 = 150),重叠 = ``[5,10]²``(25)。
        于是 ``|A∖B| = 75``、``|B∖A| = 125`` —— 传反了一定看得出来。
        (两个全等的方块会给 75 / 75,那是**测不出**传反的。)
        """
        a, b = _sq(0, 0, 10, 10), _sq(5, 5, 15, 20)
        self.assertArea(a.difference(b).area(), 75.0, 'A∖B')
        self.assertArea(b.difference(a).area(), 125.0, 'B∖A')

    def test_union_and_intersection_commute(self):
        """并集与交集**可交换** —— 而且是**同一个环**,允许循环移位。

        ⚠️ 不能断言 WKT 逐字节相同:GEOS 3.13.1 自己也不满足(实测见
        ``_canonical_rings`` 的 docstring),它只是把起点转了个位置。
        真正的不变量是"环相同(可循环移位)+ 确定性"。
        """
        a, b = _sq(0, 0, 10, 10), _sq(5, 5, 15, 15)
        for op in ('union', 'intersection'):
            self.assertEqual(_canonical_rings(getattr(a, op)(b)),
                             _canonical_rings(getattr(b, op)(a)),
                             '%s 不交换' % op)

    def test_subset_difference_is_empty(self):
        """``A ⊂ B`` ⇒ ``A∖B`` 为空,**专治"逐边看标签"那个朴素做法**。

        朴素做法会看到 A 的每条边局部都是"A 的内部在左、外部在右",而 B 的边界
        不穿过它,于是 ``locB`` 停在 ``EXTERIOR``,把 A 的边全留下 ⇒ 错答 A 自身。
        所以必须做**面级定位**(BFS),不能只看边的局部标签。
        """
        a, b = _sq(2, 2, 4, 4), _sq(0, 0, 10, 10)
        self.assertEmpty(a.difference(b), 'A ⊂ B')
        self.assertArea(a.difference(b).area(), 0.0, 'A ⊂ B')

    def test_b_vs_a_difference_is_not_empty(self):
        """上一条的对偶:``B∖A`` **非空**(面积 96)。两条一起才说明不是"恒返回空"。"""
        a, b = _sq(2, 2, 4, 4), _sq(0, 0, 10, 10)
        self.assertFalse(b.difference(a).is_empty)
        self.assertArea(b.difference(a).area(), 96.0, 'B∖A')


# ----------------------------------------------------------------------------
# 三、空结果的类型必须对(专治"一律返回 POLYGON EMPTY")
# ----------------------------------------------------------------------------
class TestEmptyResults(_OverlayAssert):
    """⚠️ 空结果的 WKT 一律渲染成 ``'GEOMETRYCOLLECTION EMPTY'``,所以
    **只能断言 ``kind`` / ``dimension`` / ``is_empty``** —— 断言 WKT 会测不出东西。"""

    def test_subset_difference_is_an_empty_polygon(self):
        """``A ⊂ B`` 的差集是 dimension 2 的空几何(JTS ``resultDimension``:
        ``DIFFERENCE`` 取第一个输入的维)。"""
        r = _sq(2, 2, 4, 4).difference(_sq(0, 0, 10, 10))
        self.assertTrue(r.is_empty)
        self.assertKindDim(r, 'polygon', 2, 'A ⊂ B 的 difference')

    def test_disjoint_intersection_is_an_empty_polygon(self):
        """相离两面的交集是空面 —— ``INTERSECTION`` 取 ``min(dim)`` = 2。"""
        r = _sq(0, 0, 10, 10).intersection(_sq(20, 0, 30, 10))
        self.assertTrue(r.is_empty)
        self.assertKindDim(r, 'polygon', 2, '相离的 intersection')

    def test_identical_difference_is_an_empty_polygon(self):
        r = _sq(0, 0, 10, 10).difference(_sq(0, 0, 10, 10))
        self.assertTrue(r.is_empty)
        self.assertKindDim(r, 'polygon', 2, '相同的 difference')

    def test_empty_result_is_not_none(self):
        """空结果**不是 ``None``** —— 是带类型的空几何。"""
        r = _sq(0, 0, 10, 10).intersection(_sq(20, 0, 30, 10))
        self.assertIsNotNone(r)
        self.assertIsInstance(r, Geometry)

    def test_empty_polygon_input(self):
        """空输入不炸:空面 ∪ 方块 = 方块,空面 ∩ 方块 = 空面。"""
        empty = Geometry.from_wkt('POLYGON EMPTY')
        box = _sq(0, 0, 10, 10)
        self.assertArea(empty.union(box).area(), 100.0, '空 ∪ 方')
        self.assertEmpty(empty.intersection(box), '空 ∩ 方')


# ----------------------------------------------------------------------------
# 四、装配口径(守 ``_like()`` 丢 ``_shells`` 那个老坑)
# ----------------------------------------------------------------------------
class TestAssembly(_OverlayAssert):
    """本项目第 6 次踩 ``_like()`` 丢 ``_shells``/``_groups``/``_env``。
    这里提前上锁:结果**必须**走 flat 形式并显式赋 ``_shells``。"""

    def test_result_shells_are_populated(self):
        """单壳结果的 ``_shells`` 必须是 ``[True]``,不是 ``None``。

        ``_shells is None`` 会让下游的洞归属 / WKT 渲染退化成"全是壳",
        带洞的结果就会静默画错。
        """
        a, b = _sq(0, 0, 10, 10), _sq(5, 5, 15, 15)
        r = a.union(b)
        self.assertIsNotNone(r._shells, 'union 结果的 _shells 被丢了')
        self.assertEqual(r._shells, [True])

    def test_hole_result_carries_a_false_shell(self):
        """带洞结果里洞那一项必须是 ``False``。

        ⚠️ 用例原来是 ``_sq(0,0,10,10).symmetric_difference(_sq(5,5,15,15))``
        —— 那会儿本库把"共边重叠的两方块"的对称差装配成 **一个带洞的面**
        (外壳 = 并集外圈,洞 = 交集方块)。面积对,几何**非法**(洞的边界在
        ``(5 10)`` / ``(10 5)`` 两个点上碰到外壳,GEOS 判
        ``Interior is disconnected``;正确结果是两个 L 形 ``MULTIPOLYGON``)。
        走环方向修好以后这个用例不再带洞,换成真正的带洞结果:
        大方形挖掉中心的小方形。见 :meth:`test_overlapping_squares_symdiff_is_two_lobes`。
        """
        a, b = _sq(0, 0, 10, 10), _sq(3, 3, 7, 7)
        r = a.difference(b)
        self.assertIsNotNone(r._shells, 'difference 结果的 _shells 被丢了')
        self.assertIn(False, r._shells, '洞没被标出来')
        self.assertEqual(r.area(), 84.0, '挖洞后的面积')

    def test_multipolygon_has_two_true_shells(self):
        a, b = _sq(0, 0, 10, 10), _sq(20, 0, 30, 10)
        r = a.union(b)
        self.assertEqual(r._shells, [True, True])

    def test_result_is_not_a_multipatch(self):
        """结果的 ``shape_type`` 必须是编码得出来的那个 —— 不是 multipatch。

        overlay 的产物一律是普通面/线/点,``shape_type`` 走
        ``geometry._OVERLAY_SHAPE`` 那张表。盯一眼是因为"装配时忘了改
        ``shape_type``"会让一个合法的 ``POLYGON`` 继承到输入的 multipatch 标记。
        """
        r = _sq(0, 0, 10, 10).union(_sq(5, 5, 15, 15))
        self.assertNotEqual(r.shape_type, C.ShapeType.MULTIPATCH)
        self.assertEqual(r.shape_type, C.ShapeType.POLYGON)


# ----------------------------------------------------------------------------
# 五、不变量(不依赖真值)
# ----------------------------------------------------------------------------
class TestInvariants(_OverlayAssert):
    """面积层面的格律。它们不告诉你"对不对",但能告诉你"是不是自洽"。"""

    def test_area_bounds(self):
        """``|A∪B| >= max(|A|,|B|)`` 且 ``|A∩B| <= min(|A|,|B|)``。"""
        for a, b in ((_sq(0, 0, 10, 10), _sq(5, 5, 15, 15)),
                     (_sq(0, 0, 10, 10), _sq(2, 2, 4, 4)),
                     (_sq(0, 0, 10, 10), _sq(20, 0, 30, 10))):
            union = a.union(b).area()
            inter = a.intersection(b).area()
            self.assertGreaterEqual(union, max(a.area(), b.area()) - 1e-9)
            self.assertLessEqual(inter, min(a.area(), b.area()) + 1e-9)

    def test_difference_is_area_monotone(self):
        """``A∖B ⊆ A`` ⇒ 面积单调不增。"""
        a, b = _sq(0, 0, 10, 10), _sq(5, 5, 15, 15)
        self.assertLessEqual(a.difference(b).area(), a.area() + 1e-9)

    def test_envelope_stays_inside_the_union_of_envelopes(self):
        """结果的包围盒必须落在两个输入包围盒的并**之内** —— 专治坐标算飞。"""
        a, b = _sq(0, 0, 10, 10), _sq(5, 5, 15, 15)
        for op in _OPS:
            r = getattr(a, op)(b)
            if r.is_empty:
                continue
            x0, y0, x1, y1 = r.envelope()
            self.assertGreaterEqual(x0, -1e-9, op)
            self.assertGreaterEqual(y0, -1e-9, op)
            self.assertLessEqual(x1, 15.0 + 1e-9, op)
            self.assertLessEqual(y1, 15.0 + 1e-9, op)

    def test_results_are_deterministic(self):
        """同一个调用跑两遍必须逐位相同 —— 专治哈希序 / 集合迭代序泄漏进结果。"""
        a, b = _sq(0, 0, 10, 10), _sq(5, 5, 15, 15)
        for op in _OPS:
            self.assertEqual(getattr(a, op)(b).wkt(), getattr(a, op)(b).wkt(), op)

    def test_union_result_is_valid(self):
        """并集结果至少过得了 ``is_valid()``(本库的 ``is_valid`` 是部分实现,
        但自交环这一档它是查得动的)。"""
        for a, b in ((_sq(0, 0, 10, 10), _sq(5, 5, 15, 15)),
                     (_sq(0, 0, 10, 10), _sq(10, 0, 20, 10))):
            r = a.union(b)
            self.assertTrue(r.is_valid(), r.wkt())


# ----------------------------------------------------------------------------
# 六、别名
# ----------------------------------------------------------------------------
class TestAlias(_OverlayAssert):
    """``sym_difference`` 是 ``symmetric_difference`` 的别名(跟 shapely 的名字)。"""

    def test_sym_difference_matches_symmetric_difference(self):
        a, b = _sq(0, 0, 10, 10), _sq(5, 5, 15, 15)
        self.assertEqual(a.sym_difference(b).wkt(),
                         a.symmetric_difference(b).wkt())

    def test_sym_difference_exists(self):
        self.assertTrue(callable(Geometry.from_wkt('POINT (0 0)').sym_difference))


# ----------------------------------------------------------------------------
# 七、Z(与 GEOS 实测的机制一致:节点上按各贡献源线段插值取平均)
# ----------------------------------------------------------------------------
class TestZ(_OverlayAssert):
    """Z 的规则(见 ``_planar._NAN`` / ``_add_node_z`` / ``_overlay_ops._ElevationGrid``):

    * **只要一边带 Z,结果就带 Z**(不是"两边都带才有");
    * 输出顶点的 Z = 该点上各**贡献源线段**插值 Z 的平均,NaN 不参与;
    * **M 一律丢弃**。

    ⚠️ 比较方式:两个结果**可以合法地拥有不同的共线顶点**(见 ``DESIGN.md §2.24``
    末条),所以按 **``(x, y) → z`` 的字典**比,不按"z 数组逐位"比。
    """

    #: 平面 === 值(interior 的点按各输入插值后取平均,手算):
    #:   (5,5)  : 只在 B 内部,靠 B 的顶点 → 7
    #:   (5,10) : A 的边 (10,10)-(0,10) 插值 → 5;B 的边 (5,15)-(5,5) 插值 → 7;均值 6
    #:   (10,10): 只在 A 内部 → 5
    #:   (10,5) : A 的边 (10,0)-(10,10) → 5;B 的边 (5,5)-(15,5) → 7;均值 6
    _A_Z = 'POLYGON Z((0 0 5,10 0 5,10 10 5,0 10 5,0 0 5))'
    _B_Z = 'POLYGON Z((5 5 7,15 5 7,15 15 7,5 15 7,5 5 7))'

    @staticmethod
    def _zmap(geom):
        """``{(x, y): z}``。``point_count`` 个点,``xy_parts[0]`` 交错、``z_parts[0]``
        逐点 —— 这个"一个交错一个不交错"的不对称是 ``_FlatCoords`` 的既有约定。"""
        xy = geom.xy_parts[0]
        zs = geom.z_parts[0]
        return {(xy[2 * i], xy[2 * i + 1]): zs[i] for i in range(len(zs))}

    def test_both_3d_intersection_z(self):
        """两个都带 Z(值不同)⇒ 交点 Z 是两边插值的平均。"""
        r = Geometry.from_wkt(self._A_Z).intersection(
            Geometry.from_wkt(self._B_Z))
        self.assertTrue(r.has_z, '结果应该带 Z')
        self.assertFalse(r.has_m, 'overlay 不产 M')
        self.assertArea(r.area(), 25.0, 'Z 不该影响平面结果')
        want = {(5.0, 5.0): 7.0, (5.0, 10.0): 6.0,
                (10.0, 10.0): 5.0, (10.0, 5.0): 6.0}
        self.assertEqual(self._zmap(r), want)

    def test_one_3d_one_2d_still_has_z(self):
        """⚠️ **只要一边带 Z,结果就带 Z** —— 2D 那边贡献的是 NaN,不参与平均。"""
        a2 = Geometry.from_wkt('POLYGON((0 0,10 0,10 10,0 10,0 0))')
        r = a2.intersection(Geometry.from_wkt(self._B_Z))
        self.assertTrue(r.has_z, '一边带 Z 时结果也该带 Z')
        # A 是 2D(全 NaN),B 全 7 ⇒ 每个顶点的 Z 都来自 B 一侧。
        self.assertEqual(set(self._zmap(r).values()), {7.0})

    def test_uniform_z_propagates_to_new_nodes(self):
        """两边 Z 都是常数 5 ⇒ 结果**每一个**新节点也是 5。"""
        a = Geometry.from_wkt('POLYGON Z((0 0 5,10 0 5,10 10 5,0 10 5,0 0 5))')
        b = Geometry.from_wkt('POLYGON Z((5 5 5,15 5 5,15 15 5,5 15 5,5 5 5))')
        r = a.intersection(b)
        self.assertEqual(set(self._zmap(r).values()), {5.0})

    def test_2d_inputs_stay_2d(self):
        """两个都不带 Z ⇒ 结果不带 Z(**不要凭空造个 0 出来**)。"""
        r = _sq(0, 0, 10, 10).intersection(_sq(5, 5, 15, 15))
        self.assertFalse(r.has_z, '2D 输入不该产出 Z')

    def test_m_is_dropped(self):
        """M **一律丢弃** —— GEOS 的 overlay 不产 M。"""
        a = Geometry.from_wkt('POLYGON M((0 0 1,10 0 2,10 10 3,0 10 4,0 0 1))')
        r = a.intersection(_sq(5, 5, 15, 15))
        self.assertFalse(r.has_m, 'M 应该被丢掉')

    def test_z_on_empty_result(self):
        """空结果不带 Z(实测:GEOS 的 ``createEmptyResult`` 不碰升降维)。"""
        a = Geometry.from_wkt('POLYGON Z((0 0 5,10 0 5,10 10 5,0 10 5,0 0 5))')
        r = a.intersection(_sq(20, 0, 30, 10))
        self.assertTrue(r.is_empty)
        self.assertFalse(r.has_z, '空结果不该带 Z')


