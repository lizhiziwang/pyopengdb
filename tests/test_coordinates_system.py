"""坐标转换(:mod:`pyopenfilegdb.coordinates_system`)的测试。

这一组测试**只有在装了 pyproj 时才跑** —— pyproj 是本库的**可选**依赖,
没装时整组 skip(不是失败)。这与 ``test_read.py`` 找不到样例库时 skip 是
同一个口径:环境缺东西不等于代码错。

里面有几条是**守着具体陷阱**的,不是凑覆盖率用的,写在各自的 docstring 里:

* ``origin`` 必须严格小于范围内的一切坐标 —— 否则 ``_grid()`` 出负数、
  ``write_varuint`` 直接 ``raise ValueError``(见
  :func:`pyopenfilegdb.coordinates_system.quantization_for`)。
* ``always_xy=True`` 不能去掉 —— 去掉之后 EPSG:4326 会按(纬度, 经度)
  的权威轴序解释,实测静默返回 ``(inf, inf)``。
* ``transform_geometry`` 必须返回**新几何**、不动原对象 —— 要素缓存解码后的
  几何,就地改会静默污染缓存。
* 量级体检要能抓到"投影坐标当经纬度"和反过来的两种喂错 —— pyproj 的
  ``errcheck`` 对这两种**都不报错**。

跑法::

    PYTHONIOENCODING=utf-8 D:/zsh/app/py_3.13.1/python -m unittest tests.test_coordinates_system -v
"""
from __future__ import annotations

import glob
import os
import shutil
import sys
import tempfile
import unittest
import warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pyopenfilegdb import (                                   # noqa: E402
    FGFT_DOUBLE,
    FGFT_STRING,
    GdbField,
    GdbSpatialRef,
    Geometry,
    OpenFileGDB,
)
from pyopenfilegdb import coordinates_system as cs            # noqa: E402
from pyopenfilegdb import core as _core                       # noqa: E402
from pyopenfilegdb._constants import ShapeType                # noqa: E402

#: 参照语料里的那个坐标系:CGCS2000 3 度带 GK zone 39。
EPSG_GK39 = 4527
EPSG_WGS84 = 4326

#: 从 ``村行政区划`` 上取的几个真实坐标(高斯-克吕格,带号 39)。
REAL_X = 39394370.79
REAL_Y = 3179362.10


def find_samples():
    """与 ``test_read.py`` 同口径:先看环境变量,否则扫 ``D:/work``。"""
    env = os.environ.get('PYOPENFILEGDB_TEST_GDB', '')
    if env:
        found = [p for p in env.split(os.pathsep) if p and os.path.isdir(p)]
        if found:
            return found
    return sorted(p for p in glob.glob('D:/work/*.gdb') if os.path.isdir(p))


SAMPLES = find_samples()

requires_pyproj = unittest.skipUnless(
    cs.HAS_PYPROJ, '没装 pyproj(可选依赖,装了才跑这组)')


# ===========================================================================
# 量化参数
# ===========================================================================
@requires_pyproj
class TestQuantization(unittest.TestCase):
    """``quantization_for``:两档的取值与那条"origin 必须更小"的硬约束。"""

    def setUp(self):
        from pyproj import CRS
        self.geog = CRS.from_epsg(EPSG_WGS84)
        self.proj = CRS.from_epsg(EPSG_GK39)

    def test_geographic_matches_default_quantization(self):
        """地理档必须与 ``core.DEFAULT_QUANTIZATION`` 逐位相同。

        那不是我们定的值 —— 它是 GDAL ``CreateGDBItems`` 与真实 ArcGIS
        空白库里 ``Shape`` 字段写的值。所以这里断言的是"没走偏",不是
        "我喜欢这个数"。
        """
        q = cs.quantization_for(self.geog, (-180.0, -90.0, 180.0, 90.0))
        for key in ('x_origin', 'y_origin', 'xy_scale', 'xy_tolerance'):
            self.assertEqual(q[key], _core.DEFAULT_QUANTIZATION[key],
                             f'{key} 与 DEFAULT_QUANTIZATION 不一致')

    def test_projected_matches_arcgis_convention(self):
        """投影档要复现参照语料里 ArcGIS 自己写的 ``scale=20000`` / ``tol=1e-4``。"""
        q = cs.quantization_for(self.proj, (REAL_X - 1e5, REAL_Y - 1e5,
                                           REAL_X + 1e5, REAL_Y + 1e5))
        self.assertEqual(q['xy_scale'], 20000.0)
        self.assertEqual(q['xy_tolerance'], 0.0001)
        # tolerance = 2 / scale 这条关系在地理档也成立(2e-6 vs 1e6)
        self.assertAlmostEqual(q['xy_tolerance'], 2.0 / q['xy_scale'], places=12)

    def test_origin_is_strictly_below_everything(self):
        """⚠️ 这条是本文件里最要紧的一条断言。

        ``_esri_geometry._grid`` 是 ``round((v - origin) * scale)``,而
        ``_util.write_varuint`` 明令 ``val < 0`` 就 ``raise ValueError``。
        所以只要有一个坐标小于 origin,写盘就会**直接炸**。
        地理档那个固定的 ``x_origin = -180`` 对投影坐标系是不安全的(完全
        可能小于 -180),这条顺带守住"投影档不许沿用地理档的 origin"。
        """
        bbox = (-5.0e5, -2.0e5, -3.0e5, 1.0e5)      # 全为负的投影坐标
        q = cs.quantization_for(self.proj, bbox)
        self.assertLess(q['x_origin'], bbox[0],
                        'x_origin 没有严格小于最小 x —— 写入会抛 ValueError')
        self.assertLess(q['y_origin'], bbox[1],
                        'y_origin 没有严格小于最小 y —— 写入会抛 ValueError')
        # 两侧都留了余量,不是"刚好小一点点"
        self.assertLess(q['x_origin'], bbox[0] - 1.0)

    def test_no_bbox_or_degenerate_bbox_returns_none(self):
        self.assertIsNone(cs.quantization_for(self.proj, None))
        self.assertIsNone(cs.quantization_for(self.proj, (1.0, 2.0, 1.0, 2.0)))
        self.assertIsNone(cs.quantization_for(self.proj, (0.0, 0.0,
                                                          float('nan'), 1.0)))

    def test_geocent_is_not_guessed(self):
        """既非地理也非投影的坐标系(地心)不猜,交给调用方用默认值。"""
        from pyproj import CRS
        geocent = CRS.from_epsg(4978)
        if geocent.is_geographic or geocent.is_projected:
            self.skipTest('这个 PROJ 版本把 4978 归类成了地理/投影')
        self.assertIsNone(cs.quantization_for(geocent, (1e6, 2e6, 3e6, 4e6)))


# ===========================================================================
# 坐标系解析
# ===========================================================================
@requires_pyproj
class TestResolveCrs(unittest.TestCase):

    def test_integer_string_and_prefix_forms_agree(self):
        from pyproj import CRS
        want = CRS.from_epsg(EPSG_GK39)
        for form in (EPSG_GK39, str(EPSG_GK39), 'EPSG:4527', 'epsg:4527'):
            with self.subTest(form=form):
                self.assertEqual(cs.resolve_crs(form), want)

    def test_crs_passthrough(self):
        from pyproj import CRS
        c = CRS.from_epsg(EPSG_WGS84)
        self.assertIs(cs.resolve_crs(c), c)

    def test_spatial_ref_uses_wkt_and_recovers_the_epsg_code(self):
        """Esri 的 WKT1 不带 ``ID`` 节点,但 PROJ 仍能认出它是 EPSG:4527。

        于是这里期望拿到**带 EPSG 身份**的那一份(多了使用域,PROJ 挑转换
        管道时会用上)。判据是两处独立证据一致:``to_epsg()`` 与文件里的
        WKID。数值上两份**逐位相同**(见 ``resolve_crs`` 的 docstring),
        所以这个升级不改变结果。
        """
        from pyproj import CRS
        wkt = _gk39_esri_wkt()
        sr = GdbSpatialRef(wkid=EPSG_GK39, latest_wkid=EPSG_GK39, wkt=wkt)
        got = cs.resolve_crs(sr)
        self.assertEqual(got, CRS.from_epsg(EPSG_GK39))
        self.assertIsNotNone(got.area_of_use)

    def test_bare_wkt_string_is_not_upgraded(self):
        """裸 WKT 字符串少了"文件里的 WKID"这第二处证据,所以不升级。

        结果数值上一样,只是没有 EPSG 身份与使用域。这条断言是**故意的**:
        它把这个差别钉住,免得以后有人"顺手"让它也升级。
        """
        from pyproj import CRS
        got = cs.resolve_crs(_gk39_esri_wkt())
        self.assertEqual(got.name, CRS.from_epsg(EPSG_GK39).name)
        self.assertNotEqual(got, CRS.from_epsg(EPSG_GK39))
        self.assertIsNone(got.area_of_use)

    def test_layer_object_resolves_via_its_spatial_ref(self):
        with _temp_layer_with_gk39() as (gdb, layer):
            self.assertEqual(cs.resolve_crs(layer),
                             cs.resolve_crs(layer.spatial_ref))

    def test_empty_spatial_ref_raises(self):
        with self.assertRaises(cs.CoordTransformError) as ctx:
            cs.resolve_crs(GdbSpatialRef())
        self.assertIn('没有坐标系', str(ctx.exception))

    def test_garbage_raises_readable_error(self):
        for bad in ('这不是坐标系', 999999, 'EPSG:notanumber', b'4527'):
            with self.subTest(bad=bad):
                with self.assertRaises(cs.CoordTransformError):
                    cs.resolve_crs(bad)

    def test_bool_is_rejected(self):
        """``bool`` 是 ``int`` 的子类;不先挡掉,``True`` 会被当成 EPSG:1。"""
        with self.assertRaises(cs.CoordTransformError):
            cs.resolve_crs(True)

    def test_empty_string_raises(self):
        with self.assertRaises(cs.CoordTransformError):
            cs.resolve_crs('   ')


# ===========================================================================
# 逐个几何的转换
# ===========================================================================
@requires_pyproj
class TestTransformGeometry(unittest.TestCase):

    def test_known_point_value(self):
        """一个真实坐标的已知结果(对着 pyproj 直接算的值钉住)。"""
        g = Geometry.from_wkt(f'POINT({REAL_X} {REAL_Y})')
        out = g.to_crs(EPSG_WGS84, src=EPSG_GK39)
        x, y = out.coordinates
        self.assertAlmostEqual(x, 115.91881971302487, places=9)
        self.assertAlmostEqual(y, 28.725838734163123, places=9)

    def test_axis_order_is_always_xy(self):
        """⚠️ 守 ``always_xy=True``。

        去掉它之后,EPSG:4326 会按权威轴序(纬度, 经度)解释,于是目标
        坐标会被**静默**换成错的一对;实测在参照语料那个坐标上直接返回
        ``(inf, inf)``。这条断言把"结果必须是有限且落在经纬度范围内"钉住,
        任何轴序回退都会当场失败。
        """
        g = Geometry.from_wkt(f'POINT({REAL_X} {REAL_Y})')
        x, y = g.to_crs(EPSG_WGS84, src=EPSG_GK39).coordinates
        self.assertTrue(x == x and y == y, f'拿到了 NaN: {(x, y)}')
        self.assertTrue(abs(x) <= 180.0 and abs(y) <= 90.0,
                        f'轴序像是反了或转换失败: {(x, y)}')
        # 反着转一次,应当回到原量级(而不是 inf)
        back = Geometry.from_wkt(f'POINT({x} {y})').to_crs(EPSG_GK39,
                                                          src=EPSG_WGS84)
        bx, by = back.coordinates
        self.assertAlmostEqual(bx, REAL_X, delta=1e-3)
        self.assertAlmostEqual(by, REAL_Y, delta=1e-3)

    def test_returns_new_object_and_leaves_original_alone(self):
        """⚠️ 守"不就地改" —— 要素缓存解码后的几何,就地改会静默污染缓存。

        与 ``OGRGeometry::Transform()`` 的语义**相反**,是刻意的(见
        ``transform_geometry`` 的 docstring)。

        **这条用例自己踩过两次坑,都值得记下来 —— 两次都是"守卫没在守"。**

        1. 最初写成 ``before = g.coordinates`` 再比 ``g.coordinates``:读一次就
           把派生视图物化进了 ``_coords``,此后实现就算把坐标整个换掉,断言
           仍返回那份旧缓存 —— 照样绿。
        2. 改成"和独立参照几何比 ``xy_parts``"之后,还是抓不住 —— 因为
           **POINT 几何的 ``xy_parts`` 根本不来自 ``_flat``**。``_like`` 对
           point 走的是另一条路(裸元组存进 ``_coords``,``_flat`` 恒为
           ``None``),所以"把 ``_flat`` 换掉"这个变异体对 POINT 是个**空操作**。

        结论:必须**既覆盖 point 又覆盖 polygon** —— 前者走 ``_coords``,
        后者走 ``_flat``,两条存储路径是分开的。断言同时压 ``xy_parts`` 与
        ``coordinates``,哪条路径被换掉都跑不掉。
        """
        cases = {
            'point': f'POINT({REAL_X} {REAL_Y})',
            'polygon': (f'POLYGON (({REAL_X} {REAL_Y}, {REAL_X + 100} {REAL_Y}, '
                        f'{REAL_X + 100} {REAL_Y + 100}, {REAL_X} {REAL_Y + 100}, '
                        f'{REAL_X} {REAL_Y}))'),
        }
        for tag, wkt in cases.items():
            with self.subTest(kind=tag):
                g = Geometry.from_wkt(wkt)
                ref = Geometry.from_wkt(wkt)          # 没被碰过的参照
                self.assertEqual(g.xy_parts, ref.xy_parts)     # 起点确实一致

                out = g.to_crs(EPSG_WGS84, src=EPSG_GK39)

                self.assertIsNot(out, g)
                self.assertEqual(g.xy_parts, ref.xy_parts,
                                 f'{tag}:原几何的坐标存储被换掉了')
                self.assertEqual(g.coordinates, ref.coordinates,
                                 f'{tag}:原几何的派生坐标视图被改了')
                self.assertEqual(g.shape_type, ref.shape_type)
                # 转换结果确实不一样,否则上面几条等于什么都没验
                self.assertNotEqual(out.xy_parts, g.xy_parts)

    def test_empty_and_null_geometries(self):
        for wkt in ('POINT EMPTY', 'LINESTRING EMPTY', 'POLYGON EMPTY',
                    'MULTIPOINT EMPTY'):
            with self.subTest(wkt=wkt):
                g = Geometry.from_wkt(wkt)
                out = cs.transform_geometry(g, EPSG_WGS84, src=EPSG_GK39,
                                            check=False)
                self.assertIsNot(out, g)
                self.assertTrue(out.is_empty)
                self.assertEqual(out.shape_type, g.shape_type)

    def test_none_stays_none(self):
        self.assertIsNone(cs.transform_geometry(None, EPSG_WGS84, src=EPSG_GK39))

    def test_z_passes_through_a_2d_transformer(self):
        """二维转换器收到 Z 会**原样透传** —— 这是 pyproj 实测行为,不是我加的。

        所以这里期望 Z 不变、XY 变。不要去"顺手"给 Z 也做点什么。
        """
        g = Geometry.from_wkt(f'POINT Z ({REAL_X} {REAL_Y} 55.5)', has_z=True)
        out = g.to_crs(EPSG_WGS84, src=EPSG_GK39)
        self.assertTrue(out.has_z)
        self.assertEqual(out.coordinates[2], 55.5)
        self.assertNotEqual(out.coordinates[0], REAL_X)

    def test_m_is_preserved(self):
        """M 与坐标系无关,必须原样保留(GeoJSON 那条路径会丢 M,这条不会)。

        读的是 ``m_parts`` / ``z_parts`` 这类**裸数组** —— 这正是库内部
        规定的读法。``.coordinates`` 也拿得到,但它是惰性物化的派生视图,
        在遍历循环里碰它等于把省下的钱又花回去(见 ``geometry.coordinates``
        的 docstring)。
        """
        g = Geometry.from_wkt(
            f'LINESTRING ZM ({REAL_X} {REAL_Y} 5 1, {REAL_X + 100} {REAL_Y} 9 2)',
            has_z=True, has_m=True)
        out = g.to_crs(EPSG_WGS84, src=EPSG_GK39)
        self.assertTrue(out.has_m)
        self.assertTrue(out.has_z)
        self.assertEqual(list(out.m_parts[0]), [1.0, 2.0])
        self.assertEqual(list(out.z_parts[0]), [5.0, 9.0])
        # XY 确实变了(不是把整条几何原样搬过去)
        self.assertNotEqual(list(out.xy_parts[0])[0], REAL_X)

    def test_multipart_rings_are_kept(self):
        """多壳面的 parts 结构必须原样搬过去,不能被摊平或重排。"""
        g = Geometry.from_wkt(
            f'POLYGON (({REAL_X} {REAL_Y}, {REAL_X + 30} {REAL_Y}, '
            f'{REAL_X + 30} {REAL_Y + 30}, {REAL_X} {REAL_Y + 30}, '
            f'{REAL_X} {REAL_Y}), '
            f'({REAL_X + 5} {REAL_Y + 5}, {REAL_X + 10} {REAL_Y + 5}, '
            f'{REAL_X + 10} {REAL_Y + 10}, {REAL_X + 5} {REAL_Y + 10}, '
            f'{REAL_X + 5} {REAL_Y + 5}))')
        out = g.to_crs(EPSG_WGS84, src=EPSG_GK39)
        self.assertEqual(g.part_count, out.part_count)
        self.assertEqual(len(out.xy_parts[0]), len(g.xy_parts[0]))
        self.assertEqual(len(out.xy_parts[1]), len(g.xy_parts[1]))
        self.assertEqual(out.shape_type, ShapeType.POLYGON)
        # 洞的面积远小于外环 —— 说明环没被弄混
        self.assertLess(out.area(), out.convex_hull().area())

    def test_caller_transformer_is_reused(self):
        """给了 transformer 就不该再去解析 CRS(批量路径的性能前提)。"""
        tr = cs.make_transformer(EPSG_GK39, EPSG_WGS84)
        g = Geometry.from_wkt(f'POINT({REAL_X} {REAL_Y})')
        a = cs.transform_geometry(g, EPSG_WGS84, transformer=tr)
        b = g.to_crs(EPSG_WGS84, src=EPSG_GK39)
        self.assertEqual(a.coordinates, b.coordinates)

    def test_missing_src_raises(self):
        """几何自己不知道自己是什么坐标系 —— 没给 src 必须报错,不能猜。"""
        g = Geometry.from_wkt(f'POINT({REAL_X} {REAL_Y})')
        with self.assertRaises(cs.CoordTransformError) as ctx:
            cs.transform_geometry(g, EPSG_WGS84)
        self.assertIn('src', str(ctx.exception))

    # ---- 量级体检 -------------------------------------------------------
    def test_warns_when_projected_coords_given_as_geographic(self):
        """投影坐标当经纬度喂进去 -> 告警。

        这条体检**必须由本模块自己做**:pyproj 的 ``errcheck=True`` 对
        域外点**不报错**,实测只会安静地返回一个有限但完全错误的数。
        """
        g = Geometry.from_wkt(f'POINT({REAL_X} {REAL_Y})')
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            cs.transform_geometry(g, EPSG_GK39, src=EPSG_WGS84)
        msgs = [str(w.message) for w in caught
                if issubclass(w.category, cs.CoordTransformWarning)]
        self.assertEqual(len(msgs), 1, f'期望 1 条告警,拿到 {len(caught)} 条')
        self.assertIn('地理坐标系', msgs[0])

    def test_warns_when_geographic_coords_given_as_projected(self):
        g = Geometry.from_wkt('POINT(115.9188 28.7258)')
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            cs.transform_geometry(g, EPSG_WGS84, src=EPSG_GK39)
        msgs = [str(w.message) for w in caught
                if issubclass(w.category, cs.CoordTransformWarning)]
        self.assertEqual(len(msgs), 1)
        self.assertIn('投影坐标系', msgs[0])

    def test_no_warning_on_a_correct_pair(self):
        g = Geometry.from_wkt(f'POINT({REAL_X} {REAL_Y})')
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            g.to_crs(EPSG_WGS84, src=EPSG_GK39)
        self.assertEqual([str(w.message) for w in caught
                          if issubclass(w.category, cs.CoordTransformWarning)], [])

    def test_check_false_silences_the_warning(self):
        g = Geometry.from_wkt(f'POINT({REAL_X} {REAL_Y})')
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            cs.transform_geometry(g, EPSG_GK39, src=EPSG_WGS84, check=False)
        self.assertEqual(caught, [])

    def test_warning_category_is_filterable(self):
        """告警必须是**专属类别**,好让调用方精确忽略它。"""
        self.assertTrue(issubclass(cs.CoordTransformWarning, UserWarning))
        g = Geometry.from_wkt(f'POINT({REAL_X} {REAL_Y})')
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            warnings.filterwarnings('ignore', category=cs.CoordTransformWarning)
            cs.transform_geometry(g, EPSG_GK39, src=EPSG_WGS84)
        self.assertEqual(caught, [])


# ===========================================================================
# 图层:建 + 写 + 读回(自给自足,不需要样例库)
# ===========================================================================
@requires_pyproj
class TestLayerTransformSelfContained(unittest.TestCase):
    """用临时库跑完整的"建目标图层 -> 写 -> 读回"一遍。

    刻意**不**依赖参照语料 —— 这条链路的正确性不该只有装了 4 GB 数据才验得了。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pyopenfilegdb_crs_')
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _build_source(self):
        path = os.path.join(self.tmp, 'src.gdb')
        gdb = OpenFileGDB.create(path)
        layer = gdb.create_layer(
            '源', geometry_type='polygon',
            spatial_ref={'wkt': _gk39_esri_wkt(), 'wkid': EPSG_GK39,
                         'latest_wkid': EPSG_GK39,
                         'x_origin': 33876800.0, 'y_origin': -10002100.0,
                         'xy_scale': 20000.0, 'xy_tolerance': 0.0001},
            fields=[GdbField('NAME', FGFT_STRING, length=32),
                    GdbField('VAL', FGFT_DOUBLE)])
        for i in range(1, 4):
            x, y = REAL_X + i * 100.0, REAL_Y + i * 50.0
            g = Geometry.from_wkt(
                f'POLYGON (({x} {y}, {x + 100} {y}, {x + 100} {y + 100}, '
                f'{x} {y + 100}, {x} {y}))')
            layer.write_feature({'NAME': f'F{i}', 'VAL': float(i), 'Shape': g})
        return gdb, layer

    def test_write_then_read_back_geographic(self):
        gdb, src = self._build_source()
        self.addCleanup(gdb.close)
        dst = cs.create_transformed_layer(src, gdb, EPSG_WGS84, name='目标')
        n = cs.write_transformed(src, dst, EPSG_WGS84, check=False)
        self.assertEqual(n, 3)
        gdb.close()          # 落盘;close() 幂等,addCleanup 里再调一次无害

        with OpenFileGDB.open(os.path.join(self.tmp, 'src.gdb')) as back:
            got = back.get_layer('目标')
            self.assertEqual(got.record_count, 3)
            self.assertEqual(got.spatial_ref.effective_wkid, EPSG_WGS84)
            # 量化参数走地理档
            gf = got.geometry_field
            self.assertEqual(gf.xy_scale, 1e6)

            tr = cs.make_transformer(EPSG_GK39, EPSG_WGS84)
            for oid in (1, 2, 3):
                want = cs.transform_geometry(src.read_feature(oid).geometry,
                                             EPSG_WGS84, transformer=tr)
                have = got.read_feature(oid).geometry
                for a, b in zip(want.envelope(), have.envelope()):
                    # 容差 = 一个量化步(2e-6 度)的两倍,留够余量
                    self.assertAlmostEqual(a, b, delta=4e-6)
                self.assertEqual(got.read_feature(oid).attributes['NAME'],
                                 f'F{oid}')

    def test_write_then_read_back_projected_keeps_wkid_and_quantization(self):
        """投影 -> 投影:WKID 要落盘,量化参数要按投影档推。"""
        gdb, src = self._build_source()
        self.addCleanup(gdb.close)
        dst = cs.create_transformed_layer(src, gdb, EPSG_GK39, name='目标')
        cs.write_transformed(src, dst, EPSG_GK39, check=False)
        gdb.close()          # 落盘;close() 幂等,addCleanup 里再调一次无害

        with OpenFileGDB.open(os.path.join(self.tmp, 'src.gdb')) as back:
            got = back.get_layer('目标')
            self.assertEqual(got.spatial_ref.effective_wkid, EPSG_GK39)
            gf = got.geometry_field
            self.assertEqual(gf.xy_scale, 20000.0)
            self.assertEqual(gf.xy_tolerance, 0.0001)
            # ⚠️ origin 必须在所有坐标之下,否则 write_varuint 会抛
            self.assertLess(gf.x_origin, gf.xmin + 1.0)
            self.assertLess(gf.y_origin, gf.ymin + 1.0)
            for oid in (1, 2, 3):
                a = src.read_feature(oid).geometry.envelope()
                b = got.read_feature(oid).geometry.envelope()
                for u, v in zip(a, b):
                    self.assertAlmostEqual(u, v, delta=1e-4)

    def test_target_layer_wkid_is_not_zero(self):
        """⚠️ 守一个真踩过的坑:``spatial_ref`` 传**字符串** WKT 会让
        ``core._quantization_parts`` 把 wkid/latest_wkid 一律写成 0
        (见 ``core.py`` 的 str 分支)。

        WKT 照样能读,但 WKID 是 ArcGIS/QGIS 认坐标系的快路径,丢了就得靠
        解析 WKT 去猜 —— 所以 ``create_transformed_layer`` 走 dict 而不是
        字符串。
        """
        gdb, src = self._build_source()
        self.addCleanup(gdb.close)
        dst = cs.create_transformed_layer(src, gdb, EPSG_WGS84, name='目标')
        self.assertEqual(dst.spatial_ref.wkid, EPSG_WGS84)
        self.assertEqual(dst.spatial_ref.latest_wkid, EPSG_WGS84)
        self.assertEqual(dst.spatial_ref.effective_wkid, EPSG_WGS84)

    def test_quantization_override_is_honoured(self):
        gdb, src = self._build_source()
        self.addCleanup(gdb.close)
        dst = cs.create_transformed_layer(
            src, gdb, EPSG_GK39, name='目标',
            quantization={'x_origin': 33876800.0, 'y_origin': -10002100.0,
                          'xy_scale': 20000.0, 'xy_tolerance': 0.0001})
        gf = dst.geometry_field
        self.assertEqual(gf.x_origin, 33876800.0)
        self.assertEqual(gf.xy_scale, 20000.0)

    def test_fields_are_copied_but_oid_is_left_to_create_layer(self):
        """属性字段照抄;OID 字段由 ``create_layer`` 生成,不能重复加。"""
        gdb, src = self._build_source()
        self.addCleanup(gdb.close)
        dst = cs.create_transformed_layer(src, gdb, EPSG_WGS84, name='目标')
        names = [f.name for f in dst.fields]
        self.assertEqual(names.count('OBJECTID'), 1,
                         f'OBJECTID 被加了两次: {names}')
        self.assertEqual([f.name for f in src.fields], names,
                         '属性字段与源图层不一致')
        # ⚠️ 几何字段**不在** ``fields`` 里 —— 它是另一个属性
        # (``geometry_field``);别指望在 fields 里找到 'Shape'。
        self.assertNotIn('Shape', names)
        self.assertEqual(dst.geometry_field_name, src.geometry_field_name)
        self.assertEqual(dst.geometry_type, src.geometry_type)

    def test_transform_layer_is_lazy_and_does_not_pollute_the_cache(self):
        """逐条转换:产出的是**新的**要素对象,源图层不受影响。

        ⚠️ **这条用例证明不了"实现没有就地改"。** ``transform_layer`` 内部
        走 ``read_features()``,每次解码出**新的**几何对象,所以它压根碰不到
        外面那个 ``feat`` —— 实现就算真是就地改的,这条也照样绿(实测过)。
        那是 ``test_returns_new_object_and_leaves_original_alone`` 该管的事。
        这里管的是另外两件:产出条数/过滤对不对、坐标确实换了坐标系。
        """
        gdb, src = self._build_source()
        self.addCleanup(gdb.close)

        feat = src.read_feature(1)                    # 攥住这一个对象
        before_env = feat.geometry.envelope()
        before_parts = feat.geometry.xy_parts

        out = list(cs.transform_layer(src, EPSG_WGS84, check=False))
        self.assertEqual(len(out), 3)
        self.assertEqual(feat.geometry.envelope(), before_env)
        self.assertEqual(feat.geometry.xy_parts, before_parts)
        # 产出的几何落在经纬度范围内(确实转了)
        self.assertLess(abs(out[0].geometry.envelope()[0]), 180.0)

    def test_passing_a_cached_feature_geometry_does_not_reproject_it(self):
        """最要命的那种用法:直接把 ``feat.geometry`` 交出去转。

        如果 ``transform_geometry`` 是就地改的,这一句之后 ``feat.geometry``
        就永久变成经纬度了 —— 而调用方什么异常都看不到。

        ⚠️ 比对的是 **``xy_parts``(裸存储)**,不是 ``envelope()`` ——
        ``envelope()`` 的结果缓存在 ``_env`` 上,换掉坐标存储之后它照旧返回
        旧值,拿它比对是抓不住"换存储"这类变异的(实测)。
        """
        gdb, src = self._build_source()
        self.addCleanup(gdb.close)
        feat = src.read_feature(2)
        g = feat.geometry
        before_parts = g.xy_parts
        before_env = g.envelope()
        out = cs.transform_geometry(g, EPSG_WGS84, src=EPSG_GK39, check=False)
        self.assertEqual(feat.geometry.xy_parts, before_parts,
                         '源要素缓存里的坐标存储被换掉了')
        self.assertEqual(feat.geometry.envelope(), before_env)
        self.assertLess(abs(out.envelope()[0]), 180.0)

    def test_transform_layer_passes_filters_through(self):
        gdb, src = self._build_source()
        self.addCleanup(gdb.close)
        got = list(cs.transform_layer(src, EPSG_WGS84, limit=2, check=False))
        self.assertEqual([f.oid for f in got], [1, 2])

    def test_write_transformed_reports_count(self):
        gdb, src = self._build_source()
        self.addCleanup(gdb.close)
        dst = cs.create_transformed_layer(src, gdb, EPSG_WGS84, name='目标')
        self.assertEqual(cs.write_transformed(src, dst, EPSG_WGS84,
                                              limit=2, check=False), 2)


# ===========================================================================
# 参照语料(找不到就 skip)
# ===========================================================================
@unittest.skipUnless(SAMPLES, '找不到样例 .gdb(设 PYOPENFILEGDB_TEST_GDB)')
@requires_pyproj
class TestTransformRealCorpus(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.gdb = None
        for path in SAMPLES:
            try:
                gdb = OpenFileGDB.open(path)
            except Exception:                                   # noqa: BLE001
                continue
            for name in gdb.list_feature_classes():
                try:
                    layer = gdb.get_layer(name)
                except Exception:                               # noqa: BLE001
                    continue
                sr = layer.spatial_ref
                if sr and sr.effective_wkid:
                    cls.gdb, cls.layer = gdb, layer
                    return
            gdb.close()
        raise unittest.SkipTest('样例库里没有带坐标系的图层')

    @classmethod
    def tearDownClass(cls):
        if cls.gdb is not None:
            cls.gdb.close()

    def test_round_trip_is_stable(self):
        """投影 -> 地理 -> 投影,应回到原值(容差按量化步给)。"""
        tr = cs.make_transformer(self.layer.spatial_ref, EPSG_WGS84)
        back = cs.make_transformer(EPSG_WGS84, self.layer.spatial_ref)
        for feat in self.layer.read_features(limit=5):
            with self.subTest(oid=feat.oid):
                g0 = feat.geometry
                g1 = cs.transform_geometry(g0, EPSG_WGS84, transformer=tr,
                                           check=False)
                g2 = cs.transform_geometry(g1, self.layer.spatial_ref,
                                           transformer=back, check=False)
                for a, b in zip(g0.envelope(), g2.envelope()):
                    self.assertAlmostEqual(a, b, delta=1e-3)

    def test_shape_is_preserved_across_the_transform(self):
        """形状(面积 / 凸包面积)应当近似不变。

        ⚠️ **不能直接比面积。** 投影坐标系里面积是平方米,换到地理坐标系
        就成了"平方度" —— 两者根本没有可比性(实测比值 1.08e10,那不是误差,
        是单位不同)。我一开始就是按"面积应当一样"写的,结果拿到 1.08e10。

        真正该守的不变量是**形状**:高斯-克吕格(横轴墨卡托)是**保角**投影,
        所以"自身面积 / 凸包面积"这个无量纲的形态描述子在一层之内基本不变。
        容差放到 1%,因为缩放因子在一条几何的两端也会有微小变化。
        """
        tr = cs.make_transformer(self.layer.spatial_ref, EPSG_WGS84)
        for feat in self.layer.read_features(limit=5):
            with self.subTest(oid=feat.oid):
                g0 = feat.geometry
                g1 = cs.transform_geometry(g0, EPSG_WGS84, transformer=tr,
                                           check=False)
                r0 = g0.area() / g0.convex_hull().area()
                r1 = g1.area() / g1.convex_hull().area()
                self.assertAlmostEqual(r0, r1, delta=0.01 * abs(r0) + 1e-12)

    def test_relative_areas_are_preserved(self):
        """两条要素的面积之比也该保住 —— 同一条保角映射下,缩放因子约掉。"""
        tr = cs.make_transformer(self.layer.spatial_ref, EPSG_WGS84)
        feats = list(self.layer.read_features(limit=2))
        if len(feats) < 2:
            self.skipTest('语料里不足两条要素')
        g0, g1 = (f.geometry for f in feats)
        h0 = cs.transform_geometry(g0, EPSG_WGS84, transformer=tr, check=False)
        h1 = cs.transform_geometry(g1, EPSG_WGS84, transformer=tr, check=False)
        self.assertAlmostEqual(g0.area() / g1.area(), h0.area() / h1.area(),
                               delta=0.01)

    def test_layer_extent_check_fires_only_when_wrong(self):
        """整层的量级体检:正确的一对静默,喂错的一对**只报一次**(不是每条一次)。"""
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            list(cs.transform_layer(self.layer, EPSG_WGS84, limit=20))
        self.assertEqual([str(w.message) for w in caught
                          if issubclass(w.category, cs.CoordTransformWarning)], [])

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            # 把源坐标系说成 WGS84 —— 于是图层的投影坐标就成了"离谱的经纬度"
            list(cs.transform_layer(self.layer, EPSG_WGS84, src=EPSG_WGS84,
                                    limit=20))
        msgs = [str(w.message) for w in caught
                if issubclass(w.category, cs.CoordTransformWarning)]
        self.assertEqual(len(msgs), 1,
                         f'应当整层只报一次,实得 {len(msgs)} 次')

    def test_transform_geometry_does_not_decode_whole_layer(self):
        """逐条转 5 条不该把整层都解码(只读属性的那条路要仍然是惰性的)。"""
        n = sum(1 for _ in cs.transform_layer(self.layer, EPSG_WGS84, limit=5))
        self.assertEqual(n, 5)


def _gk39_esri_wkt() -> str:
    """参照语料里那份 Esri WKT(不带 ``ID`` 节点)。

    刻意**从这里硬编码**而不是从样例库读 —— 这样没有语料的机器也能跑
    (去找 pyproj 要 ``to_wkt(version='WKT1_ESRI')`` 会带 ``AUTHORITY``,
    那就不叫"Esri 那一份"了,``to_epsg()`` 的行为也不同)。
    """
    return ('PROJCS["CGCS2000_3_Degree_GK_Zone_39",GEOGCS["GCS_China_Geodetic_'
            'Coordinate_System_2000",DATUM["D_China_2000",SPHEROID["CGCS2000",'
            '6378137.0,298.257222101]],PRIMEM["Greenwich",0.0],'
            'UNIT["Degree",0.0174532925199433]],PROJECTION["Gauss_Kruger"],'
            'PARAMETER["False_Easting",39500000.0],'
            'PARAMETER["False_Northing",0.0],'
            'PARAMETER["Central_Meridian",117.0],'
            'PARAMETER["Scale_Factor",1.0],'
            'PARAMETER["Latitude_Of_Origin",0.0],UNIT["Meter",1.0]]')


class _temp_layer_with_gk39:
    """上下文管理器:一个带 GK39 坐标系的临时库 + 图层(给 ``resolve_crs`` 用)。"""

    def __enter__(self):
        self.tmp = tempfile.mkdtemp(prefix='pyopenfilegdb_crs_')
        gdb = OpenFileGDB.create(os.path.join(self.tmp, 'a.gdb'))
        layer = gdb.create_layer(
            'L', geometry_type='point',
            spatial_ref={'wkt': _gk39_esri_wkt(), 'wkid': EPSG_GK39,
                         'latest_wkid': EPSG_GK39})
        self._gdb = gdb
        return gdb, layer

    def __exit__(self, *exc):
        self._gdb.close()
        shutil.rmtree(self.tmp, ignore_errors=True)
        return False


if __name__ == '__main__':                                      # pragma: no cover
    unittest.main()
