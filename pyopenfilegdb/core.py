"""``OpenFileGDB`` —— ``.gdb`` 目录级门面(打开 / 新建 / 列图层 / 建图层)。

一个 FileGDB 就是 **一个目录**(习惯上以 ``.gdb`` 结尾),里面每个图层
一个 ``.gdbtable``,外加一组 ``a000000NN`` 编号的系统表描述"库里有什么"。
本模块负责:

* 目录级文件的读写(``gdb`` / ``timestamps`` / 7 张系统表样板);
* 把 ``GDB_SystemCatalog`` + ``GDB_Items`` 拼成"图层清单";
* 新建图层时,把新表登记进 ``GDB_SystemCatalog`` / ``GDB_Items`` /
  ``GDB_ItemRelationships`` / ``GDB_SpatialRefs``。

GDAL 里对应 ``OGROpenFileGDBDataSource``
(``ogr/ogrsf_frmts/openfilegdb/ogropenfilegdbdatasource.cpp`` 与
``..._write.cpp``)。本模块的函数/步骤注释都标了对应的 GDAL 位置。

.. warning::
    写路径只做 **ArcGIS 10.x**(``.gdbtable`` 版本 3)。版本 4 的库能读
    (GDAL 也只读不写,见 ``OGROpenFileGDBDataSource::Open`` 对
    ``nVersion > 3`` 的处理),但 :meth:`OpenFileGDB.create_layer` 会拒绝。
"""
from __future__ import annotations

import os
import shutil
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from . import _constants as C
from ._datatypes import (
    GdbFeature,
    GdbField,
    GdbFormatError,
    GdbGeomField,
    GdbItem,
    GdbNotFoundError,
    GdbSpatialRef,
    GdbVersionError,
    GdbWriteError,
)
from ._gdbtable import GdbTable
from ._gdb_template import SYSTEM_TABLES
from ._system_catalog import (
    GdbItems,
    GdbSystemCatalog,
    SYSTEM_CATALOG_PHYSICAL,
    SYSTEM_TABLE_LOGICAL_NAMES,
    _looks_like_items_table,
    find_system_tables,
    generate_uuid,
    item_uuid,
    make_feature_class_xml,
    make_table_xml,
    parse_definition_xml,
    physical_name_for,
    relationship_uuid,
    uuid_eq,
)
from .layer import GdbLayer

__all__ = ['OpenFileGDB']


# ============================================================================
# 目录级固定文件
# ============================================================================
#: ``<gdb>/gdb`` 文件的全部内容。GDAL 注释:"Write what the FileGDB SDK
#: writes..."(ogropenfilegdbdatasource_write.cpp:1298)。
GDB_FILE_BYTES = b'\x05\x00\x00\x00\xDE\xAD\xBE\xEF'

#: ``<gdb>/timestamps`` 的长度:400 个 0xFF。同样照抄 SDK 行为。
#: 它记录每个系统表最后一次同步的时间戳(20 张表 × 20 字节),
#: 本库不维护其内容(读路径也不需要),只保证存在且长度正确。
TIMESTAMPS_FILE_SIZE = 400


# ============================================================================
# 新建图层时几何字段的默认量化参数
# ============================================================================
#: 默认坐标系 WGS84 的 ESRI WKT。取自 GDAL ``CreateGDBItems`` 里硬编码的
#: ``ESRI_WKT_WGS84``(ogropenfilegdbdatasource_write.cpp:790)。
DEFAULT_WKT_WGS84 = (
    'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",'
    '6378137.0,298.257223563]],PRIMEM["Greenwich",0.0],'
    'UNIT["Degree",0.0174532925199433]]'
)

#: WGS84 的 WKID。
DEFAULT_WKID_WGS84 = 4326

#: 默认量化参数,与 GDAL ``CreateGDBItems`` 里那个 FileGDBGeomField 完全一致
#: (``-180, -90, 1e6, 2e-6`` + ``SetZOriginScaleTolerance(-1e5, 1e4, 1e-3)``
#: + ``SetMOriginScaleTolerance(-1e5, 1e4, 1e-3)``)。实测 ArcGIS 新建的空库
#: 里 GDB_Items 的 Shape 字段用的就是这一组值,所以拿它当"没指定坐标系"
#: 时的默认值,是名副其实的"和 ArcGIS 一样"。
DEFAULT_QUANTIZATION: Dict[str, float] = {
    'x_origin': -180.0,
    'y_origin': -90.0,
    'xy_scale': 1000000.0,
    'xy_tolerance': 0.000002,
    'z_origin': -100000.0,
    'z_scale': 10000.0,
    'z_tolerance': 0.001,
    'm_origin': -100000.0,
    'm_scale': 10000.0,
    'm_tolerance': 0.001,
}

#: 空间索引格网分辨率(GDAL 默认 ``{0.012, 0.4, 12.0}``)。
DEFAULT_GRID_RESOLUTIONS = [0.012, 0.4, 12.0]

#: 表级几何类型字符串 -> ``FGTGT_*``。也接受 ``esriGeometryXxx`` 写法。
_GEOM_TYPE_NAMES: Dict[str, int] = {
    'none': C.FGTGT_NONE,
    'null': C.FGTGT_NONE,
    'point': C.FGTGT_POINT,
    'esrigeometrypoint': C.FGTGT_POINT,
    'multipoint': C.FGTGT_MULTIPOINT,
    'esrigeometrymultipoint': C.FGTGT_MULTIPOINT,
    'line': C.FGTGT_LINE,
    'polyline': C.FGTGT_LINE,
    'linestring': C.FGTGT_LINE,
    'arc': C.FGTGT_LINE,
    'esrigeometryline': C.FGTGT_LINE,
    'esrigeometrypolyline': C.FGTGT_LINE,
    'polygon': C.FGTGT_POLYGON,
    'area': C.FGTGT_POLYGON,
    'esrigeometrypolygon': C.FGTGT_POLYGON,
    'multipatch': C.FGTGT_MULTIPATCH,
    'esrigeometrymultipatch': C.FGTGT_MULTIPATCH,
}

#: ``FGTGT_*`` -> Definition XML 里的 ``<ShapeType>`` 字符串。
_FGTGT_TO_ESRI_GEOM: Dict[int, str] = {
    C.FGTGT_NONE: '',
    C.FGTGT_POINT: 'esriGeometryPoint',
    C.FGTGT_MULTIPOINT: 'esriGeometryMultipoint',
    C.FGTGT_LINE: 'esriGeometryPolyline',
    C.FGTGT_POLYGON: 'esriGeometryPolygon',
    C.FGTGT_MULTIPATCH: 'esriGeometryMultipatch',
}


class OpenFileGDB:
    """一个 ``.gdb`` 目录(FileGDB 数据集)。

    用法::

        # 读
        with OpenFileGDB.open(r'D:/data/foo.gdb') as gdb:
            print(gdb.list_feature_classes())
            fc = gdb.get_layer('道路')
            for feat in fc.read_features(where='WIDTH > 3.5', limit=10):
                print(feat.oid, feat['NAME'], feat.geometry.wkt()[:40])

        # 建 + 写
        with OpenFileGDB.create(r'D:/data/bar.gdb') as gdb:
            fc = gdb.create_layer('道路', geometry_type='polyline',
                                  fields=[GdbField('NAME', C.FGFT_STRING,
                                                   length=64)])
            fc.write_feature({'NAME': 'G1', 'Shape': my_geometry})

    :param path: ``.gdb`` 目录。
    :param update: 是否以可写方式打开(对应 GDAL 的 ``GDALUpdate`` /
        ``GA_Update``)。只读打开时写方法会抛 :class:`GdbWriteError`。
    """

    def __init__(self, path: str, update: bool = False) -> None:
        self.path = os.path.abspath(os.fspath(path))
        self.name = os.path.basename(self.path)
        self.update = bool(update)
        self._closed = False

        self._catalog: Optional[GdbSystemCatalog] = None
        self._items: Optional[GdbItems] = None
        self._items_table: Optional[GdbTable] = None

        # 物理表名 -> 已打开的 GdbTable(读写共用,避免同一文件开两个句柄)
        self._tables: Dict[str, GdbTable] = {}
        # 逻辑名/物理名 -> GdbLayer
        self._layers: Dict[str, GdbLayer] = {}
        # 解析过的 Definition XML 缓存,键是物理表名
        self._definitions: Dict[str, Dict[str, Any]] = {}
        # 系统表的物理名(逻辑名 -> 物理名),延迟加载
        self._system_tables: Optional[Dict[str, str]] = None

    # ======================================================================
    # 打开
    # ======================================================================
    @classmethod
    def open(cls, path: str, update: bool = False) -> 'OpenFileGDB':
        """打开一个已存在的 ``.gdb`` 目录。

        对应 GDAL ``GDALOpenEx(..., GDAL_OF_VECTOR | [GDAL_OF_UPDATE])``。

        :raises GdbNotFoundError: 目录或 ``a00000001.gdbtable`` 不存在。
        :raises GdbVersionError: ``.gdbtable`` 版本不是 3/4(例如 FileGDB 9.x)。
        """
        self = cls(path, update=update)
        if not os.path.isdir(self.path):
            raise GdbNotFoundError(f'不是目录: {self.path}')
        # 把 catalog 读进来 —— 这一步就会做版本校验
        _ = self.system_catalog
        if update and self.version not in C.WRITABLE_VERSIONS:
            raise GdbVersionError(
                f'{self.name}: FileGDB 版本 {self.version} 不支持写入'
                f'(GDAL 同样只支持对版本 {C.WRITABLE_VERSIONS} 做 update)'
            )
        return self

    # ----------------------------------------------------------------------
    @classmethod
    def create(cls, path: str, overwrite: bool = False,
               with_geometry_template: bool = True) -> 'OpenFileGDB':
        """新建一个空的 ``.gdb``。

        对应 GDAL ``OGROpenFileGDBDataSource::Create``
        (ogropenfilegdbdatasource_write.cpp:1281)。步骤逐一对齐:

        1. 目录名必须以 ``.gdb`` 结尾(否则 GDAL 直接报 NotSupported);
        2. 目录已存在则失败,除非 ``overwrite=True``;
        3. 写 ``gdb`` 文件(8 字节 ``05 00 00 00 DE AD BE EF``);
        4. 写 ``timestamps`` 文件(400 个 0xFF);
        5. 建 7 张系统表 ``a00000001`` .. ``a00000007``。

        .. note::
            第 5 步用的是 :mod:`._gdb_template` 里那张"从真实 ArcGIS 空库
            抄下来的样板表" —— 字段定义与全部行数据都原样复刻,因此建出来
            的库在结构上与 ArcGIS 的"新建文件地理数据库"一致(逐字节校验
            见 ``tests/test_create_write.py``)。

            不建 ``a00000008``(GDB_ReplicaLog):GDAL 也不建,注释写着
            "GDB_ReplicaLog can be omitted"。它只在使用复制功能时才需要。

            **不写** ``.gdbindexes`` / ``.atx`` / ``.spx`` / ``.freelist``:
            GDAL 的 ``m_bDirtyGdbIndexesFile`` 默认 false,只有显式调
            ``CreateIndex()`` 才会落盘,而 ``Create()`` 路径不调它。

        :param with_geometry_template: 是否把模板里 GDB_Items 的 Shape 字段
            与 GDB_SpatialRefs 的初始坐标系一起写进去(默认 True;关掉会得到
            一个没有默认坐标系的最小库)。
        :returns: 已以 ``update=True`` 打开的 :class:`OpenFileGDB`。
        """
        path = os.path.abspath(os.fspath(path))
        if os.path.splitext(path)[1].lower() != '.gdb':
            raise GdbWriteError(
                f'目录名必须以 .gdb 结尾(GDAL 同样这么要求): {path}'
            )
        if os.path.exists(path):
            if not overwrite:
                raise GdbWriteError(f'{path} 已存在')
            if not os.path.isdir(path):
                raise GdbWriteError(f'{path} 已存在且不是目录')
            shutil.rmtree(path)
        os.makedirs(path)

        # --- 3. gdb 文件 ---------------------------------------------------
        with open(os.path.join(path, 'gdb'), 'wb') as f:
            f.write(GDB_FILE_BYTES)

        # --- 4. timestamps 文件 --------------------------------------------
        with open(os.path.join(path, 'timestamps'), 'wb') as f:
            f.write(b'\xFF' * TIMESTAMPS_FILE_SIZE)

        # --- 5. 系统表 ------------------------------------------------------
        for spec in SYSTEM_TABLES:
            if not with_geometry_template and spec['name'] == 'a00000004':
                continue
            _write_template_table(path, spec)

        return cls.open(path, update=True)

    # ======================================================================
    # 生命周期
    # ======================================================================
    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """关闭所有打开的表(写过的会先落盘)。幂等。"""
        if self._closed:
            return
        for table in list(self._tables.values()):
            try:
                table.close()
            except Exception:
                pass
        self._tables.clear()
        self._layers.clear()
        self._definitions.clear()
        self._closed = True

    def __enter__(self) -> 'OpenFileGDB':
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def __del__(self) -> None:  # pragma: no cover - 兜底
        try:
            self.close()
        except Exception:
            pass

    def _check_open(self) -> None:
        if self._closed:
            raise GdbWriteError(f'{self.name}: 数据集已关闭')

    def _require_update(self) -> None:
        self._check_open()
        if not self.update:
            raise GdbWriteError(
                f'{self.name}: 只读打开,不能写;'
                f'请用 OpenFileGDB.open(path, update=True) 或 OpenFileGDB.create(path)'
            )

    # ======================================================================
    # 目录级元信息
    # ======================================================================
    @property
    def version(self) -> int:
        """FileGDB 格式版本(``.gdbtable`` 头部的第 0 个 uint32)。

        3 = ArcGIS 10.x(本项目的主要目标)、4 = 较新的 ArcGIS Pro。
        GDAL 对版本 4 只读不写,本库亦然。
        """
        return self.system_catalog.table.version

    @property
    def system_catalog(self) -> GdbSystemCatalog:
        """``GDB_SystemCatalog`` 的视图(懒加载 + 缓存)。

        这个视图对象自己缓存了行列表,所以写系统表之后必须调
        :meth:`_invalidate_views` 让它重建。
        """
        self._check_open()
        if self._catalog is None:
            physical = (self._physical_for('GDB_SystemCatalog')
                        or SYSTEM_CATALOG_PHYSICAL)
            self._catalog = GdbSystemCatalog(self.path,
                                             self._open_table(physical))
        return self._catalog

    @property
    def items(self) -> Optional[GdbItems]:
        """``GDB_Items`` 的视图;这个库没有 GDB_Items 时返回 ``None``。"""
        self._check_open()
        if self._items is None:
            physical = self._physical_for('GDB_Items')
            if physical:
                try:
                    table = self._open_table(physical)
                except (GdbNotFoundError, GdbFormatError):
                    table = None
                if table is not None and _looks_like_items_table(table):
                    self._items = GdbItems(self.path, table, physical)
                    return self._items
            # 兜底:按 GDAL 的方式满目录找一个结构像 GDB_Items 的表
            try:
                self._items = GdbItems.read(self.path)
            except (GdbNotFoundError, GdbFormatError):
                self._items = None
        return self._items

    def _invalidate_views(self) -> None:
        """丢掉 catalog / items / 图层的缓存视图。

        系统表被追加或删除行之后必须调用:视图对象缓存了行列表,不清掉的话
        新登记进来的图层在 :meth:`list_layers` 里看不见 —— 这正是 GDAL 里
        ``m_oMapLayers`` 需要 ``RefreshXMLDefinitionInMemory`` 的原因。
        """
        self._catalog = None
        self._items = None
        self._layers.clear()
        self._definitions.clear()

    def system_table_names(self) -> Dict[str, str]:
        """``{物理表名: 逻辑表名}`` 的系统表映射。

        不靠固定编号,而是扫描目录 + 读 catalog(见
        :func:`~pyopenfilegdb._system_catalog.find_system_tables`)。
        """
        self._check_open()
        if self._system_tables is None:
            self._system_tables = find_system_tables(self.path)
        return self._system_tables

    def _physical_for(self, logical: str) -> Optional[str]:
        """逻辑表名 -> 物理表名。

        ``find_system_tables`` 返回的是反过来的映射(物理 -> 逻辑),而且
        系统表的编号 **不保证** 是 a00000001..a00000007:GDAL 的
        ``Open`` 也是先扫目录、再靠 catalog 认名字,绝不假设编号固定。
        """
        for physical, name in self.system_table_names().items():
            if name == logical:
                return physical
        return None

    def spatial_refs(self) -> List[Tuple[int, GdbSpatialRef]]:
        """``GDB_SpatialRefs`` 里的坐标系清单 ``[(ID, GdbSpatialRef), ...]``。

        对应 GDAL ``OGROpenFileGDBDataSource::Open`` 里把 GDB_SpatialRefs
        整表读进 ``m_oMapSpatialRefs`` 那段。
        """
        out: List[Tuple[int, GdbSpatialRef]] = []
        physical = self._physical_for('GDB_SpatialRefs')
        if not physical:
            return out
        try:
            table = self._open_table(physical)
        except (GdbNotFoundError, GdbFormatError):
            return out
        for row, values in table.iter_rows():
            wkt = values.get('SRTEXT') or ''
            if not wkt:
                continue
            srs = GdbSpatialRef(wkt=wkt, wkid=0)
            out.append((row + 1, srs))
        return out

    # ======================================================================
    # 图层枚举
    # ======================================================================
    def _iter_layer_entries(self, include_system: bool = False
                            ) -> Iterator[Tuple[str, str, Optional[GdbItem]]]:
        """产出 ``(逻辑名, 物理表名, GdbItem | None)``。

        物理名不是从文件名猜的,而是照 GDAL 的规矩算:第 ``i`` 条(**0 基**)
        catalog 记录对应 ``a%08x % (i + 1)``
        (ogropenfilegdbdatasource.cpp:1084)。逻辑名与 item 的对应优先按
        ``GDB_Items.Name`` 找 —— 真实 ArcGIS 文件里 ``PhysicalName`` 存的就是
        ``a00000009`` 这种物理名,而 GDAL 建的文件里存的是"图层名的大写",
        按名字找两种都能命中。
        """
        items = self.items
        for row, values in self.system_catalog.table.iter_rows():
            name = values.get('Name')
            if not isinstance(name, str) or not name:
                continue
            if not include_system and name in SYSTEM_TABLE_LOGICAL_NAMES:
                continue
            physical = physical_name_for(row + 1)
            item = None
            if items is not None:
                item = items.by_name(name) or items.by_physical_name(physical)
            yield name, physical, item

    def _classify(self, physical: str, item: Optional[GdbItem]) -> str:
        """判定图层种类:``'feature_class'`` / ``'table'``。

        优先信 ``GDB_Items`` 的 Definition(ArcGIS 写的 ``DatasetType``);
        拿不到就退化为"看这张表有没有几何字段"。
        """
        if item is not None:
            kind = self._definition_for(item, physical).get('kind')
            if kind:
                return kind
        try:
            table = self._open_table(physical)
        except (GdbNotFoundError, GdbFormatError):
            return 'table'
        return 'feature_class' if table.geom_field is not None else 'table'

    def _definition_for(self, item: GdbItem, physical: str) -> Dict[str, Any]:
        """解析并缓存一个 item 的 Definition XML。"""
        cached = self._definitions.get(physical)
        if cached is not None:
            return cached
        out: Dict[str, Any] = {}
        if item.definition_xml:
            try:
                out = parse_definition_xml(item.definition_xml)
                out['kind'] = ('feature_class' if out.get('is_feature_class')
                               else 'table' if out.get('is_table')
                               else '')
            except GdbFormatError:
                out = {}
        self._definitions[physical] = out
        return out

    def list_layers(self) -> List[str]:
        """所有用户图层(要素类 + 非空间表)的逻辑名,按 catalog 顺序。"""
        return [name for name, _p, _i in self._iter_layer_entries()]

    def list_feature_classes(self) -> List[str]:
        """所有 **要素类**(带几何的表)的逻辑名。"""
        return [name for name, physical, item in self._iter_layer_entries()
                if self._classify(physical, item) == 'feature_class']

    def list_tables(self) -> List[str]:
        """所有 **非空间表** 的逻辑名。"""
        return [name for name, physical, item in self._iter_layer_entries()
                if self._classify(physical, item) == 'table']

    def list_system_tables(self) -> List[str]:
        """系统表的逻辑名(``GDB_SystemCatalog`` / ``GDB_Items`` / ...)。"""
        return [name for name, _p, _i in self._iter_layer_entries(True)
                if name in SYSTEM_TABLE_LOGICAL_NAMES]

    # ----------------------------------------------------------------------
    def get_layer(self, name: str) -> GdbLayer:
        """按名字取图层。

        ``name`` 可以是:

        * 逻辑名(``'道路'``、``'GDB_Items'``)—— 大小写敏感优先;
        * 物理表名(``'a00000009'``);
        * 带 ``\\`` 的 catalog 路径(``'\\\\道路'``)。

        :raises GdbNotFoundError: 库里没有这个图层。
        """
        self._check_open()
        # 先把 ``'\\道路'`` 这种 catalog 路径形式归一成裸名字再查缓存,否则
        # 同一个图层会按不同写法各建一个 GdbLayer 实例(句柄是共享的,但
        # 调用方会看到不同的对象)。
        target = name.strip().strip('\\') if name else ''
        if target in self._layers:
            return self._layers[target]
        if name in self._layers:
            return self._layers[name]
        lowered = target.lower()

        found: Optional[Tuple[str, str, Optional[GdbItem]]] = None
        for entry in self._iter_layer_entries(include_system=True):
            logical, physical, _item = entry
            if logical == target or physical.lower() == lowered:
                found = entry
                break
        if found is None:
            # 退化到大小写无关
            for entry in self._iter_layer_entries(include_system=True):
                if entry[0].lower() == lowered:
                    found = entry
                    break
        if found is None:
            raise GdbNotFoundError(
                f'{self.name}: 没有名为 {name!r} 的图层;'
                f'现有图层: {self.list_layers() + self.list_system_tables()}'
            )

        logical, physical, item = found
        definition = self._definition_for(item, physical) if item else {}
        layer = GdbLayer(self, logical, self._open_table(physical),
                         item=item, definition=definition)
        self._layers[target] = layer
        self._layers[logical] = layer
        self._layers[physical] = layer
        return layer

    def __getitem__(self, name: str) -> GdbLayer:
        return self.get_layer(name)

    def __iter__(self) -> Iterator[GdbLayer]:
        for name in self.list_layers():
            yield self.get_layer(name)

    def __len__(self) -> int:
        return len(self.list_layers())

    # ======================================================================
    # 打开底层的表
    # ======================================================================
    def _open_table(self, physical: str) -> GdbTable:
        """打开(并缓存)一个物理表。

        ``update=True`` 的数据集对 **所有** 表都以可写方式打开 —— 因为建图层
        时要往系统表里追加记录。对用户图层来说这意味着 :class:`GdbLayer` 的
        写方法是可用的,与 GDAL ``bUpdate=TRUE`` 的语义一致。
        """
        self._check_open()
        cached = self._tables.get(physical)
        if cached is not None:
            return cached

        path = os.path.join(self.path, physical + '.gdbtable')
        if not os.path.isfile(path):
            raise GdbNotFoundError(f'{self.name}: 缺少表文件 {physical}.gdbtable')
        table = (GdbTable.open_for_write(path) if self.update
                 else GdbTable.from_file(path))
        self._tables[physical] = table
        return table

    def _system_table(self, logical: str, write: bool = False) -> GdbTable:
        """取一张系统表(``write=True`` 时以可写方式打开)。"""
        physical = self._physical_for(logical)
        if not physical:
            raise GdbNotFoundError(f'{self.name}: 找不到系统表 {logical}')
        if write:
            self._require_update()
        return self._open_table(physical)

    # ======================================================================
    # 建图层
    # ======================================================================
    def create_layer(self, name: str,
                     fields: Optional[Sequence[Any]] = None,
                     geometry_type: Any = 'null',
                     spatial_ref: Any = None,
                     has_z: bool = False,
                     has_m: bool = False,
                     oid_field: str = 'OBJECTID',
                     geometry_field: str = 'Shape',
                     alias_name: str = '',
                     documentation: str = '') -> GdbLayer:
        """新建一个要素类(或非空间表)并登记进系统表。

        对应 GDAL ``OGROpenFileGDBDataSource::ICreateLayer`` +
        ``OGROpenFileGDBLayer::Create`` + ``RegisterTable``。完整步骤如下
        (顺序与 GDAL 一致):

        1. 物理编号 = ``1 + GDB_SystemCatalog 的槽位数``,物理名 ``a%08x``;
        2. 建 ``<物理名>.gdbtable`` / ``.gdbtablx``,字段顺序为
           OBJECTID、几何字段(如果有)、其余属性字段;
        3. 往 ``GDB_SystemCatalog`` 追加 ``(Name, FileFormat=0)``;
        4. 往 ``GDB_Items`` 追加一条 item:UUID(新生成)、Type(要素类/表的
           类型 GUID)、Name、PhysicalName、Path、DatasetSubtype1=1、
           DatasetSubtype2=表级几何类型、DatasetInfo1=几何字段名、URL=''、
           Definition=XML、Properties=1;
        5. 往 ``GDB_ItemRelationships`` 追加 ``根目录 -> 新图层`` 的
           ``DatasetInFolder`` 关系;
        6. 坐标系不在 ``GDB_SpatialRefs`` 里就追加一行。

        :param name: 图层逻辑名。
        :param fields: 属性字段。元素可以是 :class:`~pyopenfilegdb.GdbField`,
            或 ``{'name','type','length','nullable','required','editable',
            'alias','default'}`` 字典。``type`` 接受 ``FGFT_*`` 数值或
            ``'esriFieldTypeString'`` 这类 ESRI 字符串。
        :param geometry_type: ``'point'`` / ``'polyline'`` / ``'polygon'`` /
            ``'multipoint'`` / ``'multipatch'`` / ``'null'``(默认,表示
            非空间表),也接受 ``'esriGeometryPolygon'`` 与 ``FGTGT_*`` 数值。
        :param spatial_ref: :class:`~pyopenfilegdb.GdbSpatialRef`、WKT 字符串,
            或含 ``wkt``/``wkid``/``latest_wkid`` 及量化参数
            (``x_origin``/``xy_scale``/...)的字典。``None`` 表示用默认的
            WGS84 + ArcGIS 默认量化参数。
        :param has_z: 表级 ``hasZ``。
        :param has_m: 表级 ``hasM``。
        :param oid_field: OBJECTID 字段名。
        :param geometry_field: 几何字段名。
        :param alias_name: 写进 Definition XML 的 ``<AliasName>``。
        :param documentation: 写进 ``GDB_Items.Documentation`` 的 XML。
        :returns: 新建的 :class:`GdbLayer`。
        """
        self._require_update()
        self._check_layer_name(name)

        fgtgt, kind = _normalize_geom_type(geometry_type)
        is_spatial = fgtgt != C.FGTGT_NONE

        if self._layer_exists(name):
            raise GdbWriteError(f'{self.name}: 图层 {name!r} 已存在')

        # --- 1. 物理编号 ---------------------------------------------------
        # GDAL: `1 + oTable.GetTotalRecordCount()`,拿的是 **槽位数**(含
        # 已删除),不是有效行数。
        n_table = 1 + self.system_catalog.table.record_count
        physical = physical_name_for(n_table)
        path = os.path.join(self.path, physical + '.gdbtable')
        if os.path.exists(path):
            raise GdbWriteError(
                f'{self.name}: 物理文件 {physical}.gdbtable 已被占用'
                f'(catalog 与磁盘不同步?)'
            )

        # --- 2. 建表 --------------------------------------------------------
        table_fields = self._build_layer_fields(
            fields, fgtgt, has_z, has_m, oid_field, geometry_field,
            spatial_ref, name)
        table = GdbTable.create(
            path, table_fields,
            table_geom_type=fgtgt,
            has_z=has_z, has_m=has_m,
            strings_are_utf8=False,       # 与 GDAL 默认(CONFIGURATION_KEYWORD 未指定)一致
            creator=None,                 # 与 ArcGIS 头部布局一致
        )
        self._tables[physical] = table

        # --- 3/4/5/6. 登记 --------------------------------------------------
        srs_parts = _quantization_parts(spatial_ref)
        try:
            self._register_in_system_catalog(name)
            layer_guid = self._register_in_items(
                name, physical, is_spatial, fgtgt, geometry_field,
                table_fields, srs_parts, alias_name, documentation,
                has_z=has_z, has_m=has_m)
            self._register_in_item_relationships(layer_guid)
            self._register_spatial_ref(srs_parts)
            table.close()                 # 落盘,之后按需重开
            del self._tables[physical]
        except Exception:
            # 登记失败就把半成品清掉,别留下一个 catalog 里没有的孤儿表
            table.close()
            self._tables.pop(physical, None)
            _unlink_table_files(self.path, physical)
            raise

        # 目录里多了一个 .gdbtable,系统表映射要重扫
        self._system_tables = None
        self._invalidate_views()
        return self.get_layer(name)

    # ----------------------------------------------------------------------
    def _check_layer_name(self, name: str) -> None:
        """图层名校验(比 GDAL 略严:GDAL 只挡重名)。"""
        if not isinstance(name, str) or not name.strip():
            raise GdbWriteError('图层名不能为空')
        if '\\' in name or '/' in name:
            raise GdbWriteError(f'图层名不能含路径分隔符: {name!r}')
        if len(name) > 160:
            raise GdbWriteError('图层名超过 160 字符(GDB_Items.Name 的宽度上限)')
        if name in SYSTEM_TABLE_LOGICAL_NAMES:
            raise GdbWriteError(f'{name!r} 是系统表名,不能用作图层名')

    def _layer_exists(self, name: str) -> bool:
        lowered = name.lower()
        return any(n.lower() == lowered for n in self.list_layers())

    def _build_layer_fields(self, fields: Optional[Sequence[Any]], fgtgt: int,
                            has_z: bool, has_m: bool, oid_field: str,
                            geometry_field: str, spatial_ref: Any,
                            layer_name: str) -> List[GdbField]:
        """按 GDAL 的字段顺序组出完整字段清单。

        GDAL 的调用顺序是"几何字段 -> OBJECTID -> 属性字段",而落盘顺序是
        ``OBJECTID, Shape, ...``(:class:`GdbTable.create` 会把 OID 提到最前)。
        这里直接按最终顺序给,少一次搬移。
        """
        out: List[GdbField] = [GdbField(
            name=oid_field, field_type=C.FGFT_OBJECTID,
            nullable=False, required=True, editable=False)]

        if fgtgt != C.FGTGT_NONE:
            parts = _quantization_parts(spatial_ref)
            out.append(GdbGeomField(
                name=geometry_field, length=0,
                nullable=True, required=True, editable=True, alias='',
                wkt=parts['wkt'],
                # 表级 hasZ/hasM 与几何字段自己的 Z/M 量化参数是两回事;
                # GDAL 在 Create() 里只在 caller 显式给了 z/m 原点时才置位,
                # 这里跟随表级标志,保证文件描述区自洽。
                has_z_origin_scale_tolerance=bool(has_z),
                has_m_origin_scale_tolerance=bool(has_m),
                x_origin=parts['x_origin'], y_origin=parts['y_origin'],
                xy_scale=parts['xy_scale'], xy_tolerance=parts['xy_tolerance'],
                m_origin=parts['m_origin'], m_scale=parts['m_scale'],
                m_tolerance=parts['m_tolerance'],
                z_origin=parts['z_origin'], z_scale=parts['z_scale'],
                z_tolerance=parts['z_tolerance'],
                # 新建表还没有任何几何,范围写 ESRI 的 NaN(不是 Python 的
                # float('nan') —— 两者差最低位,见 _constants.ESRI_NAN)
                xmin=C.ESRI_NAN, ymin=C.ESRI_NAN,
                xmax=C.ESRI_NAN, ymax=C.ESRI_NAN,
                spatial_index_grid_resolutions=list(DEFAULT_GRID_RESOLUTIONS),
            ))

        seen = {f.name.lower() for f in out}
        for spec in (fields or ()):
            f = _coerce_field(spec)
            if f.name.lower() in seen:
                raise GdbWriteError(
                    f'图层 {layer_name!r}: 字段名 {f.name!r} 重复'
                )
            if f.is_oid or f.is_geometry:
                raise GdbWriteError(
                    f'图层 {layer_name!r}: 不要在 fields 里手写 '
                    f'OBJECTID/几何字段(用 oid_field / geometry_field 参数)'
                )
            seen.add(f.name.lower())
            out.append(f)
        return out

    # ----------------------------------------------------------------------
    def _register_in_system_catalog(self, layer_name: str) -> None:
        """往 GDB_SystemCatalog 追加 ``(Name, FileFormat=0)``。

        对应 ``RegisterLayerInSystemCatalog``
        (ogropenfilegdbdatasource_write.cpp:145)。
        """
        table = self._system_table('GDB_SystemCatalog', write=True)
        table.append_feature(GdbFeature(oid=0, attributes={
            'Name': layer_name,
            'FileFormat': 0,
        }))
        table.sync()
        self._invalidate_views()

    def _register_in_items(self, name: str, physical: str, is_spatial: bool,
                           fgtgt: int, geometry_field: str,
                           table_fields: List[GdbField],
                           srs_parts: Dict[str, Any], alias_name: str,
                           documentation: str, has_z: bool = False,
                           has_m: bool = False) -> str:
        """往 GDB_Items 追加一条 item,返回新生成的 item GUID。

        对应 ``RegisterFeatureClassInItems`` / ``RegisterASpatialTableInItems``
        (ogropenfilegdbdatasource_write.cpp:459 / 508)。

        ``PhysicalName`` 这里写的是 **真实物理表名**(``a00000009``),与实测
        的真实 ArcGIS 文件一致。注意 GDAL 写的是"图层名的大写" —— 那是个
        占位值(GDAL 的读路径根本不看 ``PhysicalName``,它靠
        ``GDB_SystemCatalog`` 定位文件),两边的做法都能被对方正常读取。
        """
        table = self._system_table('GDB_Items', write=True)
        # DSID = 1 + GDB_Items 当前槽位数(GDAL 用 GetTotalRecordCount())
        dsid = 1 + table.record_count

        layer_guid = generate_uuid()
        fields_for_xml = _xml_field_list(table_fields)
        # GDAL 的 TARGET_ARCGIS_VERSION 默认不是 Pro 3.2+:命名空间用 10.3。
        # 但只要用了 Pro 3.2 才有的字段类型(INT64/DATE/TIME/带偏移的
        # DATETIME),GDAL 就会把 m_bArcGISPro32OrLater 置真
        # (ogropenfilegdblayer.cpp:641-653),命名空间随之变成 10.8 —— 这里
        # 照抄同样的开关逻辑,保证写出的 XML 与字段类型自洽。
        pro = any(f.field_type in (C.FGFT_INT64, C.FGFT_DATE, C.FGFT_TIME,
                                   C.FGFT_DATETIME_WITH_OFFSET)
                  for f in table_fields)

        if is_spatial:
            definition = make_feature_class_xml(
                name, fields_for_xml, srs_parts,
                _FGTGT_TO_ESRI_GEOM[fgtgt],
                has_z=has_z, has_m=has_m,
                oid_field=_oid_name(table_fields),
                shape_field=geometry_field,
                catalog_path='\\' + name,
                alias_name=alias_name,
                dsid=dsid,
                shape_nullable=True,
                area_field_name='', length_field_name='',
                pro=pro,
            )
        else:
            definition = make_table_xml(
                name, fields_for_xml,
                oid_field=_oid_name(table_fields),
                catalog_path='\\' + name,
                alias_name=alias_name,
                dsid=dsid,
                pro=pro,
            )

        attrs: Dict[str, Any] = {
            'UUID': layer_guid,
            'Type': item_uuid('Feature Class' if is_spatial else 'Table'),
            'Name': name,
            'PhysicalName': physical,
            'Path': '\\' + name,
            'URL': '',
            'Definition': definition,
            'Documentation': documentation or None,
            'Properties': 1,
        }
        if is_spatial:
            # DatasetSubtype1 恒为 1;DatasetSubtype2 是表级几何类型;
            # DatasetInfo1 是几何字段名(见 RegisterFeatureClassInItems)。
            attrs['DatasetSubtype1'] = 1
            attrs['DatasetSubtype2'] = fgtgt
            attrs['DatasetInfo1'] = geometry_field

        table.append_feature(GdbFeature(oid=0, attributes=attrs))
        table.sync()
        self._invalidate_views()
        return layer_guid

    def _register_in_item_relationships(self, layer_guid: str) -> None:
        """往 GDB_ItemRelationships 追加"根目录包含该图层"的关系。

        对应 ``RegisterInItemRelationships`` +
        ``OGROpenFileGDBLayer::RegisterTable``
        (ogropenfilegdbdatasource_write.cpp:164 / ogropenfilegdblayer_write.cpp:2803)。
        关系类型是 ``DatasetInFolder``;方向是 ``根条目 -> 图层条目``。
        """
        root_guid = self.root_guid()
        if not root_guid:
            raise GdbWriteError(
                f'{self.name}: GDB_Items 里找不到根条目(Path == "\\\\"),'
                f'无法登记图层关系'
            )
        table = self._system_table('GDB_ItemRelationships', write=True)
        table.append_feature(GdbFeature(oid=0, attributes={
            'UUID': generate_uuid(),
            'OriginID': root_guid,
            'DestID': layer_guid,
            'Type': relationship_uuid('DatasetInFolder'),
            'Properties': 1,
        }))
        table.sync()
        self._invalidate_views()

    def root_guid(self) -> str:
        """根条目的 GUID(``GDB_Items`` 里 ``Path == '\\\\'`` 那一行的 UUID)。

        对应 GDAL ``OGROpenFileGDBDataSource::Open`` 里
        ``if (strcmp(pszPath, "\\\\") == 0) m_osRootGUID = psUUID->String;``
        (ogropenfilegdbdatasource.cpp:731)。
        """
        items = self.items
        if items is None:
            return ''
        for item in items:
            if item.path == '\\' or (not item.path and not item.name):
                return item.uuid
        return ''

    def _register_spatial_ref(self, srs_parts: Dict[str, Any]) -> None:
        """坐标系没登记过就追加一行。

        对应 ``GetExistingSpatialRef`` / ``AddNewSpatialRef``
        (ogropenfilegdbdatasource_write.cpp:42 / 101):先全表找 SRTEXT 相同
        且 11 个量化参数 **全部相等** 的行,找到就不写。
        """
        wkt = srs_parts.get('wkt') or ''
        if not wkt:
            return
        table = self._system_table('GDB_SpatialRefs', write=True)

        numeric = ('FalseX', 'FalseY', 'XYUnits', 'FalseZ', 'ZUnits',
                   'FalseM', 'MUnits', 'XYTolerance', 'ZTolerance',
                   'MTolerance')
        wanted = [
            srs_parts.get('x_origin'), srs_parts.get('y_origin'),
            srs_parts.get('xy_scale'), srs_parts.get('z_origin'),
            srs_parts.get('z_scale'), srs_parts.get('m_origin'),
            srs_parts.get('m_scale'), srs_parts.get('xy_tolerance'),
            srs_parts.get('z_tolerance'), srs_parts.get('m_tolerance'),
        ]
        for _row, values in table.iter_rows():
            if values.get('SRTEXT') != wkt:
                continue
            got = [values.get(name) for name in numeric]
            if all(a is not None and b is not None and float(a) == float(b)
                   for a, b in zip(got, wanted)):
                return

        attrs: Dict[str, Any] = {'SRTEXT': wkt}
        attrs.update(dict(zip(numeric, [float(v) for v in wanted])))
        table.append_feature(GdbFeature(oid=0, attributes=attrs))
        table.sync()
        self._invalidate_views()

    # ======================================================================
    # 删图层
    # ======================================================================
    def delete_layer(self, name: str) -> None:
        """删除一个图层:摘掉所有登记项,再删掉该表的全部磁盘文件。

        对应 GDAL ``OGROpenFileGDBDataSource::DeleteLayer``
        (ogropenfilegdbdatasource_write.cpp:1381):

        1. 从 ``GDB_SystemCatalog`` 删掉那行;
        2. 从 ``GDB_Items`` 删掉那行(顺便拿到它的 GUID);
        3. 从 ``GDB_ItemRelationships`` 删掉 OriginID/DestID 命中该 GUID 的行;
        4. 删掉目录里所有以物理名为前缀的文件(``.gdbtable`` /
           ``.gdbtablx`` / ``.atx`` / ``.spx`` / ``.freelist`` /
           ``.gdbindexes``)。

        .. warning::
            **第 1 步是真正的删除,不可撤销。** GDAL 同样用"逻辑删除 catalog
            行"来达到目的,所以这里跟随:被删的物理槽位不会被回收,后续
            新图层拿到的编号只会往后走(本库不维护 ``.freelist``)。
        """
        self._require_update()
        layer = self.get_layer(name)
        physical = layer.physical_name
        guid = layer.item.uuid if layer.item is not None else ''

        # 1. GDB_SystemCatalog
        self._delete_rows('GDB_SystemCatalog',
                          lambda v: v.get('Name') == layer.name)
        # 2. GDB_Items
        if not guid:
            items_physical = self._physical_for('GDB_Items') or ''
            for row, values in self._open_table(items_physical).iter_rows():
                if values.get('Name') == layer.name:
                    guid = values.get('UUID') or ''
                    break
        self._delete_rows('GDB_Items',
                          lambda v: v.get('Name') == layer.name)
        # 3. GDB_ItemRelationships
        if guid:
            self._delete_rows(
                'GDB_ItemRelationships',
                lambda v: (uuid_eq(v.get('OriginID'), guid)
                           or uuid_eq(v.get('DestID'), guid)))

        # 4. 文件
        self._close_table(physical)
        self._layers.pop(name, None)
        self._definitions.pop(physical, None)
        _unlink_table_files(self.path, physical)

    def _delete_rows(self, logical: str, predicate: Any) -> int:
        """把系统表里满足条件的行做逻辑删除,返回删掉的行数。

        删完必须 :meth:`_invalidate_views` —— :class:`GdbSystemCatalog` /
        :class:`GdbItems` 会把行列表缓存在实例里,不丢掉的话调用方还会从
        旧快照里读到已经删掉的行。
        """
        table = self._system_table(logical, write=True)
        victims = [row for row, values in table.iter_rows()
                   if predicate(values)]
        for row in victims:
            table.delete_feature(row + 1)
        if victims:
            table.sync()
            self._invalidate_views()
        return len(victims)

    def _close_table(self, physical: str) -> None:
        table = self._tables.pop(physical, None)
        if table is not None:
            try:
                table.close()
            except Exception:
                pass

    # ======================================================================
    def __repr__(self) -> str:  # pragma: no cover - 调试用
        if self._closed:
            return f'<OpenFileGDB {self.name} (closed)>'
        return (f'<OpenFileGDB {self.name} v{self.version} '
                f'layers={len(self.list_layers())}'
                f'{" update" if self.update else ""}>')


# ============================================================================
# 建库辅助
# ============================================================================
def _write_template_table(gdb_dir: str, spec: Dict[str, Any]) -> None:
    """按 :mod:`._gdb_template` 的一条样板建一张系统表。

    样板的 ``fields`` 元素是
    ``(名字, FGFT, nullable, required, editable, maxWidth, geom_params)``,
    ``geom_params`` 仅几何字段非 ``None``。
    """
    fields = [_build_template_field(fs) for fs in spec['fields']]
    table = GdbTable.create(
        os.path.join(gdb_dir, spec['name'] + '.gdbtable'),
        fields,
        table_geom_type=spec['geom_type'],
        has_z=spec['has_z'],
        has_m=spec['has_m'],
        strings_are_utf8=spec['strings_are_utf8'],
        creator=None,
    )
    attrs_names = [fs[0] for fs in spec['fields']]
    for row in spec['rows']:
        attrs: Dict[str, Any] = {}
        for fname, fs, value in zip(attrs_names, spec['fields'], row):
            ftype = fs[1]
            if ftype in (C.FGFT_OBJECTID, C.FGFT_GEOMETRY):
                continue        # OID 由行号推导;样板行的几何一律为空
            attrs[fname] = _esri_nanify(value)
        table.append_feature(GdbFeature(oid=0, attributes=attrs, geometry=None))
    table.close()


def _build_template_field(spec: Tuple[Any, ...]) -> GdbField:
    """:mod:`._gdb_template` 的字段元组 -> :class:`GdbField`。"""
    name, ftype, nullable, required, editable, width, gp = spec
    if gp is None:
        return GdbField(name=name, field_type=ftype, length=width,
                        nullable=nullable, required=required,
                        editable=editable, alias='')
    (wkt, xo, yo, xys, xyt, has_m, mo, ms, mt, has_z, zo, zs, zt,
     xmin, ymin, xmax, ymax, grids) = gp
    return GdbGeomField(
        name=name, length=0, nullable=nullable, required=required,
        editable=editable, alias='',
        wkt=wkt,
        has_m_origin_scale_tolerance=has_m,
        has_z_origin_scale_tolerance=has_z,
        x_origin=xo, y_origin=yo, xy_scale=xys, xy_tolerance=xyt,
        m_origin=mo, m_scale=ms, m_tolerance=mt,
        z_origin=zo, z_scale=zs, z_tolerance=zt,
        xmin=_esri_nanify(xmin), ymin=_esri_nanify(ymin),
        xmax=_esri_nanify(xmax), ymax=_esri_nanify(ymax),
        spatial_index_grid_resolutions=list(grids or ()),
    )


def _esri_nanify(value: Any) -> Any:
    """把样板里的 Python NaN 换成 ESRI 的 NaN 位模式。

    :mod:`._gdb_template` 用 ``float('nan')`` 表示"没有范围",那是 Python 的
    规范 quiet NaN(载荷 0);ArcGIS 写的是载荷最低位为 1 的那个,两者差一位,
    会让文件与 ArcGIS 不再逐字节相同。见 :data:`._constants.ESRI_NAN`。
    """
    if isinstance(value, float) and value != value:
        return C.ESRI_NAN
    return value


def _unlink_table_files(gdb_dir: str, physical: str) -> List[str]:
    """删掉目录里所有以 ``physical`` 开头的文件,返回删掉的文件名。

    对应 GDAL ``DeleteLayer`` 结尾那段 ``VSIReadDir`` + ``STARTS_WITH`` +
    ``VSIUnlink``。
    """
    removed: List[str] = []
    prefix = physical.lower()
    for entry in sorted(os.listdir(gdb_dir)):
        if entry.lower().startswith(prefix):
            full = os.path.join(gdb_dir, entry)
            if os.path.isfile(full):
                os.remove(full)
                removed.append(entry)
    return removed


# ============================================================================
# 字段 / 几何类型归一化
# ============================================================================
def _coerce_field(spec: Any) -> GdbField:
    """把 ``GdbField`` / 字典 / 三元组归一成 :class:`GdbField`。

    ``type`` 接受 ``FGFT_*`` 数值或 ESRI 字段类型字符串
    (``'esriFieldTypeString'`` / ``'String'`` / ``'str'`` ...)。
    """
    if isinstance(spec, GdbField):
        return spec

    if isinstance(spec, dict):
        name = spec.get('name') or spec.get('field_name') or ''
        ftype = _coerce_field_type(spec.get('type', spec.get('field_type')))
        length = int(spec.get('length') or 0)
        nullable = bool(spec.get('nullable', True))
        required = bool(spec.get('required', False))
        editable = bool(spec.get('editable', True))
        alias = spec.get('alias', spec.get('alias_name', '')) or ''
        default = spec.get('default', spec.get('default_value'))
    elif isinstance(spec, (tuple, list)) and len(spec) >= 2:
        name, ftype = spec[0], _coerce_field_type(spec[1])
        length = int(spec[2]) if len(spec) > 2 and spec[2] is not None else 0
        nullable = bool(spec[3]) if len(spec) > 3 else True
        required = bool(spec[4]) if len(spec) > 4 else False
        editable = bool(spec[5]) if len(spec) > 5 else True
        alias = spec[6] if len(spec) > 6 else ''
        default = spec[7] if len(spec) > 7 else None
    else:
        raise GdbWriteError(
            f'无法识别的字段定义 {spec!r};请给 GdbField、dict 或元组'
        )

    if not name:
        raise GdbWriteError(f'字段定义缺少名字: {spec!r}')
    if ftype == C.FGFT_STRING and length <= 0:
        length = C.DEFAULT_STRING_LENGTH
    return GdbField(name=str(name), field_type=ftype, length=length,
                    nullable=nullable, required=required,
                    editable=editable, alias=str(alias or ''),
                    default_value=default)


def _coerce_field_type(value: Any) -> int:
    """字段类型 -> ``FGFT_*``。"""
    if value is None:
        return C.FGFT_STRING
    if isinstance(value, int) and value in _FGFT_VALUES:
        return value
    if isinstance(value, str):
        key = value.strip().lower()
        if key in _FIELD_TYPE_ALIASES:
            return _FIELD_TYPE_ALIASES[key]
        from ._system_catalog import fgft_field_type
        fgft = fgft_field_type(value)
        if fgft != C.FGFT_UNDEFINED:
            return fgft
    raise GdbWriteError(f'无法识别的字段类型 {value!r}')


#: 合法的 ``FGFT_*`` 取值集合(用于把 int 与字符串区分开)。
_FGFT_VALUES = frozenset({
    C.FGFT_INT16, C.FGFT_INT32, C.FGFT_FLOAT32, C.FGFT_FLOAT64,
    C.FGFT_STRING, C.FGFT_DATETIME, C.FGFT_OBJECTID, C.FGFT_GEOMETRY,
    C.FGFT_BINARY, C.FGFT_RASTER, C.FGFT_GUID, C.FGFT_GLOBALID,
    C.FGFT_XML, C.FGFT_INT64, C.FGFT_DATE, C.FGFT_TIME,
    C.FGFT_DATETIME_WITH_OFFSET,
})

#: 常用简写 -> ``FGFT_*``(ESRI 全名由 ``fgft_field_type`` 处理)。
_FIELD_TYPE_ALIASES: Dict[str, int] = {
    'short': C.FGFT_INT16, 'smallinteger': C.FGFT_INT16, 'int16': C.FGFT_INT16,
    'int': C.FGFT_INT32, 'integer': C.FGFT_INT32, 'long': C.FGFT_INT32,
    'int32': C.FGFT_INT32,
    'bigint': C.FGFT_INT64, 'int64': C.FGFT_INT64,
    'float': C.FGFT_FLOAT64, 'double': C.FGFT_FLOAT64, 'real': C.FGFT_FLOAT64,
    'float64': C.FGFT_FLOAT64,
    'single': C.FGFT_FLOAT32, 'float32': C.FGFT_FLOAT32,
    'str': C.FGFT_STRING, 'text': C.FGFT_STRING, 'string': C.FGFT_STRING,
    'date': C.FGFT_DATETIME, 'datetime': C.FGFT_DATETIME,
    'dateonly': C.FGFT_DATE, 'timeonly': C.FGFT_TIME,
    'blob': C.FGFT_BINARY, 'binary': C.FGFT_BINARY,
    'guid': C.FGFT_GUID, 'globalid': C.FGFT_GLOBALID,
    'xml': C.FGFT_XML,
}


def _normalize_geom_type(value: Any) -> Tuple[int, str]:
    """几何类型 -> ``(FGTGT_*, 'feature_class'|'table')``。"""
    if value is None:
        return C.FGTGT_NONE, 'table'
    if isinstance(value, bool):
        raise GdbWriteError(f'无法识别的几何类型 {value!r}')
    if isinstance(value, int):
        if value in C.VALID_TABLE_GEOM_TYPES:
            return value, 'feature_class'
        if value == C.FGTGT_NONE:
            return C.FGTGT_NONE, 'table'
        raise GdbWriteError(f'不是合法的表级几何类型(FGTGT_*): {value}')
    if isinstance(value, str):
        key = value.strip().lower().replace('_', '').replace(' ', '')
        if key in _GEOM_TYPE_NAMES:
            fgtgt = _GEOM_TYPE_NAMES[key]
            return fgtgt, ('table' if fgtgt == C.FGTGT_NONE else 'feature_class')
    raise GdbWriteError(
        f'无法识别的几何类型 {value!r};'
        f"可用: 'point'/'polyline'/'polygon'/'multipoint'/'multipatch'/'null'"
    )


def _oid_name(fields: Iterable[GdbField]) -> str:
    """从字段清单里取 OBJECTID 字段名。"""
    for f in fields:
        if f.is_oid:
            return f.name
    return 'OBJECTID'


def _xml_field_list(fields: Sequence[GdbField]) -> List[Dict[str, Any]]:
    """:class:`GdbField` 列表 -> ``make_*_xml`` 认得的字典列表。"""
    out: List[Dict[str, Any]] = []
    for f in fields:
        out.append({
            'name': f.name,
            'fgft': f.field_type,
            'length': f.length,
            'nullable': f.nullable,
            'required': f.required,
            'editable': f.editable,
            'alias': f.alias,
            'default': f.default_value,
        })
    return out


def _quantization_parts(spatial_ref: Any) -> Dict[str, Any]:
    """空间参考入参 -> 既给字段描述区、也给 Definition XML 用的一套参数。

    返回的键与 :func:`._system_catalog._spatial_ref_parts` 兼容(那一步会被
    ``make_feature_class_xml`` 再走一遍),所以这里顺带把量化参数也塞进去,
    保证 XML 里写的 ``<XOrigin>`` 与字段描述区里的值一致 —— ArcGIS 就是
    这么做的(实测 ``项目红线`` 的 XML 里 ``XYScale`` 与描述区相符)。
    """
    parts = dict(DEFAULT_QUANTIZATION)
    parts.update({'wkt': DEFAULT_WKT_WGS84, 'wkid': DEFAULT_WKID_WGS84,
                  'latest_wkid': DEFAULT_WKID_WGS84})

    if spatial_ref is None:
        return parts
    if isinstance(spatial_ref, GdbSpatialRef):
        parts['wkt'] = spatial_ref.wkt or DEFAULT_WKT_WGS84
        parts['wkid'] = spatial_ref.wkid or 0
        parts['latest_wkid'] = spatial_ref.latest_wkid or spatial_ref.wkid or 0
        return parts
    if isinstance(spatial_ref, str):
        parts['wkt'] = spatial_ref
        parts['wkid'] = 0
        parts['latest_wkid'] = 0
        return parts
    if isinstance(spatial_ref, dict):
        for key, value in spatial_ref.items():
            if value is not None and key in parts:
                parts[key] = value
        if 'wkid' in spatial_ref and 'latest_wkid' not in spatial_ref:
            parts['latest_wkid'] = spatial_ref['wkid'] or 0
        if not parts.get('wkt'):
            parts['wkt'] = DEFAULT_WKT_WGS84
        return parts
    raise GdbWriteError(f'无法识别的空间参考: {spatial_ref!r}')
