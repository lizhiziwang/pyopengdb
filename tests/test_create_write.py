"""Tier 2/3 写路径的回归测试:建空库、建要素类、写/改/删要素、删图层。

两类断言:

1. **结构正确性** —— 建出来的库必须自洽:头部对得上、catalog/items/
   relationships/spatial refs 四处登记齐全、重新打开能读回同样内容。
2. **与真实 ArcGIS 逐字节对比** —— 7 张系统表的字段描述区与记录体,
   在剥离 GDAL 特有的 ``DE AD BE EF`` 终止符之后,必须与
   ``D:/work/新建文件地理数据库.gdb`` 里的对应文件完全相同。这个对比是
   "我们真的复刻了 FileGDB 格式"的直接证据;样例库不在时自动跳过。

跑法::

    /d/zsh/app/py_3.13.1/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import math
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pyopenfilegdb import (                          # noqa: E402
    FGFT_DOUBLE,
    FGFT_INT32,
    FGFT_STRING,
    GdbFeature,
    GdbField,
    Geometry,
    GdbSpatialRef,
    GdbWriteError,
    OpenFileGDB,
)
from pyopenfilegdb import _constants as C            # noqa: E402
from pyopenfilegdb._esri_geometry import from_wkt    # noqa: E402
from pyopenfilegdb._esri_geometry import (           # noqa: E402
    _close_parts,
    _strip_part_closures,
    encode_geometry,
    read_varuint,
)
from pyopenfilegdb._gdbtable import GdbTable         # noqa: E402
from pyopenfilegdb._gdb_template import SYSTEM_TABLES  # noqa: E402
from pyopenfilegdb._util import get_uint32, get_uint64  # noqa: E402

#: 真实 ArcGIS 建的空库,用来做逐字节比对。
ARCGIS_TEMPLATE = os.environ.get(
    'PYOPENFILEGDB_TEST_TEMPLATE', 'D:/work/新建文件地理数据库.gdb')

#: GDAL 在字段描述区尾部写、ArcGIS 不写的 4 字节。
GDAL_TERMINATOR = b'\xDE\xAD\xBE\xEF'


class _TempGdbCase(unittest.TestCase):
    """每个测试用一个新的空目录,跑完删掉。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='pyopenfilegdb_test_')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def gdb_path(self, name='out.gdb'):
        return os.path.join(self.tmp, name)


# ======================================================================
# 建库
# ======================================================================
class TestCreateEmptyGdb(_TempGdbCase):

    def test_creates_directory_and_marker_files(self):
        path = self.gdb_path()
        gdb = OpenFileGDB.create(path)
        self.addCleanup(gdb.close)

        self.assertTrue(os.path.isdir(path))
        with open(os.path.join(path, 'gdb'), 'rb') as f:
            self.assertEqual(f.read(), b'\x05\x00\x00\x00\xDE\xAD\xBE\xEF')
        with open(os.path.join(path, 'timestamps'), 'rb') as f:
            self.assertEqual(f.read(), b'\xFF' * 400)

        # 7 张核心系统表,各自带 .gdbtablx
        for spec in SYSTEM_TABLES:
            self.assertTrue(
                os.path.isfile(os.path.join(path, spec['name'] + '.gdbtable')),
                spec['name'])
            self.assertTrue(
                os.path.isfile(os.path.join(path, spec['name'] + '.gdbtablx')),
                spec['name'])

        # 不写索引/自由空间文件(GDAL 的 Create() 也不写)
        leftovers = [n for n in os.listdir(path)
                     if n.endswith(('.gdbindexes', '.atx', '.spx', '.freelist'))]
        self.assertEqual(leftovers, [])

    def test_header_layout_has_no_creator_string(self):
        """ArcGIS 的 .gdbtable 从偏移 40 就是字段描述区,没有 creator 串。"""
        path = self.gdb_path()
        gdb = OpenFileGDB.create(path)
        self.addCleanup(gdb.close)
        for spec in SYSTEM_TABLES:
            full = os.path.join(path, spec['name'] + '.gdbtable')
            with self.subTest(table=spec['name']):
                with open(full, 'rb') as f:
                    head = f.read(40)
                self.assertEqual(get_uint64(head, 32),
                                 C.GDBTABLE_MAIN_HEADER_SIZE)
                self.assertEqual(get_uint32(head, 0), C.FGDB_VERSION_10)
                self.assertEqual(get_uint32(head, 12), 5)   # magic
                self.assertEqual(get_uint32(head, 16), 0)
                self.assertEqual(get_uint32(head, 20), 0)

    def test_no_gdbindexes_but_expected_file_set(self):
        path = self.gdb_path()
        gdb = OpenFileGDB.create(path)
        self.addCleanup(gdb.close)
        names = set(os.listdir(path))
        self.assertIn('gdb', names)
        self.assertIn('timestamps', names)
        self.assertEqual(
            len([n for n in names if n.endswith('.gdbtable')]),
            len(SYSTEM_TABLES))

    def test_refuses_bad_extension_and_existing_dir(self):
        with self.assertRaises(GdbWriteError):
            OpenFileGDB.create(os.path.join(self.tmp, 'no_extension'))
        path = self.gdb_path()
        OpenFileGDB.create(path).close()
        with self.assertRaises(GdbWriteError):
            OpenFileGDB.create(path)
        # overwrite=True 允许重建
        gdb = OpenFileGDB.create(path, overwrite=True)
        self.addCleanup(gdb.close)
        self.assertEqual(gdb.list_layers(), [])

    def test_system_table_contents_match_arcgis(self):
        """新建库的 7 张系统表与真实 ArcGIS 空库逐段一致。

        比较的是 :func:`_normalized` 拆出来的几段,刻意跳过的部分和理由都
        写在那里。最要紧的一条是 ``offset_field_desc``:两边都得是 40,这
        才说明我们的头部布局与 ArcGIS 一模一样 —— GDAL 会在这里写一个
        ``"GDAL <release>"`` 创建者字符串,把字段描述区推到 40 之后
        (``filegdbtable_write.cpp`` 里"写创建者不属于 spec,只是这台机器上
        的文件可能留有 ghost 区域"那段注释)。
        """
        if not os.path.isdir(ARCGIS_TEMPLATE):
            self.skipTest(f'没有 ArcGIS 样例库: {ARCGIS_TEMPLATE}')
        path = self.gdb_path()
        gdb = OpenFileGDB.create(path)
        self.addCleanup(gdb.close)

        for spec in SYSTEM_TABLES:
            name = spec['name']
            mine_path = os.path.join(path, name + '.gdbtable')
            ref_path = os.path.join(ARCGIS_TEMPLATE, name + '.gdbtable')
            with self.subTest(table=name):
                if not os.path.isfile(ref_path):
                    self.skipTest(f'模板缺少 {name}')
                mine = _normalized(mine_path)
                ref = _normalized(ref_path)
                self.assertEqual(mine['version'], ref['version'],
                                 f'{name}: 版本号不同')
                self.assertEqual(mine['magics'], ref['magics'],
                                 f'{name}: magic 段不同')
                self.assertEqual(mine['offset_field_desc'],
                                 ref['offset_field_desc'],
                                 f'{name}: 字段描述区偏移不同 —— '
                                 f'说明我们多写了创建者字符串')
                self.assertEqual(mine['field_desc'], ref['field_desc'],
                                 f'{name}: 字段描述区不同')
                self.assertTrue(
                    ref['records'].startswith(mine['records']),
                    f'{name}: 我们的记录区不是模板记录区的前缀')

    def test_version_and_lists_on_empty_gdb(self):
        gdb = OpenFileGDB.create(self.gdb_path())
        self.addCleanup(gdb.close)
        self.assertEqual(gdb.version, C.FGDB_VERSION_10)
        self.assertEqual(gdb.list_layers(), [])
        self.assertEqual(gdb.list_feature_classes(), [])
        self.assertEqual(gdb.list_tables(), [])
        self.assertEqual(len(gdb.items.feature_classes()), 0)
        # 模板里 GDB_SpatialRefs 带 2 行(GDAL 建的库会追加 1 行 WGS84)
        self.assertGreaterEqual(len(gdb.spatial_refs()), 1)


# ======================================================================
# 建图层 + 写要素
# ======================================================================
class TestCreateLayerAndWrite(_TempGdbCase):

    def setUp(self):
        super().setUp()
        self.gdb = OpenFileGDB.create(self.gdb_path())
        self.addCleanup(self.gdb.close)

    # ------------------------------------------------------------------
    def test_physical_name_and_registration(self):
        n_before = len(self.gdb.system_catalog.items())
        layer = self.gdb.create_layer(
            '点位', geometry_type='point',
            fields=[GdbField('NAME', FGFT_STRING, length=32)])

        # 物理编号 = 1 + 建库时的槽位数(catalog 里含 GDB_ReplicaLog 那一行)
        self.assertEqual(layer.physical_name,
                         'a%08x' % (n_before + 1))
        self.assertTrue(os.path.isfile(layer.path))
        self.assertEqual([f.name for f in layer.fields], ['OBJECTID', 'NAME'])
        self.assertEqual(layer.geometry_field_name, 'Shape')

        # catalog / items / relationships 三处都登记了
        names = [n for _i, n, _f in self.gdb.system_catalog.items()]
        self.assertIn('点位', names)
        item = self.gdb.items.by_name('点位')
        self.assertIsNotNone(item)
        self.assertEqual(item.item_type, 'Feature Class')
        self.assertEqual(item.path, '\\点位')
        # 真实 ArcGIS 文件里 PhysicalName 存的就是物理表名
        self.assertEqual(item.physical_name, layer.physical_name)

        rel = self.gdb._system_table('GDB_ItemRelationships')
        found = False
        for _row, values in rel.iter_rows():
            if (values.get('OriginID') == self.gdb.root_guid()
                    and values.get('DestID') == item.uuid):
                found = True
                self.assertTrue(values.get('Type'))
        self.assertTrue(found, 'GDB_ItemRelationships 里没有根->图层的记录')

        # Definition XML 里写对了名字、几何类型与字段
        xml = item.definition_xml
        self.assertIn('DEFeatureClassInfo', xml)
        self.assertIn('<Name>点位</Name>', xml)
        self.assertIn('esriGeometryPoint', xml)
        self.assertIn('<ShapeFieldName>Shape</ShapeFieldName>', xml)

    def test_duplicate_and_bad_names_are_refused(self):
        self.gdb.create_layer('A', geometry_type=None)
        with self.assertRaises(GdbWriteError):
            self.gdb.create_layer('A', geometry_type=None)
        with self.assertRaises(GdbWriteError):
            self.gdb.create_layer('B\\C', geometry_type=None)
        with self.assertRaises(GdbWriteError):
            self.gdb.create_layer('GDB_Items', geometry_type=None)
        with self.assertRaises(GdbWriteError):
            self.gdb.create_layer('D', geometry_type='hexagon')

    def test_duplicate_field_names_are_refused(self):
        with self.assertRaises(GdbWriteError):
            self.gdb.create_layer('A', geometry_type=None, fields=[
                GdbField('X', FGFT_INT32), GdbField('x', FGFT_STRING, 4)])

    # ------------------------------------------------------------------
    def test_write_point_features_and_read_back(self):
        layer = self.gdb.create_layer(
            '点位', geometry_type='point',
            fields=[GdbField('NAME', FGFT_STRING, 32),
                    GdbField('VALUE', FGFT_DOUBLE)])
        coords = [(116.3901, 39.9101), (116.3902, 39.9102), (116.3903, 39.9103)]
        for i, (x, y) in enumerate(coords):
            oid = layer.write_feature({
                'NAME': 'P%d' % i, 'VALUE': float(i) * 1.5,
                'Shape': Geometry.from_wkt('POINT (%r %r)' % (x, y)),
            })
            self.assertEqual(oid, i + 1)
        self.assertEqual(layer.record_count, 3)
        # 全表范围是 **原样的 double**(minX, minY, maxX, maxY),不走坐标
        # 量化 —— GDAL 的 EncodeEnvelope 也是直接写 double。所以这里跟
        # 写入值之间只该有 IEEE754 的舍入差,不该按量化格对齐。
        self.assertAlmostEqual(layer.extent[0], 116.3901, places=9)
        self.assertAlmostEqual(layer.extent[2], 116.3903, places=9)
        got = list(layer.read_features())
        self.assertEqual([f.oid for f in got], [1, 2, 3])
        self.assertEqual([f['NAME'] for f in got], ['P0', 'P1', 'P2'])
        for feat, (x, y) in zip(got, coords):
            px, py = feat.geometry.coordinates[:2]
            self.assertAlmostEqual(px, x, places=5)
            self.assertAlmostEqual(py, y, places=5)
        self.assertEqual((got[1].geometry.has_z, got[1].geometry.has_m),
                         (False, False))

    def test_write_polyline_polygon_multipoint(self):
        cases = [
            ('线', 'polyline', 'LINESTRING (0 0, 1 1, 2 0)', 'polyline'),
            ('线多段', 'polyline', 'MULTILINESTRING ((0 0,1 1),(5 5,6 6))',
             'polyline'),
            ('面', 'polygon', 'POLYGON ((0 0, 4 0, 4 4, 0 4))', 'polygon'),
            ('面带洞', 'polygon',
             'POLYGON ((0 0, 10 0, 10 10, 0 10), (2 2, 2 4, 4 4, 4 2))',
             'polygon'),
            ('多点', 'multipoint', 'MULTIPOINT ((1 1), (2 2), (3 3))',
             'multipoint'),
        ]
        for name, gtype, wkt, kind in cases:
            with self.subTest(layer=name):
                layer = self.gdb.create_layer(name, geometry_type=gtype)
                layer.write_feature({'Shape': Geometry.from_wkt(wkt)})
                feats = list(layer.read_features())
                self.assertEqual(len(feats), 1)
                self.assertEqual(feats[0].geometry.kind, kind)

    def test_polygon_ring_orientation_normalized(self):
        """Esri 要求外环顺时针、内环逆时针;方向不对时必须整环反转。"""
        layer = self.gdb.create_layer('面', geometry_type='polygon')
        # 逆时针外环 -> 写出来会被反转成顺时针
        layer.write_feature({
            'Shape': Geometry.from_wkt('POLYGON ((0 0, 4 0, 4 4, 0 4))')})
        feats = list(layer.read_features())
        parts, points = feats[0].geometry.coordinates
        self.assertEqual(len(parts), 1)
        ring = points[parts[0]:]
        self.assertTrue(_is_clockwise(ring), '外环应为顺时针')

    def test_write_geometry_none_and_null_attributes(self):
        layer = self.gdb.create_layer(
            '点位', geometry_type='point',
            fields=[GdbField('NAME', FGFT_STRING, 16)])
        layer.write_feature({'NAME': None, 'Shape': None})
        layer.write_feature({'NAME': '有几何',
                             'Shape': Geometry.from_wkt('POINT (1 2)')})
        got = list(layer.read_features())
        self.assertIsNone(got[0]['NAME'])
        self.assertIsNone(got[0].geometry)
        self.assertEqual(got[1]['NAME'], '有几何')
        self.assertIsNotNone(got[1].geometry)

    def test_write_empty_string_vs_null(self):
        layer = self.gdb.create_layer(
            'T', geometry_type=None, fields=[GdbField('S', FGFT_STRING, 16)])
        layer.write_feature({'S': ''})
        layer.write_feature({'S': None})
        got = list(layer.read_features())
        self.assertEqual(got[0]['S'], '')
        self.assertIsNone(got[1]['S'])

    def test_write_z_and_m(self):
        layer = self.gdb.create_layer(
            '带Z', geometry_type='point', has_z=True,
            fields=[GdbField('N', FGFT_STRING, 8)])
        layer.write_feature({'N': 'z1',
                             'Shape': Geometry.from_wkt('POINT Z (1 2 3)')})
        feat = next(iter(layer.read_features()))
        self.assertTrue(feat.geometry.has_z)
        self.assertEqual(feat.geometry.shape_type, C.ShapeType.POINTZ)
        self.assertEqual(tuple(feat.geometry.coordinates[:3]), (1.0, 2.0, 3.0))

        m_layer = self.gdb.create_layer(
            '带M', geometry_type='polyline', has_m=True)
        m_layer.write_feature({
            'Shape': Geometry.from_wkt('LINESTRING M (0 0 7, 1 1 8)')})
        feat = next(iter(m_layer.read_features()))
        self.assertTrue(feat.geometry.has_m)
        self.assertFalse(feat.geometry.has_z)
        self.assertEqual(feat.geometry.shape_type, C.ShapeType.POLYLINEM)

    def test_write_various_field_types(self):
        import datetime
        layer = self.gdb.create_layer('类型', geometry_type=None, fields=[
            GdbField('S', FGFT_STRING, 64),
            GdbField('I16', C.FGFT_INT16),
            GdbField('I32', FGFT_INT32),
            GdbField('F32', C.FGFT_FLOAT32),
            GdbField('F64', FGFT_DOUBLE),
            GdbField('DT', C.FGFT_DATETIME),
            GdbField('B', C.FGFT_BINARY),
            GdbField('G', C.FGFT_GUID),
        ])
        when = datetime.datetime(2024, 5, 6, 7, 8, 9, tzinfo=datetime.timezone.utc)
        layer.write_feature({
            'S': 'hello', 'I16': -5, 'I32': 123456, 'F32': 1.5,
            'F64': -2.25, 'DT': when,
            'B': b'\x00\x01\x02\xff',
            'G': '{11111111-2222-3333-4444-555555555555}',
        })
        feat = next(iter(layer.read_features()))
        self.assertEqual(feat['S'], 'hello')
        self.assertEqual(feat['I16'], -5)
        self.assertEqual(feat['I32'], 123456)
        self.assertAlmostEqual(feat['F32'], 1.5, places=6)
        self.assertEqual(feat['F64'], -2.25)
        self.assertEqual(feat['DT'].year, 2024)
        self.assertEqual(feat['DT'].hour, 7)
        self.assertEqual(bytes(feat['B']), b'\x00\x01\x02\xff')
        self.assertIn('5555', str(feat['G']))

    def test_unicode_layer_and_values(self):
        layer = self.gdb.create_layer(
            '图层😀', geometry_type='point',
            fields=[GdbField('名称', FGFT_STRING, 64)])
        layer.write_feature({'名称': '北京市·海淀区',
                             'Shape': Geometry.from_wkt('POINT (116.3 39.9)')})
        self.assertIn('图层😀', self.gdb.list_feature_classes())
        feat = next(iter(self.gdb.get_layer('图层😀').read_features()))
        self.assertEqual(feat['名称'], '北京市·海淀区')

    def test_many_features_survive_reopen(self):
        layer = self.gdb.create_layer(
            '批量', geometry_type='point',
            fields=[GdbField('I', FGFT_INT32)])
        for i in range(2500):
            layer.write_feature({'I': i,
                                 'Shape': Geometry.from_wkt(
                                     'POINT (%d %d)' % (i % 100, i // 100))})
        self.gdb.close()
        with OpenFileGDB.open(self.gdb_path()) as reopened:
            again = reopened.get_layer('批量')
            self.assertEqual(again.record_count, 2500)
            values = [f['I'] for f in again.read_features()]
            self.assertEqual(values, list(range(2500)))

    def test_geometry_family_mismatch_is_refused(self):
        layer = self.gdb.create_layer('面', geometry_type='polygon')
        with self.assertRaises(GdbWriteError):
            layer.write_feature({
                'Shape': Geometry.from_wkt('LINESTRING (0 0, 1 1)')})

    def test_point_promotion_to_multipoint_is_allowed(self):
        """多点图层接受单个 POINT —— 一个点就是"只有一个点的多点",放行。

        反过来(point 图层收 MULTIPOINT)必须拒绝:那会丢掉"这是个多点"
        这个事实,读回来会静默变形。见下一个测试。
        """
        layer = self.gdb.create_layer('点', geometry_type='multipoint')
        layer.write_feature({
            'Shape': Geometry.from_wkt('MULTIPOINT ((1 1), (2 2))')})
        layer.write_feature({'Shape': Geometry.from_wkt('POINT (5 6)')})
        self.assertEqual(layer.record_count, 2)
        kinds = [f.geometry.kind for f in layer.read_features()]
        self.assertEqual(kinds, ['multipoint', 'point'])
        self.assertEqual(layer.read_feature(2).geometry.coordinates[:2],
                         (5.0, 6.0))

    def test_multipoint_into_point_layer_is_refused(self):
        layer = self.gdb.create_layer('单点', geometry_type='point')
        with self.assertRaises(GdbWriteError):
            layer.write_feature({
                'Shape': Geometry.from_wkt('MULTIPOINT ((1 1), (2 2))')})

    def test_ring_closure_round_trip(self):
        """环的闭合点:内部表示不闭合,盘上闭合,两个方向都要能来回。

        ArcGIS 写的环是闭合的(实测 1746/1746 个环首尾点相同),本库内部
        表示不闭合(见 :class:`Geometry` 的 docstring),所以编解码各要
        做一次转换。
        """
        geom = Geometry.from_wkt('POLYGON ((0 0, 4 0, 4 4, 0 4))')
        parts, pts = geom.coordinates
        self.assertEqual(len(pts), 4, 'from_wkt 应削掉闭合点')

        closed_parts, closed_pts = _close_parts(parts, pts)
        self.assertEqual(closed_parts, [0])
        self.assertEqual(len(closed_pts), 5, '编码前应补上闭合点')
        self.assertEqual(closed_pts[0], closed_pts[-1])

        back_parts, back_pts = _strip_part_closures(closed_parts, closed_pts)
        self.assertEqual(back_parts, parts)
        self.assertEqual(back_pts, pts, '两个变换应互为逆运算')

    def test_polygon_is_encoded_with_closed_ring(self):
        """落到字节上看:盘上的 nPoints 得把闭合点算进去。"""
        blob = encode_geometry(
            Geometry.from_wkt('POLYGON ((0 0, 4 0, 4 4, 0 4))'))
        _shape_type, pos = read_varuint(blob, 0)
        n_points, _ = read_varuint(blob, pos)
        self.assertEqual(n_points, 5)

    def test_polyline_is_encoded_open(self):
        """折线不闭合:Esri 只要求多边形的环闭合。"""
        blob = encode_geometry(Geometry.from_wkt('LINESTRING (0 0, 1 1, 2 0)'))
        _shape_type, pos = read_varuint(blob, 0)
        n_points, _ = read_varuint(blob, pos)
        self.assertEqual(n_points, 3)

    def test_declared_dimensions_survive_encode(self):
        """声明了 M 但坐标没给 M 的点,编码后读回来仍是 M 点(值为 NaN)。"""
        layer = self.gdb.create_layer(
            'M层', geometry_type='point', has_m=True,
            fields=[GdbField('NAME', FGFT_STRING, 16)])
        layer.write_feature({
            'Shape': from_wkt('POINT (3 4)', has_m=True), 'NAME': 'x'})
        back = layer.read_feature(1)
        self.assertTrue(back.geometry.has_m)
        self.assertEqual(back.geometry.shape_type, C.ShapeType.POINTM)
        self.assertEqual(back.geometry.coordinates[:2], (3.0, 4.0))
        self.assertTrue(math.isnan(back.geometry.coordinates[2]))

    # ------------------------------------------------------------------
    def test_update_and_delete_features(self):
        layer = self.gdb.create_layer(
            'U', geometry_type='point',
            fields=[GdbField('NAME', FGFT_STRING, 64),
                    GdbField('V', FGFT_DOUBLE)])
        for i in range(6):
            layer.write_feature({
                'NAME': 'n%d' % i, 'V': float(i),
                'Shape': Geometry.from_wkt('POINT (%d %d)' % (i, i))})

        # 原地改(长度不变)
        feat = layer.read_feature(2)
        feat['V'] = 99.5
        layer.update_feature(feat)
        self.assertEqual(layer.read_feature(2)['V'], 99.5)
        self.assertEqual(layer.read_feature(2).geometry.coordinates[:2],
                         (1.0, 1.0))

        # 变长改(字符串变长 -> 追加到文件末尾,旧槽标脏)
        feat = layer.read_feature(3)
        feat['NAME'] = 'x' * 60
        layer.update_feature(feat)
        self.assertEqual(layer.read_feature(3)['NAME'], 'x' * 60)
        self.assertEqual([f.oid for f in layer.read_features()],
                         [1, 2, 3, 4, 5, 6])

        # 改几何
        feat = layer.read_feature(4)
        feat['Shape'] = Geometry.from_wkt('POINT (50 60)')
        layer.update_feature(feat)
        self.assertEqual(layer.read_feature(4).geometry.coordinates[:2],
                         (50.0, 60.0))

        # 删
        layer.delete_feature(5)
        self.assertEqual([f.oid for f in layer.read_features()],
                         [1, 2, 3, 4, 6])
        self.assertEqual(layer.record_count, 5)
        self.assertIsNone(layer.read_feature(5))
        # 删不存在的 OID 是静默无操作
        layer.delete_feature(999)
        self.assertIsNone(layer.read_feature(5))
        self.assertEqual(layer.table.valid_record_count, 5)

    def test_update_without_oid_is_refused(self):
        layer = self.gdb.create_layer('U', geometry_type=None)
        with self.assertRaises(GdbWriteError):
            layer.update_feature({'A': 1})

    def test_updates_survive_reopen(self):
        layer = self.gdb.create_layer(
            'U', geometry_type=None, fields=[GdbField('V', FGFT_INT32)])
        for i in range(4):
            layer.write_feature({'V': i})
        feat = layer.read_feature(1)
        feat['V'] = 42
        layer.update_feature(feat)
        layer.delete_feature(3)
        self.gdb.close()
        with OpenFileGDB.open(self.gdb_path()) as reopened:
            again = reopened.get_layer('U')
            self.assertEqual([(f.oid, f['V']) for f in again.read_features()],
                             [(1, 42), (2, 1), (4, 3)])

    # ------------------------------------------------------------------
    def test_delete_layer_removes_rows_and_files(self):
        keep = self.gdb.create_layer('保留', geometry_type=None)
        drop = self.gdb.create_layer('删除', geometry_type='point')
        drop.write_feature({'Shape': Geometry.from_wkt('POINT (1 1)')})
        drop.write_feature({'Shape': Geometry.from_wkt('POINT (2 2)')})
        physical = drop.physical_name
        guid = drop.item.uuid

        self.gdb.delete_layer('删除')
        self.assertEqual(self.gdb.list_layers(), ['保留'])
        self.assertIsNotNone(self.gdb.get_layer('保留'))
        # 该表的所有文件都不在了
        self.assertEqual(
            [n for n in os.listdir(self.gdb.path)
             if n.lower().startswith(physical)], [])
        # 登记项也清了
        catalog_names = [n for _i, n, _f in self.gdb.system_catalog.items()]
        self.assertNotIn('删除', catalog_names)
        self.assertIsNone(self.gdb.items.by_name('删除'))
        self.assertIsNone(self.gdb.items.by_uuid(guid))
        for _row, values in self.gdb._system_table(
                'GDB_ItemRelationships').iter_rows():
            self.assertNotEqual(values.get('DestID'), guid)
        # 重开后依然看不到
        self.gdb.close()
        with OpenFileGDB.open(self.gdb_path()) as reopened:
            self.assertEqual(reopened.list_layers(), ['保留'])

    def test_new_layer_after_delete_does_not_reuse_physical_name(self):
        a = self.gdb.create_layer('A', geometry_type=None)
        self.gdb.delete_layer('A')
        b = self.gdb.create_layer('B', geometry_type=None)
        self.assertNotEqual(a.physical_name, b.physical_name)
        self.assertTrue(os.path.isfile(b.path))
        # 编号只会往后走(不回收槽位,见 delete_layer 的说明)
        self.assertGreater(int(b.physical_name[1:], 16),
                           int(a.physical_name[1:], 16))

    # ------------------------------------------------------------------
    def test_readonly_datasource_refuses_writes(self):
        layer = self.gdb.create_layer('R', geometry_type=None)
        self.gdb.close()
        with OpenFileGDB.open(self.gdb_path()) as ro:
            self.assertFalse(ro.update)
            got = ro.get_layer('R')
            with self.assertRaises(GdbWriteError):
                got.write_feature({})
            with self.assertRaises(GdbWriteError):
                got.delete_feature(1)
            with self.assertRaises(GdbWriteError):
                ro.create_layer('new', geometry_type=None)
            with self.assertRaises(GdbWriteError):
                ro.delete_layer('R')
        del layer

    def test_write_after_close_is_refused(self):
        layer = self.gdb.create_layer('C', geometry_type=None)
        self.gdb.close()
        with self.assertRaises(Exception):
            layer.write_feature({})

    def test_context_manager_closes(self):
        path = self.gdb_path('ctx.gdb')
        with OpenFileGDB.create(path) as gdb:
            gdb.create_layer('X', geometry_type=None)
        self.assertTrue(gdb.closed)
        with OpenFileGDB.open(path) as gdb2:
            self.assertTrue(gdb2.list_layers())

    def test_spatial_ref_registered_once(self):
        """同一套 WKT+量化参数只登记一行(对应 GDAL GetExistingSpatialRef)。"""
        before = len(self.gdb.spatial_refs())
        for i in range(3):
            self.gdb.create_layer('S%d' % i, geometry_type='point')
        self.assertEqual(len(self.gdb.spatial_refs()), before)

    def test_custom_spatial_ref_is_registered(self):
        wkt = ('PROJCS["CGCS2000_3_Degree_GK_CM_114E",GEOGCS["GCS_China_'
               'Geodetic_Coordinate_System_2000",DATUM["D_China_2000",'
               'SPHEROID["CGCS2000",6378137.0,298.257222101]],'
               'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]],'
               'PROJECTION["Gauss_Kruger"],PARAMETER["False_Easting",500000.0],'
               'PARAMETER["False_Northing",0.0],'
               'PARAMETER["Central_Meridian",114.0],'
               'PARAMETER["Scale_Factor",1.0],PARAMETER["Latitude_Of_Origin",0.0],'
               'UNIT["Meter",1.0]]')
        before = len(self.gdb.spatial_refs())
        layer = self.gdb.create_layer(
            '投影', geometry_type='point',
            spatial_ref={'wkt': wkt, 'wkid': 4547, 'latest_wkid': 4547,
                         'x_origin': -5120900.0, 'y_origin': 0.0,
                         'xy_scale': 10000.0, 'xy_tolerance': 0.0001})
        self.assertEqual(len(self.gdb.spatial_refs()), before + 1)
        self.assertEqual(layer.spatial_ref.effective_wkid, 4547)
        self.assertAlmostEqual(layer.geometry_field.xy_scale, 10000.0)
        self.assertIn('4547', layer.item.definition_xml)
        # 量化参数必须与字段描述区一致,否则坐标会整体错位
        self.assertAlmostEqual(layer.geometry_field.x_origin, -5120900.0)

    def test_sync_is_idempotent(self):
        layer = self.gdb.create_layer('Y', geometry_type='point')
        layer.write_feature({'Shape': Geometry.from_wkt('POINT (1 2)')})
        size = os.path.getsize(layer.path)
        for _ in range(3):
            layer.sync()
        self.assertEqual(os.path.getsize(layer.path), size)

    def test_generated_uuids_are_unique_and_braced(self):
        seen = set()
        for i in range(5):
            layer = self.gdb.create_layer('G%d' % i, geometry_type=None)
            guid = layer.item.uuid
            self.assertTrue(guid.startswith('{') and guid.endswith('}'))
            self.assertNotIn(guid, seen)
            seen.add(guid)
            self.assertEqual(len(guid), 38)


# ======================================================================
# WHERE 子句(写路径上自己造数据,不依赖真实库)
# ======================================================================
class TestWhereClause(_TempGdbCase):

    def setUp(self):
        super().setUp()
        self.gdb = OpenFileGDB.create(self.gdb_path())
        self.addCleanup(self.gdb.close)
        self.layer = self.gdb.create_layer(
            'W', geometry_type='point',
            fields=[GdbField('NAME', FGFT_STRING, 32),
                    GdbField('N', FGFT_INT32),
                    GdbField('V', FGFT_DOUBLE)])
        rows = [('a', 1, 1.5), ('b', 2, 2.5), ('c', None, 3.5),
                ('ab', 4, None), ('Abc', 5, 5.5)]
        for i, (name, n, v) in enumerate(rows):
            self.layer.write_feature(
                {'NAME': name, 'N': n, 'V': v,
                 'Shape': Geometry.from_wkt('POINT (%d 0)' % i)})

    def _oids(self, where):
        return [f.oid for f in self.layer.read_features(where=where)]

    def test_is_null_and_not_null(self):
        self.assertEqual(self._oids('N IS NULL'), [3])
        self.assertEqual(self._oids('N IS NOT NULL'), [1, 2, 4, 5])
        self.assertEqual(self._oids('V IS NULL'), [4])

    def test_equality_against_null_literal_is_false(self):
        """SQL 三值逻辑:`N = NULL` 不匹配任何行,判空要用 IS NULL。"""
        self.assertEqual(self._oids('N = NULL'), [])
        self.assertEqual(self._oids('N <> NULL'), [])

    def test_comparison_and_boolean_ops(self):
        self.assertEqual(self._oids('N > 2'), [4, 5])
        self.assertEqual(self._oids('N >= 2 AND N <= 4'), [2, 4])
        self.assertEqual(self._oids('N < 2 OR N > 4'), [1, 5])
        self.assertEqual(self._oids('NOT (N > 2)'), [1, 2])
        self.assertEqual(self._oids('(N > 2) AND (NAME <> \'ab\')'), [5])

    def test_string_and_like(self):
        self.assertEqual(self._oids("NAME = 'a'"), [1])
        self.assertEqual(sorted(self._oids("NAME LIKE 'a%'")), [1, 4])
        # '_b%' = 第 2 个字符必须是 'b' 且后面随便:'b' 自己只有 1 个字符,
        # 匹配不上;'ab' 与 'Abc' 都命中(大小写敏感)。
        self.assertEqual(sorted(self._oids("NAME LIKE '_b%'")), [4, 5])
        self.assertEqual(self._oids("NAME IN ('a', 'b')"), [1, 2])
        # 第 3 行的 NAME 是 'c'(空的那个是 N),所以它属于 NOT IN 的结果
        self.assertEqual(self._oids("NAME NOT IN ('a', 'b')"), [3, 4, 5])
        # 非字符串列做 LIKE:不可比 -> UNKNOWN,不匹配任何行
        self.assertEqual(self._oids('N LIKE \'1%\''), [])

    def test_three_valued_logic(self):
        """NULL 参与布尔运算时按 SQL 真值表走,UNKNOWN 最终不算命中。

        ``N`` 为空的是第 3 行。``NOT (N > 2)`` 在二值逻辑下会把它选出来
        (因为 ``None > 2`` 为假,取反就成了真);SQL 里它应该是 UNKNOWN,
        所以结果里只有 1、2。
        """
        self.assertEqual(self._oids('NOT (N > 2)'), [1, 2])
        self.assertEqual(self._oids('NOT (N > 2 OR NAME = \'a\')'), [2])
        self.assertEqual(self._oids('NOT (N IS NULL)'), [1, 2, 4, 5])
        # UNKNOWN AND FALSE = FALSE,所以这一条能确定地排除第 3 行
        self.assertEqual(self._oids('N > 2 AND N IS NULL'), [])
        # UNKNOWN OR TRUE = TRUE
        self.assertEqual(sorted(self._oids('N > 2 OR NAME = \'c\'')), [3, 4, 5])

    def test_in_with_null_operand(self):
        """``NULL IN (...)`` 是 UNKNOWN;列表里混了 NULL 也只可能给 UNKNOWN。"""
        self.assertEqual(self._oids("N IN (1, 2)"), [1, 2])
        self.assertEqual(self._oids("N IN (1, NULL)"), [1])
        self.assertEqual(self._oids("N NOT IN (1, NULL)"), [])

    def test_type_mismatch_yields_empty(self):
        """字符串列与数字比大小:Python 语义是 TypeError,这里不匹配任何行。"""
        self.assertEqual(self._oids('NAME > 1'), [])

    def test_combined_with_bbox_and_limit(self):
        got = list(self.layer.read_features(
            where='N IS NOT NULL', bbox=(-0.5, -0.5, 2.5, 0.5), limit=2))
        self.assertEqual([f.oid for f in got], [1, 2])


# ======================================================================
# 与真实库的交叉验证:把真实库的要素"抄"进新库再读回来
# ======================================================================
@unittest.skipUnless(os.path.isdir(ARCGIS_TEMPLATE),
                     f'没有 ArcGIS 样例库: {ARCGIS_TEMPLATE}')
class TestCopyFromRealGdb(_TempGdbCase):
    """从真实 ArcGIS 库读要素、写进新建的库,再比对内容。

    这是端到端的"读满/写满"验证:几何、空值、字符串、数值都要一字节不差地
    走一遍量化/反量化回路。
    """

    def test_copy_polygon_layer(self):
        src = OpenFileGDB.open(ARCGIS_TEMPLATE)
        self.addCleanup(src.close)
        names = src.list_feature_classes()
        if not names:
            self.skipTest('样例库里没有要素类')
        source = src.get_layer(names[0])

        dst = OpenFileGDB.create(self.gdb_path('copy.gdb'))
        self.addCleanup(dst.close)
        fields = [GdbField(f.name, f.field_type, f.length,
                           nullable=f.nullable, alias=f.alias)
                  for f in source.attribute_fields]
        target = dst.create_layer(source.name[:60],
                                  geometry_type=source.geometry_type,
                                  spatial_ref={'wkt': source.spatial_ref.wkt,
                                               'wkid': source.spatial_ref.wkid},
                                  fields=fields)

        copied = 0
        for feat in source.read_features(limit=25):
            attrs = dict(feat.attributes)
            attrs['Shape'] = feat.geometry
            target.write_feature(attrs)
            copied += 1
        self.assertGreater(copied, 0)

        back = {f.oid: f for f in target.read_features()}
        self.assertEqual(len(back), copied)
        for oid, feat in back.items():
            with self.subTest(oid=oid):
                self.assertIsNotNone(feat.geometry)
        # 重新打开仍然一致
        dst.close()
        with OpenFileGDB.open(self.gdb_path('copy.gdb')) as again:
            self.assertEqual(again.get_layer(target.name).record_count, copied)


# ======================================================================
# WKT 解析
# ======================================================================
class TestFromWkt(unittest.TestCase):

    def test_supported_types(self):
        cases = {
            'POINT (1 2)': 'point',
            'POINT(1 2)': 'point',
            'MULTIPOINT ((1 1), (2 2))': 'multipoint',
            'MULTIPOINT (1 1, 2 2)': 'multipoint',
            'LINESTRING (0 0, 1 1)': 'polyline',
            'MULTILINESTRING ((0 0,1 1),(2 2,3 3))': 'polyline',
            'POLYGON ((0 0, 1 0, 1 1))': 'polygon',
            'MULTIPOLYGON (((0 0,1 0,1 1)))': 'polygon',
        }
        for wkt, kind in cases.items():
            with self.subTest(wkt=wkt):
                self.assertEqual(from_wkt(wkt).kind, kind)

    def test_dimensions(self):
        self.assertEqual(from_wkt('POINT Z (1 2 3)').shape_type,
                         C.ShapeType.POINTZ)
        self.assertEqual(from_wkt('POINT M (1 2 3)').shape_type,
                         C.ShapeType.POINTM)
        self.assertEqual(from_wkt('POINT ZM (1 2 3 4)').shape_type,
                         C.ShapeType.POINTZM)
        self.assertEqual(from_wkt('POINT (1 2 3)').shape_type,
                         C.ShapeType.POINTZ)
        # 显式声明了维数就算数,坐标没给够的分量补 NaN(Esri 的"该维为空")
        geom = from_wkt('POINT (1 2)', has_m=True)
        self.assertEqual(geom.shape_type, C.ShapeType.POINTM)
        self.assertEqual(geom.coordinates[:2], (1.0, 2.0))
        self.assertTrue(math.isnan(geom.coordinates[2]))

    def test_empty_and_srid_prefix(self):
        self.assertTrue(from_wkt('POINT EMPTY').is_empty)
        self.assertEqual(from_wkt('SRID=4326;POINT (1 2)').coordinates, (1.0, 2.0))

    def test_ring_closure_is_stripped(self):
        geom = from_wkt('POLYGON ((0 0, 4 0, 4 4, 0 4, 0 0))')
        parts, points = geom.coordinates
        self.assertEqual(parts, [0])
        self.assertEqual(len(points), 4, '闭合点应被去掉')

    def test_bad_wkt_raises(self):
        for bad in ('', 'CIRCLE (1 2)', 'POINT (1)', 'POLYGON ((0 0, 1 1))'):
            with self.subTest(wkt=bad):
                with self.assertRaises(GdbWriteError):
                    from_wkt(bad)


# ======================================================================
# 辅助
# ======================================================================
def _normalized(path):
    """把 ``.gdbtable`` 归一成可逐字节比较的几段,返回 ``dict``。

    * ``version``            —— 头部 ``+0`` 的格式版本号
    * ``magics``             —— 头部 ``+12`` 起的 12 字节,即 ``5 / 0 / 0``
                                三个 magic
    * ``offset_field_desc``  —— 头部 ``+32`` 的字段描述区偏移。两边都是 40
                                就证明我们 **没有** 写创建者字符串,布局与
                                ArcGIS 同款
    * ``field_desc``         —— 字段描述区正文,末尾 GDAL 特有的
                                ``DE AD BE EF`` 已剥掉
    * ``records``            —— 记录区

    头部 ``+4`` 的有效记录数、``+8`` 的最大缓冲长度、``+24`` 的文件总长
    **不参与比较**:前两者随库里登记的行数变(模板是从一个已有更多行的
    库里抽出来的),``+8`` 还额外差 4 —— 正是那个 ``DE AD BE EF`` 被算进了
    字段描述区长度。记录区也只做 **前缀** 比较,理由同上。
    """
    with open(path, 'rb') as f:
        raw = f.read()
    offset_field_desc = get_uint64(raw, 32)
    field_desc_length = get_uint32(raw, offset_field_desc)
    records_start = offset_field_desc + 4 + field_desc_length
    body = raw[offset_field_desc + 4: records_start]
    if body.endswith(GDAL_TERMINATOR):
        body = body[:-4]
    return {
        'version': raw[0:4],
        'magics': raw[12:24],
        'offset_field_desc': raw[32:40],
        'field_desc': body,
        'records': raw[records_start:],
    }


def _q(value, layer):
    """把坐标系里的浮点数按该图层的量化格对齐(默认 1e-6)。"""
    origin = layer.geometry_field.x_origin
    scale = layer.geometry_field.xy_scale
    return round((value - origin) * scale) / scale + origin


def _is_clockwise(ring):
    """鞋带公式判环方向(与 :mod:`._esri_geometry` 内部口径一致)。"""
    area = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i][0], ring[i][1]
        x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
        area += x1 * y2 - x2 * y1
    return area < 0


if __name__ == '__main__':
    unittest.main(verbosity=2)
