# -*- coding: utf-8 -*-
"""``Geometry.buffer`` —— 手推真值、有牙齿的守卫、不变量、两处 GEOS 分岔。

跑法::

    /d/zsh/app/py_3.13.1/python -m unittest tests.test_buffer -v

本文件**不需要样例数据**(全部几何在内存里构造),也**不 import shapely /
osgeo** —— 对拍 GEOS 的活在 ``tools/verify_buffer.py`` 里,只有 ``tools/``
才允许碰它们(见 README「与 GDAL 的关系」)。

真值从哪来
----------
* 每一条面积都是**对着几何定义手算**的,**不看实现**。这是本文件存在的理由:
  它抓的是"实现和理解一起错",比只测不变量可靠。
* ⚠️ 圆角 / 圆帽一律是**内接正多边形**(顶点落在半径 ``d`` 的圆上,弦在圆内),
  所以面积**严格小于**理想圆的 ``πd²``。写 ``π`` 就错了。``quad_segs=8`` 的
  整圆恰好是 32 边形,记

      ``S32 = 16·sin(π/16) = 3.121445152258052…``

  于是:整圆的四分之一 = ``S32/4``,半圆 = ``S32/2``,``d=1`` 的圆帽一对
  = ``S32``。``POINT(0 0).buffer(1)`` 的面积就是 ``S32``。
* ⚠️ ``buffer(-d)`` 是**闵可夫斯基侵蚀**,不是"符号一翻就完事"。凸角的侵蚀
  边界还是**直线**:把凸集对圆盘做侵蚀,等于把每条支撑半平面内推 ``d``,
  所以 10×10 方块的 ``buffer(-1)`` 是**尖角内方块 [1,9]²**,面积**恰好 64**。
  不是"圆角内缩"的 ``60+π`` —— 那是把侵蚀错当成"偏移曲线反向"才会得到的数。
  这一点 2026-09-30 和 GEOS 3.13.1 逐位核对过(``tools/verify_buffer.py``)。
* 反过来,**洞**在侵蚀时是**长大**的(材料侵蚀 = 洞膨胀),而膨胀会把洞的凸角
  磨圆 —— 所以 ``buffer(-1)`` 让一个 2×2 的洞变成"圆角 4×4",面积
  ``12 + S32``,不是 ``16``。同一个 ``buffer(-1)`` 调用里壳是尖角、洞是圆角,
  这不是 bug,是侵蚀的定义。
"""
from __future__ import annotations

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pyopenfilegdb import _constants as C                        # noqa: E402
from pyopenfilegdb import _buffer_ops as B                       # noqa: E402
from pyopenfilegdb._geometry_ops import (                        # noqa: E402
    orient, ring_signed_area2)
from pyopenfilegdb.geometry import Geometry                      # noqa: E402

#: ``quad_segs=8`` 时**单位圆**的内接 32 边形面积。所有圆角真值都由它派生。
S32 = 16.0 * math.sin(math.pi / 16.0)

#: ``LINESTRING (0 0, 10 0)`` 去掉两个端帽后的"矩形"面积 = 长 × 2d。
_LINE_RECT = 20.0


def _area(geom):
    return geom.area()


class _AreaAssert(unittest.TestCase):
    """面积断言统一走这里 —— 容差随量级走,不写死死数。"""

    def assertArea(self, got, want, msg=''):
        delta = 1e-9 * max(1.0, abs(want))
        if abs(got - want) > delta:
            self.fail('%s面积 %.15g,期望 %.15g(差 %.3g,容差 %.3g)'
                      % (msg and msg + ': ', got, want, got - want, delta))


# ----------------------------------------------------------------------------
# 一、手推真值表(不依赖任何参照实现)
# ----------------------------------------------------------------------------
class TestBufferTruthTable(_AreaAssert):
    """每条真值都是对着定义算出来的 —— 见模块 docstring 的推导。"""

    # -- 点 -----------------------------------------------------------------
    def test_point_round_is_a_32_gon(self):
        """圆帽点缓冲 = 内接 32 边形:顶点数 ``4*quad_segs``,面积 ``S32``。"""
        g = Geometry.from_wkt('POINT (0 0)').buffer(1)
        self.assertFalse(g.is_empty)
        self.assertEqual(g.point_count, 32)
        self.assertArea(_area(g), S32)
        self.assertEqual(tuple(g.envelope()), (-1.0, -1.0, 1.0, 1.0))

    def test_point_square_cap_is_a_square(self):
        """方帽 = 边长 2d 的正方形,面积 ``4``(**不是** ``π``)。"""
        g = Geometry.from_wkt('POINT (0 0)').buffer(1, cap='square')
        self.assertEqual(g.point_count, 4)
        self.assertArea(_area(g), 4.0)
        self.assertEqual(tuple(g.envelope()), (-1.0, -1.0, 1.0, 1.0))

    def test_point_flat_cap_is_empty(self):
        """零长度线 + 平帽 → 曲线本身退化成空。这是 JTS 的显式分支。"""
        self.assertTrue(Geometry.from_wkt('POINT (0 0)').buffer(1, cap='flat')
                        .is_empty)

    def test_point_zero_and_negative_are_empty(self):
        """``isLineOffsetEmpty``:``d == 0`` 或 (``d < 0`` 且非单侧) → 空。"""
        p = Geometry.from_wkt('POINT (0 0)')
        self.assertTrue(p.buffer(0).is_empty)
        self.assertTrue(p.buffer(-1).is_empty)

    # -- 线 -----------------------------------------------------------------
    def test_line_round_is_a_stadium(self):
        """矩形 ``10 × 2`` + 两个半圆(= 一个整圆) = ``20 + S32``。"""
        g = Geometry.from_wkt('LINESTRING (0 0, 10 0)').buffer(1)
        self.assertArea(_area(g), _LINE_RECT + S32)
        self.assertEqual(tuple(g.envelope()), (-1.0, -1.0, 11.0, 1.0))

    def test_line_flat_is_exactly_a_rectangle(self):
        g = Geometry.from_wkt('LINESTRING (0 0, 10 0)').buffer(1, cap='flat')
        self.assertArea(_area(g), 20.0)
        self.assertEqual(tuple(g.envelope()), (0.0, -1.0, 10.0, 1.0))

    def test_line_square_extends_by_d_at_each_end(self):
        """方帽各往外伸 ``d``:``(10+2) × 2 = 24``。"""
        g = Geometry.from_wkt('LINESTRING (0 0, 10 0)').buffer(1, cap='square')
        self.assertArea(_area(g), 24.0)
        self.assertEqual(tuple(g.envelope()), (-1.0, -1.0, 11.0, 1.0))

    # -- 面:三个 join 档 ----------------------------------------------------
    def test_square_mitre_fills_the_corners_exactly(self):
        """斜接:四角补成直角三角,每角补 ``d² = 1`` → ``100 + 40 + 4``。"""
        sq = Geometry.from_wkt('POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0))')
        self.assertArea(_area(sq.buffer(1, join='mitre')), 144.0)

    def test_square_bevel_cuts_half_of_each_corner(self):
        """倒角:每角只补一个 ``d²/2`` 的三角 → ``100 + 40 + 4*(1/2)``。"""
        sq = Geometry.from_wkt('POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0))')
        self.assertArea(_area(sq.buffer(1, join='bevel')), 142.0)

    def test_square_round_corners_are_four_quarter_circles(self):
        """圆角:四个四分之一圆 = 一个整圆 → ``100 + 40 + S32``。"""
        sq = Geometry.from_wkt('POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0))')
        self.assertArea(_area(sq.buffer(1)), 100.0 + 40.0 + S32)

    # -- 面:侵蚀 ------------------------------------------------------------
    def test_square_erosion_keeps_SHARP_corners(self):
        """⚠️ 侵蚀不是"偏移曲线反向":凸角边界仍是直线,面积**恰好 64**。

        这里断的是"**尖角**内方块",不是 ``60+π``。见模块 docstring 的推导。
        """
        sq = Geometry.from_wkt('POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0))')
        g = sq.buffer(-1)
        self.assertEqual(g.point_count, 4, '侵蚀后的方块必须只有 4 个顶点')
        self.assertArea(_area(g), 64.0)
        self.assertEqual(tuple(g.envelope()), (1.0, 1.0, 9.0, 9.0))

    def test_square_eroded_away_is_empty(self):
        """``isRingFullyEroded``:``2*|d| > min(宽, 高)`` → 整块没了。"""
        sq = Geometry.from_wkt('POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0))')
        self.assertTrue(sq.buffer(-6).is_empty)

    # -- 洞 -----------------------------------------------------------------
    def test_hole_is_filled_by_a_positive_buffer(self):
        """2×2 的洞外扩 1 就被壳吞了 —— 结果是**无洞**的 ``143.1214…``。"""
        holed = Geometry.from_wkt(
            'POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0),'
            ' (4 4, 4 6, 6 6, 6 4, 4 4))')
        plain = Geometry.from_wkt('POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0))')
        g = holed.buffer(1)
        self.assertEqual(len(g.xy_parts), 1, '洞应当被填平,不该留下内环')
        self.assertArea(_area(g), _area(plain.buffer(1)))

    def test_hole_GROWS_and_gets_rounded_when_eroding(self):
        """侵蚀材料 = 膨胀洞。洞从 2×2 长成"圆角 4×4" = ``12 + S32``。

        于是总面 = 壳 ``64`` − 洞 ``12 + S32`` = ``52 − S32``。
        ⚠️ 同一个调用里壳是**尖角**、洞是**圆角** —— 这是侵蚀的定义,不是 bug。
        """
        holed = Geometry.from_wkt(
            'POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0),'
            ' (4 4, 4 6, 6 6, 6 4, 4 4))')
        g = holed.buffer(-1)
        self.assertEqual(g._shells, [True, False])
        self.assertArea(_area(g), 64.0 - (12.0 + S32))

    # -- 多部件 / 退化输入 ---------------------------------------------------
    def test_multipolygon_of_two_boxes_stays_two(self):
        """两个相离方块 → 仍是两个多边形(``getNumGeometries() > 1``,不走
        ``keepLargestArea``)。每个 = ``4 + 8 + S32``。"""
        mp = Geometry.from_wkt(
            'MULTIPOLYGON (((0 0, 2 0, 2 2, 0 2, 0 0)),'
            ' ((10 10, 12 10, 12 12, 10 12, 10 10)))')
        g = mp.buffer(1)
        self.assertEqual(len(g._ring_groups()), 2)
        self.assertArea(_area(g), 2.0 * (4.0 + 8.0 + S32))

    def test_self_intersecting_input_triggers_keep_largest_area(self):
        """领结 → 触发 ``keepLargestArea``,只留最大的那块,不是两块都留。"""
        bow = Geometry.from_wkt('POLYGON ((0 0, 10 10, 10 0, 0 10, 0 0))')
        g = bow.buffer(1)
        self.assertEqual(len(g._ring_groups()), 1)
        self.assertGreater(_area(g), 0.0)
        # 精确面积来自 GEOS 对拍(tools/verify_buffer.py),不在这里手推 ——
        # 自交输入的"哪个环"依赖 noder 的分裂顺序,手推容易推错方向。
        self.assertArea(_area(g), 52.4832194879, '领结缓冲')

    # -- 重合曲线 -----------------------------------------------------------
    def test_coincident_duplicate_lines_match_a_single_line(self):
        """两条完全重合的折线,缓冲结果与单条**逐位相同**。

        机制在 ``insertUniqueEdge``:同向重合边的 ``depthDelta`` 累加(此处
        ``1+1=2``),有向边仍只留一条;真正把 delta 归零的是"标签相反"那种
        情形,见 :meth:`TestBufferTeeth.test_merged_duplicate_edge_has_zero_delta`。
        """
        ln = Geometry.from_wkt('LINESTRING (0 0, 10 0)')
        two = Geometry.from_wkt(
            'MULTILINESTRING ((0 0, 10 0), (0 0, 10 0))')
        self.assertEqual(two.buffer(1).wkt(), ln.buffer(1).wkt())


# ----------------------------------------------------------------------------
# 二、"有牙齿"的守卫 —— 每一条都做过变异测试,改掉对应分支必须变红
# ----------------------------------------------------------------------------
class TestBufferTeeth(_AreaAssert):

    def test_quad_segs_actually_changes_vertex_count(self):
        """证明 ``quad_segs`` 真的进了 ``filletAngleQuantum``,不是装样子。"""
        p = Geometry.from_wkt('POINT (0 0)')
        counts = [p.buffer(1, quad_segs=q).point_count for q in (1, 2, 4, 8, 16)]
        self.assertEqual(counts, [4, 8, 16, 32, 64])
        areas = [p.buffer(1, quad_segs=q).area() for q in (1, 2, 4, 8, 16)]
        self.assertEqual(areas, sorted(areas), '段数越多应越贴近真圆')
        self.assertEqual(areas[0], 2.0, 'quad_segs=1 是内接正方形,面积 2')

    def test_negative_distance_actually_erodes(self):
        """证明 ``d < 0`` 走的是侵蚀分支,没有被 ``abs()`` 吞掉。"""
        sq = Geometry.from_wkt('POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0))')
        self.assertLess(_area(sq.buffer(-1)), _area(sq))
        self.assertLess(_area(sq.buffer(-3)), _area(sq.buffer(-1)))
        self.assertNotAlmostEqual(_area(sq.buffer(-1)), _area(sq.buffer(1)))

    def test_new_point_is_off_the_source_line(self):
        """本仓的老药方:圆角**必须真的产生新顶点**。

        若圆角退化成菱形(只在 x/y 轴上落点),面积仍"看起来差不多" —— 这条
        会红。``quad_segs=1`` 恰好就是那个菱形,于是同一条断言在两边给出
        相反答案,证明它测的是真东西。
        """
        p = Geometry.from_wkt('POINT (0 0)')
        off_axis = [i for i in range(0, p.buffer(1).point_count * 2, 2)
                    if abs(p.buffer(1).xy_parts[0][i]) > 1e-12
                    and abs(p.buffer(1).xy_parts[0][i + 1]) > 1e-12]
        self.assertTrue(off_axis, 'quad_segs=8 的圆上应当有象限内的顶点')
        diamond = p.buffer(1, quad_segs=1)
        self.assertEqual(
            [i for i in range(0, diamond.point_count * 2, 2)
             if abs(diamond.xy_parts[0][i]) > 1e-12
             and abs(diamond.xy_parts[0][i + 1]) > 1e-12],
            [], 'quad_segs=1 是菱形,顶点全在坐标轴上 —— 守卫能分辨两者')

    def test_result_shells_are_populated(self):
        """守 ``Geometry._like()`` 丢 ``_shells`` 那个坑。

        这条**必须单独存在**:不设 ``_shells`` 时 ``_roles()`` 会退化成
        "part 0 是外环、其余是洞"的猜测,对一个单环结果**面积照样对**,
        要等到有多环时才炸 —— 光看面积抓不住。
        """
        sq = Geometry.from_wkt('POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0))')
        g = sq.buffer(1)
        self.assertIsNotNone(g._shells, 'buffer 结果必须显式带上 _shells')
        self.assertEqual(len(g._shells), len(g.xy_parts))
        self.assertEqual(g._shells, [True])
        holed = Geometry.from_wkt(
            'POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0),'
            ' (4 4, 4 6, 6 6, 6 4, 4 4))')
        self.assertEqual(holed.buffer(-1)._shells, [True, False])

    def test_merged_duplicate_edge_has_zero_delta(self):
        """直接打内部:重合边的 ``depthDelta`` 合并规则。

        ⚠️ 零 delta 的条件是"**走向相同、标签相反**",不是"走向相反" ——
        实测走向相反合出来是 ±2。区别来自 ``insertUniqueEdge`` **只在方向
        相反时**才 ``flip()`` 标签。写错的注释会把人引到错的模型上,所以
        正反两种情形都断言在这里。
        """
        fwd = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0), (0.0, 0.0)]
        rev = list(reversed(fwd))
        INT, EXT = B.INTERIOR, B.EXTERIOR

        _n, _u, _v, ed = B._noded_edges([(fwd, INT, EXT), (fwd, EXT, INT)])
        self.assertEqual(set(ed), {0}, '同向 + 标签相反 → 每条边 delta 归零')
        self.assertEqual(len(ed), 4)

        _n, _u, _v, ed = B._noded_edges([(fwd, INT, EXT), (rev, EXT, INT)])
        self.assertEqual(set(ed), {2}, '走向相反 → 累加成 ±2,不是 0')

        _n, _u, _v, ed = B._noded_edges([(fwd, INT, EXT), (fwd, INT, EXT)])
        self.assertEqual(set(ed), {2}, '标签也相同的两条 → 2')

    def test_simplifier_uses_the_GEOS_argument_order(self):
        """守 ``BufferInputLineSimplifier`` 的第一处分岔。

        GEOS ``isShallowSampled`` 比的是"**中间点**到 ``(p0, 第 i 点)``",在
        ``i == i0`` 时退化成 ``|中间点 − p0|``;JTS 的参数序则退化成 0。于是
        存在这样一类浅凹角:垂直距离**够小**(纯几何看该删),但 GEOS 因为
        "中间点离 p0 很远"而**保留**,JTS 会删掉。

        下面这组点就是这一类(垂直距离 1e-3 < 容差 1e-2,而中间点离 p0 有 10)。
        抄回 JTS 的参数序 → 这条立刻红。
        """
        p0, mid, p2 = (0.0, 0.0), (10.0, -0.001), (20.0, 0.0)
        line = [(30.0, 40.0), p0, mid, p2, (30.0, -40.0), (40.0, 0.0)]
        self.assertGreater(orient(*p0, *mid, *p2), 0.0, '要 CCW 才进简化分支')
        self.assertLess(B._point_seg_distance(mid[0], mid[1], p0[0], p0[1],
                                              p2[0], p2[1]), 0.01,
                        '垂直距离要小于容差,否则测的是另一条分支')

        out = B._simplify_input_line(list(line), 0.01)
        self.assertIn(mid, out, 'GEOS 口径下这个浅凹角必须保留')
        self.assertEqual(len(out), len(line), '整条线不该被删掉任何点')

    def test_offset_segment_separation_factor_is_the_GEOS_value(self):
        """守 ``OffsetSegmentGenerator`` 的三个启发式因子取的是 GEOS 的数。

        ⚠️ ``_OFFSET_SEGMENT_SEPARATION_FACTOR`` 是本模块里**唯一**一处 JTS 与
        GEOS 数值不同的常量:JTS ``OffsetSegmentGenerator.java:37`` 是 ``.05``,
        GEOS ``oseggen.cpp:48`` 是 ``1.0E-3``(**差 50 倍**)。取 GEOS 是因为
        对拍 oracle 就是 GEOS,而 GDAL 的 ``OGRGeometry::Buffer`` 也只是转发。

        这条是**常量钉子**:它自身不证明行为,但能挡住"顺手改回 JTS 数值"。
        真正的行为后果在语料上有 148 对用例的差别(见 DESIGN.md §2.23)。
        """
        self.assertEqual(B._OFFSET_SEGMENT_SEPARATION_FACTOR, 1.0e-3)
        self.assertNotEqual(B._OFFSET_SEGMENT_SEPARATION_FACTOR, 0.05,
                            'JTS 的 0.05 会让拐角走"近似共线"捷径,少一个顶点')
        self.assertEqual(B._INSIDE_TURN_VERTEX_SNAP_DISTANCE_FACTOR, 1.0e-3)
        self.assertEqual(B._CURVE_VERTEX_SNAP_DISTANCE_FACTOR, 1.0e-4)
        self.assertEqual(B._MAX_CLOSING_SEG_LEN_FACTOR, 80)


# ----------------------------------------------------------------------------
# 三、不变量(不依赖真值)
# ----------------------------------------------------------------------------
class TestBufferInvariants(_AreaAssert):

    _SQ = 'POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0))'

    def test_area_is_monotone_in_distance(self):
        sq = Geometry.from_wkt(self._SQ)
        areas = [_area(sq.buffer(d)) for d in (0.5, 1.0, 3.0, 5.0)]
        self.assertEqual(areas, sorted(areas), f'面积应随 d 单调: {areas}')

    def test_result_is_deterministic(self):
        sq = Geometry.from_wkt(self._SQ)
        for d in (0.25, 1.0, -1.0):
            self.assertEqual(sq.buffer(d).wkt(), sq.buffer(d).wkt(),
                             f'd={d} 两次结果必须逐位相同')

    def test_envelope_stays_within_the_inflated_box(self):
        sq = Geometry.from_wkt(self._SQ)
        for d in (0.5, 1.0, 4.0):
            e = sq.buffer(d).envelope()
            for got, want in zip(e, (-d, -d, 10 + d, 10 + d)):
                self.assertLessEqual(got, want + 1e-9)
                self.assertGreaterEqual(got, want - 1e-9)

    def test_result_is_valid(self):
        """⚠️ 本库的 ``is_valid()`` 是**部分实现** —— ``True`` 不等于 OGC 有效。"""
        sq = Geometry.from_wkt(self._SQ)
        self.assertTrue(sq.buffer(1).is_valid())
        self.assertTrue(sq.buffer(-1).is_valid())

    def test_shells_are_esri_wound(self):
        """输出绕向:**外壳顺时针(带符号面积为负)、洞逆时针(为正)**。"""
        sq = Geometry.from_wkt(self._SQ)
        self.assertLess(ring_signed_area2(sq.buffer(1).xy_parts[0]), 0.0)
        holed = Geometry.from_wkt(
            'POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0),'
            ' (4 4, 4 6, 6 6, 6 4, 4 4))')
        g = holed.buffer(-1)
        self.assertLess(ring_signed_area2(g.xy_parts[0]), 0.0)
        self.assertGreater(ring_signed_area2(g.xy_parts[1]), 0.0)


# ----------------------------------------------------------------------------
# 四、API 口径
# ----------------------------------------------------------------------------
class TestBufferApi(_AreaAssert):

    def test_multipatch_raises(self):
        g = Geometry.from_wkt('POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0))')
        g.shape_type = C.ShapeType.MULTIPATCH
        with self.assertRaises(NotImplementedError):
            g.buffer(1)

    def test_result_is_always_2d(self):
        """Z / M 一律丢掉 —— 与 GEOS 的 buffer 同口径。"""
        z = Geometry.from_wkt(
            'POLYGON Z ((0 0 5, 10 0 5, 10 10 5, 0 10 5, 0 0 5))')
        g = z.buffer(1)
        self.assertFalse(g.has_z)
        self.assertFalse(g.has_m)
        self.assertArea(_area(g), _area(
            Geometry.from_wkt('POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0))')
            .buffer(1)))

    def test_string_and_integer_styles_agree(self):
        """字符串与 GEOS 的整数常量必须等价,别名也要收。"""
        ln = Geometry.from_wkt('LINESTRING (0 0, 10 0)')
        sq = Geometry.from_wkt('POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0))')
        self.assertEqual(ln.buffer(1, cap=2).wkt(), ln.buffer(1, cap='flat').wkt())
        self.assertEqual(ln.buffer(1, cap=3).wkt(),
                         ln.buffer(1, cap='square').wkt())
        self.assertEqual(ln.buffer(1, cap='butt').wkt(),
                         ln.buffer(1, cap='flat').wkt())
        self.assertEqual(sq.buffer(1, join=2).wkt(),
                         sq.buffer(1, join='mitre').wkt())
        self.assertEqual(sq.buffer(1, join='miter').wkt(),
                         sq.buffer(1, join='mitre').wkt())
        self.assertRaises(ValueError, ln.buffer, 1, cap='nope')

    def test_single_sided_on_an_open_line(self):
        """开放折线上 ``single_sided`` 与 GEOS 一致(已逐位核对)。

        ``d = +1`` 取左侧(``y ∈ [0, 1]``),``d = -1`` 取右侧 —— 都是一块
        ``10 × 1`` 的矩形。
        """
        ln = Geometry.from_wkt('LINESTRING (0 0, 10 0)')
        left = ln.buffer(1, single_sided=True)
        self.assertArea(_area(left), 10.0)
        self.assertEqual(tuple(left.envelope()), (0.0, 0.0, 10.0, 1.0))
        right = ln.buffer(-1, single_sided=True)
        self.assertArea(_area(right), 10.0)
        self.assertEqual(tuple(right.envelope()), (0.0, -1.0, 10.0, 0.0))

    def test_single_sided_on_a_closed_input_follows_JTS_not_GEOS(self):
        """⚠️ 已知的**刻意**分岔:闭合输入上本库给 JTS 的结果。

        GEOS 在单侧管线之后还有一步 ``OverlayNG`` + ``Polygonizer``(取"输入
        边界 ∪ 缓冲边界"围出的最大面),本库不做。10×10 方块 ``d=1`` 单侧:
        GEOS 给 **100**(方块本身那块面),本库给 **143.1214…**(方块外那圈带)。

        这条**不是**把差异藏起来 —— 它把差异钉在测试里,谁哪天决定补上那步
        后处理,这条会红,提醒他同步改 docstring 与 DESIGN §2.23。
        """
        sq = Geometry.from_wkt('POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0))')
        g = sq.buffer(1, single_sided=True)
        self.assertArea(_area(g), 100.0 + 40.0 + S32)
        self.assertNotAlmostEqual(_area(g), 100.0)

    def test_empty_result_is_a_polygon_geometry_not_none(self):
        g = Geometry.from_wkt('POINT (0 0)').buffer(-1)
        self.assertIsInstance(g, Geometry)
        self.assertEqual(g.kind, 'polygon')
        self.assertIsNone(g.envelope())


if __name__ == '__main__':                                   # pragma: no cover
    unittest.main()
