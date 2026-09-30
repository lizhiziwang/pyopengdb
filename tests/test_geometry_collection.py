# -*- coding: utf-8 -*-
"""``GEOMETRYCOLLECTION`` —— 本库唯一的**纯内存**几何类型。

跑法::

    /d/zsh/app/py_3.13.1/python -m unittest tests.test_geometry_collection -v

为什么单开一个文件
------------------
FileGDB 的 ``ShapeType`` 里**没有** ``GEOMETRYCOLLECTION`` 这一档,所以它不是
一个"能读能写的 Esri 类型",而是一个**只作为 overlay 结果出现**的内存类型
(``POLYGON ∪ 面外的 POINT``)。正因为它在盘上没有对应物,它的每一条能力都是
**本库自己定的口径**,必须逐条钉住:

* 哪些能算(:attr:`dimension` / :meth:`envelope` / :meth:`area` / :meth:`length`
  / :meth:`relate` / WKT / GeoJSON),
* 哪些**明确拒绝**(坐标属性抛 ``TypeError``;``centroid`` / ``distance`` /
  ``convex_hull`` / ``simplify`` / ``segmentize`` / ``buffer`` / overlay 抛
  ``NotImplementedError``,消息里给理由),
* 哪些**必须报错而不是静默**(写盘 —— :func:`encode_geometry` 抛
  ``GdbWriteError``,绝不悄悄写个 NULL 出去)。

本文件**不 import shapely / osgeo** —— 对拍 GEOS 的活在 ``tools/`` 里,只有
``tools/`` 才允许碰它们(见 README「与 GDAL 的关系」)。

两处**与 GEOS 不一致**的口径(都是量过的,不是猜的)
---------------------------------------------------
1. **DE-9IM 的"内部"按"各子几何内部的并"算。** OGC 的 DE-9IM 对**混合维度**
   集合本来就没有把内部/边界定义到"逐点唯一",GEOS 3.13.1 实测走的是
   "**维度最高的子几何说了算**"。两套都对不上唯一真值,本库选了可证明的那套
   (见 ``geometry._relate_collection`` 的 docstring)。
2. **``area`` / ``length`` 是子几何之和**(跟 GEOS 的
   ``GeometryCollection``),⚠️ 而 **GDAL 的
   ``OGRGeometryCollection::get_Area()`` 恒返回 0** —— 这处分岔在
   :meth:`Geometry.area` 的 docstring 里写着,本文件也钉一条。
"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pyopenfilegdb import _constants as C                        # noqa: E402
from pyopenfilegdb._esri_geometry import (encode_geometry,       # noqa: E402
                                          from_geojson)
from pyopenfilegdb.geometry import Geometry                      # noqa: E402

SQ = 'POLYGON ((0 0, 1 0, 1 1, 0 0))'          # 面积 0.5,周长 2 + √2
PT = 'POINT (1 2)'                             # 在 SQ 外面
LN = 'LINESTRING (0 0, 5 5)'                   # 长 5√2
BIG = 'POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0))'

_OPS = ('intersection', 'union', 'difference', 'symmetric_difference')

#: DE-9IM 的 9 个格子按行优先的顺序,写注释用。
_DE9IM_LAYOUT = ('II', 'IB', 'IE', 'BI', 'BB', 'BE', 'EI', 'EB', 'EE')


def _g(wkt):
    return Geometry.from_wkt(wkt)


def _gc(*items):
    """集合构造器:每一项可以是 WKT 串,也可以已经是 :class:`Geometry`。"""
    return Geometry.geometry_collection(
        [x if isinstance(x, Geometry) else _g(x) for x in items])


def _transpose(m):
    """DE-9IM 转置:把 (行,列) 换成 (列,行)。

    ``relate(a, b)`` 与 ``relate(b, a)`` 必须互为转置 —— 这是本文件里**唯一**
    一条不需要真值、也不依赖 oracle 的硬不变量(转置是 9 格的置换,而"逐格
    取最大 + 收紧"在每一格上都是独立的一元运算,两者天然可交换)。
    """
    assert len(m) == 9
    return (m[0] + m[3] + m[6]
            + m[1] + m[4] + m[7]
            + m[2] + m[5] + m[8])


def _from_matrix(m):
    """按 OGC 的定义从 DE-9IM 反推四个谓词,用来和库里的实现对照。

    只反推有唯一标准的这几条(模式串是 OGC 原文):

    ==================  ==============
    ``intersects``      ``II``/``IB``/``BI``/``BB`` 任一非 ``F``
    ``contains``        ``II`` 非 ``F`` 且 ``EI`` / ``EB`` 都是 ``F``
    ``equals``          ``T*F**FFF*``
    ``disjoint``        ``intersects`` 的反面
    ==================  ==============
    """
    def ne(i):
        return m[i] != 'F'

    return {
        'intersects': ne(0) or ne(1) or ne(3) or ne(4),
        'contains': ne(0) and not ne(6) and not ne(7),
        'equals': ne(0) and not ne(2) and not ne(5) and not ne(6) and not ne(7),
        'disjoint': not (ne(0) or ne(1) or ne(3) or ne(4)),
    }


class _GCBase(unittest.TestCase):
    """共用的小工具。"""

    def assertRaisesMsg(self, exc, needle, fn, *args, **kwargs):
        try:
            fn(*args, **kwargs)
        except exc as e:
            self.assertIn(needle, str(e))
            return str(e)
        except Exception as e:                      # noqa: BLE001
            self.fail('期望 %s,实际 %s: %s'
                      % (exc.__name__, type(e).__name__, e))
        self.fail('期望 %s,但什么都没抛' % exc.__name__)


# ---------------------------------------------------------------------------
# 一、构造与基本属性
# ---------------------------------------------------------------------------
class TestConstruction(_GCBase):

    def test_kind_is_geometrycollection(self):
        self.assertEqual(_gc(SQ, PT).kind, 'geometrycollection')

    def test_shape_type_is_a_memory_only_constant(self):
        """⚠️ 这条守的是"它不是 Esri 的值"这个前提。

        ``-1`` 落在 Esri 那套 ``0``–``54`` 之外,也落在 OGR 的 ``1``–``7``
        之外 —— 任何"按编号查表"的老代码都不会把它误认成某个真类型。
        """
        gc = _gc(SQ)
        self.assertEqual(gc.shape_type, C.ShapeType.GEOMETRYCOLLECTION)
        self.assertEqual(C.ShapeType.GEOMETRYCOLLECTION, -1)
        self.assertFalse(C.ShapeType.has_z(C.ShapeType.GEOMETRYCOLLECTION))
        self.assertFalse(C.ShapeType.has_m(C.ShapeType.GEOMETRYCOLLECTION))
        self.assertNotIn(C.ShapeType.GEOMETRYCOLLECTION, C.VALID_TABLE_GEOM_TYPES)

    def test_children_are_kept_as_given(self):
        """空子几何**不丢**、嵌套 GC **不摊平** —— 结构原样保留。"""
        empty = Geometry()
        gc = _gc(SQ, PT)
        n = Geometry.geometry_collection([empty, gc])
        self.assertEqual(n.geometry_count, 2)
        self.assertEqual(n.geometry_at(0).is_empty, True)
        self.assertEqual(n.geometry_at(1).kind, 'geometrycollection')

    def test_empty_collection(self):
        e = Geometry.geometry_collection([])
        self.assertEqual(e.kind, 'geometrycollection')
        self.assertTrue(e.is_empty)
        self.assertEqual(e.geometry_count, 0)
        self.assertEqual(e.wkt(), 'GEOMETRYCOLLECTION EMPTY')

    def test_geometry_count_and_at(self):
        gc = _gc(SQ, PT, LN)
        self.assertEqual(gc.geometry_count, 3)
        self.assertEqual(gc.geometry_at(1).wkt(), PT)
        self.assertEqual(gc.geometry_at(2).wkt(), LN)

    def test_geometry_at_negative_index(self):
        gc = _gc(SQ, PT)
        self.assertEqual(gc.geometry_at(-1).wkt(), PT)

    def test_geometry_at_out_of_range(self):
        gc = _gc(SQ, PT)
        self.assertRaisesMsg(IndexError, '越界', gc.geometry_at, 9)
        self.assertRaisesMsg(IndexError, '共 2 个', gc.geometry_at, -3)

    def test_geometries_is_a_copy(self):
        """``geometries()`` 给的是新列表 —— 原地 ``append`` 不该改到这个几何。"""
        gc = _gc(SQ, PT)
        kids = gc.geometries()
        self.assertEqual(len(kids), 2)
        kids.append(_g(LN))
        self.assertEqual(gc.geometry_count, 2)

    def test_non_geometry_child_names_the_index(self):
        """消息里必须点出**第几个**错了 —— 一次传几十个子几何时,只说
        "有一个不是 Geometry" 等于没说。⚠️ 下标从 **1** 数(给用户看的)。"""
        self.assertRaisesMsg(TypeError, '第 2 个', Geometry.geometry_collection,
                             [_g(SQ), 'not a geometry'])

    def test_count_on_a_plain_polygon_raises(self):
        """⚠️ 刻意不返回 0:**"有 0 个子几何"和"装不下子几何"是两件事**。"""
        for name in ('geometry_count',):
            self.assertRaisesMsg(TypeError, '只对 GEOMETRYCOLLECTION',
                                 getattr, _g(SQ), name)
        self.assertRaisesMsg(TypeError, '只对 GEOMETRYCOLLECTION',
                             _g(SQ).geometry_at, 0)
        self.assertRaisesMsg(TypeError, '只对 GEOMETRYCOLLECTION',
                             _g(SQ).geometries)


# ---------------------------------------------------------------------------
# 二、度量
# ---------------------------------------------------------------------------
class TestMetrics(_GCBase):

    def test_dimension_is_the_max_child_dimension(self):
        self.assertEqual(Geometry.geometry_collection([]).dimension, 0)
        self.assertEqual(_gc(PT).dimension, 0)
        self.assertEqual(_gc(PT, LN).dimension, 1)
        self.assertEqual(_gc(PT, LN, SQ).dimension, 2)

    def test_area_is_the_sum_of_children(self):
        """⚠️ **GDAL 分岔**:``OGRGeometryCollection::get_Area()`` 恒返回 0,
        本库跟 **GEOS** 的 ``GeometryCollection``(子几何之和)。"""
        self.assertEqual(_gc(SQ, PT, LN).area(), 0.5)
        self.assertEqual(_gc(BIG, SQ).area(), 100.5)
        self.assertEqual(Geometry.geometry_collection([]).area(), 0.0)

    def test_length_is_the_sum_of_children(self):
        import math
        want = 2.0 + math.sqrt(2.0) + 5.0 * math.sqrt(2.0)
        self.assertAlmostEqual(_gc(SQ, LN).length(), want, places=12)
        self.assertEqual(Geometry.geometry_collection([]).length(), 0.0)

    def test_envelope_is_the_union_of_children(self):
        self.assertEqual(_gc(SQ, PT).envelope(), (0.0, 0.0, 1.0, 2.0))
        self.assertEqual(_gc(SQ, BIG).envelope(), (0.0, 0.0, 10.0, 10.0))
        self.assertIsNone(Geometry.geometry_collection([]).envelope())

    def test_point_count_and_part_count(self):
        gc = _gc(SQ, PT, LN)
        self.assertEqual(gc.point_count, 3 + 1 + 2)
        self.assertEqual(gc.part_count, 3)

    def test_is_empty_when_every_child_is_empty(self):
        e = Geometry.geometry_collection([])
        self.assertTrue(e.is_empty)
        self.assertTrue(Geometry.geometry_collection([Geometry()]).is_empty)
        self.assertTrue(_gc(SQ).is_empty is False)
        self.assertFalse(Geometry.geometry_collection([Geometry(), _g(SQ)]).is_empty)

    def test_is_ring_and_clockwise_are_false(self):
        """⚠️ 是 ``False`` 不是抛异常 —— "这个集合是不是一个环"有明确答案:
        不是。抛异常反而会让 ``if g.is_ring:`` 这种写法炸掉。"""
        gc = _gc(SQ, PT)
        self.assertFalse(gc.is_ring)
        self.assertFalse(gc.is_clockwise)

    def test_is_valid_and_is_simple_are_all_children(self):
        self.assertTrue(_gc(SQ, PT, LN).is_valid())
        self.assertTrue(_gc(SQ, PT, LN).is_simple())
        # 自相交的环 —— 不是合法面;一个坏子几何就把整个集合带坏
        bad = Geometry.from_wkt('POLYGON ((0 0, 10 10, 10 0, 0 10, 0 0))')
        self.assertFalse(bad.is_valid())
        self.assertFalse(_gc(SQ, bad).is_valid())
        self.assertTrue(_gc(SQ).is_valid())

    def test_z_flag_is_any_child(self):
        z = Geometry.geometry_collection([_g('POINT Z (1 2 3)'), _g(SQ)])
        self.assertTrue(z.has_z)
        self.assertFalse(z.has_m)
        self.assertFalse(_gc(SQ, PT).has_z)

    def test_structure_survives_the_empty_copy_path(self):
        """⚠️ 本项目第 6 次踩的那个坑的 GC 版本。

        ``Geometry._like()`` 是"换一批坐标"的口子,GC 没有坐标,它必须**原样
        抄子几何列表**,而不是掉进 ``if not parts:`` 那一档造出 ``_geoms=None``
        —— 后者 ``is_empty`` 也还是 ``True``,但 ``geometry_count`` 会从
        "2 个空子几何"变成 0,结构被悄悄丢掉。走 ``transform_geometry`` 的
        空几何分支正好能碰到这条路。
        """
        from pyopenfilegdb.coordinates_system import transform_geometry
        gc = Geometry.geometry_collection([Geometry(), Geometry.from_wkt('POLYGON EMPTY')])
        self.assertEqual(gc.geometry_count, 2)
        out = transform_geometry(gc, 3857, src=4326)
        self.assertEqual(out.geometry_count, 2)
        self.assertTrue(out.is_empty)
        self.assertTrue(out.exactly_equals(gc))


# ---------------------------------------------------------------------------
# 三、明确拒绝的两档
# ---------------------------------------------------------------------------
class TestCoordinatesRefused(_GCBase):

    def test_every_coordinate_attribute_raises_type_error(self):
        """⚠️ **必须报错,不能拼一份出来。** GC 的"坐标"没有单一含义,
        拼出来调用方会把它当成单一几何读(OGR 对 GC 也不给坐标)。"""
        gc = _gc(SQ, PT)
        for name in ('coordinates', 'xy_parts', 'z_parts', 'm_parts'):
            self.assertRaisesMsg(TypeError, name, getattr, gc, name)

    def test_message_says_what_to_do_instead(self):
        gc = _gc(SQ, PT)
        msg = self.assertRaisesMsg(TypeError, 'GEOMETRYCOLLECTION 没有',
                                   getattr, gc, 'xy_parts')
        self.assertIn('geometry_at', msg)
        self.assertIn('geometries()', msg)


class TestRefusals(_GCBase):

    def test_metric_operations_are_refused_with_a_reason(self):
        """``NotImplementedError`` 而不是"返回一个假的数" —— 静默返回面积 0
        或"质心取平均"比报错糟得多:调用方分不出"算出来是 0"和"没算"。"""
        gc = _gc(SQ, PT)
        cases = [
            ('centroid', lambda: gc.centroid()),
            ('distance', lambda: gc.distance(_g(SQ))),
            ('convex_hull', lambda: gc.convex_hull()),
            ('simplify', lambda: gc.simplify(1.0)),
            ('segmentize', lambda: gc.segmentize(1.0)),
            ('buffer', lambda: gc.buffer(1.0)),
        ]
        for name, fn in cases:
            msg = self.assertRaisesMsg(NotImplementedError, name + '()', fn)
            self.assertIn('DESIGN.md', msg)

    def test_overlay_is_refused_in_both_directions(self):
        """GC 作为 overlay 的**输入**还没做(要走 GEOS 的
        ``StructuredCollection``),两个方向都要挡。"""
        gc = _gc(SQ, PT)
        sq = _g(SQ)
        for op in _OPS:
            self.assertRaisesMsg(NotImplementedError, 'GEOMETRYCOLLECTION',
                                 getattr(gc, op), sq)
            self.assertRaisesMsg(NotImplementedError, 'GEOMETRYCOLLECTION',
                                 getattr(sq, op), gc)

    def test_a_plain_geometry_still_works(self):
        """反面守:上面那堆拒绝**只**对 GC 生效,别把普通几何一起挡掉。"""
        self.assertAlmostEqual(_g(BIG).difference(_g(SQ)).area(), 99.5)


# ---------------------------------------------------------------------------
# 四、WKT
# ---------------------------------------------------------------------------
class TestWkt(_GCBase):

    def test_round_trip(self):
        gc = _gc(SQ, PT)
        back = Geometry.from_wkt(gc.wkt())
        self.assertEqual(back.kind, 'geometrycollection')
        self.assertTrue(back.exactly_equals(gc))

    def test_mixed_dimension_children_keep_their_own_z(self):
        """⚠️ **外层不写 ``Z`` / ``M`` 后缀。** WKT1 的
        ``GEOMETRYCOLLECTION Z (...)`` 意思是"每个子几何都是 Z",而本库的 GC
        允许混合维度;硬套会把 2D 子几何改成"Z 值全 NaN"。所以后缀只写在
        带 Z 的那个**子几何**上。"""
        gc = Geometry.geometry_collection([_g('POINT Z (1 2 3)'), _g(SQ)])
        wkt = gc.wkt()
        self.assertEqual(wkt, 'GEOMETRYCOLLECTION (POINT Z (1 2 3), '
                              'POLYGON ((0 0, 1 0, 1 1, 0 0)))')
        self.assertNotIn('GEOMETRYCOLLECTION Z', wkt)
        back = Geometry.from_wkt(wkt)
        self.assertTrue(back.has_z)
        self.assertFalse(back.geometry_at(1).has_z)
        self.assertEqual(back.geometry_at(0).coordinates, (1.0, 2.0, 3.0))

    def test_nested_collections_round_trip(self):
        n = Geometry.geometry_collection([_gc(SQ, PT), _g(LN)])
        back = Geometry.from_wkt(n.wkt())
        self.assertEqual(back.geometry_count, 2)
        self.assertEqual(back.geometry_at(0).kind, 'geometrycollection')
        self.assertTrue(back.exactly_equals(n))

    def test_empty_renders_as_empty_collection(self):
        self.assertEqual(Geometry.geometry_collection([]).wkt(),
                         'GEOMETRYCOLLECTION EMPTY')
        self.assertTrue(Geometry.from_wkt('GEOMETRYCOLLECTION EMPTY').is_empty)

    def test_outer_z_suffix_is_rejected(self):
        """⚠️ 这是**明确拒绝**而不是"尽量解析" —— 收下它就等于把 2D 子几何
        读成 Z 值全 NaN。"""
        self.assertRaisesMsg(
            Exception, '外层 Z / M',
            Geometry.from_wkt, 'GEOMETRYCOLLECTION Z (POINT Z (1 2 3))')
        self.assertRaisesMsg(
            Exception, '外层 Z / M',
            Geometry.from_wkt, 'GEOMETRYCOLLECTION M (POINT M (1 2 3))')

    def test_broken_bodies_are_rejected(self):
        cases = [
            ('GEOMETRYCOLLECTION (POINT (1 2)', '括号不平衡'),
            ('GEOMETRYCOLLECTION (POINT (1 2),)', '第 2 个子几何是空的'),
            ('GEOMETRYCOLLECTION ()', '没有子几何'),
        ]
        for wkt, needle in cases:
            self.assertRaisesMsg(Exception, needle, Geometry.from_wkt, wkt)


# ---------------------------------------------------------------------------
# 五、GeoJSON
# ---------------------------------------------------------------------------
class TestGeoJson(_GCBase):

    def test_geo_interface_shape(self):
        """GeoJSON 本来就有 ``GeometryCollection`` 这个标准类型。"""
        d = _gc(SQ, PT).__geo_interface__
        self.assertEqual(d['type'], 'GeometryCollection')
        self.assertEqual([g['type'] for g in d['geometries']],
                         ['Polygon', 'Point'])

    def test_round_trip(self):
        gc = _gc(SQ, PT)
        back = from_geojson(gc.__geo_interface__)
        self.assertEqual(back.kind, 'geometrycollection')
        self.assertTrue(back.exactly_equals(gc))

    def test_nested_round_trip(self):
        n = Geometry.geometry_collection([_gc(SQ, PT), _g(LN)])
        back = from_geojson(n.__geo_interface__)
        self.assertTrue(back.exactly_equals(n))

    def test_empty_is_an_empty_geometry(self):
        """空 GC 走的是全库统一的"空 → 空几何"口径(与 ``from_wkt`` 一致),
        不是"空的 GEOMETRYCOLLECTION 实例" —— 两者 ``is_empty`` 都是 ``True``。"""
        back = from_geojson(Geometry.geometry_collection([]).__geo_interface__)
        self.assertTrue(back.is_empty)


# ---------------------------------------------------------------------------
# 六、相等性:比**结构**,不是比点集
# ---------------------------------------------------------------------------
class TestEquality(_GCBase):

    def test_eq_compares_the_child_sequence(self):
        self.assertTrue(_gc(SQ, PT) == _gc(SQ, PT))
        self.assertFalse(_gc(SQ, PT) == _gc(PT, SQ))

    def test_order_matters(self):
        """⚠️ 顺序有意义(与 ``OGRGeometryCollection::Equals`` 同口径:逐子
        几何比)。要"空间等价"请用 :meth:`equals`。"""
        self.assertFalse(_gc(SQ, PT) == _gc(PT, SQ))

    def test_empty_vs_collection_of_one_empty_differ(self):
        """⚠️ 结构不同就是不同 —— ``GC()`` 与 ``GC(POLYGON EMPTY)`` 的
        ``geometry_count`` 是 0 与 1。两条判据都必须放在 ``is_empty``
        **之前**,否则先判空会把它们算成相等。"""
        a = Geometry.geometry_collection([])
        b = Geometry.geometry_collection([Geometry.from_wkt('POLYGON EMPTY')])
        self.assertTrue(a.is_empty and b.is_empty)
        self.assertFalse(a == b)
        self.assertFalse(a.exactly_equals(b))

    def test_exactly_equals_matches_eq_on_collections(self):
        pairs = [(_gc(SQ, PT), _gc(SQ, PT), True),
                 (_gc(SQ, PT), _gc(PT, SQ), False),
                 (Geometry.geometry_collection([]), _gc(SQ), False)]
        for a, b, want in pairs:
            self.assertEqual(a.exactly_equals(b), want)
            self.assertEqual(a == b, want)

    def test_type_mismatch_is_not_equal(self):
        self.assertNotEqual(_gc(SQ), _g(SQ))
        self.assertFalse(_gc(SQ).exactly_equals(_g(SQ)))


# ---------------------------------------------------------------------------
# 七、relate / 谓词
# ---------------------------------------------------------------------------
class TestRelate(_GCBase):

    #: 一条"什么都没沾"的对照基线。
    def test_empty_collection_matrix(self):
        e = Geometry.geometry_collection([])
        self.assertEqual(e.relate(_g(SQ)), 'FFFFFFFF2')
        self.assertEqual(_g(SQ).relate(e), 'FFFFFFFF2')

    def test_transpose_invariant_over_a_battery(self):
        """**唯一一条不需要 oracle 的硬不变量。** 转置是 9 格的置换,而
        "逐格取最大 + 逐列/逐行收紧"在每一格上都是独立的一元运算,两者可交换
        —— 所以不管嵌多深都必须一致。"""
        kids = [SQ, BIG, PT, LN, 'POINT (50 50)',
                'POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0), '
                '(4 4, 6 4, 6 6, 4 6, 4 4))']
        others = [SQ, BIG, PT, LN, 'LINESTRING (5 5, 15 15)']
        combos = [_gc(kids[0], kids[2]),
                  _gc(kids[3], kids[2]),
                  _gc(kids[0], kids[3], kids[4]),
                  _gc(kids[5], kids[3]),
                  Geometry.geometry_collection([_gc(kids[0], kids[2]), _g(LN)])]
        for gc in combos:
            for w in others:
                other = _g(w)
                self.assertEqual(gc.relate(other),
                                 _transpose(other.relate(gc)),
                                 'GC=%s vs %s' % (gc.wkt(), w))

    def test_nested_collection_is_flattened_for_relate(self):
        """嵌套 GC 的 DE-9IM 与摊平后**完全一样** —— 摊平是递归的。"""
        nested = Geometry.geometry_collection([_gc(SQ, PT), _g(LN)])
        flat = _gc(SQ, PT, LN)
        for w in (SQ, PT, LN, BIG):
            self.assertEqual(nested.relate(_g(w)), flat.relate(_g(w)))

    def test_all_empty_children_are_dropped(self):
        """空子几何对 9 格的贡献是 ``FFFFFFFF2``,赢不了非空的那对,所以
        丢掉不改变结果 —— 但``geometry_count`` 仍然看得到它们。"""
        gc = Geometry.geometry_collection([Geometry(), _g(SQ), Geometry()])
        self.assertEqual(gc.geometry_count, 3)
        self.assertEqual(gc.relate(_g(SQ)), _gc(SQ).relate(_g(SQ)))

    def test_the_other_geometry_is_covered_by_a_child(self):
        """``GC(SQ, PT)`` 里的 ``PT`` 被算在 GC 的**内部** —— 那个点在 GC
        里,``II`` 是 ``0`` 而不是 ``F``,于是 ``contains`` 成立。

        ⚠️ 这条正是"逐格取最大"的正面用法:朴素做法若不收紧 ``EI`` / ``EB``,
        这里会算成"不包含"。``GT0F2FF10F2`` 是**错的**那个值,一并钉在这里。
        """
        gc = _gc(SQ, PT)
        m = gc.relate(_g(PT))
        self.assertEqual(m, '0F2FF1FF2')
        self.assertNotEqual(m, '0F2FF10F2')
        self.assertEqual(m[6], 'F')          # EI
        self.assertEqual(m[7], 'F')          # EB
        self.assertTrue(gc.contains(_g(PT)))
        self.assertTrue(_g(PT).within(gc))

    def test_the_outer_cells_are_tightened(self):
        """``GC`` 对**自己的子几何**:``ext(GC) ∩ cl(SQ)`` 按定义必须是空集
        (``SQ ⊆ GC``),所以 ``EI`` / ``EB`` 都只能是 ``F``。

        ⚠️ **GEOS 3.13.1 在这里给的是 ``2F0F1F212``**(``EI = 2`` /
        ``EB = 1``),那是**可证伪**的错值 —— 所以本文件不把 GEOS 当这档的
        oracle,只钉本库自己的口径(见 ``geometry._relate_collection``)。
        """
        m = _gc(SQ, PT).relate(_g(SQ))
        self.assertEqual(m, '2F0F1FFF2')
        self.assertEqual(m[6], 'F')
        self.assertEqual(m[7], 'F')

    def test_predicates_agree_with_the_matrix(self):
        """谓词必须是 ``relate`` 的纯函数 —— 两套实现各算一遍,逐条比。

        这条抓的是"谓词走了另一条捷径于是和矩阵打架"这一类。"""
        combos = [_gc(SQ, PT), _gc(SQ, LN, PT), _gc(BIG, _g('POINT (50 50)')),
                  Geometry.geometry_collection([_gc(SQ, PT), _g(LN)])]
        others = [SQ, PT, LN, BIG, 'POINT (50 50)', 'LINESTRING (5 5, 15 15)']
        for gc in combos:
            for w in others:
                other = _g(w)
                want = _from_matrix(gc.relate(other))
                got = {'intersects': gc.intersects(other),
                       'contains': gc.contains(other),
                       'equals': gc.equals(other),
                       'disjoint': gc.disjoint(other)}
                self.assertEqual(got, want, 'GC=%s vs %s' % (gc.wkt(), w))

    def test_multipatch_child_raises(self):
        """GC 自己那个 ``kind`` 是 ``'geometrycollection'``,顶层的
        ``_require_no_multipatch`` 看不见里面装了什么 —— 摊平那一步必须补上。"""
        patch = _g(SQ)
        patch.shape_type = C.ShapeType.MULTIPATCH
        gc = Geometry.geometry_collection([_g(SQ), patch])
        self.assertRaisesMsg(NotImplementedError, 'multipatch',
                             gc.relate, _g(SQ))


# ---------------------------------------------------------------------------
# 八、写盘必须报错
# ---------------------------------------------------------------------------
class TestWriteRefused(_GCBase):

    def test_non_empty_collection_cannot_be_encoded(self):
        """⚠️ **明确报错,不是悄悄写个 NULL。** 悄悄写 NULL 等于把整个几何
        吃掉 —— 盘上多一条空要素,而且没人知道。"""
        self.assertRaisesMsg(Exception, '写不进 FileGDB',
                             encode_geometry, _gc(SQ, PT))

    def test_message_says_what_to_do_instead(self):
        msg = self.assertRaisesMsg(Exception, 'ShapeType', encode_geometry,
                                   _gc(SQ, PT))
        self.assertIn('拆开', msg)

    def test_all_empty_collection_still_writes_null(self):
        """全部子几何都空 == 空几何,写 NULL 是**同一个含义**,没必要报错。

        ``SHPT_NULL == 0``,所以 blob 就是一个字节的 ``00``。
        """
        blob = encode_geometry(Geometry.geometry_collection([Geometry()]))
        self.assertEqual(blob, b'\x00')

    def test_geojson_still_works_when_the_blob_does_not(self):
        """反面守:拒绝的**只有** Esri blob 这一条路,GeoJSON 照常。"""
        self.assertIn('GeometryCollection', _gc(SQ, PT).to_geojson())


if __name__ == '__main__':      # pragma: no cover
    unittest.main()
