"""Tier 1 读路径的回归测试:``.gdbtable`` / ``.gdbtablx`` 解析、Esri 几何解码、
系统表解析,以及 ``OpenFileGDB.get_layer().read_features()`` 整条链。

跑法::

    /d/zsh/app/py_3.13.1/python -m unittest discover -s tests -v

样例库的找法:先看环境变量 ``PYOPENFILEGDB_TEST_GDB``(多个目录用
``os.pathsep`` 分隔),否则扫 ``D:/work/*.gdb``。一个都找不到时整组测试
skip(不会失败)—— 本仓库不附带样例数据。
"""
from __future__ import annotations

import glob
import os
import sys
import unittest
from array import array

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pyopenfilegdb import _accel                      # noqa: E402
from pyopenfilegdb import _constants as C            # noqa: E402
from pyopenfilegdb import _esri_geometry as _EG      # noqa: E402
from pyopenfilegdb import OpenFileGDB                # noqa: E402
from pyopenfilegdb._datatypes import (               # noqa: E402
    GdbError,
    GdbFormatError,
    GdbNotFoundError,
)
from pyopenfilegdb.geometry import Geometry          # noqa: E402
from pyopenfilegdb._gdbtable import GdbTable         # noqa: E402
from pyopenfilegdb._system_catalog import (          # noqa: E402
    GdbItems,
    GdbSystemCatalog,
    find_system_tables,
    parse_definition_xml,
)
from pyopenfilegdb._util import get_uint32, get_uint64  # noqa: E402

#: 单个图层全量遍历的记录数上限;超过就只读前 N 条(测试要跑得快)。
FULL_SCAN_LIMIT = 5000
#: 超过这个记录数就只抽查前 LIMIT_PROBE 条。
LIMIT_PROBE = 200


def find_samples():
    """返回可用的样例 ``.gdb`` 目录列表。"""
    env = os.environ.get('PYOPENFILEGDB_TEST_GDB', '')
    if env:
        found = [p for p in env.split(os.pathsep) if p and os.path.isdir(p)]
        if found:
            return found
    return sorted(p for p in glob.glob('D:/work/*.gdb') if os.path.isdir(p))


SAMPLES = find_samples()


@unittest.skipUnless(SAMPLES, '找不到样例 .gdb(设 PYOPENFILEGDB_TEST_GDB)')
class TestReadRealGdb(unittest.TestCase):
    """对真实 ArcGIS 库做只读校验。"""

    @classmethod
    def setUpClass(cls):
        cls.samples = SAMPLES
        cls.opened = {}
        for path in cls.samples:
            try:
                cls.opened[path] = OpenFileGDB.open(path)
            except GdbError:
                # 个别库损坏/版本不支持:不让它拖垮整组测试,但必须至少有一个能开
                continue
        if not cls.opened:
            raise unittest.SkipTest('样例库全都打不开')

    @classmethod
    def tearDownClass(cls):
        for gdb in cls.opened.values():
            gdb.close()

    # ------------------------------------------------------------------
    # 目录 / 版本
    # ------------------------------------------------------------------
    def test_version_is_supported(self):
        for path, gdb in self.opened.items():
            with self.subTest(gdb=os.path.basename(path)):
                self.assertIn(gdb.version, C.SUPPORTED_VERSIONS)

    def test_open_missing_directory_raises(self):
        with self.assertRaises(GdbNotFoundError):
            OpenFileGDB.open('Z:/definitely/not/here.gdb')

    def test_open_missing_catalog_raises(self):
        # 空目录 = 没有 a00000001.gdbtable
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(GdbNotFoundError):
                OpenFileGDB.open(tmp)

    # ------------------------------------------------------------------
    # .gdbtable / .gdbtablx 头部自洽
    # ------------------------------------------------------------------
    def test_gdbtable_header_self_consistent(self):
        """头部 + 字段描述区 + 每条记录的长度必须自洽。

        对应 GDAL ``FileGDBTable::Open`` 读出来的那几项,以及
        ``GetOffsetOfFeature`` 依赖的偏移有效性。

        ⚠️ 有三条"看起来天经地义"的假设,在真实 ArcGIS 库上 **都是错的**:

        1. **记录区不是连续的。** 字段描述区与第一条记录之间可能有一大段
           "幽灵区"(``新建文件地理数据库.gdb/a0000000a``:描述区在 2220
           结束,第一条记录在 2925,中间 705 字节是旧残骸);记录之间也有
           空洞(3253 之后空到 5126)。从记录区起点顺序遍历迟早会把残骸
           当成长度读,得到 6 亿这种数。GDAL 从不顺序扫描,一律走
           ``.gdbtablx``;本库同理。
        2. **记录不一定排在字段描述区之后。** 上栗县烟花那个库的
           ``a00000009`` 里,字段描述区被放到了文件末尾(偏移 81704),
           159 条记录全在它前面,最早的一条就在偏移 40。所以
           ``offset_field_desc`` 只是"描述区在哪",不是"记录从哪开始"。
        3. **tablx 里的偏移不递增。** 删掉记录后腾出的空间会被后面新建的
           记录复用(reuse 靠 ``.freelist`` 记账),所以"行号大的记录"完全
           可能在文件里更靠前。上面那张表第一个槽位的偏移是 74018,第二
           个却是 63077。

        还有一处:已删除的槽位写的是 **负长度**(``0xFFFFFFF8`` = -8),
        不是"最高位置 1 + 真实长度";用无符号读会得到 2147483640。
        """
        for path, gdb in self.opened.items():
            for physical in sorted(gdb.system_table_names()):
                full = os.path.join(gdb.path, physical + '.gdbtable')
                table = GdbTable.from_file(full)
                with self.subTest(gdb=os.path.basename(path), table=physical):
                    self.assertGreaterEqual(table.offset_field_desc,
                                            C.GDBTABLE_MAIN_HEADER_SIZE)
                    self.assertGreater(table.field_desc_length, 0)
                    # 字段描述区整个落在文件里,且能解析出至少一个字段
                    self.assertLessEqual(
                        table.records_start, table.file_size,
                        f'{physical}: 字段描述区越过文件末尾')
                    self.assertTrue(table.fields,
                                    f'{physical}: 字段描述区解析出 0 个字段')
                    # ...但不能要求一定有 OBJECTID:``GDB_DBTune`` 就没有
                    # (只有 Keyword / ParameterName / ConfigString 三个字符串
                    # 字段),它的行号同样是隐含的 FID。

                    with open(full, 'rb') as f:
                        raw = f.read()
                    self.assertEqual(table.file_size, len(raw),
                                     f'{physical}: 头部文件长度与实际不符')

                    if table.tablx is None:
                        continue
                    used = [o for o in
                            (table.tablx.offset_for_row(r)
                             for r in range(table.tablx.record_count)) if o]
                    self.assertEqual(len(used), len(set(used)),
                                     f'{physical}: 两个槽位指向同一个偏移')
                    for offset in used:
                        self.assertLessEqual(
                            offset + 4, len(raw),
                            f'{physical}: 偏移 {offset} 越过文件末尾')
                        length = get_uint32(raw, offset)
                        self.assertLess(length, 0x80000000,
                                        f'{physical}: 偏移 {offset} 的槽位'
                                        f'仍标着"已删除"(长度 {length})')
                        self.assertLessEqual(
                            offset + 4 + length, len(raw),
                            f'{physical}: 偏移 {offset} 处记录长度 {length} '
                            f'越过文件末尾')

    def test_tablx_slot_count_matches_table(self):
        """``.gdbtablx`` 的槽位数 == ``.gdbtable`` 头部的有效记录数上限。"""
        for path, gdb in self.opened.items():
            for name in gdb.list_layers():
                layer = gdb.get_layer(name)
                table = layer.table
                if table.tablx is None:
                    continue
                with self.subTest(gdb=os.path.basename(path), layer=name):
                    self.assertGreaterEqual(table.tablx.record_count,
                                            table.valid_record_count)
                    occupied = sum(
                        1 for row in range(table.tablx.record_count)
                        if table.tablx.offset_for_row(row))
                    self.assertEqual(occupied, table.valid_record_count)

    # ------------------------------------------------------------------
    # 图层清单
    # ------------------------------------------------------------------
    def test_layer_lists_are_consistent(self):
        for path, gdb in self.opened.items():
            with self.subTest(gdb=os.path.basename(path)):
                layers = gdb.list_layers()
                fcs = gdb.list_feature_classes()
                tables = gdb.list_tables()
                self.assertEqual(sorted(layers), sorted(fcs + tables))
                self.assertEqual(len(set(layers)), len(layers))
                physicals = set()
                for name in layers:
                    layer = gdb.get_layer(name)
                    self.assertEqual(layer.feature_class_name, name)
                    self.assertTrue(os.path.isfile(layer.path))
                    physicals.add(layer.physical_name)
                self.assertEqual(len(physicals), len(layers),
                                 '两个图层指向了同一个物理文件')

    def test_get_layer_by_physical_and_path(self):
        for path, gdb in self.opened.items():
            layers = gdb.list_layers()
            if not layers:
                continue
            name = layers[0]
            layer = gdb.get_layer(name)
            with self.subTest(gdb=os.path.basename(path)):
                self.assertIs(gdb.get_layer(layer.physical_name), layer)
                self.assertIs(gdb.get_layer('\\' + name), layer)
                self.assertIs(gdb[name], layer)
                with self.assertRaises(GdbNotFoundError):
                    gdb.get_layer('不存在的图层名__')

    def test_system_tables_present(self):
        """7 张核心系统表都在(``GDB_ReplicaLog`` 允许缺席)。"""
        required = ['GDB_SystemCatalog', 'GDB_DBTune', 'GDB_SpatialRefs',
                    'GDB_Items', 'GDB_ItemTypes', 'GDB_ItemRelationships',
                    'GDB_ItemRelationshipTypes']
        for path, gdb in self.opened.items():
            names = set(gdb.list_system_tables())
            with self.subTest(gdb=os.path.basename(path)):
                for want in required:
                    self.assertIn(want, names)
                self.assertIsNotNone(gdb.items)
                self.assertIsNotNone(gdb.root_guid())

    def test_catalog_rows_point_at_existing_files(self):
        """catalog 里每个逻辑名都能对上一个真实文件(ReplicaLog 除外)。"""
        for path, gdb in self.opened.items():
            mapping = find_system_tables(gdb.path)   # {物理名: 逻辑名}
            with self.subTest(gdb=os.path.basename(path)):
                for physical, logical in mapping.items():
                    self.assertTrue(
                        os.path.isfile(
                            os.path.join(gdb.path, physical + '.gdbtable')),
                        f'{logical} -> {physical} 对应的文件不存在')

    def test_gdb_items_rows_round_trip(self):
        """GDB_Items 的每一行都要能被解析成 Definition XML。"""
        for path, gdb in self.opened.items():
            items = gdb.items
            if items is None:
                continue
            for item in items:
                with self.subTest(gdb=os.path.basename(path), item=item.name):
                    if item.definition_xml:
                        parsed = parse_definition_xml(item.definition_xml)
                        self.assertTrue(parsed['root_tag'])

    # ------------------------------------------------------------------
    # 读要素
    # ------------------------------------------------------------------
    def test_read_features_round_trip(self):
        """全量/限量遍历:OID 递增、属性键都在字段清单里、几何种类与图层相符。"""
        for path, gdb in self.opened.items():
            for name in gdb.list_feature_classes():
                layer = gdb.get_layer(name)
                limit = (None if layer.record_count <= FULL_SCAN_LIMIT
                         else LIMIT_PROBE)
                allowed = {f.name for f in layer.attribute_fields}
                last_oid = 0
                seen = 0
                with self.subTest(gdb=os.path.basename(path), layer=name):
                    for feat in layer.read_features(limit=limit):
                        seen += 1
                        self.assertGreater(feat.oid, last_oid)
                        last_oid = feat.oid
                        self.assertEqual(set(feat.attributes), allowed)
                        if feat.geometry is not None:
                            self.assertIn(feat.geometry.kind,
                                          ('point', 'polyline', 'polygon',
                                           'multipoint', 'multipatch'))
                    self.assertGreater(seen, 0)
                    self.assertEqual(layer.record_count,
                                     layer.table.valid_record_count)

    def test_read_feature_by_oid_matches_iteration(self):
        for path, gdb in self.opened.items():
            for name in gdb.list_feature_classes():
                layer = gdb.get_layer(name)
                probe = [(f.oid, f) for f in layer.read_features(limit=5)]
                for oid, feat in probe:
                    with self.subTest(gdb=os.path.basename(path),
                                      layer=name, oid=oid):
                        again = layer.read_feature(oid)
                        self.assertIsNotNone(again)
                        self.assertEqual(again.oid, feat.oid)
                        self.assertEqual(again.attributes, feat.attributes)
                if probe:
                    # 越界的 OID 与"合法但空"的槽位一样返回 None —— 这是
                    # GDAL ``GetFeature`` 的行为(打条 warning 后返回 null)。
                    # 想区分"空槽"和"根本不可能存在的 OID",看
                    # ``layer.table.tablx.record_count``。
                    out_of_range = layer.table.tablx.record_count + 999
                    self.assertIsNone(layer.read_feature(out_of_range))
                    self.assertIsNone(layer.read_feature(0))
                    self.assertIsNone(layer.read_feature(-1))

    def test_len_and_iter(self):
        for path, gdb in self.opened.items():
            for name in gdb.list_feature_classes():
                layer = gdb.get_layer(name)
                with self.subTest(gdb=os.path.basename(path), layer=name):
                    self.assertEqual(len(layer), layer.record_count)
                    first = next(iter(layer))
                    self.assertEqual(first.oid, next(
                        layer.read_features(limit=1)).oid)

    def test_limit_offset_and_fields(self):
        for path, gdb in self.opened.items():
            for name in gdb.list_feature_classes():
                layer = gdb.get_layer(name)
                if layer.record_count < 3:
                    continue
                with self.subTest(gdb=os.path.basename(path), layer=name):
                    head = [f.oid for f in layer.read_features(limit=2)]
                    self.assertEqual(len(head), 2)
                    tail = [f.oid for f in layer.read_features(offset=1, limit=2)]
                    self.assertEqual(tail[0], head[1])
                    names = [f.name for f in layer.attribute_fields][:1]
                    if names:
                        one = next(layer.read_features(fields=names, limit=1))
                        self.assertEqual(list(one.attributes), names)

    def test_extent_matches_geometry_bounds(self):
        """字段描述区里的全表包围盒必须 **包含** 逐要素算出来的范围。

        这里刻意不用"相等":那个包围盒是 ArcGIS 在写几何时顺手扩大的,
        只增不减 —— 删要素、或把要素改小,它都不会跟着缩回去。实测
        ``八里湖新区…林地图斑`` 的 xmin 是 109.43,而现存要素的最小
        xmin 是 114.75,差 5.3 度,纯属历史遗留。

        但它必须是个 **上界**:真解析错了(比如读错字段位置),算出来的
        范围几乎不可能正好落在这个盒子里。
        """
        for path, gdb in self.opened.items():
            for name in gdb.list_feature_classes():
                layer = gdb.get_layer(name)
                if layer.record_count == 0 or layer.record_count > FULL_SCAN_LIMIT:
                    continue
                xs_min = ys_min = float('inf')
                xs_max = ys_max = float('-inf')
                for feat in layer.read_features():
                    geom = feat.geometry
                    if geom is None or geom.is_empty:
                        continue
                    xmin, ymin, xmax, ymax = _geom_bbox(geom)
                    xs_min = min(xs_min, xmin)
                    ys_min = min(ys_min, ymin)
                    xs_max = max(xs_max, xmax)
                    ys_max = max(ys_max, ymax)
                if xs_min == float('inf'):
                    continue
                extent = layer.extent
                with self.subTest(gdb=os.path.basename(path), layer=name):
                    self.assertIsNotNone(extent)
                    # 允许一个量化格的误差(ArcGIS 默认 xy_scale 1e9)
                    eps = 4.0 / max(abs(layer.geometry_field.xy_scale), 1.0)
                    xmin, ymin, xmax, ymax = extent
                    self.assertLessEqual(xmin, xs_min + eps,
                                         '包围盒没盖住最左边的点')
                    self.assertLessEqual(ymin, ys_min + eps,
                                         '包围盒没盖住最下边的点')
                    self.assertGreaterEqual(xmax, xs_max - eps,
                                            '包围盒没盖住最右边的点')
                    self.assertGreaterEqual(ymax, ys_max - eps,
                                            '包围盒没盖住最上边的点')

    def test_bbox_filter_is_subset_of_full(self):
        for path, gdb in self.opened.items():
            for name in gdb.list_feature_classes():
                layer = gdb.get_layer(name)
                if layer.record_count == 0 or layer.record_count > FULL_SCAN_LIMIT:
                    continue
                extent = layer.extent
                if not extent:
                    continue
                # 取中间那一小块,结果必须是非空且是全集的子集
                xmin, ymin, xmax, ymax = extent
                mid = (xmin + (xmax - xmin) * 0.4, ymin + (ymax - ymin) * 0.4,
                       xmin + (xmax - xmin) * 0.6, ymin + (ymax - ymin) * 0.6)
                inside = {f.oid for f in layer.read_features(bbox=mid)}
                everything = {f.oid for f in layer.read_features()}
                with self.subTest(gdb=os.path.basename(path), layer=name):
                    self.assertTrue(inside <= everything)

    # ------------------------------------------------------------------
    # WHERE 子句
    # ------------------------------------------------------------------
    def test_where_numeric_and_string(self):
        for path, gdb in self.opened.items():
            for name in gdb.list_feature_classes():
                layer = gdb.get_layer(name)
                numeric = [f for f in layer.attribute_fields
                           if f.field_type in (C.FGFT_INT16, C.FGFT_INT32,
                                               C.FGFT_INT64, C.FGFT_FLOAT32,
                                               C.FGFT_FLOAT64)]
                text = [f for f in layer.attribute_fields
                        if f.field_type == C.FGFT_STRING]
                if numeric:
                    col = numeric[0].name
                    with self.subTest(gdb=os.path.basename(path),
                                      layer=name, where=col):
                        got = list(layer.read_features(
                            where=f'{col} IS NOT NULL', limit=5))
                        for feat in got:
                            self.assertIsNotNone(feat[col])
                        nulls = list(layer.read_features(
                            where=f'{col} IS NULL', limit=200))
                        for feat in nulls:
                            self.assertIsNone(feat[col])
                if text:
                    col = text[0].name
                    with self.subTest(gdb=os.path.basename(path),
                                      layer=name, where=col + ' LIKE'):
                        sample = next((f for f in layer.read_features(limit=50)
                                       if f[col]), None)
                        if sample is not None:
                            like = layer.read_features(
                                where=f"{col} LIKE '%'", limit=5)
                            self.assertTrue(list(like))

    def test_where_is_python_side_consistent(self):
        """SQL 子集的语义必须与等价的 Python 过滤保持一致。"""
        for path, gdb in self.opened.items():
            for name in gdb.list_feature_classes():
                layer = gdb.get_layer(name)
                numeric = [f for f in layer.attribute_fields
                           if f.field_type in (C.FGFT_INT16, C.FGFT_INT32,
                                               C.FGFT_INT64, C.FGFT_FLOAT32,
                                               C.FGFT_FLOAT64)]
                if not numeric or layer.record_count > FULL_SCAN_LIMIT:
                    continue
                col = numeric[0].name
                everything = list(layer.read_features())
                values = [f[col] for f in everything if f[col] is not None]
                if not values:
                    continue
                cut = sorted(values)[len(values) // 2]
                sql = {f.oid for f in layer.read_features(where=f'{col} > {cut!r}')}
                py = {f.oid for f in everything
                      if f[col] is not None and f[col] > cut}
                with self.subTest(gdb=os.path.basename(path), layer=name):
                    self.assertEqual(sql, py)

    def test_where_syntax_error(self):
        """语法错误抛 :class:`GdbFormatError`;字段名不存在抛 NotFound。"""
        path, gdb = next(iter(self.opened.items()))
        name = gdb.list_feature_classes()[0]
        layer = gdb.get_layer(name)
        col = layer.attribute_fields[0].name
        for bad in (f'{col} =', f'({col} = 1', f'{col} ==== 1',
                    f'{col} = 1 {col} 2', f'{col} LIKE'):
            with self.subTest(where=bad):
                with self.assertRaises(GdbFormatError):
                    list(layer.read_features(where=bad))

    def test_where_unknown_field_and_unicode_names(self):
        """字段名不存在要报 NotFound;中文列名照样能解析。"""
        path, gdb = next(iter(self.opened.items()))
        name = gdb.list_feature_classes()[0]
        layer = gdb.get_layer(name)
        with self.assertRaises(GdbNotFoundError):
            list(layer.read_features(where='NO_SUCH_FIELD__ = 1'))
        # 中文列的 ``IS [NOT] NULL``:能解析就是胜利(值本身不重要)
        unicode_cols = [f.name for f in layer.attribute_fields
                        if not f.name.isascii()]
        for col in unicode_cols[:3]:
            with self.subTest(col=col):
                list(layer.read_features(where=f'{col} IS NOT NULL', limit=1))
                list(layer.read_features(where=f"{col} LIKE '%'", limit=1))

    # ------------------------------------------------------------------
    # 几何
    # ------------------------------------------------------------------
    def test_geometry_wkt_round_trip(self):
        """读出来的几何转 WKT 再解析回来,坐标必须一致。"""
        for path, gdb in self.opened.items():
            for name in gdb.list_feature_classes():
                layer = gdb.get_layer(name)
                for feat in layer.read_features(limit=50):
                    geom = feat.geometry
                    if geom is None or geom.is_empty:
                        continue
                    with self.subTest(gdb=os.path.basename(path), layer=name,
                                      oid=feat.oid):
                        again = Geometry.from_wkt(geom.wkt())
                        self.assertEqual(again.kind, geom.kind)
                        self.assertEqual(again.has_z, geom.has_z)
                        _assert_coords_equal(self, geom, again)
                    # ⚠️ 这里**不能** break。原来自从有了这么一句,每个图层就
                    # 只测第一条要素 —— 而第一条恰好是面,于是 LINESTRING 的
                    # WKT 曾经长期产出非法文本(``LINESTRING ((0 0), (1 1))``,
                    # 本库自己的 from_wkt 都读不回来)而套件全绿。
                    # 见 test_geometry.TestWkt 里逐类型的定字符串断言。

    def test_geometry_type_matches_layer(self):
        expected = {
            C.FGTGT_POINT: {'point', 'multipoint'},
            C.FGTGT_MULTIPOINT: {'multipoint', 'point'},
            C.FGTGT_LINE: {'polyline'},
            C.FGTGT_POLYGON: {'polygon'},
            C.FGTGT_MULTIPATCH: {'multipatch'},
        }
        for path, gdb in self.opened.items():
            for name in gdb.list_feature_classes():
                layer = gdb.get_layer(name)
                fgtgt = layer.table_geom_type
                if fgtgt not in expected:
                    continue
                for feat in layer.read_features(limit=50):
                    geom = feat.geometry
                    if geom is None or geom.is_empty:
                        continue
                    with self.subTest(gdb=os.path.basename(path), layer=name,
                                      oid=feat.oid):
                        self.assertIn(geom.kind, expected[fgtgt])

    def test_spatial_ref_is_esri_wkt(self):
        for path, gdb in self.opened.items():
            for name in gdb.list_feature_classes():
                layer = gdb.get_layer(name)
                srs = layer.spatial_ref
                with self.subTest(gdb=os.path.basename(path), layer=name):
                    if srs and srs.wkt:
                        head = srs.wkt.strip().upper()
                        self.assertTrue(
                            head.startswith('GEOGCS') or head.startswith('PROJCS')
                            or head.startswith('LOCAL_CS'))


# ======================================================================
# 辅助
# ======================================================================
def _geom_bbox(geom):
    pts = _all_points(geom)
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _all_points(geom):
    """把任意几何摊成点列表。"""
    if geom.kind == 'point':
        return [geom.coordinates]
    if geom.kind == 'multipoint':
        return list(geom.coordinates)
    parts, points = geom.coordinates
    return list(points)


def _assert_coords_equal(case, a, b):
    pa, pb = _all_points(a), _all_points(b)
    case.assertEqual(len(pa), len(pb))
    for pa_i, pb_i in zip(pa, pb):
        for va, vb in zip(pa_i, pb_i):
            case.assertAlmostEqual(va, vb, places=6)


# ----------------------------------------------------------------------------
# C 加速路径 vs 纯 Python 回退
# ----------------------------------------------------------------------------
def _exactly_same(a, b):
    """逐位比较,NaN 视作相等(NaN != NaN,直接 == 会误报)。"""
    if isinstance(a, float) or isinstance(b, float):
        try:
            return a == b or (a != a and b != b)
        except TypeError:
            return False
    if isinstance(a, (list, tuple, array)) and isinstance(b, (list, tuple, array)):
        return (len(a) == len(b)
                and all(_exactly_same(x, y) for x, y in zip(a, b)))
    return a == b


def _geom_key(g):
    """比对用的规范形态。

    ⚠️ ``xy_parts`` / ``z_parts`` / ``m_parts`` 是几何的**主存储**,而
    ``coordinates`` 是从它惰性物化出来的派生视图(见 DESIGN.md §2.19.5)。
    只比 ``coordinates`` 的话,万一主存储和物化各错一处、恰好互相抵消,
    测试照样绿。所以两样都进来 —— 顺带把"两条路产出的容器类型一致"
    (都是 array('d'),不是一边 list 一边 array)也钉住。
    """
    if g is None:
        return None
    return (g.shape_type, g.has_z, g.has_m, g.coordinates,
            [type(p).__name__ for p in g.xy_parts], g.xy_parts,
            g.z_parts, g.m_parts)


@unittest.skipUnless(_accel.HAS_ACCEL,
                     '没有 C 加速模块(先跑 setup.py build_ext --inplace)')
class TestAccelAgreesWithPython(unittest.TestCase):
    """C 加速路径与纯 Python 回退必须**逐位相同**。

    两条实现并存的前提就是"能证明它们等价",证据只有差分对拍:同一份
    几何 blob 跑两遍,比坐标、比消费位置。这里只抽一小撮,保证每次跑测试
    都能挡住回归;几百万顶点级别的全量对拍在 ``tools/verify_accel.py``。
    """

    #: 每个图层抽多少条(样例库里有 4,494 万顶点的图层,不能全量跑)。
    MAX_FEATURES = 40

    @classmethod
    def setUpClass(cls):
        cls.opened = {}
        for path in SAMPLES:
            try:
                cls.opened[path] = OpenFileGDB.open(path)
            except GdbError:
                continue
        if not cls.opened:
            raise unittest.SkipTest('样例库全都打不开')

    @classmethod
    def tearDownClass(cls):
        for gdb in cls.opened.values():
            gdb.close()

    def test_geometry_bit_identical(self):
        checked = 0
        for path, gdb in self.opened.items():
            for name in gdb.list_feature_classes():
                ly = gdb.get_layer(name)
                tab = ly.table
                if tab is None or tab.geom_field is None:
                    continue
                for feat in ly.read_features(limit=self.MAX_FEATURES):
                    lg = feat._geometry
                    raw = getattr(lg, '_raw', None)
                    if raw is None and hasattr(lg, '_read_bytes'):
                        try:
                            raw = lg._read_bytes(lg._len)
                        except OSError:
                            continue
                    if not raw:
                        continue

                    with _accel.use(True):
                        ga, pa = _EG.decode_geometry_ex(
                            raw, 0, tab.geom_field, tab.has_z, tab.has_m)
                    with _accel.use(False):
                        gb, pb = _EG.decode_geometry_ex(
                            raw, 0, tab.geom_field, tab.has_z, tab.has_m)

                    where = f'{os.path.basename(path)}/{name} 行 {feat.oid}'
                    self.assertEqual(pa, pb, f'{where}: 消费位置不同')
                    self.assertTrue(
                        _exactly_same(_geom_key(ga), _geom_key(gb)),
                        f'{where}: 几何不同\n  C : {_geom_key(ga)}\n'
                        f'  PY: {_geom_key(gb)}')
                    checked += 1
        self.assertGreater(checked, 0, '一条几何都没比到,样例库是不是空了')

    def test_toggle_actually_switches(self):
        """确认 use() 真的把分派切走了 —— 否则上面那条测试会假通过。"""
        self.assertIsNotNone(_accel.decode_xy_flat)
        self.assertIsNotNone(_accel.decode_scalar_flat)
        with _accel.use(False):
            self.assertIsNone(_accel.decode_xy_flat)
            self.assertIsNone(_accel.decode_scalar_flat)
            self.assertIsNone(_accel.decode_xy)
            self.assertIsNone(_accel.decode_scalar)
            # 就是同一个模块对象
            self.assertIsNone(_EG._accel.decode_xy_flat)
        self.assertIsNotNone(_accel.decode_xy_flat)

    def test_dispatch_reads_module_attribute_every_call(self):
        """热函数必须**每次调用**重读 ``_accel.decode_xy_flat``。

        ⚠️ 这是防"假通过"的关键一条。如果哪天有人图快把它在模块导入时抓成
        一个局部名,``use(False)`` 就切不走了 —— 那时 ``test_geometry_bit_identical``
        会退化成"C 对 C"的空转,照样全绿,却什么都没验。这里塞一个探针
        进去,证明真身确实被调到了、也能被换掉。
        """
        real = _accel.decode_xy_flat
        calls = []

        def spy(*a):
            calls.append(a)
            return real(*a)

        class _Q:
            xy_scale = 1.0
            x_origin = 0.0
            y_origin = 0.0

        # 四个单字节 varint:dx 5 → 14,dy 7 → 18
        blob = b'\x05\x07\x09\x0b'
        want = array('d', [5.0, 7.0, 14.0, 18.0])       # 交错 (x, y, x, y)
        try:
            with _accel.use(True):
                _accel.decode_xy_flat = spy
                dr = _EG._DeltaReader(blob, 0)
                pts, dx, dy = _EG._read_xy_array(blob, dr, 2, _Q(), 0, 0)
                self.assertEqual(pts, want)
                self.assertEqual((dr.pos, dx, dy), (4, 14, 18))
                self.assertTrue(calls, '开着加速却没走 C —— 分派把函数抓死了?')

            calls.clear()
            with _accel.use(False):
                dr = _EG._DeltaReader(blob, 0)
                pts, dx, dy = _EG._read_xy_array(blob, dr, 2, _Q(), 0, 0)
                self.assertEqual(pts, want)
                self.assertEqual((dr.pos, dx, dy), (4, 14, 18))
                self.assertFalse(calls, '关了加速还在调 C')
        finally:
            _accel.decode_xy_flat = real

    def test_corrupt_blob_raises_format_error(self):
        """损坏的 blob 两条路都要抛 GdbFormatError,且不能漏出别的异常类型。

        ⚠️ 这一条盯的是 C 侧的三道闸:点数先验再分配(否则 MemoryError)、
        参数校验也抛 IndexError(否则 ValueError)、超长 varint 上限与
        Python 取同一个值(否则两边接受域不同)。``iter_rows`` 只 catch
        GdbFormatError,漏出别的类型会废掉整个图层迭代。
        """
        from pyopenfilegdb._constants import ShapeType as ST
        from pyopenfilegdb._util import write_varuint

        def vu(v):
            b = bytearray()
            write_varuint(b, v)
            return bytes(b)

        head = vu(ST.POLYGON) + vu(1) + vu(1) + vu(0) * 4
        cases = {
            '点数天文数字': vu(ST.POLYGON) + vu(2 ** 40) + vu(1) + vu(0) * 4
                            + b'\x00' * 16,
            '点数数组缺失': head,
            'XY 里 20 个续字节': head + b'\x80' * 20,
            'varint 截断': head + b'\xff',
        }
        for desc, blob in cases.items():
            seen = []
            for on in (True, False):
                with _accel.use(on):
                    with self.assertRaises(
                            GdbFormatError,
                            msg=f'{desc}: {"C" if on else "纯 Python"} '
                                f'没有抛 GdbFormatError'):
                        _EG.decode_geometry(blob, None, False, False)
                    seen.append('GdbFormatError')
            self.assertEqual(seen[0], seen[1], f'{desc}: 两条路异常不同')

    def test_huge_n_points_does_not_allocate(self):
        """C 入口收到天文数字的点数时必须是 IndexError,不能是 MemoryError。

        直接打入口,不经过几何解码 —— 这样"点数先验再分配"那道闸是唯一
        能挡住分配的东西(2**40 个指针 ≈ 8 TB;flat 版要的还是一块连续
        8 TB 的 buffer,更拦不住)。
        """
        blob = b'\x00' * 32
        with self.assertRaises(IndexError):
            _accel.decode_xy(blob, 0, 2 ** 40, 1.0, 0.0, 0.0, 0, 0)
        with self.assertRaises(IndexError):
            _accel.decode_scalar(blob, 0, 2 ** 40, 1.0, 0.0, 0)
        with self.assertRaises(IndexError):
            _accel.decode_xy_flat(blob, 0, 2 ** 40, 1.0, 0.0, 0.0, 0, 0)
        with self.assertRaises(IndexError):
            _accel.decode_scalar_flat(blob, 0, 2 ** 40, 1.0, 0.0, 0)

    def test_zero_points_mirrors_python(self):
        """n_points == 0 时连 pos 越界都不检查 —— 与 Python 循环体不跑一致。"""
        got = _accel.decode_xy(b'', 999, 0, 1.0, 0.0, 0.0, 7, 9)
        self.assertEqual(got, ([], 999, 7, 9))
        got = _accel.decode_scalar(b'', 999, 0, 1.0, 0.0, 5)
        self.assertEqual(got, ([], 999, 5))
        # flat 版同样:*pos 原样带回,容器是空的 array('d')
        got = _accel.decode_xy_flat(b'', 999, 0, 1.0, 0.0, 0.0, 7, 9)
        self.assertEqual(got, (array('d'), 999, 7, 9))
        got = _accel.decode_scalar_flat(b'', 999, 0, 1.0, 0.0, 5)
        self.assertEqual(got, (array('d'), 999, 5))


if __name__ == '__main__':
    unittest.main(verbosity=2)
