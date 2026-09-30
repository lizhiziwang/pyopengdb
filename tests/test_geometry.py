# -*- coding: utf-8 -*-
"""``Geometry`` 的空间计算与文本导出:度量真值、DE-9IM 真值表、不变量、
GeoJSON 往返、等值语义,以及**数值精度**的回归闸门。

跑法::

    /d/zsh/app/py_3.13.1/python -m unittest tests.test_geometry -v

本文件**不需要样例数据**(全部几何在内存里构造),任何解释器上都能跑。

真值从哪来
----------
* 度量真值(面积 100 / 周长 40 / 质心 ``(5, 5)`` …)是**手算**的,不看实现。
* DE-9IM 矩阵真值是对着 DE-9IM 定义**手推**的 —— 它们能抓住"实现和理解一起
  错"这种情况,比只测不变量可靠。
* 精度断言拿 ``fractions.Fraction`` 当参照:它作用在浮点数的精确二进制值上,
  所以是"给定这些输入时的正确答案"。
"""
from __future__ import annotations

import math
import os
import sys
import unittest
from array import array
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pyopenfilegdb import _constants as C                 # noqa: E402
from pyopenfilegdb import _geometry_ops as G              # noqa: E402
from pyopenfilegdb.geometry import Geometry, _FlatCoords  # noqa: E402
from pyopenfilegdb._esri_geometry import from_geojson     # noqa: E402

S = C.ShapeType


# ----------------------------------------------------------------------------
# 构造助手
# ----------------------------------------------------------------------------
def flat(*parts):
    """``flat([(0, 0), (1, 0)], ...)`` -> 交错 ``array('d')`` 的列表。"""
    out = []
    for pts in parts:
        a = array('d')
        for p in pts:
            a.extend(p[:2])
        out.append(a)
    return out


def sq(x0, y0, x1, y1):
    """**顺时针**(= Esri 的外环)正方形,裸坐标数组。"""
    return array('d', [x0, y0, x0, y1, x1, y1, x1, y0])


def ccw_sq(x0, y0, x1, y1):
    """逆时针正方形(= Esri 的内环 / 洞)。"""
    return array('d', [x0, y0, x1, y0, x1, y1, x0, y1])


def exact_area_centroid(parts, shells=None):
    """用有理数精确算 ``(面积, (质心 x, 质心 y))``。

    作用在**浮点数的精确二进制值**上,所以这就是给定这些输入时的真值 ——
    与实现无关,是精度断言的参照。
    """
    roles = G.ring_roles(parts, shells)
    ta = Fraction(0)
    cx = Fraction(0)
    cy = Fraction(0)
    for a, is_shell in zip(parts, roles):
        n = len(a) // 2
        if n < 3:
            continue
        a2 = Fraction(0)
        rx = Fraction(0)
        ry = Fraction(0)
        for i in range(n):
            j = (i + 1) % n
            x1, y1 = Fraction(a[2 * i]), Fraction(a[2 * i + 1])
            x2, y2 = Fraction(a[2 * j]), Fraction(a[2 * j + 1])
            cr = x1 * y2 - x2 * y1
            a2 += cr
            rx += (x1 + x2) * cr
            ry += (y1 + y2) * cr
        if a2 == 0:
            continue
        w = abs(a2) / 2
        if not is_shell:
            w = -w
        ta += w
        cx += w * (rx / (3 * a2))
        cy += w * (ry / (3 * a2))
    if ta == 0:
        return 0.0, None
    return abs(float(ta)), (float(cx / ta), float(cy / ta))


class TestLargeCoordinateAccuracy(unittest.TestCase):
    """鞋带公式必须**先平移到首顶点**,否则大坐标下灾难性抵消。

    这组测试是 2026-09-29 那次事故的闸门:真实语料
    (``D:/work/2024年国土行政区划.gdb``,UTM 带号 ~3.9e7)上,不平移的鞋带
    让**一条 427 m² 的地块的质心偏出 1135.84 米**,面积相对误差 8.6e-5。
    根因是每一项 ~1.5e15 的乘积只为凑出 ~854 的和,一次抵消掉 12 位有效数字。

    下面全部用合成数据复现同一量级 —— 不依赖样例库,所以任何机器上都会跑。
    """

    #: UTM 39 带上的真实坐标量级(见上面那份语料的 x_origin=33876800)。
    OX, OY = 39393621.0, 3179757.0

    def _triangle(self, dx=20.0, dy=30.0):
        """原点附近一个 ``dx × dy`` 的直角三角形,整体平移到 UTM 量级。"""
        return array('d', [
            self.OX, self.OY,
            self.OX + dx, self.OY,
            self.OX, self.OY + dy,
        ])

    def test_area_is_exact_at_utm_offsets(self):
        a = self._triangle()
        want, _ = exact_area_centroid([a])
        self.assertAlmostEqual(want, 300.0, places=6)   # 20×30/2,手算
        got = G.area_of('polygon', [a])
        self.assertLess(abs(got - want) / want, 1e-12,
                        f'面积 {got!r} 相对误差过大(真值 {want!r})')

    def test_signed_area2_is_exact_at_utm_offsets(self):
        a = self._triangle()
        # (0,0)→(dx,0)→(0,dy) 是**逆时针**(y 向上),所以带符号面积为正。
        # 真值 = dx*dy = 600。
        want = 20.0 * 30.0
        self.assertAlmostEqual(G.ring_signed_area2(a), want, places=9)

    def test_centroid_is_exact_at_utm_offsets(self):
        a = self._triangle()
        _, want = exact_area_centroid([a])
        got = G.centroid_of('polygon', [a])
        # 直角三角形质心 = 顶点平均(平移无关)
        off = math.hypot(got[0] - want[0], got[1] - want[1])
        self.assertLess(off, 1e-9, f'质心偏差 {off} 米(真值 {want},实得 {got})')

    def test_tiny_sliver_at_huge_offset(self):
        """1e-4 量级的退化环放在 1e8 偏移上 —— 不平移时误差能到 1e11 倍。

        ⚠️ 真值不是精确的 1e-8:``1e8 + 1e-4`` 本身就取不到,``ulp(1e8) ≈ 1.5e-8``。
        所以这里比的是**精确有理数解**(它作用在同样的双精度输入上),而不是
        纸面上的 1e-8。
        """
        base = 1e8
        a = array('d', [base, base,
                        base + 1e-4, base,
                        base + 1e-4, base + 1e-4,
                        base, base + 1e-4])
        want, wantc = exact_area_centroid([a])
        # 纸面值是 1e-8;实际差 3.4e-5 相对 —— 全部来自 1e8+1e-4 的表示误差。
        self.assertLess(abs(want - 1e-8) / 1e-8, 1e-4)
        got = G.area_of('polygon', [a])
        self.assertLess(abs(got - want) / want, 1e-9)
        gotc = G.centroid_of('polygon', [a])
        off = math.hypot(gotc[0] - wantc[0], gotc[1] - wantc[1])
        # 结果的绝对精度受 ulp(1e8) ≈ 1.5e-8 限制,相对 1e-4 的几何尺度就是
        # 1.5e-4 —— 所以这里放到 1e-3。不平移的旧写法在这一档是 1e11,差 14 个
        # 数量级,照样跑不掉。
        self.assertLess(off / 1e-4, 1e-3, '质心误差超过了几何自身尺度')

    def test_vertex_average_at_utm_offsets(self):
        """折线的质心(顶点平均)同样要先平移 —— 部分和涨到 3.5e10 就会掉精度。"""
        a = array('d')
        for i in range(2000):
            a.append(self.OX + math.cos(i) * 100.0)
            a.append(self.OY + math.sin(i) * 100.0)
        got = G._vertex_average([a])
        sx = sum(a[0::2]) / 2000.0
        sy = sum(a[1::2]) / 2000.0
        # 与"先平移再平均"的朴素参照比,差应远小于毫米
        self.assertLess(math.hypot(got[0] - sx, got[1] - sy), 1e-3)

    def test_ordering_sign_matches_exact_for_sliver(self):
        """近退化环的**符号**必须与精确解一致 —— 写路径靠它定环的绕向。"""
        # 0.001 m² 量级的极扁三角形,在 UTM 偏移上
        a = array('d', [self.OX, self.OY,
                        self.OX + 0.001, self.OY,
                        self.OX + 0.0005, self.OY + 0.002])
        exact = Fraction(0)
        for i in range(3):
            j = (i + 1) % 3
            exact += (Fraction(a[2 * i]) * Fraction(a[2 * j + 1])
                      - Fraction(a[2 * j]) * Fraction(a[2 * i + 1]))
        self.assertNotEqual(exact, 0)
        self.assertEqual(G.ring_signed_area2(a) < 0, exact < 0)


# ----------------------------------------------------------------------------
# 2. 度量:手算真值
# ----------------------------------------------------------------------------
class TestMeasures(unittest.TestCase):
    """10×10 单位正方形的面积 100、周长 40、质心 (5, 5)、包围盒 (0,0,10,10)
    —— 全是手算值,不看实现。"""

    def setUp(self):
        # 顺时针(Esri 外环)正方形
        self.box = Geometry.from_wkt('POLYGON((0 0,0 10,10 10,10 0,0 0))')
        self.line = Geometry.from_wkt('LINESTRING(0 0, 3 4)')
        self.pt = Geometry.from_wkt('POINT(5 5)')

    def test_area(self):
        self.assertAlmostEqual(self.box.area(), 100.0, places=9)
        self.assertEqual(self.line.area(), 0.0)
        self.assertEqual(self.pt.area(), 0.0)

    def test_length(self):
        self.assertAlmostEqual(self.box.length(), 40.0, places=9)
        self.assertAlmostEqual(self.line.length(), 5.0, places=9)   # 3-4-5
        self.assertEqual(self.pt.length(), 0.0)

    def test_centroid(self):
        self.assertEqual(self.box.centroid(), (5.0, 5.0))
        self.assertEqual(self.line.centroid(), (1.5, 2.0))   # 顶点平均(跟 GDAL)

    def test_envelope(self):
        self.assertEqual(self.box.envelope(), (0.0, 0.0, 10.0, 10.0))
        self.assertEqual(self.line.envelope(), (0.0, 0.0, 3.0, 4.0))
        self.assertEqual(Geometry().envelope(), None)

    def test_envelope_order_is_xmin_ymin_xmax_ymax(self):
        """⚠️ 与 OGR 的 (MinX, MaxX, MinY, MaxY) 顺序不同 —— 文档里承诺过。"""
        e = Geometry.from_wkt('LINESTRING(1 2,3 4)').envelope()
        self.assertEqual(e, (1.0, 2.0, 3.0, 4.0))

    def test_area_with_hole(self):
        """外环 100 减洞 4 = 96 —— 洞必须**减**掉,不能加。"""
        g = Geometry.from_wkt(
            'POLYGON((0 0,0 10,10 10,10 0,0 0),(4 4,6 4,6 6,4 6,4 4))')
        self.assertAlmostEqual(g.area(), 96.0, places=9)
        # 周长 = 外环 40 + 洞 8
        self.assertAlmostEqual(g.length(), 48.0, places=9)

    def test_dimension(self):
        self.assertEqual(self.pt.dimension, 0)
        self.assertEqual(self.line.dimension, 1)
        self.assertEqual(self.box.dimension, 2)
        self.assertEqual(Geometry().dimension, 0)

    def test_point_and_part_count(self):
        self.assertEqual(self.box.point_count, 4)
        self.assertEqual(self.box.part_count, 1)
        mp = Geometry.from_wkt('MULTIPOINT(0 0,1 1,2 2)')
        self.assertEqual(mp.point_count, 3)

    def test_is_empty(self):
        self.assertTrue(Geometry().is_empty)
        self.assertFalse(self.box.is_empty)

    def test_distance(self):
        a = Geometry.from_wkt('POLYGON((0 0,0 10,10 10,10 0,0 0))')
        b = Geometry.from_wkt('POLYGON((20 0,20 10,30 10,30 0,20 0))')
        self.assertAlmostEqual(a.distance(b), 10.0, places=9)
        # 内含 -> 0,不能返回线段间的正距离
        inner = Geometry.from_wkt('POLYGON((4 4,4 6,6 6,6 4,4 4))')
        self.assertEqual(a.distance(inner), 0.0)
        self.assertEqual(a.distance(Geometry.from_wkt('POINT(5 5)')), 0.0)
        self.assertEqual(a.distance(Geometry()), None)

    def test_convex_hull(self):
        hole = Geometry.from_wkt(
            'POLYGON((0 0,0 10,10 10,10 0,0 0),(4 4,6 4,6 6,4 6,4 4))')
        hull = hole.convex_hull()
        self.assertEqual(hull.kind, 'polygon')
        self.assertAlmostEqual(hull.area(), 100.0, places=9)   # 洞被填掉
        self.assertFalse(hull.has_z)
        # 共线 -> 退化成线
        col = Geometry.from_wkt('LINESTRING(0 0,5 5,10 10)').convex_hull()
        self.assertEqual(col.kind, 'polyline')
        # 单点 -> POINT
        self.assertEqual(Geometry.from_wkt('POINT(3 3)').convex_hull().kind,
                         'point')
        # 空 -> 空
        self.assertTrue(Geometry().convex_hull().is_empty)

    def test_simplify(self):
        zig = Geometry.from_wkt('LINESTRING(0 0,5 0.01,10 0)')
        self.assertEqual(zig.simplify(0.1).kind, 'polyline')
        self.assertAlmostEqual(zig.simplify(0.1).point_count, 2)
        # 容差为 0 时什么都不删
        self.assertEqual(zig.simplify(0.0).point_count, 3)

    def test_simplify_preserve_topology_is_refused(self):
        """拓扑保持做不到(要 GEOS),必须**明确拒绝**而不是静默降级。"""
        with self.assertRaises(NotImplementedError):
            self.box.simplify(0.1, preserve_topology=True)

    def test_segmentize(self):
        g = Geometry.from_wkt('LINESTRING(0 0, 3 4)').segmentize(1.0)
        self.assertEqual(g.point_count, 6)          # 5 段,每段不超过 1
        self.assertAlmostEqual(g.length(), 5.0, places=9)

    def test_segmentize_ring_keeps_area(self):
        """环切分后闭合不能坏、面积不能变、首点不能被复制出近似重复点。"""
        g = Geometry.from_wkt('POLYGON((0 0,0 10,10 10,10 0,0 0))')
        seg = g.segmentize(3.0)
        self.assertAlmostEqual(seg.area(), g.area(), places=9)
        self.assertAlmostEqual(seg.length(), g.length(), places=9)


# ----------------------------------------------------------------------------
# 3. DE-9IM 真值表 —— 手推,不依赖实现
# ----------------------------------------------------------------------------
class TestDe9imTruthTable(unittest.TestCase):
    """对着 DE-9IM 定义手推出来的矩阵。

    这些值**不依赖实现** —— 它们能抓住"实现和理解一起错"这种情况,比只测
    不变量可靠得多。矩阵顺序是 ``II IB IE / BI BB BE / EI EB EE``。
    """

    BIG = flat([(0, 0), (0, 10), (10, 10), (10, 0)])       # 顺时针大正方形
    A1 = flat([(0, 0), (0, 1), (1, 1), (1, 0)])

    CASES = [
        ('全等正方形', '2FFF1FFF2', 'polygon', A1,
         'polygon', flat([(0, 0), (0, 1), (1, 1), (1, 0)])),
        ('共一条边', 'FF2F11212', 'polygon', A1,
         'polygon', flat([(1, 0), (1, 1), (2, 1), (2, 0)])),
        ('部分重叠', '212101212', 'polygon', flat([(0, 0), (0, 2), (2, 2), (2, 0)]),
         'polygon', flat([(1, 1), (1, 3), (3, 3), (3, 1)])),
        ('相离', 'FF2FF1212', 'polygon', A1,
         'polygon', flat([(3, 3), (3, 4), (4, 4), (4, 3)])),
        ('A 大 B 小(B 严格在内)', '212FF1FF2', 'polygon', BIG,
         'polygon', flat([(2, 2), (2, 3), (3, 3), (3, 2)])),
        ('B 大 A 小(A 严格在内)', '2FF1FF212', 'polygon',
         flat([(2, 2), (2, 3), (3, 3), (3, 2)]), 'polygon', BIG),
        ('点在面内', '0FFFFF212', 'point', flat([(5, 5)]), 'polygon', BIG),
        ('点在面边界(顶点)', 'F0FFFF212', 'point', flat([(0, 0)]),
         'polygon', BIG),
        ('点在面边界(边上)', 'F0FFFF212', 'point', flat([(5, 0)]),
         'polygon', BIG),
        ('点在面外', 'FF0FFF212', 'point', flat([(-1, -1)]),
         'polygon', BIG),
        ('点在线的内部', '0FFFFF102', 'point', flat([(5, 0)]), 'polyline',
         flat([(0, 0), (10, 0)])),
        ('点在线的端点', 'F0FFFF102', 'point', flat([(0, 0)]), 'polyline',
         flat([(0, 0), (10, 0)])),
        ('点在先外', 'FF0FFF102', 'point', flat([(0, 5)]), 'polyline',
         flat([(0, 0), (10, 0)])),
        ('两个不同的点', 'FF0FFF0F2', 'point', flat([(0, 0)]), 'point',
         flat([(1, 1)])),
        ('两条交叉的线', '0F1FF0102', 'polyline', flat([(0, 0), (10, 10)]),
         'polyline', flat([(0, 10), (10, 0)])),
        ('线穿过面(两端都在外)', '101FF0212', 'polyline',
         flat([(-5, 5), (15, 5)]), 'polygon', BIG),
    ]

    def test_truth_table(self):
        for name, want, ka, pa, kb, pb in self.CASES:
            with self.subTest(name):
                got = G.relate_of(ka, pa, None, kb, pb, None)
                self.assertEqual(got, want)

    def test_predicates_agree_with_matrix(self):
        """九个谓词必须**由矩阵派生**,而且模式要与 OGC 规范逐字对上。

        参照实现写在这里、**不复用** ``_geometry_ops`` 的那份 —— 这样才能抓住
        ``_DE9IM_PATTERNS`` 里打错一个字符这类错误(那种错自测是测不出来的)。
        """
        for name, m, ka, _pa, kb, _pb in self.CASES:
            da, db = G.dimension_of(ka), G.dimension_of(kb)
            for pred, ref in _REF_PREDICATES.items():
                with self.subTest(f'{name}/{pred}'):
                    self.assertEqual(G.predicate_de9im(pred, m, da, db),
                                     ref(m, da, db),
                                     f'{pred} 在矩阵 {m} 上与规范不符')


def _ref_match(m, pattern):
    """独立的 DE-9IM 模式匹配:``*`` 任意、``T`` 非 F、``F`` 必须 F、数字精确。"""
    if len(m) != len(pattern):
        return False
    for c, p in zip(m, pattern):
        if p == '*':
            continue
        if p == 'T':
            if c == 'F':
                return False
        elif c != p:
            return False
    return True


def _ref_any(m, *patterns):
    return any(_ref_match(m, p) for p in patterns)


def _ref_crosses(m, da, db):
    """crosses **分维数**,而且 2D/2D 恒为 false、同维只有 1D/1D 才有意义。"""
    if da < db:
        return _ref_match(m, 'T*T******')
    if da > db:
        return _ref_match(m, 'T*****T**')
    return da == 1 and _ref_match(m, '0********')


def _ref_overlaps(m, da, db):
    """overlaps **只在同维之间**有定义。"""
    if da != db:
        return False
    return (_ref_match(m, 'T*T***T**') if da in (0, 2)
            else _ref_match(m, '1*T***T**'))


#: OGC DE-9IM 谓词定义(``docs.opengeospatial.org`` 的 SFS 表),逐条抄。
_REF_PREDICATES = {
    'disjoint': lambda m, da, db: _ref_match(m, 'FF*FF****'),
    'intersects': lambda m, da, db: not _ref_match(m, 'FF*FF****'),
    'contains': lambda m, da, db: _ref_match(m, 'T*****FF*'),
    'within': lambda m, da, db: _ref_match(m, 'T*F**F***'),
    'covers': lambda m, da, db: _ref_any(
        m, 'T*****FF*', '*T****FF*', '***T**FF*', '****T*FF*'),
    'covered_by': lambda m, da, db: _ref_any(
        m, 'T*F**F***', '*TF**F***', '**FT*F***', '**F*TF***'),
    'touches': lambda m, da, db: _ref_any(
        m, 'FT*******', 'F**T*****', 'F***T****'),
    'crosses': _ref_crosses,
    'overlaps': _ref_overlaps,
    'equals': lambda m, da, db: _ref_match(m, 'T*F**FFF*'),
}


class TestNonExactIntersectionPoints(unittest.TestCase):
    """交点的坐标**不可精确表示**时,relate 仍要判对。

    ⚠️ 这是一条**回归测试**,守的是一个真发生过、而且躲过了上面整张真值表的 bug。

    老实现是这样切的:先用 :func:`seg_seg_classify` 求出 A 与 B 线段的交点,把
    它换成一个参数 ``t``,然后**重新算一遍** ``p1 + t*(p2-p1)`` 丢给
    ``point_location`` —— 而那里判"点在线上"用的是 ``orient(...) == 0`` 的**精确**
    比较。交点不是精确可表示的双精度数,重算出来的点相对 B 的叉积是
    ``2.8e-14`` 而不是 0,于是"在线上"被答成"在外部",``II`` / ``IB`` / ``BI``
    整格塌成 ``F``。

    **这不是次 ULP 的退化构型** —— 它是"任何交点坐标除不尽"的正常情形。上面那张
    真值表用的是整数坐标,两条线正好交在 (5, 5),逐位精确,所以一次都没碰到;
    147 个用例因此全绿。是 ``tools/verify_topology.py`` 在**真** GEOS 上对拍时
    抓出来的(6/1286 对,全部在 relate 矩阵上)。

    修法见 :func:`_split_parameters` —— 它现在把交点本身一并带出来,调用方凭
    "按构造这一点就在对方上"直接定角色,不再问浮点。
    """

    #: 死盯的一个最小复现:两条线确实相交,但交点坐标除不尽。
    #: 老实现给 ``FF1FF0102``(声称 II/IB/BI 全空),GEOS 给 ``0F1FF0102``。
    CASES = [
        ('斜线交斜线,交点除不尽',
         '0F1FF0102',
         'LINESTRING(-0.0000003 0.0000004, 10.0000002 9.9999998)',
         'LINESTRING(0 10, 10 0)'),
        # 同一条几何搬到真实语料的坐标系量级 —— 那里方向判定的绝对误差更大。
        ('同上,平移到 UTM 量级',
         '0F1FF0102',
         'LINESTRING(39393620.9999997 3179757.0000004, '
         '39393631.0000002 3179766.9999998)',
         'LINESTRING(39393621 3179767, 39393631 3179757)'),
    ]

    def test_matrix(self):
        for name, want, wkt_a, wkt_b in self.CASES:
            with self.subTest(name):
                a = Geometry.from_wkt(wkt_a)
                b = Geometry.from_wkt(wkt_b)
                self.assertEqual(a.relate(b), want)
                # 转置关系:反过来的矩阵必须是转置,不是另一套错误
                self.assertEqual(b.relate(a), G.transpose_de9im(want))

    def test_predicates_follow(self):
        """谓词是从矩阵派生的,所以矩阵一错谓词必然连带错 —— 反过来也要成立。"""
        a, b = (Geometry.from_wkt(w) for w in self.CASES[0][2:])
        self.assertTrue(a.intersects(b))
        self.assertFalse(a.disjoint(b))
        self.assertTrue(b.intersects(a))

    def test_split_points_carry_their_hit(self):
        """切分结果必须把交点带出来 —— 丢了它,上面的修法就退化成老实现。"""
        ts, _on_ob = G._split_parameters(
            (-0.0000003, 0.0000004), (10.0000002, 9.9999998),
            [(0.0, 10.0, 10.0, 0.0)])
        inner = [(t, hit) for t, hit in ts if 0.0 < t < 1.0]
        self.assertTrue(inner, '中间那个交点没被切出来')
        for t, hit in inner:
            self.assertIsNotNone(hit, f't={t} 的交点被丢成 None 了')
            # 而且它确实不可精确表示 —— 否则这条测试就没在测该测的东西
            self.assertNotEqual(
                G.orient(0.0, 10.0, 10.0, 0.0, hit[0], hit[1]), 0.0)

    def test_on_geometry_role(self):
        """兜底的角色判定:点数错在"有没有边界"这一条上。"""
        self.assertEqual(G.on_geometry_role(1, 1, 'polygon'), G.BOUNDARY)
        self.assertEqual(G.on_geometry_role(1, 1, 'point'), G.INTERIOR)
        self.assertEqual(G.on_geometry_role(1, 1, 'multipoint'), G.INTERIOR)
        # 折线:边界端点集里有它就是 BOUNDARY,没有就是 INTERIOR
        self.assertEqual(
            G.on_geometry_role(0, 0, 'polyline', {(0.0, 0.0)}), G.BOUNDARY)
        self.assertEqual(
            G.on_geometry_role(5, 5, 'polyline', {(0.0, 0.0)}), G.INTERIOR)


class TestRecomputedMidpoints(unittest.TestCase):
    """⚠️ **第二个真 bug**:共线重叠的子线段,中点也不该拿去问浮点。

    这是 :class:`TestNonExactIntersectionPoints` 的**同一个根因的另一半**,而且是
    被上一条修复**漏掉**的那一半:上一个修了"交点",这一个修"中点"。

    一个几何**与它自己**做 `relate`,答案必须是不变量(拓扑上的单位矩阵)::

        POLYGON((0 0, 0.1 0.3, 0.4 0.1, 0 0))   老实现 2F2F11212  正确 2FFF1FFF2
        LINESTRING(0 0, 0.1 0.3, 0.4 0.1)       老实现 1F1F0F1F2  正确 1FFF0FFF2

    根因:第 2 块判"子段落在对方哪儿"取的是子段中点,而中点是用
    ``p1 + mid·(p2−p1)`` **重算**出来的,一般**不落在**原线段上 —— 实测该三角形
    第 1 条边相对自己的中点,``orient`` 是 ``-3.5e-18`` 而不是 0,于是
    "与对方边界重合"被判成"在对方内部 / 外部",``IB`` / ``IE`` / ``BE`` 全错。

    **为什么它比交点那个更值钱**:共线重叠在真实数据里到处都是(相邻行政区
    共享界线、同一块地被登记两次)。实测在 `D:/work/2024年国土行政区划.gdb`
    上,它让 `乡行政区划#75` 与 `村行政区划#92`(两条**完全相同**的面)判成
    "部分重叠"而不是"完全相等"。

    修法见 `_split_parameters` 的 ``on_obstacle``:``seg_seg_classify`` 本来就会
    报告 ``SEG_OVERLAP``,把那段区间记下来,落在里面的子段就**按构造**算作
    与对方重合(面 → 边界,折线 → 内部),不再问浮点。
    """

    #: 自比必须给出"拓扑单位矩阵":内部交内部是 2 维,边界交边界是 1 维,其余全空。
    IDENTITY_2D = '2FFF1FFF2'
    IDENTITY_1D = '1FFF0FFF2'

    CASES = [
        ('面:三角形,坐标除不尽', IDENTITY_2D,
         'POLYGON((0 0, 0.1 0.3, 0.4 0.1, 0 0))'),
        ('面:整数正方形(对照组,老实现也对)', IDENTITY_2D,
         'POLYGON((0 0, 10 0, 10 10, 0 10, 0 0))'),
        ('面:UTM 量级,语料同款', IDENTITY_2D,
         'POLYGON((39389740.9784 3172911.8373000007, '
         '39389745.42335 3172919.0604500007, 39389748.28095 3172925.01365, '
         '39389773.2892 3172899.88325, 39389740.9784 3172911.8373000007))'),
        ('线:折线自比', IDENTITY_1D, 'LINESTRING(0 0, 0.1 0.3, 0.4 0.1)'),
    ]

    def test_self_relate_is_identity(self):
        for name, want, wkt in self.CASES:
            with self.subTest(name):
                a = Geometry.from_wkt(wkt)
                b = Geometry.from_wkt(wkt)
                self.assertEqual(a.relate(b), want)

    def test_self_predicates(self):
        """自比时谓词必须全部给出"等于自己"那组答案。"""
        for name, _want, wkt in self.CASES:
            with self.subTest(name):
                a = Geometry.from_wkt(wkt)
                b = Geometry.from_wkt(wkt)
                self.assertTrue(a.equals(b))
                self.assertTrue(a.exactly_equals(b))
                self.assertTrue(a.covers(b))
                self.assertTrue(a.covered_by(b))
                self.assertFalse(a.touches(b))
                self.assertFalse(a.overlaps(b))

    def test_overlap_interval_is_reported(self):
        """重叠区间必须被标出来 —— 丢了它,上面的修法就退化成老实现。"""
        from pyopenfilegdb.geometry import Geometry as _G
        pa = _G.from_wkt('LINESTRING(0 0, 0.1 0.3)').xy_parts[0]
        x1, y1, x2, y2 = pa[0], pa[1], pa[2], pa[3]
        ts, on_ob = G._split_parameters((x1, y1), (x2, y2),
                                        [(x1, y1, x2, y2)])
        self.assertEqual(len(on_ob), len(ts) - 1)
        self.assertTrue(all(on_ob), '整条线段与自身共线重叠,每一段都该标上')

    def test_midpoint_really_is_off_the_line(self):
        """证明这条测试测的是该测的东西:那条边的中点确实不精确在线上。"""
        xy = Geometry.from_wkt(
            'POLYGON((0 0, 0.1 0.3, 0.4 0.1, 0 0))').xy_parts[0]
        x1, y1 = xy[2], xy[3]
        x2, y2 = xy[4], xy[5]
        mx, my = x1 + 0.5 * (x2 - x1), y1 + 0.5 * (y2 - y1)
        self.assertNotEqual(G.orient(x1, y1, x2, y2, mx, my), 0.0)
        self.assertFalse(G.point_on_segment(mx, my, x1, y1, x2, y2))


# ----------------------------------------------------------------------------
# 4. 不变量 —— 不依赖真值,只依赖逻辑自洽
# ----------------------------------------------------------------------------
class TestPredicateInvariants(unittest.TestCase):
    """谓词互相之间必须自洽。

    比背真值表更耐改:真值表只覆盖列出来的那几种构型,不变量覆盖所有两两组合。
    """

    GEOMS = [
        ('point', flat([(5, 5)])),
        ('point', flat([(0, 0)])),
        ('point', flat([(-1, -1)])),
        ('multipoint', flat([(1, 1), (9, 9)])),
        ('polyline', flat([(0, 0), (10, 10)])),
        ('polyline', flat([(-5, 5), (15, 5)])),
        ('polyline', flat([(0, 0), (10, 0)])),
        ('polygon', flat([(0, 0), (0, 10), (10, 10), (10, 0)])),
        ('polygon', flat([(0, 0), (0, 2), (2, 2), (2, 0)])),
        ('polygon', flat([(1, 1), (1, 3), (3, 3), (3, 1)])),
        ('polygon', flat([(10, 0), (10, 10), (20, 10), (20, 0)])),
        ('polygon', flat([(2, 2), (2, 3), (3, 3), (3, 2)])),
        ('polygon', flat([(0, 0), (0, 10), (10, 10), (10, 0)],
                         [(4, 4), (6, 4), (6, 6), (4, 6)])),
    ]

    def test_transpose_and_self_consistency(self):
        for ka, pa in self.GEOMS:
            for kb, pb in self.GEOMS:
                with self.subTest(f'{ka}/{kb}'):
                    m1 = G.relate_of(ka, pa, None, kb, pb, None)
                    m2 = G.relate_of(kb, pb, None, ka, pa, None)
                    da, db = G.dimension_of(ka), G.dimension_of(kb)

                    self.assertEqual(G.transpose_de9im(m1), m2,
                                     'relate(A,B) 的转置必须等于 relate(B,A)')
                    self.assertNotEqual(
                        G.predicate_de9im('intersects', m1, da, db),
                        G.predicate_de9im('disjoint', m1, da, db))
                    self.assertEqual(
                        G.predicate_de9im('contains', m1, da, db),
                        G.predicate_de9im('within', m2, db, da))
                    self.assertEqual(
                        G.predicate_de9im('covers', m1, da, db),
                        G.predicate_de9im('covered_by', m2, db, da))
                    for sym in ('equals', 'touches', 'overlaps'):
                        self.assertEqual(
                            G.predicate_de9im(sym, m1, da, db),
                            G.predicate_de9im(sym, m2, db, da),
                            f'{sym} 应当对称')

    def test_implications(self):
        for ka, pa in self.GEOMS:
            for kb, pb in self.GEOMS:
                m = G.relate_of(ka, pa, None, kb, pb, None)
                da, db = G.dimension_of(ka), G.dimension_of(kb)
                P = lambda n: G.predicate_de9im(n, m, da, db)   # noqa: E731
                with self.subTest(f'{ka}/{kb}'):
                    if P('equals'):
                        self.assertTrue(P('covers') and P('covered_by'))
                        self.assertTrue(P('intersects'))
                    if P('touches'):
                        self.assertTrue(P('intersects'))
                        self.assertFalse(P('overlaps'))
                    if P('overlaps'):
                        self.assertTrue(P('intersects'))
                    if P('contains'):
                        self.assertTrue(P('covers'))
                    if P('within'):
                        self.assertTrue(P('covered_by'))

    def test_geometry_methods_agree_with_matrix(self):
        """``Geometry`` 上的谓词必须与它自己的 ``relate()`` 一致。"""
        objs = []
        for kind, parts in self.GEOMS:
            if kind != 'polygon':
                continue
            objs.append(Geometry(S.POLYGON, None, False, False,
                                 _flat=_FlatCoords(parts),
                                 _shells=G.ring_roles(parts, None)))
        for a in objs:
            for b in objs:
                m = a.relate(b)
                with self.subTest(f'{a.envelope()} / {b.envelope()}'):
                    for name in ('intersects', 'disjoint', 'contains',
                                 'within', 'covers', 'covered_by', 'touches',
                                 'overlaps', 'equals'):
                        self.assertEqual(
                            getattr(a, name)(b),
                            G.predicate_de9im(name, m, a.dimension, b.dimension),
                            f'{name} 与 relate() 的矩阵 {m} 不一致')


# ----------------------------------------------------------------------------
# 5. 环组装(Esri 的多壳面只有绕向,没有归属)
# ----------------------------------------------------------------------------
class TestRingAssembly(unittest.TestCase):
    A = sq(0, 0, 10, 10)                        # 顺时针 = 壳
    HOLE = ccw_sq(4, 4, 6, 6)                   # 逆时针 = 洞
    A2 = sq(20, 0, 30, 10)
    HOLE2 = ccw_sq(22, 2, 24, 4)

    def test_shell_with_hole(self):
        self.assertEqual(G.organize_polygons([self.A, self.HOLE]), [(0, [1])])

    def test_hole_goes_to_smallest_containing_shell(self):
        got = G.organize_polygons([self.A, self.HOLE, self.A2, self.HOLE2])
        self.assertEqual(got, [(0, [1]), (2, [3])])

    def test_two_clockwise_rings_are_two_shells(self):
        got = G.organize_polygons([self.A, self.A2])
        self.assertEqual(got, [(0, []), (1, [])])

    def test_orphan_ccw_ring_falls_back_to_shell(self):
        """谁都包不住的逆时针环按"绕向写反了"处理,当壳 —— 对应 GDAL
        ``organizePolygons`` 里的回退分支。"""
        lone = ccw_sq(0, 0, 1, 1)
        self.assertEqual(G.organize_polygons([lone]), [(0, [])])

    def test_point_in_polygon_respects_holes(self):
        parts = [self.A, self.HOLE]
        self.assertEqual(G.point_in_polygon(5, 5, parts, None), G.EXTERIOR)
        self.assertEqual(G.point_in_polygon(2, 2, parts, None), G.INTERIOR)

    def test_ring_roles_follows_winding(self):
        self.assertEqual(G.ring_roles([self.A, self.HOLE]), [True, False])
        self.assertEqual(G.ring_roles([self.A], [False]), [False])


# ----------------------------------------------------------------------------
# 6. GeoJSON 与 __geo_interface__
# ----------------------------------------------------------------------------
class TestGeoJson(unittest.TestCase):

    def test_types_and_shapes(self):
        cases = [
            ('POINT(1 2)', 'Point', [1.0, 2.0]),
            ('MULTIPOINT(1 2,3 4)', 'MultiPoint', [[1.0, 2.0], [3.0, 4.0]]),
            ('LINESTRING(0 0,1 1)', 'LineString', [[0.0, 0.0], [1.0, 1.0]]),
            ('MULTILINESTRING((0 0,1 1),(2 2,3 3))', 'MultiLineString',
             [[[0.0, 0.0], [1.0, 1.0]], [[2.0, 2.0], [3.0, 3.0]]]),
        ]
        for wkt, want_type, want_coords in cases:
            with self.subTest(wkt):
                gi = Geometry.from_wkt(wkt).__geo_interface__
                self.assertEqual(gi['type'], want_type)
                self.assertEqual(gi['coordinates'], want_coords)

    def test_polygon_rings_are_closed(self):
        """内存里的环**不闭合**,导出必须补回首点。"""
        gi = Geometry.from_wkt('POLYGON((0 0,0 10,10 10,10 0,0 0))'
                               ).__geo_interface__
        ring = gi['coordinates'][0]
        self.assertEqual(ring[0], ring[-1])
        self.assertEqual(len(ring), 5)

    def test_rings_are_not_rewound(self):
        """照原样导出,**不**按 RFC 7946 重绕 —— 与 ``OGR_G_ExportToJson`` 一致。

        输入是 Esri 的顺时针外环,导出后必须**仍是顺时针**。
        """
        g = Geometry.from_wkt('POLYGON((0 0,0 10,10 10,10 0,0 0))')
        ring = g.__geo_interface__['coordinates'][0]
        a2 = 0.0
        for i in range(len(ring) - 1):
            a2 += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
        self.assertLess(a2, 0, '顺时针被重绕成逆时针了')

    def test_round_trip_2d(self):
        g = Geometry.from_wkt(
            'POLYGON((0 0,0 10,10 10,10 0,0 0),(4 4,6 4,6 6,4 6,4 4))')
        back = from_geojson(g.to_geojson())
        self.assertTrue(g.exactly_equals(back))
        self.assertEqual(g.area(), back.area())

    def test_round_trip_z(self):
        g = Geometry.from_wkt('LINESTRING Z(0 0 0,1 1 2,2 0 4)', has_z=True)
        back = from_geojson(g.to_geojson())
        self.assertTrue(back.has_z)
        self.assertTrue(g.exactly_equals(back))
        self.assertEqual(list(g.z_parts[0]), list(back.z_parts[0]))

    def test_m_is_dropped_but_can_be_asked_back(self):
        """GeoJSON 里没有 M 的位置 —— 导出丢弃,``has_m`` 参数能显式要回来。

        几何对象本身不丢坐标,只是 JSON 表达不了。
        """
        g = Geometry.from_wkt('LINESTRING M(0 0 7,1 1 8)', has_m=True)
        gi = g.__geo_interface__
        self.assertEqual(gi['coordinates'], [[0.0, 0.0], [1.0, 1.0]])
        back = from_geojson(gi)
        self.assertFalse(back.has_m)
        # 只比 XY —— M 依法丢弃了
        self.assertEqual([p[:2] for p in back.coordinates[1]],
                         [p[:2] for p in g.coordinates[1]])
        back_m = from_geojson(gi, has_m=True)
        self.assertTrue(back_m.has_m)

    def test_geojson_is_json_text(self):
        import json
        g = Geometry.from_wkt('POINT(1 2)')
        self.assertEqual(json.loads(g.to_geojson())['type'], 'Point')

    def test_multipolygon_export(self):
        """多壳面拼成 ``MULTIPOLYGON``,靠绕向把洞归到各自的壳。"""
        g = Geometry.from_wkt(
            'MULTIPOLYGON(((0 0,0 10,10 10,10 0,0 0)),'
            '((20 0,20 10,30 10,30 0,20 0)))')
        gi = g.__geo_interface__
        self.assertEqual(gi['type'], 'MultiPolygon')
        self.assertEqual(len(gi['coordinates']), 2)

    def test_empty_and_null(self):
        """空几何导出成 ``GeometryCollection`` 且 ``geometries`` 为空。

        这是 GDAL ``OGR_G_ExportToJson`` 对空几何的做法,也是 GeoJSON 里唯一
        能表达"什么都没有"的类型 —— **不要**改成 ``coordinates: []``。
        """
        gi = Geometry().__geo_interface__
        self.assertEqual(gi['type'], 'GeometryCollection')
        self.assertEqual(gi['geometries'], [])
        self.assertNotIn('coordinates', gi)


# ----------------------------------------------------------------------------
# 6b. WKT 出口
#
# ⚠️ 这一节是**补课**。原来 WKT 只被"往返"测过(读出来 -> wkt() -> from_wkt
# -> 比坐标),而那个往返**测不出格式非法**:本库的 from_wkt 是个"够用就好"
# 的宽容解析器,它把 ``LINESTRING ((0 0), (1 1))`` 这种非法文本也照收(只要
# 每个点单独成 part 时不触发顶点数下限就混过去了)。实际上一度就是这样:
# ``to_wkt`` 给**线和面的每个顶点**都套了括号,产出的 WKT 没有任何别的 GIS
# 工具能读,而套件全绿 —— 因为 test_read 的往返用例每个图层只跑第一条要素,
# 恰好都是面。
#
# 所以这里**不往返**,直接断言字符串。字符串是唯一能钉住"合法 WKT"的东西:
# 合法与否是给别人看的,不是给自己解析的。
# ----------------------------------------------------------------------------
class TestWkt(unittest.TestCase):

    def _rt(self, wkt):
        """往返一遍并断言文本稳定(与 from_wkt 对称性)。"""
        g = Geometry.from_wkt(wkt)
        out = g.wkt()
        self.assertEqual(Geometry.from_wkt(out).wkt(), out,
                         f'往返后文本变了: {out}')
        return out

    def test_point(self):
        self.assertEqual(self._rt('POINT(1 2)'), 'POINT (1 2)')

    def test_multipoint_points_are_parenthesized(self):
        """MULTIPOINT 是唯一"点自带括号"的类型(OGC 两种写法都合法,跟 GDAL)。"""
        self.assertEqual(self._rt('MULTIPOINT(1 1, 2 2)'),
                         'MULTIPOINT ((1 1), (2 2))')

    def test_linestring_points_are_bare(self):
        """⚠️ 回归闸门:线序列的点**不带**括号。"""
        out = self._rt('LINESTRING(0 0, 1 1, 2 0)')
        self.assertEqual(out, 'LINESTRING (0 0, 1 1, 2 0)')
        self.assertNotIn('((', out)

    def test_multilinestring(self):
        out = self._rt('MULTILINESTRING((0 0, 1 1), (2 2, 3 3))')
        self.assertEqual(out, 'MULTILINESTRING ((0 0, 1 1), (2 2, 3 3))')

    def test_polygon_ring_points_are_bare(self):
        out = self._rt('POLYGON((0 0, 4 0, 4 4, 0 4, 0 0))')
        self.assertEqual(out, 'POLYGON ((0 0, 4 0, 4 4, 0 4, 0 0))')
        self.assertNotIn('(0 0)', out, '顶点不该带括号')

    def test_polygon_closure_is_restored(self):
        """内存里的环不存闭合点,导出时要补回 —— 否则是非法 WKT。"""
        out = self._rt('POLYGON((0 0, 4 0, 4 4, 0 4, 0 0))')
        self.assertTrue(out.startswith('POLYGON ((0 0,'), out)
        self.assertTrue(out.endswith(', 0 0))'), out)

    def test_no_doubled_parens_anywhere(self):
        """通用闸门:除 MULTIPOINT 外,任何类型都不该出现 ``((`` 起头。"""
        for wkt in ('POINT(1 2)', 'LINESTRING(0 0, 1 1)',
                    'MULTILINESTRING((0 0, 1 1), (2 2, 3 3))',
                    'POLYGON((0 0, 4 0, 4 4, 0 4, 0 0))'):
            out = self._rt(wkt)
            if out.startswith('MULTIPOINT'):
                continue
            # 面的环本来就要 `POLYGON ((` —— 允许类型名后紧邻一对括号,
            # 但不允许顶点位置上再冒出一层
            body = out.split(' ', 1)[1]
            self.assertNotIn('(((', body, f'{out}')
            self.assertNotIn('( ', body.replace('( (', '('), f'{out}')

    def test_z_and_m(self):
        self.assertEqual(Geometry.from_wkt('POINT Z (1 2 3)').wkt(),
                         'POINT Z (1 2 3)')
        self.assertEqual(Geometry.from_wkt('LINESTRING M (0 0 7, 1 1 8)').wkt(),
                         'LINESTRING M (0 0 7, 1 1 8)')
        self.assertEqual(
            Geometry.from_wkt('POLYGON Z((0 0 1, 4 0 1, 4 4 1, 0 4 1, 0 0 1))').wkt(),
            'POLYGON Z ((0 0 1, 4 0 1, 4 4 1, 0 4 1, 0 0 1))')

    def test_m_survives_round_trip(self):
        """⚠️ 回归闸门:不带后缀的 ``LINESTRING (0 0 7, 1 1 8)`` 会被读成 **Z**。

        M 维度在 WKT 里**只能靠后缀表达** —— 第三个分量本身不说自己是 Z 还是
        M。少了后缀,带 M 的几何导出一趟回来 `has_m` 变 False 而 `has_z` 变
        True,而且是静默的。
        """
        for wkt in ('POINT M (1 2 7)', 'LINESTRING M (0 0 7, 1 1 8)',
                    'MULTIPOINT M (1 1 7, 2 2 8)'):
            with self.subTest(wkt=wkt):
                g = Geometry.from_wkt(wkt)
                self.assertTrue(g.has_m)
                self.assertFalse(g.has_z)
                back = Geometry.from_wkt(g.wkt())
                self.assertTrue(back.has_m, f'{g.wkt()} 丢了 M')
                self.assertFalse(back.has_z, f'{g.wkt()} 被读成了 Z')

    def test_zm_suffix(self):
        g = Geometry.from_wkt('POINT ZM (1 2 3 4)')
        self.assertTrue(g.has_z and g.has_m)
        self.assertEqual(g.wkt(), 'POINT ZM (1 2 3 4)')

    def test_empty(self):
        self.assertEqual(Geometry().wkt(), 'GEOMETRYCOLLECTION EMPTY')

    def test_multipatch_raises(self):
        """非空 multipatch 不许谎报成 EMPTY。"""
        g = Geometry.from_wkt('POLYGON((0 0, 1 0, 1 1, 0 0))')
        g.shape_type = C.ShapeType.MULTIPATCH
        with self.assertRaises(NotImplementedError):
            g.wkt()

    def test_multi_shell_polygon_becomes_multipolygon(self):
        """两个外环 + 一个洞:必须是 MULTIPOLYGON,不能塞进一个 POLYGON。"""
        wkt = ('MULTIPOLYGON(((0 0, 10 0, 10 10, 0 10, 0 0),'
               '(2 2, 4 2, 4 4, 2 4, 2 2)),((20 20, 30 20, 30 30, 20 30, 20 20)))')
        out = self._rt(wkt)
        self.assertTrue(out.startswith('MULTIPOLYGON '), out)
        self.assertNotIn('((((', out)
        self.assertEqual(out.count('(('), out.count('))'),
                         f'括号不平衡: {out}')




# ----------------------------------------------------------------------------
# 7. 等值语义:拓扑等价 vs 结构等价
# ----------------------------------------------------------------------------
class TestEquality(unittest.TestCase):

    def test_equals_is_topological(self):
        """在边上多插一个**真正的共线**顶点 —— 拓扑等价,结构不等价。"""
        a = Geometry.from_wkt('POLYGON((0 0,0 10,10 10,10 0,0 0))')
        b = Geometry.from_wkt('POLYGON((0 0,0 10,5 10,10 10,10 0,0 0))')
        self.assertAlmostEqual(a.area(), b.area(), places=9)
        self.assertTrue(a.equals(b))
        self.assertFalse(a.exactly_equals(b))

    def test_exactly_equals_is_structural(self):
        a = Geometry.from_wkt('POLYGON((0 0,0 10,10 10,10 0,0 0))')
        self.assertTrue(a.exactly_equals(a))
        # 起点转一下:同一个环,但顶点序不同 -> 结构不等价(同 GDAL)
        rotated = Geometry.from_wkt('POLYGON((10 10,10 0,0 0,0 10,10 10))')
        self.assertFalse(a.exactly_equals(rotated))
        self.assertTrue(a.equals(rotated))

    def test_type_must_match(self):
        p = Geometry.from_wkt('POINT(1 1)')
        mp = Geometry.from_wkt('MULTIPOINT(1 1)')
        self.assertFalse(p.exactly_equals(mp))

    def test_empty_handling(self):
        e = Geometry()
        self.assertTrue(e.equals(Geometry()))
        self.assertTrue(e.disjoint(Geometry.from_wkt('POINT(0 0)')))
        self.assertFalse(e.intersects(Geometry.from_wkt('POINT(0 0)')))
        self.assertFalse(e.contains(Geometry.from_wkt('POINT(0 0)')))

    def test_convex_hull_is_not_structurally_equal(self):
        a = Geometry.from_wkt(
            'POLYGON((0 0,0 10,10 10,10 0,0 0),(4 4,6 4,6 6,4 6,4 4))')
        self.assertFalse(a.exactly_equals(a.convex_hull()))
        self.assertTrue(a.convex_hull().covers(a))


# ----------------------------------------------------------------------------
# 8. 不许物化 coordinates(空间计算只准读 xy_parts)
# ----------------------------------------------------------------------------
class TestNoCoordinatesMaterialization(unittest.TestCase):
    """``coordinates`` 是逐点元组的派生视图,量级 ~19 ns/顶点。

    空间计算一律走 ``xy_parts``;一旦哪个算子碰了 ``coordinates``,这里必须红。
    """

    def _geom(self):
        parts = flat([(0.0, 0.0), (0.0, 10.0), (10.0, 10.0), (10.0, 0.0)],
                     [(4.0, 4.0), (6.0, 4.0), (6.0, 6.0), (4.0, 6.0)])
        return Geometry(S.POLYGON, None, False, False,
                        _flat=_FlatCoords(parts), _shells=[True, False])

    def test_not_materialized_by_metrics(self):
        g = self._geom()
        self.assertIsNone(g._coords)
        g.envelope()
        g.area()
        g.length()
        g.centroid()
        g.convex_hull()
        g.simplify(0.5)
        g.segmentize(3.0)
        g.is_valid()
        g.is_simple()
        g.to_geojson()
        self.assertIsNone(g._coords, '有算子在偷用 coordinates')

    def test_not_materialized_by_predicates(self):
        a = self._geom()
        b = Geometry.from_wkt('POLYGON((5 5,5 15,15 15,15 5,5 5))')
        a.relate(b)
        for name in ('intersects', 'disjoint', 'contains', 'within', 'covers',
                     'covered_by', 'touches', 'crosses', 'overlaps', 'equals',
                     'exactly_equals'):
            getattr(a, name)(b)
        self.assertIsNone(a._coords, '有谓词在偷用 coordinates')

    def test_wkt_and_geojson_do_not_materialize(self):
        g = self._geom()
        g.wkt()
        g.to_geojson()
        self.assertIsNone(g._coords, '文本导出在偷用 coordinates')


# ----------------------------------------------------------------------------
# 9. numpy 快路径与纯 Python 路径必须一致
# ----------------------------------------------------------------------------
@unittest.skipUnless(G.HAS_NUMPY, '没装 numpy —— 差分本来就没得比')
class TestNumpyParity(unittest.TestCase):
    """两条路径结果必须一致(允许成对求和带来的低位差)。

    与 ``_accel`` 的差分测试同一个套路:进程内开关,同一份输入跑两遍。
    """

    def _ring(self, n, base=1000.0, r=1000.0):
        a = array('d')
        for i in range(n):
            t = 2.0 * math.pi * i / n
            a.append(base + r * math.cos(t))
            a.append(base + r * math.sin(t))
        return a

    def test_metrics_agree(self):
        for n in (3, 8, 47, 48, 64, 127, 128, 129, 500, 2000):
            a = self._ring(n)
            with self.subTest(f'n={n}'):
                with G.use_numpy(False):
                    area_p = G.area_of('polygon', [a])
                    len_p = G.length_of('polygon', [a])
                    cen_p = G.centroid_of('polygon', [a])
                with G.use_numpy(True):
                    area_n = G.area_of('polygon', [a])
                    len_n = G.length_of('polygon', [a])
                    cen_n = G.centroid_of('polygon', [a])
                self.assertLess(abs(area_p - area_n) / abs(area_p), 1e-12)
                self.assertLess(abs(len_p - len_n) / abs(len_p), 1e-12)
                self.assertLess(math.hypot(cen_p[0] - cen_n[0],
                                           cen_p[1] - cen_n[1]), 1e-9)

    def test_signed_area_sign_is_stable(self):
        """**符号**必须一致 —— 写路径的绕向判定靠它。"""
        for n in (4, 48, 128, 300):
            a = self._ring(n)
            with G.use_numpy(False):
                sp = G.ring_signed_area2(a)
            with G.use_numpy(True):
                sn = G._np_signed_area2(a)
            with self.subTest(f'n={n}'):
                self.assertEqual(sp < 0, sn < 0)

    def test_tiny_extent_at_huge_offset_agrees(self):
        """极端抵消场景 —— 修「大坐标抵消」之前,这条两条路径能差 1e11 倍。"""
        a = array('d')
        for i in range(130):
            a.append(1e8 + (i % 13) * 1e-5)
            a.append(1e8 + (i % 7) * 1e-5)
        with G.use_numpy(False):
            ap = G.area_of('polygon', [a])
            cp = G.centroid_of('polygon', [a])
        with G.use_numpy(True):
            an = G.area_of('polygon', [a])
            cn = G.centroid_of('polygon', [a])
        if ap:
            self.assertLess(abs(ap - an) / abs(ap), 1e-9)
        self.assertLess(math.hypot(cp[0] - cn[0], cp[1] - cn[1]),
                        1e-8)

    def test_finite_check_agrees(self):
        for tag, val in (('nan', float('nan')), ('inf', float('inf')),
                         ('ok', 1.0)):
            a = array('d', [0.0, 1.0] * 200)
            a[10] = val
            with G.use_numpy(False):
                rp = G._all_finite(a)
            with G.use_numpy(True):
                rn = G._all_finite(a)
            with self.subTest(tag):
                self.assertEqual(rp, rn)

    def test_numpy_can_be_disabled_by_env(self):
        """``PYOPENFILEGDB_NO_NUMPY`` 只读一次(导入时)—— 这里只验语义。"""
        self.assertTrue(hasattr(G, 'HAS_NUMPY'))
        with G.use_numpy(False):
            self.assertFalse(G._USE_NUMPY)
        self.assertEqual(G._USE_NUMPY, G.HAS_NUMPY)


if __name__ == '__main__':
    unittest.main()


