"""``GdbLayer`` —— 一个要素类 / 表的读写门面。

本模块把三块底层能力缝在一起:

* :mod:`._gdbtable` —— 记录体的解码/编码(字段值、空值位图、几何 blob)
* :mod:`._esri_geometry` —— Esri 几何的压缩点串编解码
* :mod:`._system_catalog` —— ``GDB_Items`` 里的 Definition XML(坐标系、
  别名、范围等元信息)

GDAL 里对应 ``OGROpenFileGDBLayer``
(``ogr/ogrsf_frmts/openfilegdb/ogropenfilegdblayer.cpp`` 及
``ogropenfilegdblayer_write.cpp``)。与 GDAL 的一个重要差别:GDAL 的图层
必须在 ``bUpdate`` 的 datasource 上才能写,本库的 :class:`GdbLayer` 自己
记住"这张表是不是以可写方式打开的",只读打开的表调用写方法会抛
:class:`GdbWriteError`,不会悄悄失败。
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from . import _constants as C
from ._datatypes import (
    GdbFeature,
    GdbField,
    GdbFormatError,
    GdbGeomField,
    GdbNotFoundError,
    GdbSpatialRef,
    GdbWriteError,
)
from .geometry import Geometry
from ._gdbtable import GdbTable

__all__ = ['GdbLayer']


class GdbLayer:
    """一个要素类(或非空间表)的读写接口。

    通常由 :meth:`pyopenfilegdb.OpenFileGDB.get_layer` / ``create_layer()``
    返回,不直接构造。

    :param datasource: 所属的 :class:`~pyopenfilegdb.core.OpenFileGDB`。
    :param name: 逻辑名(``GDB_SystemCatalog.Name``),例如 ``'道路'``。
    :param table: 已打开的 :class:`~pyopenfilegdb._gdbtable.GdbTable`。
    :param item: 对应的 :class:`~pyopenfilegdb._datatypes.GdbItem`(可为
        ``None`` —— 系统表就没有 item)。
    :param definition: ``GDB_Items.Definition`` 的解析结果(可为 ``None``)。
    """

    def __init__(self, datasource: Any, name: str, table: GdbTable,
                 item: Any = None, definition: Optional[Dict[str, Any]] = None
                 ) -> None:
        self.datasource = datasource
        self.name = name
        self.table = table
        self.item = item
        self.definition: Dict[str, Any] = definition or {}
        self._closed = False

    # ======================================================================
    # 元信息
    # ======================================================================
    @property
    def feature_class_name(self) -> str:
        """要素类的逻辑名(与 :attr:`name` 同值,任务书要求这个拼法)。"""
        return self.name

    @property
    def physical_name(self) -> str:
        """磁盘上的表名,例如 ``'a00000009'``。"""
        return self.table.basename

    @property
    def path(self) -> str:
        """``.gdbtable`` 的完整路径。"""
        return self.table.path

    @property
    def fields(self) -> List[GdbField]:
        """字段清单(含 OBJECTID,**不含**几何字段)。

        与 ``.gdbtable`` 字段描述区的顺序一致。几何字段单独用
        :attr:`geometry_field` / :attr:`geometry_field_name` 拿。
        """
        return [f for f in self.table.fields if not f.is_geometry]

    @property
    def attribute_fields(self) -> List[GdbField]:
        """:attr:`fields` 里去掉 OBJECTID 的部分 —— 即
        :attr:`GdbFeature.attributes` 会出现的键。"""
        return [f for f in self.fields if not f.is_oid]

    @property
    def geometry_field(self) -> Optional[GdbGeomField]:
        """几何字段(非空间表返回 ``None``)。"""
        return self.table.geom_field

    @property
    def geometry_field_name(self) -> str:
        """几何字段名,非空间表返回空串。"""
        gf = self.table.geom_field
        return gf.name if gf is not None else ''

    @property
    def oid_field_name(self) -> str:
        """OBJECTID 字段名;没有 OID 字段的表返回空串。"""
        i = self.table.oid_field_index
        return self.table.fields[i].name if i >= 0 else ''

    @property
    def geometry_type(self) -> str:
        """``point`` / ``polyline`` / ``polygon`` / ``multipoint`` /
        ``multipatch`` / ``null``。

        取自 **表级** 几何类型(字段描述区头部第 8 字节,FGTGT_*),与
        记录里几何 blob 的 shape type 是两套编号 —— 见 :mod:`._constants`。
        """
        return self.table.geometry_type

    @property
    def table_geom_type(self) -> int:
        """表级几何类型的原始 ``FGTGT_*`` 数值。"""
        return self.table.table_geom_type

    @property
    def has_z(self) -> bool:
        """表级 ``hasZ``(字段描述区 nLayerFlags 的 bit31)。"""
        return self.table.has_z

    @property
    def has_m(self) -> bool:
        """表级 ``hasM``(bit30)。"""
        return self.table.has_m

    @property
    def strings_are_utf8(self) -> bool:
        """字符串字段是否按 UTF-8 解码(bit8)。"""
        return self.table.strings_are_utf8

    @property
    def spatial_ref(self) -> GdbSpatialRef:
        """空间参考。

        WKT 来自几何字段描述区;WKID 从 ``GDB_Items.Definition`` 的
        ``<SpatialReference>`` 里取(表里只有 WKT,没有 WKID)。非空间表
        返回空 :class:`GdbSpatialRef`。
        """
        gf = self.table.geom_field
        wkt = gf.wkt if gf is not None else ''
        if not wkt:
            wkt = self.definition.get('wkt', '') or ''
        return GdbSpatialRef(
            wkid=int(self.definition.get('wkid', 0) or 0),
            latest_wkid=int(self.definition.get('latest_wkid', 0) or 0),
            wkt=wkt,
            name=self.definition.get('name', self.name),
        )

    @property
    def is_table(self) -> bool:
        """是否是非空间表(没有几何字段)。"""
        return self.table.geom_field is None

    @property
    def record_count(self) -> int:
        """有效记录数(不含空洞)。"""
        return self.table.valid_record_count

    @property
    def extent(self) -> Optional[Tuple[float, float, float, float]]:
        """全表包围盒 ``(xmin, ymin, xmax, ymax)``。

        优先用几何字段描述区里维护的范围(ArcGIS/GDAL 写入时都会更新它);
        它是 NaN(还没有任何几何)时退回 Definition XML 里的 ``<Extent>``。

        .. warning::
            这是个 **只增不减的上界**,不是"现存要素的真实范围"。ArcGIS 在
            写几何时把新点并进去,删要素或把要素改小时不会收回来 —— 实测有
            库里差到 5 度。想要真实范围就自己遍历一遍
            (``read_features`` 顺手取 min/max)。
        """
        gf = self.table.geom_field
        if gf is not None and not any(
                abs(v) != abs(v) for v in (gf.xmin, gf.ymin, gf.xmax, gf.ymax)):
            return (gf.xmin, gf.ymin, gf.xmax, gf.ymax)
        ext = self.definition.get('extent')
        return tuple(ext) if ext else None   # type: ignore[return-value]

    @property
    def indexes(self) -> List[Any]:
        """这张表的索引描述符列表(:class:`~pyopenfilegdb._gdbindex.GdbIndex`)。"""
        from ._gdbindex import read_gdbindexes
        try:
            return read_gdbindexes(self.table.path, self.oid_field_name)
        except GdbFormatError:
            return []

    @property
    def has_spatial_index(self) -> bool:
        """磁盘上是否存在 ``.spx``。"""
        from ._gdbindex import has_spatial_index
        return has_spatial_index(self.table.path)

    # ======================================================================
    # 读
    # ======================================================================
    def _ensure_readable(self) -> None:
        if self._closed:
            raise GdbWriteError(f'图层 {self.name!r} 已关闭')

    def _flush_pending(self) -> None:
        """读之前把挂起的写落盘,好让读路径看到最新内容。

        ``iter_rows()`` 会另开一个只读句柄顺序扫描(记录体从那个句柄读,
        行偏移取自内存里的 ``tablx``),所以:

        * 有挂起改动 -> 先 :meth:`GdbTable.sync`(记录体字节 + 表头 + 索引
          一次写清)。写要素本身不再逐条落盘,这一句是"同一图层写完立刻读
          得到"的保证;GDAL 那边靠同一个 ``FileGDBTable`` 对象共享句柄天然
          如此。
        * 没挂起改动 -> 只 flush 一下写句柄,零代价。

        ⚠️ 它管的是**同一个图层对象**。没 sync 之前用另一个
        ``OpenFileGDB`` 句柄(或另一个进程)打开同一个库,读到的仍是旧表头/旧
        索引 —— 这一点与 GDAL 相同。
        """
        table = self.table
        if getattr(table, '_dirty', False) or getattr(table, '_dirty_geom_bbox', False):
            table.sync()
            return
        fp = getattr(table, '_fp', None)
        if fp is not None:
            fp.flush()

    def read_features(self, where: Any = None, bbox: Any = None,
                      limit: Optional[int] = None, offset: int = 0,
                      fields: Optional[Sequence[str]] = None
                      ) -> Iterator[GdbFeature]:
        """顺序扫描,产出 :class:`GdbFeature`。

        索引(``.atx`` / ``.spx``)在本库里只作元信息暴露,查询一律走全表
        扫描 —— 见 :mod:`._gdbindex` 的说明。结果与用索引完全相同,只是
        大表上慢一些。

        :param where: 属性过滤。三种写法:

            * ``None``:不过滤。
            * 可调用对象 ``f(feature) -> bool``:直接当谓词用,最灵活。
            * 字符串:一个 **SQL 子集**(语法见 :class:`_WhereParser`),
              例如 ``"POP > 1000 AND NAME LIKE 'A%'"``、
              ``"TYPE IN ('a','b') OR POP IS NULL"``。

        :param bbox: ``(xmin, ymin, xmax, ymax)``,按 **几何自身** 的包围盒
            做相交判断(不是全表范围)。
        :param limit: 最多产出多少条。
        :param offset: 先跳过多少条(在过滤之后计数)。
        :param fields: 只要这些属性字段(几何与 OID 永远保留)。``None``
            表示全部。

        :raises GdbWriteError: 图层已关闭。
        """
        self._ensure_readable()
        predicate = _make_predicate(where, self.table)
        self._flush_pending()

        wanted = None
        if fields is not None:
            wanted = {n.lower() for n in fields}

        emitted = 0
        skipped = 0
        for feature in self.table.iter_features():
            if predicate is not None and predicate(feature) is not True:
                continue
            if bbox is not None:
                # 两道筛:先用几何 blob 自带的存储包围盒粗筛(不用解点数组,
                # 而多边形动辄几万个顶点),过了再做逐点的精确判断。
                # 这正是 GDAL 空间过滤的做法 —— 粗筛拿到的包围盒已经放宽过
                # 一个量化步长,是真实范围的超集,所以排除掉的必定不相交,
                # 不会错杀(见 _esri_geometry.peek_envelope)。
                env = feature.stored_envelope()
                if env is not None and not _bbox_intersects(env, bbox):
                    continue
                gbox = _geometry_bbox(feature.geometry)
                if gbox is None or not _bbox_intersects(gbox, bbox):
                    continue
            if skipped < offset:
                skipped += 1
                continue
            if wanted is not None:
                feature.attributes = {
                    k: v for k, v in feature.attributes.items()
                    if k.lower() in wanted
                }
            yield feature
            emitted += 1
            if limit is not None and emitted >= limit:
                return

    def read_feature(self, oid: int) -> Optional[GdbFeature]:
        """按 OID 读一条;该槽为空/已删除时返回 ``None``。

        OID 从 1 开始,对应 ``.gdbtablx`` 里第 ``oid - 1`` 个槽位。
        返回的 :attr:`GdbFeature.attributes` 不含几何与 OBJECTID(后者就是
        ``oid`` 本身)。
        """
        self._ensure_readable()
        self._flush_pending()
        if oid <= 0:
            return None
        values = self.table.read_row(oid - 1)
        if values is None:
            return None
        return self.table._values_to_feature(oid, values)

    def __iter__(self) -> Iterator[GdbFeature]:
        return self.read_features()

    def __len__(self) -> int:
        return self.record_count

    # ======================================================================
    # 写
    # ======================================================================
    def _ensure_writable(self) -> None:
        self._ensure_readable()
        if self.table._fp is None:
            raise GdbWriteError(
                f'图层 {self.name!r} 是以只读方式打开的;'
                f'请用 OpenFileGDB.open(path, update=True) 或 create_layer()'
            )

    def write_feature(self, feature: Any) -> int:
        """追加一条要素,返回新 OID(从 1 开始)。

        对应 GDAL ``OGROpenFileGDBLayer::ICreateFeature`` 与
        ``FileGDBTable::CreateFeature``。

        :param feature: :class:`GdbFeature`,或 ``{'FIELD': value, ...}``
            这样的字典(此时用 :attr:`geometry_field_name` 作为几何键)。
            字典里没提到的字段按字段定义取默认值;可空字段落空值。
            ⚠️ ``attributes`` 里**不在字段定义里**的键(名字写错、
            或把 ``OBJECTID`` 塞进来)会被**静默忽略** —— 不报错,等于没写。
        :returns: 新要素的 OID。

        .. note::
            OID **不写进记录体**。FileGDB 靠 ``.gdbtablx`` 的槽位下标隐式
            表示 OID(= 下标 + 1),所以新要素的 OID 恒为"当前槽位数 + 1",
            与 :class:`GdbFeature` 里填的 ``oid`` 无关。

        .. note::
            传 **:class:`GdbFeature`** 时新 OID 会**写回** ``feature.oid``,
            所以 ``oid = layer.write_feature(feat)`` 之后可以直接
            ``layer.update_feature(feat)``。这是照 GDAL 做的 ——
            ``OGROpenFileGDBLayer::ICreateFeature``
            (``ogr/ogrsf_frmts/openfilegdb/ogropenfilegdblayer_write.cpp``)
            结尾就是 ``poFeature->SetFID(nFID32Bit)``。
            传 dict 时不回写(临时对象),取返回值即可。
            ⚠️ OID 在 ``.oid`` 上,**不在** ``attributes`` 里(等价于 OGR 的
            FID,读回来的 ``attributes`` 本来就不含 OBJECTID 字段)。
        """
        self._ensure_writable()
        feat = self._coerce_feature(feature)
        self._check_geometry(feat.geometry)
        oid = self.table.append_feature(feat)
        # ⚠️ 这里**不** sync。写路径只做两件事:写记录体 + 就地把这一行的索引
        # 覆盖掉(O(1)),头部/索引头/trailer 攒到落盘点 —— 与 GDAL
        # ``FileGDBTable::CreateFeature`` 只置 ``m_bDirty*`` 一致
        # (``filegdbtable_write.cpp:1769``)。落盘点见 :meth:`sync`。
        if isinstance(feature, GdbFeature):
            feature.oid = oid
        self.table.sync()   # MUTATION-M1
        return oid

    def update_feature(self, feature: Any) -> None:
        """就地更新 :attr:`GdbFeature.oid` 指向的那条要素。

        对应 GDAL ``OGROpenFileGDBLayer::ISetFeature`` /
        ``FileGDBTable::UpdateFeature``:新记录体不比旧的大时原地覆盖,
        变大时追加到文件末尾并把旧槽标成已删除(长度字取负)。两种情况
        对读取方都是透明的。

        .. note::
            这是 **整条记录替换**(与 ``ISetFeature`` 相同):记录体的每个
            非 OID 字段都从 ``feature.attributes`` 取,里面没有的字段按字段
            定义取默认值(可空字段落空值)。所以**只改一两个字段**要先读回
            再改 —— ``feat = layer.read_feature(oid)`` 拿到的是完整的一条
            (OID 也在 ``feat.oid`` 上),改完直接传回来::

                feat = layer.read_feature(3)
                feat.attributes['POP'] = 301
                layer.update_feature(feat)

            自己 new 一个只填了几个字段的 :class:`GdbFeature` 去 update,
            其余字段会被写成默认值/NULL。**OID 认的是
            :attr:`GdbFeature.oid`,写进 ``attributes['OBJECTID']`` 不算**
            (本库读回来的 ``attributes`` 也不含 OID 字段)。
            :meth:`write_feature` 会把新 OID 写回要素对象,可以接力用。

        :raises GdbWriteError: 图层只读,或该 OID 不存在/已删除,
            或要素对象没带 OID。
        """
        self._ensure_writable()
        feat = self._coerce_feature(feature)
        if feat.oid <= 0:
            raise GdbWriteError(
                f'update_feature 需要要素带 OID(当前 oid = {feat.oid});'
                f'新增请用 write_feature。OID 在 GdbFeature.oid 上,'
                f'不在 attributes 里(write_feature 会把新 OID 写回要素),'
                f'或者先 read_feature(oid) 读回来的就是带 OID 的完整一条'
            )
        self._check_geometry(feat.geometry)
        self.table.update_feature(feat)

    def delete_feature(self, oid: int) -> None:
        """逻辑删除 OID 对应要素。

        对应 GDAL ``OGROpenFileGDBLayer::IDeleteFeature``。做法与
        ``FileGDBTable::DeleteFeature`` 一致:``.gdbtablx`` 槽位清 0,
        ``.gdbtable`` 里该记录的长度字取负 —— 两个标记任一都能让读取方
        判定该槽为空。**不回收空间**:本库不维护 ``.freelist``,理由见
        :class:`~pyopenfilegdb._gdbindex.GdbFreelist` 的文档。

        删除不存在的 OID 是静默无操作(与 GDAL 相同)。

        ``.gdbtablx`` 里的 0 是就地写进去的(``FileGDBTable::DeleteFeature``
        里的 ``WriteFeatureOffset(0)``),与另外两个写方法一样**不**当场 sync。
        """
        self._ensure_writable()
        self.table.delete_feature(oid)

    # ----------------------------------------------------------------------
    def _coerce_feature(self, feature: Any) -> GdbFeature:
        """把 dict / GdbFeature 归一成 :class:`GdbFeature`。"""
        if isinstance(feature, GdbFeature):
            return feature
        if isinstance(feature, dict):
            geom = feature.get(self.geometry_field_name)
            oid = feature.get('oid', feature.get(self.oid_field_name or 'OID', 0))
            attrs = {
                k: v for k, v in feature.items()
                if k not in (self.geometry_field_name, 'oid', 'geometry')
                and not (self.oid_field_name and k == self.oid_field_name)
            }
            if 'geometry' in feature:
                geom = feature['geometry']
            return GdbFeature(oid=int(oid or 0), attributes=attrs, geometry=geom)
        raise GdbWriteError(
            f'要素必须是 GdbFeature 或 dict,实得 {type(feature).__name__}'
        )

    def _check_geometry(self, geom: Optional[Geometry]) -> None:
        """几何种类与图层声明是否相容。

        只做"族"级别的检查(点/线/面/多点),而且 **单向放宽**:

        * 多点图层收单个 ``POINT`` 放行 —— 一个点就是一个"只含一个点的
          多点",没有信息损失,ArcGIS 自己也这么存;
        * point 图层收 ``MULTIPOINT`` 拒绝 —— 那会丢掉"这是个多点"的事实。

        检查的是 :attr:`Geometry.kind`(族),不比较 Z/M 维数:WriteFeature
        时维数已经由几何自身的 ``has_z``/``has_m`` 决定,而字段声明里的维数
        只影响量化参数。
        """
        if geom is None or geom.is_empty:
            return
        layer_kind = self.geometry_type
        if layer_kind in ('null', 'unknown', 'multipatch'):
            return
        kind = geom.kind
        if kind == layer_kind or kind == 'null':
            return
        if kind == 'point' and layer_kind == 'multipoint':
            return
        raise GdbWriteError(
            f'图层 {self.name!r} 的几何类型是 {layer_kind},'
            f'不能写入 {kind} 几何'
        )

    # ======================================================================
    def sync(self) -> None:
        """把挂起的改动刷到磁盘(不关闭)。

        对应 GDAL ``OGROpenFileGDBLayer::SyncToDisk()``。写方法
        (:meth:`write_feature` / :meth:`update_feature` / :meth:`delete_feature`)
        **不逐条落盘** —— 每条只写记录体和那一行的索引(O(1)),表头计数、
        包围盒、索引头/trailer 攒到这一次。所以批量写之后::

            for row in rows:
                layer.write_feature(row)
            layer.sync()          # 一次落盘;不调的话 close()/gdb.close() 会调

        没有挂起改动时是空转(幂等)。GDAL 侧的对应落盘点还有事务边界与
        ``FlushCache()``(数据源关闭时逐图层调,本库即 ``gdb.close()``)。
        """
        self.table.sync()

    def close(self) -> None:
        """落盘并关闭这张表。之后调用任何读写方法都会报错。"""
        if self._closed:
            return
        self.table.close()
        self._closed = True

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        kind = self.geometry_type
        extra = '' if kind == 'null' else f' geom={kind}'
        return (f'<GdbLayer {self.name!r} [{self.physical_name}] '
                f'records={self.record_count}{extra}>')


# ============================================================================
# 几何包围盒辅助
# ============================================================================
def _geometry_bbox(geom: Optional[Geometry]
                   ) -> Optional[Tuple[float, float, float, float]]:
    """几何自身的 XY 包围盒;空几何返回 ``None``。

    ⚠️ 正文是 :meth:`Geometry.envelope`,只扫 ``xy_parts`` —— **不物化
    ``.coordinates``**。这条不是洁癖:上面 :func:`read_features` 的 ``bbox=``
    预筛**每个要素**都要走一次这里,而 ``.coordinates`` 要付 ~19 ns/顶点
    (DESIGN.md §2.19.5),一旦退回它就等于把刚解码的点又摊平一遍。
    另见 :meth:`Geometry.envelope` 的缓存 —— 同一条几何在这里和随后的用户
    代码里调用只算一次。

    ⚠️ ``None`` 在调用方(:func:`read_features`, ``layer.py:273``)的语义是
    **"排除"**,不是"不过滤"。所以 ``multipatch`` 这一档返回 ``None`` 的实际
    效果是:**用了 ``bbox=`` 就取不到 multipatch 要素**。这是既有行为,本轮
    只是把它挪到明面上(``envelope`` 本身完全能算 multipatch 的包围盒 ——
    是这里的 kind 门槛在挡)。要改就得同时改调用方的 ``None`` 语义,不在这轮。
    """
    if geom is None or geom.is_empty:
        return None
    if geom.kind not in ('point', 'multipoint', 'polyline', 'polygon'):
        return None
    return geom.envelope()


def _bbox_intersects(a: Tuple[float, float, float, float],
                     b: Sequence[float]) -> bool:
    """两个包围盒是否相交(闭区间,边界接触算相交)。"""
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


# ============================================================================
# where 子句 —— 一个刻意做小的 SQL 子集
# ============================================================================
#: 说明:这不是 SQL 引擎。GDAL 走的是完整的 OGR SQL / SQLite 方言
#: (``ogropenfilegdblayer.cpp`` 里的 ``SetAttributeFilter`` 最终交给
#: ``OGRLayer`` 的通用过滤框架)。本库只解析最常见的几种谓词,够用来做
#: "抽几条看看"。真要复杂查询,把 ``where`` 传成 Python 函数即可。
_TOKEN_RE = re.compile(r"""
      \s*(?:
          (?P<num>[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?)
        | (?P<str>'(?:[^']|'')*')
        | (?P<name>[^\W\d]\w*)
        | (?P<op><>|!=|<=|>=|=|<|>)
        | (?P<punc>[(),])
      )""", re.VERBOSE)
# 字段名允许非 ASCII:``\w`` 在 str 模式下是 Unicode 语义,中文/日文列名
# 都能整段吃进来(实测样例库里全是 ``县代码``/``林地图斑`` 这种)。首字符
# 用 ``[^\W\d]`` 排除数字开头,避免与上面的 ``num`` 抢 token。

_KEYWORDS = frozenset({'and', 'or', 'not', 'is', 'null', 'in', 'like', 'true',
                       'false'})


class _WhereParser:
    """``WHERE`` 字符串 -> ``f(feature) -> bool``。

    支持的语法(大小写无关)::

        谓词   := 或式
        或式   := 与式 ( OR 与式 )*
        与式   := 一元 ( AND 一元 )*
        一元   := NOT 一元 | '(' 谓词 ')' | 条件
        条件   := 标识符 (
                      IS [NOT] NULL
                    | [NOT] IN '(' 字面量 {',' 字面量} ')'
                    | [NOT] LIKE 字符串
                    | ('=' | '<>' | '!=' | '<' | '<=' | '>' | '>=') 字面量
                  )
        字面量 := 数字 | 字符串 | NULL | TRUE | FALSE

    比较语义跟 Python 一致(字符串按码位、数字按数值),不做 SQL 那种隐式
    类型转换 —— 拿字符串字段跟数字比会直接变成 UNKNOWN(不匹配),不会
    抛错。

    谓词返回 **三值** ``True`` / ``False`` / ``None``:``None`` 就是 SQL 的
    UNKNOWN(某一侧是 NULL,或者两侧类型不可比)。``AND``/``OR``/``NOT``
    按 SQL 真值表组合,最后只有 ``True`` 的行才会被选中 —— 所以
    ``NOT (N > 2)`` 不会把 ``N`` 为 NULL 的行选出来。``IS [NOT] NULL``
    永远给得出 True/False,判空请用它,别用 ``= NULL``。
    """

    def __init__(self, text: str, table: GdbTable) -> None:
        self.table = table
        # 字段名大小写无关解析:统一映射到记录字典里真正用的键
        self._field_names = {f.name.lower(): f.name for f in table.fields}
        # OBJECTID 不在 GdbFeature.attributes 里(它等价于 OGR 的 FID),
        # 但 WHERE 里写 `OBJECTID > 5` 是合理的,取值时单独走 .oid。
        self._oid_field = table.oid_field_name.lower()
        self._tokens = self._tokenize(text)
        self._pos = 0
        self.predicate = self._parse_or()
        if self._pos != len(self._tokens):
            raise _WhereSyntaxError(
                f'WHERE 子句在 {self._peek()!r} 附近无法解析'
            )

    def _value_getter(self, key: str) -> Callable[[GdbFeature], Any]:
        """按字段名造一个取值函数。名字命中 OBJECTID 时读 :attr:`~GdbFeature.oid`。"""
        if self._oid_field and key.lower() == self._oid_field:
            return lambda f: f.oid
        return lambda f: f.attributes.get(key)

    # ------------------------------------------------------------------
    @staticmethod
    def _tokenize(text: str) -> List[Tuple[str, Any]]:
        out: List[Tuple[str, Any]] = []
        pos = 0
        while pos < len(text):
            m = _TOKEN_RE.match(text, pos)
            if not m or m.end() == pos:
                if text[pos:].strip():
                    raise _WhereSyntaxError(
                        f'WHERE 子句第 {pos} 个字符起无法识别:'
                        f'{text[pos:pos + 12]!r}'
                    )
                break
            pos = m.end()
            kind = m.lastgroup
            assert kind is not None
            raw = m.group(kind)
            if kind == 'name' and raw.lower() in _KEYWORDS:
                out.append((raw.lower(), raw))
            elif kind == 'str':
                out.append(('str', raw[1:-1].replace("''", "'")))
            elif kind == 'num':
                out.append(('num', float(raw) if ('.' in raw or 'e' in raw.lower())
                            else int(raw)))
            else:
                out.append((kind, raw))
        return out

    def _peek(self) -> Optional[Any]:
        return self._tokens[self._pos][1] if self._pos < len(self._tokens) else None

    def _kind(self) -> Optional[str]:
        return self._tokens[self._pos][0] if self._pos < len(self._tokens) else None

    def _accept(self, kind: str, value: Optional[str] = None) -> bool:
        if self._pos >= len(self._tokens):
            return False
        k, v = self._tokens[self._pos]
        if k != kind:
            return False
        if value is not None and (v if isinstance(v, str) else '').lower() != value:
            return False
        self._pos += 1
        return True

    def _expect(self, kind: str, value: Optional[str] = None) -> Any:
        if not self._accept(kind, value):
            raise _WhereSyntaxError(
                f'WHERE 子句期待 {value or kind},实得 {self._peek()!r}'
            )
        return self._tokens[self._pos - 1][1]

    # ------------------------------------------------------------------
    def _parse_or(self) -> 'Predicate':
        left = self._parse_and()
        while self._accept('or'):
            right = self._parse_and()
            left = _or(left, right)
        return left

    def _parse_and(self) -> 'Predicate':
        left = self._parse_not()
        while self._accept('and'):
            right = self._parse_not()
            left = _and(left, right)
        return left

    def _parse_not(self) -> 'Predicate':
        if self._accept('not'):
            inner = self._parse_not()
            return lambda f, _i=inner: _not(_i(f))
        if self._accept('punc', '('):
            inner = self._parse_or()
            self._expect('punc', ')')
            return inner
        return self._parse_condition()

    def _parse_condition(self) -> 'Predicate':
        if self._kind() != 'name':
            raise _WhereSyntaxError(
                f'WHERE 子句期待字段名,实得 {self._peek()!r}'
            )
        token = self._accept('name')
        assert token
        raw_name = self._tokens[self._pos - 1][1]
        key = self._field_names.get(str(raw_name).lower())
        if key is None:
            raise GdbNotFoundError(
                f'WHERE 里的字段 {raw_name!r} 不存在;可用字段:'
                f'{sorted(self._field_names.values())}'
            )

        getter = self._value_getter(key)

        # IS [NOT] NULL —— 唯一"永远有确定答案"的判空写法
        if self._accept('is'):
            negate = self._accept('not')
            self._expect('null')
            if negate:
                return lambda f, _g=getter: _g(f) is not None
            return lambda f, _g=getter: _g(f) is None

        # [NOT] IN (...)
        negate = self._accept('not')
        if self._accept('in'):
            self._expect('punc', '(')
            allowed = [self._parse_literal()]
            while self._accept('punc', ','):
                allowed.append(self._parse_literal())
            self._expect('punc', ')')
            values = tuple(allowed)

            def in_predicate(f: GdbFeature, _g: Any = getter, _v: Any = values,
                             _neg: bool = negate) -> Optional[bool]:
                left = _g(f)
                if left is None:
                    return None            # NULL IN (...) 是 UNKNOWN
                has_null = False
                for candidate in _v:
                    if candidate is None:
                        has_null = True
                    elif left == candidate:
                        return not _neg
                if has_null:
                    return None            # 列表里有 NULL -> UNKNOWN
                return _neg

            return in_predicate

        if self._accept('like'):
            pattern = str(self._expect('str'))
            rx = _like_to_regex(pattern)

            def like_predicate(f: GdbFeature, _g: Any = getter, _r: Any = rx,
                               _neg: bool = negate) -> Optional[bool]:
                value = _g(f)
                if value is None:
                    return None
                if not isinstance(value, str):
                    return None            # 非字符串列做 LIKE:不可比
                hit = _r.match(value) is not None
                return hit if not _neg else not hit

            return like_predicate

        if negate:
            raise _WhereSyntaxError(
                f'WHERE 子句:NOT 后面只能是 IN 或 LIKE,实得 {self._peek()!r}'
            )

        op = self._expect('op')
        if op in ('=', '<>', '!='):
            value = self._parse_literal()
            if value is None:
                # SQL 里 `x = NULL` / `x <> NULL` 恒为"未知",不匹配任何行。
                # 判空请用 `IS NULL` / `IS NOT NULL`。
                return lambda f: None

            def equality(f: GdbFeature, _g: Any = getter, _v: Any = value,
                         _eq: bool = (op == '=')) -> Optional[bool]:
                left = _g(f)
                if left is None:
                    return None
                return (left == _v) if _eq else (left != _v)

            return equality
        return _make_cmp(getter, op, self._parse_literal())

    def _parse_literal(self) -> Any:
        kind = self._kind()
        if kind in ('num', 'str'):
            self._pos += 1
            return self._tokens[self._pos - 1][1]
        if kind == 'null':
            self._pos += 1
            return None
        if kind == 'true':
            self._pos += 1
            return True
        if kind == 'false':
            self._pos += 1
            return False
        raise _WhereSyntaxError(f'WHERE 子句期待字面量,实得 {self._peek()!r}')


class _WhereSyntaxError(GdbFormatError):
    """``where`` 字符串语法错误(继承 :class:`GdbFormatError`)。"""


#: 谓词返回三值 ``True`` / ``False`` / ``None``,``None`` 就是 SQL 的 UNKNOWN。
Predicate = Callable[[GdbFeature], Optional[bool]]


def _not(value: Optional[bool]) -> Optional[bool]:
    """SQL ``NOT``:UNKNOWN 取反还是 UNKNOWN。"""
    return None if value is None else (not value)


def _or(a: Predicate, b: Predicate) -> Predicate:
    """SQL ``OR`` 真值表:有一侧 True 就是 True;否则有一侧 UNKNOWN 就是 UNKNOWN。"""

    def combine(f: GdbFeature) -> Optional[bool]:
        av, bv = a(f), b(f)
        if av is True or bv is True:
            return True
        if av is None or bv is None:
            return None
        return False

    return combine


def _and(a: Predicate, b: Predicate) -> Predicate:
    """SQL ``AND`` 真值表:有一侧 False 就是 False;否则有一侧 UNKNOWN 就是 UNKNOWN。"""

    def combine(f: GdbFeature) -> Optional[bool]:
        av, bv = a(f), b(f)
        if av is False or bv is False:
            return False
        if av is None or bv is None:
            return None
        return True

    return combine


def _cmp(left: Any, right: Any) -> Optional[int]:
    """能比就比较(返回 -1/0/1),不能比返回 ``None``(=UNKNOWN)。

    不做 SQL 的隐式转换:``'10' > 9`` 在 SQL 里可能是真,这里按 Python
    的规矩来 —— 类型不同就不匹配,免得悄悄给出错误的筛选结果。

    ``None`` 有两种来源,调用方不必区分:操作数是 NULL,或两侧类型不可比。
    两者在 SQL 里都得 UNKNOWN。
    """
    if left is None or right is None:
        return None
    try:
        if left < right:
            return -1
        if left > right:
            return 1
        return 0
    except TypeError:
        return None


#: 比较运算符 -> "``_cmp`` 结果是否该判为真"。
_CMP_TESTS = {
    '<': lambda c: c < 0,
    '<=': lambda c: c <= 0,
    '>': lambda c: c > 0,
    '>=': lambda c: c >= 0,
}


def _make_cmp(getter: Callable[[GdbFeature], Any], op: str, value: Any
              ) -> Predicate:
    """构造一个大小比较谓词。类型不可比或遇 NULL 时返回 ``None``(UNKNOWN)。"""
    test = _CMP_TESTS[op]

    def predicate(f: GdbFeature) -> Optional[bool]:
        c = _cmp(getter(f), value)
        if c is None:
            return None
        return test(c)

    return predicate


def _like_to_regex(pattern: str) -> 're.Pattern[str]':
    """SQL ``LIKE`` 模式 -> 正则。``%`` 任意串、``_`` 单字符。"""
    out = ['^']
    for ch in pattern:
        if ch == '%':
            out.append('.*')
        elif ch == '_':
            out.append('.')
        else:
            out.append(re.escape(ch))
    out.append('$')
    return re.compile(''.join(out), re.DOTALL)


def _make_predicate(where: Any, table: GdbTable) -> Optional[Predicate]:
    """把 :meth:`GdbLayer.read_features` 的 ``where`` 参数归一成谓词。"""
    if where is None:
        return None
    if callable(where):
        return where
    if isinstance(where, str):
        text = where.strip()
        if not text:
            return None
        return _WhereParser(text, table).predicate
    raise GdbWriteError(
        f'where 必须是 str 或可调用对象,实得 {type(where).__name__}'
    )
