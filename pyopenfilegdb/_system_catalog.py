"""FileGDB 系统目录(GDB_SystemCatalog / GDB_Items)读取与 Catalog XML 解析。

本模块是 ``OpenFileGDB.open()`` 的第一站:先弄清一个 ``.gdb`` 目录里有哪些
逻辑表、哪些是要素类、各自的字段与坐标系是什么,后面 :mod:`._gdbtable` 才
知道该去打开哪个物理文件。

逐段对照的 GDAL 源码 ``ogr/ogrsf_frmts/openfilegdb/``:

=============================================  =======================================
本模块                                          GDAL 对应
=============================================  =======================================
:func:`find_system_tables`                     ``GDALOpenFileGDBDataSource::Open`` 里
                                               遍历 catalog 记录、
                                               ``m_osMapNameToIdx[Name]=i+1`` 那段
                                               (ogropenfilegdbdatasource.cpp:314)
:class:`GdbSystemCatalog`                      ``Open`` 打开 ``a00000001.gdbtable``
                                               并校验 ``Name``/``FileFormat``
:class:`GdbItems`                              ``OpenFileGDBv10``
                                               (ogropenfilegdbdatasource.cpp:658)
:func:`parse_definition_xml`                   ``OGROpenFileGDBLayer::BuildGeometryColumnGDBv10``
                                               + ``BuildLayerDefinition``
                                               (ogropenfilegdblayer.cpp:136/364)
:func:`definition_to_fields`                   ``BuildLayerDefinition`` 中 FGFT_* ->
                                               OGR 字段类型的 switch(本模块反过来
                                               由 ESRI 字符串映射回 FGFT_*)
:func:`item_uuid` / :func:`generate_uuid`      ``ogr_openfilegdb.h:48-63`` 的 UUID 常量、
                                               ``ogropenfilegdb_generate_uuid.cpp``
:func:`make_feature_class_xml`                 ``OGROpenFileGDBLayer::RefreshXMLDefinitionInMemory``
:func:`make_table_xml`                         (ogropenfilegdblayer_write.cpp:2616)
:func:`make_workspace_xml`                     ``CreateGDBItems`` 里的工作空间定义
                                               (ogropenfilegdbdatasource_write.cpp:920)
=============================================  =======================================

两个必须记牢的格式事实(均已用真实 ``.gdb`` 验证,见下):

1. **物理文件名 = ``a%08x % (逻辑序号)``,逻辑序号是它在 GDB_SystemCatalog 里的
   行号 + 1**。系统表并不是固定占 1..7 之外的某个编号 —— 本仓库 :mod:`._constants`
   里那组 ``SYSTEM_TABLE_ITEMS = 'a00000002'`` 之类的常量与真实文件不符,真实
   布局是(ogropenfilegdbdatasource_write.cpp:579 的写入顺序,也是 ArcGIS 的
   实际布局)::

       a00000001 GDB_SystemCatalog
       a00000002 GDB_DBTune
       a00000003 GDB_SpatialRefs
       a00000004 GDB_Items
       a00000005 GDB_ItemTypes
       a00000006 GDB_ItemRelationships
       a00000007 GDB_ItemRelationshipTypes
       a00000008 GDB_ReplicaLog        (FileFormat=2,部分数据集才有)

   catalog 里有 8 条记录,故 **第一个用户图层 = a00000009**。这一点与 findings
   目录下 write.md 的结论一致。因此本模块一点也不敢假设 ``GDB_Items`` 就是
   ``a00000004`` —— :func:`find_system_tables` 与 :meth:`GdbItems.read` 都按
   **名字扫描** 定位(正是 GDAL 的做法,它注释里明说 GDB_Items "has been seen
   in some datasets to not be a00000004")。

2. **要素类的字段清单一律以物理 ``.gdbtable`` 的字段描述区为准,不信任 XML。**
   GDAL 在 ogropenfilegdblayer.cpp:163 写得很清楚:``/* We cannot trust the XML
   definition to build the field definitions. */``。所以本模块的
   :func:`parse_definition_xml` / :func:`definition_to_fields` 只用于**写入**、
   **交叉校验**和**取几何/坐标系信息**,不用来当权威字段表。

XML 命名空间:真实 ArcGIS 写出的根元素既可能是 ``<DEFeatureClassInfo>``,也可能是
``<typens:DEFeatureClassInfo xmlns:typens="...">``;GDAL 的做法是
``CPLStripXMLNamespace(psTree, nullptr, TRUE)`` 之后按裸标签名查找。本模块用
:func:`_local_name` 剥离 ``{uri}`` 前缀来达到同样效果;对命名空间声明缺失的
畸形 XML 还会做一次修补重试,保证部分损坏的 catalog 不会让整个 open 失败。
"""
from __future__ import annotations

import os
import random
import re
import time
import uuid as _uuid
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from xml.etree import ElementTree as ET

from . import _constants as C
from ._datatypes import (
    GdbField,
    GdbFormatError,
    GdbGeomField,
    GdbItem,
    GdbNotFoundError,
    GdbSpatialRef,
)
from ._gdbtable import GdbTable

__all__ = [
    'SYSTEM_TABLE_NAMES',
    'find_system_tables',
    'GdbSystemCatalog',
    'GdbItems',
    'parse_definition_xml',
    'definition_to_fields',
    'item_uuid',
    'relationship_uuid',
    'generate_uuid',
    'make_feature_class_xml',
    'make_table_xml',
    'make_workspace_xml',
]


# ============================================================================
# 常量
# ============================================================================

#: 规范布局下"物理名 -> 逻辑表名"的映射。
#:
#: 取自 catalog.md §1.3 与 ogropenfilegdbdatasource_write.cpp:579 的写入顺序。
#: 注意这只是**默认猜测**:真实数据集里顺序可能不同,永远以
#: :func:`find_system_tables` 的扫描结果为准。
SYSTEM_TABLE_NAMES: Dict[str, str] = {
    'a00000001': 'GDB_SystemCatalog',
    'a00000002': 'GDB_DBTune',
    'a00000003': 'GDB_SpatialRefs',
    'a00000004': 'GDB_Items',
    'a00000005': 'GDB_ItemTypes',
    'a00000006': 'GDB_ItemRelationships',
    'a00000007': 'GDB_ItemRelationshipTypes',
    'a00000008': 'GDB_ReplicaLog',
}

#: 系统表逻辑名反向表。
SYSTEM_TABLE_LOGICAL_NAMES = frozenset(SYSTEM_TABLE_NAMES.values())

#: ``GDB_SystemCatalog`` 固定写死在 a00000001(GDAL 从不按名字找它)。
SYSTEM_CATALOG_PHYSICAL = 'a00000001'

#: GDB_Items 的逻辑表名(GDAL 按该名字定位物理文件)。
GDB_ITEMS_LOGICAL_NAME = 'GDB_Items'

#: 物理名正则:``a`` + 8 位十六进制。
_PHYSICAL_NAME_RE = re.compile(r'^[aA][0-9a-fA-F]{8}$')


def physical_name_for(index: int) -> str:
    """逻辑序号 -> 物理文件名。

    对应 GDAL ``CPLSPrintf("a%08x", idx)``(ogropenfilegdbdatasource.cpp:1084)。
    ``index`` 是 **1 基** 的,即 GDB_SystemCatalog 里的行号 + 1。
    """
    return 'a%08x' % index


def _physical_number(physical: str) -> Optional[int]:
    """``'a00000009'`` -> ``9``;不是合法物理名时返回 ``None``。"""
    base = os.path.splitext(os.path.basename(physical))[0]
    if not _PHYSICAL_NAME_RE.match(base):
        return None
    try:
        return int(base[1:], 16)
    except ValueError:  # pragma: no cover - 正则已保证
        return None


# ----------------------------------------------------------------------------
# item 类型 UUID (ogr_openfilegdb.h:48-63)
#
# ⚠️ 大小写:真实 ArcGIS 文件里 GUID 以**大写**存储(本驱动 _gdbtable 的 GUID
# 解码器也输出大写),而 GDAL 源码里的常量是小写。为贴合真实文件,本模块对外
# 统一返回**大写带花括号**的形式,所有比较一律走 :func:`uuid_eq` 做大小写无关
# 匹配。
# ----------------------------------------------------------------------------
_ITEM_TYPE_UUIDS: Dict[str, str] = {
    'Folder': '{F3783E6F-65CA-4514-8315-CE3985DAD3B1}',
    'Workspace': '{C673FE0F-7280-404F-8532-20755DD8FC06}',
    'Feature Dataset': '{74737149-DCB5-4257-8904-B9724E32A530}',
    'Feature Class': '{70737809-852C-4A03-9E22-2CECEA5B9BFA}',
    'Table': '{CD06BC3B-789D-4C51-AAFA-A467912B8965}',
    'Range Domain': '{C29DA988-8C3E-45F7-8B5C-18E51EE7BEB4}',
    'Coded Value Domain': '{8C368B12-A12E-4C7E-9638-C9C64E69E98F}',
    'Relationship Class': '{B606A7E1-FA5B-439C-849C-6E9C2481537B}',
}

#: GDB_ItemRelationships 里用到的关系类型 UUID(本模块仅在文档/常量层面暴露,
#: 真正写入由 datasource 层负责)。出处 ogr_openfilegdb.h:66-73。
_ITEM_RELATIONSHIP_UUIDS: Dict[str, str] = {
    'DatasetInFeatureDataset': '{A1633A59-46BA-4448-8706-D8ABE2B2B02E}',
    'DatasetInFolder': '{DC78F1AB-34E4-43AC-BA47-1C4EABD0E7C7}',
    'DomainInDataset': '{17E08ADB-2B31-4DCD-8FDD-DF529E88F843}',
    'DatasetsRelatedThrough': '{725BADAB-3452-491B-A795-55F32D67229C}',
}

#: :func:`item_uuid` 接受的别名(全部小写去空格)。
_ITEM_TYPE_ALIASES: Dict[str, str] = {
    'folder': 'Folder',
    'workspace': 'Workspace',
    'featuredataset': 'Feature Dataset',
    'dataset': 'Feature Dataset',
    'featureclass': 'Feature Class',
    'fc': 'Feature Class',
    'table': 'Table',
    'objectclass': 'Table',
    'object class': 'Table',
    'rangedomain': 'Range Domain',
    'codedvaluedomain': 'Coded Value Domain',
    'codeddomain': 'Coded Value Domain',
    'relationshipclass': 'Relationship Class',
    'relationship': 'Relationship Class',
}

#: GUID 反向表(大写 -> 人类可读类型名),供 :class:`GdbItems` 分类用。
_UUID_TO_ITEM_TYPE: Dict[str, str] = {
    v.upper(): k for k, v in _ITEM_TYPE_UUIDS.items()
}


def uuid_eq(a: Optional[str], b: Optional[str]) -> bool:
    """GUID 大小写无关比较。任一为 ``None``/空则返回 ``False``。"""
    if not a or not b:
        return False
    return a.strip().upper() == b.strip().upper()


def item_uuid(kind: str) -> str:
    """返回内置的 item 类型 UUID。

    出处 ``ogr_openfilegdb.h:48-63``(``psz*TypeUUID``)。返回形式为大写带花括号,
    与真实 ArcGIS 文件一致。未知 ``kind`` 返回空串(调用方据此判断"不认识")。
    """
    if not kind:
        return ''
    key = kind.strip()
    if key in _ITEM_TYPE_UUIDS:
        return _ITEM_TYPE_UUIDS[key]
    return _ITEM_TYPE_UUIDS.get(_ITEM_TYPE_ALIASES.get(key.lower().replace(' ', ''),
                                                        ''), '')


def relationship_uuid(kind: str) -> str:
    """返回内置的 item **关系** 类型 UUID(写 ``GDB_ItemRelationships`` 用)。

    出处 ``ogr_openfilegdb.h:66-73``。可用的 ``kind``:
    ``'DatasetInFolder'``(要素类放在库里)、``'DatasetInFeatureDataset'``、
    ``'DomainInDataset'``、``'DatasetsRelatedThrough'``。未知名字返回空串。

    典型用法 ``RegisterInItemRelationships(根条目 GUID, 图层 GUID,
    relationship_uuid('DatasetInFolder'))`` —— 见
    ``ogropenfilegdbdatasource_write.cpp`` 的 ``RegisterTable()``。
    """
    return _ITEM_RELATIONSHIP_UUIDS.get((kind or '').strip(), '')


def generate_uuid() -> str:
    """生成一个 UUID 字符串(带花括号,小写十六进制,版本 4)。

    参考 ``ogropenfilegdb_generate_uuid.cpp`` 的 ``OFGDBGenerateUUID``。GDAL 用
    ``std::mt19937(ntime ^ ncounter)`` 逐半字节生成,并在第 13 个半字节硬写 ``4``、
    第 17 个半字节限制在 ``8..11``(即 version=4、variant=10xx)。这里用标准库
    :mod:`random` 复刻同样的"时间 + 计数器"播种思路,不依赖 ``uuid.uuid4`` 的内部
    实现,但语义等价(随机 v4 UUID)。

    GDAL 输出 **小写**;本驱动读写路径统一按大小写无关处理,故下游比较请用
    :func:`uuid_eq`。
    """
    # 计数器 + 时间做种,模拟 GDAL 的 nCounter ^ tv_sec ^ tv_usec。
    counter = getattr(generate_uuid, '_counter', 0) + 1
    generate_uuid._counter = counter  # type: ignore[attr-defined]
    seed = (int(time.time() * 1000) ^ (counter * 2654435761)) & 0xFFFFFFFF
    rnd = random.Random(seed)

    hexdigits = '0123456789abcdef'
    parts = []
    for count in (8, 4, 4, 4, 12):
        parts.append(''.join(rnd.choice(hexdigits) for _ in range(count)))

    # version = 4(第 3 组的首字符)
    parts[2] = '4' + parts[2][1:]
    # variant = 8..b(第 4 组的首字符)
    parts[3] = rnd.choice('89ab') + parts[3][1:]
    return '{%s}' % '-'.join(parts)


# ============================================================================
# ESRI 字段类型字符串 <-> FGFT_*
#
# 字符串一侧取自 filegdb_gdbtoogrfieldtype.h 与 ogropenfilegdblayer_write.cpp
# (CreateXMLFieldDefinition, :871-937)。
# ============================================================================
_ESRI_TO_FGFT: Dict[str, int] = {
    'esrifieldtypesmallinteger': C.FGFT_INT16,
    'esrifieldtypeinteger': C.FGFT_INT32,
    'esrifieldtypesingle': C.FGFT_FLOAT32,
    'esrifieldtypedouble': C.FGFT_FLOAT64,
    'esrifieldtypestring': C.FGFT_STRING,
    'esrifieldtypedate': C.FGFT_DATETIME,
    'esrifieldtypeoid': C.FGFT_OBJECTID,
    'esrifieldtypegeometry': C.FGFT_GEOMETRY,
    'esrifieldtypeblob': C.FGFT_BINARY,
    'esrifieldtyperaster': C.FGFT_RASTER,
    'esrifieldtypeguid': C.FGFT_GUID,
    'esrifieldtypeglobalid': C.FGFT_GLOBALID,
    'esrifieldtypexml': C.FGFT_XML,
    'esrifieldtypebiginteger': C.FGFT_INT64,
    'esrifieldtypedateonly': C.FGFT_DATE,
    'esrifieldtypetimeonly': C.FGFT_TIME,
    'esrifieldtypetimestampoffset': C.FGFT_DATETIME_WITH_OFFSET,
}

_FGFT_TO_ESRI: Dict[int, str] = {
    C.FGFT_INT16: 'esriFieldTypeSmallInteger',
    C.FGFT_INT32: 'esriFieldTypeInteger',
    C.FGFT_FLOAT32: 'esriFieldTypeSingle',
    C.FGFT_FLOAT64: 'esriFieldTypeDouble',
    C.FGFT_STRING: 'esriFieldTypeString',
    C.FGFT_DATETIME: 'esriFieldTypeDate',
    C.FGFT_OBJECTID: 'esriFieldTypeOID',
    C.FGFT_GEOMETRY: 'esriFieldTypeGeometry',
    C.FGFT_BINARY: 'esriFieldTypeBlob',
    C.FGFT_RASTER: 'esriFieldTypeRaster',
    C.FGFT_GUID: 'esriFieldTypeGUID',
    C.FGFT_GLOBALID: 'esriFieldTypeGlobalID',
    C.FGFT_XML: 'esriFieldTypeXML',
    C.FGFT_INT64: 'esriFieldTypeBigInteger',
    C.FGFT_DATE: 'esriFieldTypeDateOnly',
    C.FGFT_TIME: 'esriFieldTypeTimeOnly',
    C.FGFT_DATETIME_WITH_OFFSET: 'esriFieldTypeTimestampOffset',
}

#: ESRI 字段类型在 XML 里固定的 ``<Length>``(CreateXMLFieldDefinition)。
_ESRI_TYPE_LENGTH: Dict[str, int] = {
    'esrifieldtypesmallinteger': 2,
    'esrifieldtypeinteger': 4,
    'esrifieldtypesingle': 4,
    'esrifieldtypedouble': 8,
    'esrifieldtypedate': 8,
    'esrifieldtypeoid': 4,
    'esrifieldtypegeometry': 0,
    'esrifieldtypebiginteger': 8,
    'esrifieldtypedateonly': 8,
    'esrifieldtypetimeonly': 8,
    'esrifieldtypetimestampoffset': 10,
}


def esri_field_type(field_type: int) -> str:
    """``FGFT_*`` -> ESRI 字符串;未知返回 ``esriFieldTypeString``。"""
    return _FGFT_TO_ESRI.get(field_type, 'esriFieldTypeString')


def fgft_field_type(esri_name: str) -> int:
    """ESRI 字符串 -> ``FGFT_*``;未识别返回 ``FGFT_UNDEFINED``。"""
    if not esri_name:
        return C.FGFT_UNDEFINED
    return _ESRI_TO_FGFT.get(esri_name.strip().lower(), C.FGFT_UNDEFINED)


# ============================================================================
# XML 小工具
# ============================================================================

def _local_name(tag: Any) -> str:
    """剥掉 ``{namespace}`` 前缀,只留本地标签名。

    对应 GDAL 解析前的 ``CPLStripXMLNamespace(psTree, nullptr, TRUE)``
    (ogropenfilegdblayer.cpp:143)。
    """
    if not isinstance(tag, str):
        return ''
    if tag.startswith('{') and '}' in tag:
        return tag.rsplit('}', 1)[1]
    # 万一 ET 没把前缀解析掉(畸形 XML),再手工去掉 'pfx:'
    if ':' in tag:
        return tag.rsplit(':', 1)[1]
    return tag


def _child(node: Optional[ET.Element], name: str) -> Optional[ET.Element]:
    """按本地标签名取**直接子**元素(第一个匹配)。"""
    if node is None:
        return None
    target = name.lower()
    for child in node:
        if _local_name(child.tag).lower() == target:
            return child
    return None


def _children(node: Optional[ET.Element], name: str) -> List[ET.Element]:
    """按本地标签名取所有直接子元素。"""
    if node is None:
        return []
    target = name.lower()
    return [c for c in node if _local_name(c.tag).lower() == target]


def _find_deep(node: Optional[ET.Element], name: str) -> Optional[ET.Element]:
    """深度优先查找第一个本地标签名匹配的元素(对应 ``CPLSearchXMLNode``)。"""
    if node is None:
        return None
    target = name.lower()
    for elem in node.iter():
        if _local_name(elem.tag).lower() == target:
            return elem
    return None


def _text(node: Optional[ET.Element], name: str = '',
          default: Optional[str] = None) -> Optional[str]:
    """取 ``node``(或它的 ``name`` 子元素)的文本。"""
    target = node if not name else _child(node, name)
    if target is None:
        return default
    if target.text is None:
        return default
    value = target.text
    return value if value != '' else default


def _bool_text(value: Optional[str], default: bool = False) -> bool:
    """ArcGIS 的布尔文本解析。``'true'``/``'1'`` 为真(大小写无关)。"""
    if value is None:
        return default
    return value.strip().lower() in ('true', '1')


def _int_text(value: Optional[str], default: int = 0) -> int:
    """整数文本解析,失败给默认值(ArcGIS 偶有 ``''``/``xsi:nil``)。"""
    if value is None:
        return default
    try:
        return int(value.strip())
    except (TypeError, ValueError):
        return default


def _float_text(value: Optional[str], default: float = 0.0) -> float:
    """浮点文本解析,失败给默认值。"""
    if value is None:
        return default
    try:
        return float(value.strip())
    except (TypeError, ValueError):
        return default


_PREFIX_DECL_RE = re.compile(r'^\s*<([A-Za-z_][\w.\-]*):([A-Za-z_][\w.\-]*)')


def _parse_xml_root(xml_text: str) -> Optional[ET.Element]:
    """把一段 Catalog XML 解析成根元素。

    对应 ``CPLParseXMLString``(ogropenfilegdblayer.cpp:139)。差异处理:

    * 先用标准解析;失败且是"未声明的命名空间前缀"时,给根元素补上缺失的
      ``xmlns:*`` 声明再试一次。真实文件里出现过 ``<typens:DEFeatureClassInfo>``
      却漏写 ``xmlns:typens`` 的情况,不能因此让整库打不开。
    * 仍失败返回 ``None``(调用方按"该 item 定义不可解析"处理,而不是抛异常
      中断整个数据集)。
    """
    if not xml_text:
        return None
    text = xml_text.strip()
    if not text:
        return None
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        pass

    # -- 修补:给根元素补命名空间声明,然后重试 --
    match = _PREFIX_DECL_RE.match(text)
    decls = []
    prefixes = set()
    if match:
        prefixes.add(match.group(1))
    # 属性里常见的前缀也一并补上
    for pfx in re.findall(r'\b([A-Za-z_][\w.\-]*):[A-Za-z_]', text):
        prefixes.add(pfx)
    for pfx in sorted(prefixes):
        if pfx in ('xml', 'xmlns'):
            continue
        if re.search(r'xmlns:%s\s*=' % re.escape(pfx), text):
            continue
        decls.append(' xmlns:%s="urn:pyopenfilegdb:%s"' % (pfx, pfx))
    if not decls:
        return None
    # 找到第一个 '>' 之前的根标签,把声明插进去
    insert_at = text.find('>')
    if insert_at < 0:
        return None
    head = text[:insert_at]
    tail = text[insert_at:]
    repaired = head + ''.join(decls) + tail
    try:
        return ET.fromstring(repaired)
    except ET.ParseError:
        return None


# ============================================================================
# 系统表定位
# ============================================================================

def _catalog_index_to_name(gdb_dir: str) -> Dict[int, str]:
    """读 ``a00000001.gdbtable``,返回 ``{逻辑序号(1 基): 逻辑表名}``。

    逻辑序号 = catalog 行号 + 1;物理名 = ``a%08x % 逻辑序号``。这条"由行号推
    物理名"的规则正是 GDAL ``Open`` 的核心(ogropenfilegdbdatasource.cpp:314)。
    catalog 不可读时返回 ``{}``,不抛异常 —— 部分损坏的目录不应让整库打不开。
    """
    result: Dict[int, str] = {}
    path = Path(gdb_dir) / (SYSTEM_CATALOG_PHYSICAL + '.gdbtable')
    if not path.is_file():
        return result
    try:
        table = GdbTable.from_file(str(path))
        for row, values in table.iter_rows():
            name = values.get('Name')
            if name:
                result[row + 1] = str(name)
    except Exception:
        return {}
    return result


def find_system_tables(gdb_dir: str) -> Dict[str, str]:
    """扫描 ``.gdb`` 目录里的 ``*.gdbtable``,返回 ``{物理名: 逻辑表名}``。

    实现思路与 GDAL ``GDALOpenFileGDBDataSource::Open`` 完全一致
    (ogropenfilegdbdatasource.cpp:314):**先打开 catalog,按行号推出每个表的
    物理文件名**;而不是反过来假设某个系统表固定在某个编号。

    具体步骤:

    1. 列出目录下所有 ``*.gdbtable``;
    2. 若能打开 ``a00000001.gdbtable``,读它的 ``Name`` 列,得到
       ``行号 i -> 逻辑名``,物理名即 ``a%08x % (i+1)``;
    3. 目录里有、但 catalog 没登记的文件,退而用 :data:`SYSTEM_TABLE_NAMES`
       的规范布局猜测,再不行就用物理名本身当逻辑名。

    :param gdb_dir: ``.gdb`` 目录(不是 ``.gdbtable`` 文件)。
    :returns: ``{'a00000001': 'GDB_SystemCatalog', ...}``。目录不存在时返回 ``{}``。
    """
    directory = Path(gdb_dir)
    if not directory.is_dir():
        return {}
    try:
        entries = sorted(directory.iterdir(),
                         key=lambda p: p.name.lower())
    except OSError:
        return {}

    physical_files = [
        p for p in entries
        if p.is_file() and p.name.lower().endswith('.gdbtable')
    ]
    if not physical_files:
        return {}

    names_by_index = _catalog_index_to_name(gdb_dir)

    result: Dict[str, str] = {}
    for path in physical_files:
        physical = path.stem  # 去掉 .gdbtable
        index = _physical_number(physical)
        logical = None
        if index is not None:
            logical = names_by_index.get(index)
        if not logical:
            logical = SYSTEM_TABLE_NAMES.get(physical.lower())
        if not logical:
            logical = physical
        result[physical] = logical
    return result


def _catalog_name_to_physical(gdb_dir: str) -> Dict[str, str]:
    """``{逻辑表名: 物理名}``。

    供 :class:`GdbItems` 把 item 的 ``Name`` 映射到它真正的 ``.gdbtable`` 文件。
    **不能用 GDB_Items 自己的行号** —— 两者完全不同:GDB_Items 的行号是 item
    的 ObjectID,而物理文件名用的是 **GDB_SystemCatalog** 的行号。
    """
    out: Dict[str, str] = {}
    for index, name in _catalog_index_to_name(gdb_dir).items():
        # 同名只保留第一个(罕见)
        out.setdefault(name, physical_name_for(index))
    for physical, logical in SYSTEM_TABLE_NAMES.items():
        out.setdefault(logical, physical)
    return out


def _looks_like_items_table(table: GdbTable) -> bool:
    """判断一个 ``.gdbtable`` 是否像 GDB_Items。

    条件来自 ``OpenFileGDBv10``(ogropenfilegdbdatasource.cpp:670):必须有
    ``Name``(STRING)、``Type``(GUID)、``Definition``(XML)三个字段。这里放宽
    为按名字找 + 类型大致吻合,避免个别文件 GUID 字段类型字节不规范时误判。
    """
    lower = {f.name.lower(): f for f in table.fields}
    if 'name' not in lower or 'definition' not in lower:
        return False
    definition_field = lower['definition']
    if definition_field.field_type not in (C.FGFT_XML, C.FGFT_STRING,
                                           C.FGFT_BINARY):
        return False
    return 'type' in lower or 'uuid' in lower


# ============================================================================
# GDB_SystemCatalog
# ============================================================================

class GdbSystemCatalog:
    """``GDB_SystemCatalog``(通常是 ``a00000001``)的只读包装。

    列:``ID``(OBJECTID)、``Name``(STRING)、``FileFormat``(INT32)。

    ``FileFormat``:核心系统表为 ``0``,``GDB_ReplicaLog`` 为 ``2``
    (ogropenfilegdbdatasource_write.cpp:582-590)。

    行号语义:第 ``i`` 条(**0 基**)记录对应物理文件 ``a%08x % (i+1)``。因此
    :meth:`items` 返回的 ``id`` 即 ``FileFormat`` 之外的另一条身份线索,同时也是
    :meth:`physical_name` 的入参。
    """

    def __init__(self, gdb_dir: str, table: GdbTable) -> None:
        self.gdb_dir = gdb_dir
        self.table = table
        self.path = table.path
        self._items: Optional[List[Tuple[int, str, int]]] = None

    # ------------------------------------------------------------------
    @classmethod
    def read(cls, gdb_dir: str) -> 'GdbSystemCatalog':
        """打开 ``<gdb_dir>/a00000001.gdbtable``。

        :raises GdbNotFoundError: 目录或 ``a00000001.gdbtable`` 不存在。
        :raises GdbFormatError: 表结构不是 ``Name``+``FileFormat``
            (对应 ogropenfilegdbdatasource.cpp:299 的校验)。
        """
        gdb_dir = os.fspath(gdb_dir)
        path = os.path.join(gdb_dir, SYSTEM_CATALOG_PHYSICAL + '.gdbtable')
        if not os.path.isfile(path):
            raise GdbNotFoundError(
                f'{gdb_dir}: 找不到系统目录表 {SYSTEM_CATALOG_PHYSICAL}.gdbtable'
            )
        table = GdbTable.from_file(path)

        idx_name = table.field_by_name('Name')
        idx_format = table.field_by_name('FileFormat')
        if (idx_name is None or idx_format is None
                or idx_name.field_type != C.FGFT_STRING
                or idx_format.field_type not in (C.FGFT_INT16, C.FGFT_INT32)):
            raise GdbFormatError(
                f'{path}: GDB_SystemCatalog 结构不正确 '
                f'(需要 Name:STRING 与 FileFormat:INT16/INT32)'
            )
        return cls(gdb_dir, table)

    # ------------------------------------------------------------------
    def __len__(self) -> int:
        """有效记录数(即系统目录里的表个数)。"""
        return len(self.items())

    def items(self) -> List[Tuple[int, str, int]]:
        """返回 ``[(id, name, file_format), ...]``,按记录顺序。

        ``id`` 从 1 开始(FileGDB 的 OBJECTID 由行号推导,见
        :meth:`GdbTable._decode_row_blob`)。字段缺失的行会被跳过,不抛异常。
        """
        if self._items is not None:
            return self._items
        out: List[Tuple[int, str, int]] = []
        try:
            rows = list(self.table.iter_rows())
        except Exception:
            rows = []
        for row, values in rows:
            name = values.get('Name')
            if name is None:
                continue
            record_id = values.get('ID')
            if not isinstance(record_id, int) or record_id <= 0:
                record_id = row + 1
            file_format = values.get('FileFormat')
            if not isinstance(file_format, int):
                file_format = 0
            out.append((record_id, str(name), int(file_format)))
        self._items = out
        return out

    # ------------------------------------------------------------------
    def physical_name(self, index: int) -> str:
        """逻辑序号 -> 物理表名,例如 ``9 -> 'a00000009'``。

        对应 GDAL ``CPLSPrintf("a%08x", idx)``。``index`` 为 1 基。
        """
        return physical_name_for(index)

    # ------------------------------------------------------------------
    def name_for_index(self, index: int) -> Optional[str]:
        """逻辑序号 -> 逻辑表名;序号越界返回 ``None``。"""
        for record_id, name, _fmt in self.items():
            if record_id == index:
                return name
        return None

    def file_format_for_index(self, index: int) -> int:
        """逻辑序号 -> ``FileFormat``;序号越界返回 ``0``。"""
        for record_id, _name, fmt in self.items():
            if record_id == index:
                return fmt
        return 0

    def index_for_name(self, name: str) -> int:
        """逻辑表名 -> 逻辑序号(大小写敏感优先,再退化为无关大小写)。``0`` 表示无。"""
        for record_id, item_name, _fmt in self.items():
            if item_name == name:
                return record_id
        lowered = name.lower()
        for record_id, item_name, _fmt in self.items():
            if item_name.lower() == lowered:
                return record_id
        return 0

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f'<GdbSystemCatalog {self.table.basename} tables={len(self)}>'


# ============================================================================
# GDB_Items
# ============================================================================

class GdbItems:
    """``GDB_Items`` 表的读取与 XML 解析。

    与 GDAL 一样**不假设**它在 ``a00000004``:先按 catalog 的 ``Name`` 找,找不到
    再退回 ``a00000004``,最后扫描所有 ``.gdbtable`` 做结构嗅探。

    一行 = 一个 :class:`GdbItem`。``Type`` 列是 GUID(:class:`GdbItem` 里转成
    ``'Feature Class'`` / ``'Table'`` 这类人类可读名);``Definition`` 列是要点,
    里面才有几何类型、坐标系、字段列表。
    """

    def __init__(self, gdb_dir: str, table: GdbTable,
                 physical_name: str = '') -> None:
        self.gdb_dir = gdb_dir
        self.table = table
        self.path = table.path
        self.physical_name = physical_name or table.basename
        # 逻辑名 -> 物理表名(lookup 来自 GDB_SystemCatalog,不是本表行号!)
        self._name_to_physical: Dict[str, str] = _catalog_name_to_physical(gdb_dir)
        self._items: Optional[List[GdbItem]] = None

    # ------------------------------------------------------------------
    @classmethod
    def read(cls, gdb_dir: str) -> 'GdbItems':
        """定位并打开 ``GDB_Items``。

        搜索顺序(与 GDAL 的容错精神一致,但更宽):

        1. catalog 里 ``Name == 'GDB_Items'`` 的那一行,物理名 ``a%08x % (i+1)``;
        2. ``a00000004.gdbtable``(规范位置);
        3. 目录里其余 ``.gdbtable``,挑结构像 GDB_Items 的
           (:func:`_looks_like_items_table`)。

        :raises GdbNotFoundError: 一个候选都没有。
        :raises GdbFormatError: 候选文件都打不开或结构不符。
        """
        gdb_dir = os.fspath(gdb_dir)
        candidates: List[str] = []
        seen = set()

        def _add(physical: str) -> None:
            if physical and physical not in seen:
                seen.add(physical)
                candidates.append(physical)

        # (1) 按名字扫描
        table_map = find_system_tables(gdb_dir)
        for physical, logical in table_map.items():
            if logical == GDB_ITEMS_LOGICAL_NAME:
                _add(physical)
        # (2) 规范位置
        _add('a00000004')
        # (3) 兜底:所有 gdbtable
        for physical in sorted(table_map):
            _add(physical)

        last_error: Optional[Exception] = None
        for physical in candidates:
            path = os.path.join(gdb_dir, physical + '.gdbtable')
            if not os.path.isfile(path):
                continue
            try:
                table = GdbTable.from_file(path)
            except Exception as exc:  # 单个文件坏掉不影响继续找
                last_error = exc
                continue
            if not _looks_like_items_table(table):
                continue
            return cls(gdb_dir, table, physical)

        if last_error is not None:
            raise GdbFormatError(
                f'{gdb_dir}: 找到候选文件但都无法解析为 GDB_Items ({last_error})'
            )
        raise GdbNotFoundError(f'{gdb_dir}: 找不到 GDB_Items 表')

    # ------------------------------------------------------------------
    # 记录 -> GdbItem
    # ------------------------------------------------------------------
    @staticmethod
    def _classify(values: Dict[str, Any]) -> str:
        """判定 item 类型。

        优先用 ``Type`` GUID(``ogr_openfilegdb.h`` 的常量)匹配;GUID 不认识时
        退回 GDAL 真正采用的办法 —— 嗅探 ``Definition`` 里的根元素名
        (ogropenfilegdbdatasource.cpp:794 用 ``strstr`` 找 ``DEFeatureClassInfo``)。
        """
        type_guid = values.get('Type')
        if isinstance(type_guid, str):
            known = _UUID_TO_ITEM_TYPE.get(type_guid.strip().upper())
            if known:
                return known
        definition = values.get('Definition') or ''
        if not isinstance(definition, str):
            definition = ''
        if 'DEFeatureClassInfo' in definition:
            return 'Feature Class'
        if 'DEFeatureDataset' in definition:
            return 'Feature Dataset'
        if 'DETableInfo' in definition:
            return 'Table'
        if 'DERasterDataset' in definition:
            return 'Raster Dataset'
        if 'DERasterCatalog' in definition:
            return 'Raster Catalog'
        if 'DEWorkspace' in definition:
            return 'Workspace'
        if 'GPCodedValueDomain2' in definition or 'GPCodedValueDomain' in definition:
            return 'Coded Value Domain'
        if 'GPRangeDomain2' in definition or 'GPRangeDomain' in definition:
            return 'Range Domain'
        if 'DERelationshipClass' in definition:
            return 'Relationship Class'
        return ''

    @classmethod
    def _row_to_item(cls, row: int, values: Dict[str, Any]) -> GdbItem:
        """把 GDB_Items 的一行转成 :class:`GdbItem`。"""
        definition = values.get('Definition')
        if not isinstance(definition, str):
            definition = '' if definition is None else str(definition)

        properties: Dict[str, Any] = {}
        for key in ('Type', 'PhysicalName', 'DatasetSubtype1', 'DatasetSubtype2',
                    'DatasetInfo1', 'DatasetInfo2', 'URL', 'ItemInfo'):
            if key in values:
                properties[key] = values[key]

        return GdbItem(
            uuid=str(values.get('UUID') or ''),
            name=str(values.get('Name') or ''),
            item_type=cls._classify(values),
            # 物理表名稍后由 GdbItems._row_to_item 按 catalog 补上
            physical_name='',
            path=str(values.get('Path') or ''),
            definition_xml=definition,
            properties=properties,
        )

    # ------------------------------------------------------------------
    def _load(self) -> List[GdbItem]:
        """惰性加载全部 item(单条损坏的记录跳过,不中断整表)。

        每个 item 的 ``physical_name`` 在这里按 **GDB_SystemCatalog** 的名字映射
        补上;映射不到(如 Workspace / Folder 这类没有物理表的 item)时留空。
        """
        if self._items is not None:
            return self._items
        out: List[GdbItem] = []
        for row, values in self.table.iter_rows():
            try:
                item = self._row_to_item(row, values)
            except Exception:
                continue
            item.physical_name = self._name_to_physical.get(item.name, '')
            if not item.physical_name:
                # 退而匹配 GDB_Items.PhysicalName 列(大小写无关)
                physical = item.properties.get('PhysicalName')
                if isinstance(physical, str) and physical:
                    lowered = physical.lower()
                    for logical, phys in self._name_to_physical.items():
                        if logical.lower() == lowered:
                            item.physical_name = phys
                            break
            out.append(item)
        self._items = out
        return out

    def __iter__(self) -> Iterator[GdbItem]:
        """遍历所有 item。"""
        return iter(self._load())

    def __len__(self) -> int:
        return len(self._load())

    # ------------------------------------------------------------------
    def feature_classes(self) -> List[GdbItem]:
        """只返回要素类(item_type == ``'Feature Class'``)。"""
        return [i for i in self._load() if i.item_type == 'Feature Class']

    def tables(self) -> List[GdbItem]:
        """只返回非空间表(item_type == ``'Table'``)。"""
        return [i for i in self._load() if i.item_type == 'Table']

    def datasets(self) -> List[GdbItem]:
        """只返回要素数据集(item_type == ``'Feature Dataset'``)。"""
        return [i for i in self._load() if i.item_type == 'Feature Dataset']

    def by_physical_name(self, name: str) -> Optional[GdbItem]:
        """按物理名查找。

        接受 ``'a00000009'``、``'a00000009.gdbtable'`` 甚至是相对/绝对路径;
        另外兼容 GDB_Items 的 ``PhysicalName`` 列(ArcGIS 存的是**大写逻辑名**,
        如 ``'MYLAYER'``),两种语义都试。
        """
        if not name:
            return None
        base = os.path.splitext(os.path.basename(os.fspath(name)))[0]
        base_lower = base.lower()
        for item in self._load():
            if item.physical_name.lower() == base_lower:
                return item
        # 退而匹配 GDB_Items.PhysicalName 列
        for item in self._load():
            physical = item.properties.get('PhysicalName')
            if isinstance(physical, str) and physical.lower() == base_lower:
                return item
        return None

    def by_name(self, name: str) -> Optional[GdbItem]:
        """按逻辑名查找(先精确,再大小写无关)。"""
        if not name:
            return None
        for item in self._load():
            if item.name == name:
                return item
        lowered = name.lower()
        for item in self._load():
            if item.name.lower() == lowered:
                return item
        return None

    def by_uuid(self, guid: str) -> Optional[GdbItem]:
        """按 ``UUID`` 列查找(GUID 大小写无关)。"""
        for item in self._load():
            if uuid_eq(item.uuid, guid):
                return item
        return None

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f'<GdbItems {self.physical_name} items={len(self)}>'


# ============================================================================
# Catalog Definition XML 解析
# ============================================================================

#: 根元素本地名 -> 是否空间表。用于快速判定 Definition 种类。
_DEFINITION_ROOTS = ('DEFeatureClassInfo', 'DETableInfo', 'DEFeatureDataset',
                     'DERasterDataset', 'DERasterCatalog', 'DEWorkspace')


def _parse_field_entry(node: ET.Element) -> Dict[str, Any]:
    """解析一个 ``<Field>`` 或 ``<GPFieldInfoEx>``。

    两种容器都支持:``<Fields>/<Field>``(任务书要求的简化写法)与 ArcGIS/GDAL
    真正使用的 ``<GPFieldInfoExs>/<GPFieldInfoEx>``。字段类型取值同时接受
    ``<Type>`` 与 ``<FieldType>``(GDAL 用后者)。
    """
    esri_type = (_text(node, 'FieldType') or _text(node, 'Type') or '')
    length_raw = _text(node, 'Length')
    precision = _text(node, 'Precision')
    scale = _text(node, 'Scale')

    default: Any = None
    for key in ('DefaultValueString', 'DefaultValueNumeric',
                'DefaultValueInteger', 'DefaultValue'):
        found = _child(node, key)
        if found is not None:
            default = found.text if found.text is not None else ''
            break

    return {
        'name': _text(node, 'Name') or '',
        'alias': _text(node, 'AliasName') or _text(node, 'ModelName') or '',
        'type': esri_type,
        'fgft': fgft_field_type(esri_type),
        'length': _int_text(length_raw, 0),
        'nullable': _bool_text(_text(node, 'IsNullable'), True),
        'required': _bool_text(_text(node, 'Required'), False),
        'editable': _bool_text(_text(node, 'Editable'), True),
        'precision': _int_text(precision, 0),
        'scale': _int_text(scale, 0),
        'default': default,
        'high_precision': _bool_text(_text(node, 'HighPrecision'), False),
        'domain': _text(node, 'DomainName') or '',
    }


def _parse_spatial_reference(node: Optional[ET.Element]) -> Dict[str, Any]:
    """解析 ``<SpatialReference>``。

    字段与 :class:`GdbSpatialRef` 对应;``WKID``/``LatestWKID`` 的语义见
    ``BuildSRS``(ogropenfilegdbdatasource.cpp:2135):``>32767`` 是 ESRI 代码,
    ``<=32767`` 是 EPSG。``WKT`` 以 ``{`` 开头表示是 ESRI 序列化对象,GDAL 会
    忽略(GDAL 判断 ``pszWKT[0] != '{'``)。
    """
    if node is None:
        return {
            'wkt': '', 'wkid': 0, 'latest_wkid': 0, 'name': '',
            'x_origin': 0.0, 'y_origin': 0.0, 'xy_scale': 0.0,
            'z_origin': 0.0, 'z_scale': 0.0,
            'm_origin': 0.0, 'm_scale': 0.0,
            'xy_tolerance': 0.0, 'z_tolerance': 0.0, 'm_tolerance': 0.0,
            'high_precision': False, 'left_longitude': 0.0,
            'srs_type': '',
        }
    wkt = _text(node, 'WKT') or ''
    if wkt.startswith('{'):
        # ESRI 序列化对象,不是 WKT,忽略(同 GDAL)
        wkt = ''
    srs_type_attr = node.get('{http://www.w3.org/2001/XMLSchema-instance}type') \
        or node.get('xsi:type') or ''
    return {
        'wkt': wkt,
        'wkid': _int_text(_text(node, 'WKID'), 0),
        'latest_wkid': _int_text(_text(node, 'LatestWKID'), 0),
        'vcs_wkid': _int_text(_text(node, 'VCSWKID'), 0),
        'latest_vcs_wkid': _int_text(_text(node, 'LatestVCSWKID'), 0),
        'name': _text(node, 'Name') or '',
        'x_origin': _float_text(_text(node, 'XOrigin'), 0.0),
        'y_origin': _float_text(_text(node, 'YOrigin'), 0.0),
        'xy_scale': _float_text(_text(node, 'XYScale'), 0.0),
        'z_origin': _float_text(_text(node, 'ZOrigin'), 0.0),
        'z_scale': _float_text(_text(node, 'ZScale'), 0.0),
        'm_origin': _float_text(_text(node, 'MOrigin'), 0.0),
        'm_scale': _float_text(_text(node, 'MScale'), 0.0),
        'xy_tolerance': _float_text(_text(node, 'XYTolerance'), 0.0),
        'z_tolerance': _float_text(_text(node, 'ZTolerance'), 0.0),
        'm_tolerance': _float_text(_text(node, 'MTolerance'), 0.0),
        'high_precision': _bool_text(_text(node, 'HighPrecision'), False),
        'left_longitude': _float_text(_text(node, 'LeftLongitude'), 0.0),
        'srs_type': _local_name(srs_type_attr.rsplit(':', 1)[-1]),
    }


def _parse_extent(node: Optional[ET.Element]) -> Optional[Tuple[float, float, float, float]]:
    """解析根级 ``<Extent>``,返回 ``(xmin, ymin, xmax, ymax)``。

    真实文件里 ``<Extent>`` 是 ``typens:EnvelopeN``,子元素为
    ``XMin/YMin/XMax/YMax``;也可能是 ``<Extent xsi:nil="true"/>``(GDAL 新写
    的图层如此),此时返回 ``None``。注意 ``<Extent>`` 内部还会嵌套一个
    ``<SpatialReference>`` —— 那个不是我们要的根级坐标系,解析时务必只取直接子。
    """
    if node is None:
        return None
    # xsi:nil='true' 表示空范围
    nil = node.get('{http://www.w3.org/2001/XMLSchema-instance}nil') \
        or node.get('xsi:nil')
    if _bool_text(nil, False):
        return None
    xmin = _text(node, 'XMin')
    ymin = _text(node, 'YMin')
    xmax = _text(node, 'XMax')
    ymax = _text(node, 'YMax')
    if xmin is None and ymin is None and xmax is None and ymax is None:
        return None
    return (_float_text(xmin), _float_text(ymin),
            _float_text(xmax), _float_text(ymax))


def parse_definition_xml(xml: str) -> Dict[str, Any]:
    """解析 ``DEFeatureClassInfo`` / ``DETableInfo`` 等 Catalog 定义 XML。

    对应 GDAL 的 ``BuildGeometryColumnGDBv10`` + ``BuildLayerDefinition``
    (ogropenfilegdblayer.cpp:136/364),但**只做元数据解析**,不以它为准构造
    字段(见模块 docstring 的第 2 条)。

    返回字典的键(几何相关的键在非空间表上为默认值/空):

    ==================  ==================================================
    ``name``            逻辑名(``<Name>``)
    ``path``            ``<CatalogPath>``
    ``dataset_type``    ``<DatasetType>``,如 ``esriDTFeatureClass``
    ``alias_name``      ``<AliasName>``
    ``shape_type``      ``<ShapeType>``,如 ``esriGeometryPolygon``
    ``geometry_kind``   归一化后的 ``point``/``polyline``/``polygon``/
                        ``multipoint``/``multipatch``/``null``
    ``shape_field_name`` ``<ShapeFieldName>``
    ``has_z`` / ``has_m`` ``<HasZ>`` / ``<HasM>``(布尔)
    ``has_spatial_index``
    ``oid_field_name``  ``<OIDFieldName>``
    ``wkid`` / ``latest_wkid``   ``<SpatialReference>` 里的整数值
    ``wkt``             ``<SpatialReference>/<WKT>``(ESRI 序列化对象会被忽略)
    ``spatial_ref``     :class:`GdbSpatialRef`
    ``spatial_reference``  上面那个 SRS 的原始字典(含量化参数)
    ``extent``          ``(xmin, ymin, xmax, ymax)`` 或 ``None``
    ``fields``          ``[{name,type,fgft,length,nullable,alias,default,...}]``
    ``is_feature_class``/``is_table``  布尔
    ``root_tag``        根元素本地名
    ==================  ==================================================

    :raises GdbFormatError: ``xml`` 为空或根元素不可识别。
    """
    root = _parse_xml_root(xml)
    if root is None:
        raise GdbFormatError('Definition XML 为空或不可解析')

    root_tag = _local_name(root.tag)
    if root_tag not in _DEFINITION_ROOTS:
        # 也允许别的根(如 DERelationshipClass),只是标记为未知
        pass

    is_fc = root_tag == 'DEFeatureClassInfo'
    is_table = root_tag == 'DETableInfo'

    shape_type = _text(root, 'ShapeType') or ''
    shape_field_name = _text(root, 'ShapeFieldName') or ''
    has_z = _bool_text(_text(root, 'HasZ'), False)
    has_m = _bool_text(_text(root, 'HasM'), False)

    # 字段:优先 <Fields>/<Field>,否则 ArcGIS/GDAL 的 <GPFieldInfoExs>/<GPFieldInfoEx>
    field_nodes = _children(_child(root, 'Fields'), 'Field')
    if not field_nodes:
        field_nodes = _children(_child(root, 'GPFieldInfoExs'), 'GPFieldInfoEx')
    fields = [_parse_field_entry(n) for n in field_nodes]
    fields = [f for f in fields if f['name']]

    # 根级 <SpatialReference>(注意 <Extent> 里那个嵌套的不要)
    srs_node = _child(root, 'SpatialReference')
    srs = _parse_spatial_reference(srs_node)
    spatial_ref = GdbSpatialRef(
        wkid=srs['wkid'],
        latest_wkid=srs['latest_wkid'],
        wkt=srs['wkt'],
        name=srs['name'],
    )

    extent = _parse_extent(_child(root, 'Extent'))

    return {
        'name': _text(root, 'Name') or '',
        'path': _text(root, 'CatalogPath') or '',
        'dataset_type': _text(root, 'DatasetType') or '',
        'alias_name': _text(root, 'AliasName') or '',
        'oid_field_name': _text(root, 'OIDFieldName') or '',
        'shape_type': shape_type,
        'geometry_kind': _esri_geometry_kind(shape_type),
        'shape_field_name': shape_field_name,
        'has_z': has_z,
        'has_m': has_m,
        'has_spatial_index': _bool_text(_text(root, 'HasSpatialIndex'), False),
        'has_oid': _bool_text(_text(root, 'HasOID'), True),
        'area_field_name': _text(root, 'AreaFieldName') or '',
        'length_field_name': _text(root, 'LengthFieldName') or '',
        'is_time_in_utc': _bool_text(_text(root, 'IsTimeInUTC'), True),
        'wkid': srs['wkid'],
        'latest_wkid': srs['latest_wkid'],
        'wkt': srs['wkt'],
        'spatial_ref': spatial_ref,
        'spatial_reference': srs,
        'extent': extent,
        'fields': fields,
        'is_feature_class': is_fc,
        'is_table': is_table,
        'root_tag': root_tag,
    }


def _esri_geometry_kind(shape_type: str) -> str:
    """ESRI 几何字符串 -> 归一化几何种类。

    映射表出处 ``filegdbtable.cpp:4422`` 的 ``AssocESRIGeomTypeToOGRGeomType``
    (那里是映射到 OGR 类型;这里只做归一化分类)。
    """
    mapping = {
        'esrigeometrypoint': 'point',
        'esrigeometrymultipoint': 'multipoint',
        'esrigeometryline': 'polyline',
        'esrigeometrypolyline': 'polyline',
        'esrigeometrypolygon': 'polygon',
        'esrigeometrymultipatch': 'multipatch',
    }
    return mapping.get((shape_type or '').lower(), 'null' if not shape_type else 'unknown')


def definition_to_fields(defn: Dict[str, Any],
                         skip_oid: bool = False,
                         skip_geometry: bool = False) -> List[GdbField]:
    """把 :func:`parse_definition_xml` 的结果转成 :class:`GdbField` 列表。

    对应 ``BuildLayerDefinition`` 里 FGFT_* -> OGR 的映射
    (ogropenfilegdblayer.cpp:566)的逆过程。``<Fields>/<Field>`` 与
    ``<GPFieldInfoExs>/<GPFieldInfoEx>`` 两种写法都已归一化,故这里直接消费
    ``defn['fields']``。

    几何字段会转成 :class:`GdbGeomField`(带上 WKT);OID 字段保留为
    ``FGFT_OBJECTID``。用 ``skip_oid`` / ``skip_geometry`` 可得到纯属性字段表
    (即 GDAL 暴露给 OGR 的那种)。

    :param defn: :func:`parse_definition_xml` 的返回值(也接受带 ``fields``
        键的任意字典,方便调用方手工拼)。
    """
    out: List[GdbField] = []
    wkt = defn.get('wkt') or ''
    for raw in defn.get('fields') or []:
        fgft = raw.get('fgft', C.FGFT_UNDEFINED)
        if fgft == C.FGFT_UNDEFINED:
            fgft = fgft_field_type(raw.get('type', ''))
        name = raw.get('name', '')
        if not name:
            continue
        if fgft == C.FGFT_OBJECTID and skip_oid:
            continue
        if fgft == C.FGFT_GEOMETRY and skip_geometry:
            continue

        length = int(raw.get('length') or 0)
        if fgft == C.FGFT_STRING and length <= 0:
            length = C.DEFAULT_STRING_LENGTH

        common = dict(
            name=name,
            field_type=fgft,
            length=length,
            nullable=bool(raw.get('nullable', True)),
            required=bool(raw.get('required', False)),
            editable=bool(raw.get('editable', True)),
            alias=raw.get('alias', '') or '',
            default_value=raw.get('default'),
        )
        if fgft == C.FGFT_GEOMETRY:
            out.append(GdbGeomField(wkt=wkt, **common))
        else:
            out.append(GdbField(**common))
    return out


# ============================================================================
# 生成 Catalog Definition XML
#
# 结构逐项对应 OGROpenFileGDBLayer::RefreshXMLDefinitionInMemory
# (ogropenfilegdblayer_write.cpp:2616)与 XMLSerializeGeomFieldBase(:159)。
#
# ⚠️ 与真实 ArcGIS/GDAL 的一处**有意差异**:任务书要求要素类 XML 里出现
# <Fields>/<Field> 结构,而真实 ArcGIS 10.x 用的是 <GPFieldInfoExs>/
# <GPFieldInfoEx>。本模块**两者都写**(<Fields> 在前),既满足调用方约定,又保持
# 对 ArcGIS 的可读性;读取时 parse_definition_xml 也两者都认。
# ============================================================================

_XMLNS_TYPENS = 'http://www.esri.com/schemas/ArcGIS/10.3'
_XMLNS_TYPENS_PRO = 'http://www.esri.com/schemas/ArcGIS/10.8'
_XMLNS_XSI = 'http://www.w3.org/2001/XMLSchema-instance'
_XMLNS_XS = 'http://www.w3.org/2001/XMLSchema'

#: CLSID 常量(RefreshXMLDefinitionInMemory,:2731)。
_CLSID_FEATURE_CLASS = '{52353152-891A-11D0-BEC6-00805F7C4268}'
_CLSID_TABLE = '{7A566981-C114-11D2-8A28-006097AFF44E}'


def _make_root(tag: str, pro: bool = False) -> ET.Element:
    """建根元素并挂上 typens/xsi/xs 命名空间声明(与 GDAL 写法一致)。"""
    root = ET.Element('typens:' + tag)
    root.set('xmlns:typens', _XMLNS_TYPENS_PRO if pro else _XMLNS_TYPENS)
    root.set('xmlns:xsi', _XMLNS_XSI)
    root.set('xmlns:xs', _XMLNS_XS)
    root.set('xsi:type', 'typens:' + tag)
    return root


def _append(parent: ET.Element, tag: str, value: Any) -> ET.Element:
    """追加 ``<tag>value</tag>``(``value is None`` 时追加空元素)。"""
    elem = ET.SubElement(parent, tag)
    if value is not None:
        elem.text = str(value)
    return elem


def _coerce_field(field: Any) -> Dict[str, Any]:
    """把 :class:`GdbField` 或 dict 归一化成统一的字段描述字典。"""
    if isinstance(field, GdbField):
        return {
            'name': field.name,
            'fgft': field.field_type,
            'length': field.length,
            'nullable': field.nullable,
            'required': field.required,
            'editable': field.editable,
            # GdbField.editable 默认就是 False,不能据此断定"用户要求不可编辑",
            # 故非显式来源时不写 <Editable>。
            'editable_explicit': False,
            'alias': field.alias,
            'default': field.default_value,
            'domain': '',
        }
    if isinstance(field, dict):
        raw_type = field.get('type', field.get('fgft', C.FGFT_STRING))
        if isinstance(raw_type, str):
            if raw_type.lower().startswith('esrifieldtype'):
                fgft = fgft_field_type(raw_type)
            else:
                fgft = getattr(C, 'FGFT_' + raw_type.upper(), C.FGFT_STRING)  # type: ignore[arg-type]
        else:
            fgft = int(raw_type)
        return {
            'name': field.get('name', ''),
            'fgft': fgft,
            'length': int(field.get('length') or 0),
            'nullable': bool(field.get('nullable', True)),
            'required': bool(field.get('required', False)),
            'editable': bool(field.get('editable', True)),
            # dict 里显式写了 editable 才把它写进 XML
            'editable_explicit': 'editable' in field,
            'alias': field.get('alias', ''),
            'default': field.get('default', field.get('default_value')),
            'domain': field.get('domain', ''),
        }
    raise GdbFormatError(f'无法识别的字段定义: {field!r}')


def _field_length(desc: Dict[str, Any]) -> int:
    """确定 ``<Length>``。字符串取字段宽度(0 则用默认 255),其余用定长值。"""
    esri = esri_field_type(desc['fgft'])
    if desc['fgft'] == C.FGFT_STRING:
        length = int(desc.get('length') or 0)
        return length if length > 0 else C.DEFAULT_STRING_LENGTH
    fixed = _ESRI_TYPE_LENGTH.get(esri.lower())
    if fixed is not None:
        return fixed
    return int(desc.get('length') or 0)


def _spatial_ref_parts(spatial_ref: Any) -> Dict[str, Any]:
    """归一化``空间参考``入参(:class:`GdbSpatialRef` / dict / None)。"""
    if spatial_ref is None:
        return {'wkt': '', 'wkid': 0, 'latest_wkid': 0,
                'x_origin': -400.0, 'y_origin': -400.0,
                'xy_scale': 1e9, 'z_origin': -100000.0, 'z_scale': 10000.0,
                'm_origin': -100000.0, 'm_scale': 10000.0,
                'xy_tolerance': 1e-9, 'z_tolerance': 0.001, 'm_tolerance': 0.001}
    if isinstance(spatial_ref, GdbSpatialRef):
        return {
            'wkt': spatial_ref.wkt,
            'wkid': spatial_ref.wkid,
            'latest_wkid': spatial_ref.latest_wkid or spatial_ref.wkid,
            'x_origin': -400.0, 'y_origin': -400.0, 'xy_scale': 1e9,
            'z_origin': -100000.0, 'z_scale': 10000.0,
            'm_origin': -100000.0, 'm_scale': 10000.0,
            'xy_tolerance': 1e-9, 'z_tolerance': 0.001, 'm_tolerance': 0.001,
        }
    if isinstance(spatial_ref, dict):
        out = {
            'wkt': spatial_ref.get('wkt', ''),
            'wkid': int(spatial_ref.get('wkid') or 0),
            'latest_wkid': int(spatial_ref.get('latest_wkid')
                               or spatial_ref.get('wkid') or 0),
            'x_origin': -400.0, 'y_origin': -400.0, 'xy_scale': 1e9,
            'z_origin': -100000.0, 'z_scale': 10000.0,
            'm_origin': -100000.0, 'm_scale': 10000.0,
            'xy_tolerance': 1e-9, 'z_tolerance': 0.001, 'm_tolerance': 0.001,
        }
        for key in list(out):
            if key in spatial_ref and spatial_ref[key] not in (None, ''):
                out[key] = spatial_ref[key]
        return out
    raise GdbFormatError(f'无法识别的空间参考: {spatial_ref!r}')


def _srs_kind(wkt: str) -> str:
    """由 WKT 首关键字推 ESRI 坐标系类型名(没有 WKT 时为 Unknown)。"""
    head = (wkt or '').strip().upper()
    if head.startswith('PROJCS'):
        return 'ProjectedCoordinateSystem'
    if head.startswith('GEOGCS'):
        return 'GeographicCoordinateSystem'
    return 'UnknownCoordinateSystem'


def _append_spatial_reference(parent: ET.Element, parts: Dict[str, Any]) -> ET.Element:
    """追加 ``<SpatialReference>``。对应 ``XMLSerializeGeomFieldBase``。"""
    node = ET.SubElement(parent, 'SpatialReference')
    node.set('xsi:type', 'typens:' + _srs_kind(parts.get('wkt', '')))
    if parts.get('wkt'):
        _append(node, 'WKT', parts['wkt'])
    for tag, key in (('XOrigin', 'x_origin'), ('YOrigin', 'y_origin'),
                     ('XYScale', 'xy_scale'), ('ZOrigin', 'z_origin'),
                     ('ZScale', 'z_scale'), ('MOrigin', 'm_origin'),
                     ('MScale', 'm_scale'), ('XYTolerance', 'xy_tolerance'),
                     ('ZTolerance', 'z_tolerance'), ('MTolerance', 'm_tolerance')):
        _append(node, tag, '%.17g' % float(parts.get(key, 0.0)))
    _append(node, 'HighPrecision', 'true')
    wkid = int(parts.get('wkid') or 0)
    latest = int(parts.get('latest_wkid') or 0)
    if wkid > 0:
        _append(node, 'WKID', wkid)
    if latest > 0:
        _append(node, 'LatestWKID', latest)
    return node


def _append_extent(parent: ET.Element, extent: Any, parts: Dict[str, Any]) -> ET.Element:
    """追加 ``<Extent>``。``extent`` 为 4 元组时生成 EnvelopeN,否则空 ``xsi:nil``。"""
    node = ET.SubElement(parent, 'Extent')
    if not extent:
        node.set('xsi:nil', 'true')
        return node
    node.set('xsi:type', 'typens:EnvelopeN')
    xmin, ymin, xmax, ymax = extent
    _append(node, 'XMin', '%.17g' % float(xmin))
    _append(node, 'YMin', '%.17g' % float(ymin))
    _append(node, 'XMax', '%.17g' % float(xmax))
    _append(node, 'YMax', '%.17g' % float(ymax))
    # 真实文件里 Extent 内部还嵌一个 SpatialReference
    _append_spatial_reference(node, parts)
    return node


def _append_fields_block(parent: ET.Element, entries: List[Dict[str, Any]]) -> None:
    """追加 ``<Fields><Field>...</Field></Fields>``(任务书要求的简化结构)。"""
    fields_node = ET.SubElement(parent, 'Fields')
    for desc in entries:
        field_node = ET.SubElement(fields_node, 'Field')
        _append(field_node, 'Name', desc['name'])
        _append(field_node, 'Type', esri_field_type(desc['fgft']))
        _append(field_node, 'Length', _field_length(desc))
        _append(field_node, 'IsNullable', 'true' if desc.get('nullable') else 'false')
        _append(field_node, 'AliasName', desc.get('alias') or desc['name'])
        if desc.get('default') is not None:
            _append(field_node, 'DefaultValue', desc['default'])


def _append_gpfieldinfoexs(parent: ET.Element, entries: List[Dict[str, Any]]) -> None:
    """追加 ArcGIS/GDAL 真正使用的 ``<GPFieldInfoExs>`` 块。

    逐元素顺序对应 ``CreateXMLFieldDefinition``(ogropenfilegdblayer_write.cpp:768)。
    """
    gp_fields = ET.SubElement(parent, 'GPFieldInfoExs')
    gp_fields.set('xsi:type', 'typens:ArrayOfGPFieldInfoEx')
    for desc in entries:
        item = ET.SubElement(gp_fields, 'GPFieldInfoEx')
        item.set('xsi:type', 'typens:GPFieldInfoEx')
        _append(item, 'Name', desc['name'])
        if desc.get('alias'):
            _append(item, 'AliasName', desc['alias'])
        if desc.get('default') is not None:
            _append(item, 'DefaultValue', desc['default'])
        _append(item, 'FieldType', esri_field_type(desc['fgft']))
        if desc['fgft'] in (C.FGFT_OBJECTID, C.FGFT_GEOMETRY):
            _append(item, 'IsNullable', 'true' if desc.get('nullable') else 'false')
            _append(item, 'Required', 'true')
            if desc['fgft'] == C.FGFT_OBJECTID:
                _append(item, 'Editable', 'false')
        else:
            if desc.get('nullable'):
                _append(item, 'IsNullable', 'true')
            if desc.get('required'):
                _append(item, 'Required', 'true')
            if desc.get('editable') is False and desc.get('editable_explicit'):
                _append(item, 'Editable', 'false')
        _append(item, 'Length', _field_length(desc))
        _append(item, 'Precision', 0)
        _append(item, 'Scale', 0)
        if desc.get('domain'):
            _append(item, 'DomainName', desc['domain'])


def _normalize_geometry_type(geometry_type: Any) -> str:
    """几何类型归一化成 ESRI 字符串。

    接受 ``'esriGeometryPolygon'``、``'polygon'``、``FGTGT_*`` 数值、
    ``ShapeType.*`` 数值。
    """
    if geometry_type is None or geometry_type == '':
        return ''
    if isinstance(geometry_type, str):
        if geometry_type.lower().startswith('esrigeometry'):
            return geometry_type
        table = {
            'point': 'esriGeometryPoint',
            'multipoint': 'esriGeometryMultipoint',
            'polyline': 'esriGeometryPolyline',
            'line': 'esriGeometryPolyline',
            'polygon': 'esriGeometryPolygon',
            'multipatch': 'esriGeometryMultiPatch',
        }
        return table.get(geometry_type.strip().lower(), geometry_type)
    # 数值:FGTGT_* 或 ShapeType.*
    table_num = {
        C.FGTGT_POINT: 'esriGeometryPoint',
        C.FGTGT_MULTIPOINT: 'esriGeometryMultipoint',
        C.FGTGT_LINE: 'esriGeometryPolyline',
        C.FGTGT_POLYGON: 'esriGeometryPolygon',
        C.FGTGT_MULTIPATCH: 'esriGeometryMultiPatch',
        C.ShapeType.POINT: 'esriGeometryPoint',
        C.ShapeType.MULTIPOINT: 'esriGeometryMultipoint',
        C.ShapeType.POLYLINE: 'esriGeometryPolyline',
        C.ShapeType.POLYGON: 'esriGeometryPolygon',
    }
    return table_num.get(int(geometry_type), '')


def _split_fields(fields: Any,
                  oid_field: str,
                  shape_field: str) -> Tuple[List[Dict[str, Any]],
                                             Dict[str, Any],
                                             Optional[Dict[str, Any]]]:
    """拆分字段列表。

    :returns: ``(用户属性字段, OID 描述, 几何描述)``。OID/几何字段由参数显式
        给出(或从用户列表里按 FGFT 类型拣出),不会重复出现在属性字段里。
    """
    entries = [_coerce_field(f) for f in (fields or [])]
    oid_desc = None
    geom_desc = None
    attrs: List[Dict[str, Any]] = []
    for desc in entries:
        if not desc.get('name'):
            continue
        if desc['fgft'] == C.FGFT_OBJECTID:
            if oid_desc is None:
                desc['name'] = desc['name'] or oid_field
                oid_desc = desc
            continue
        if desc['fgft'] == C.FGFT_GEOMETRY:
            if geom_desc is None:
                geom_desc = desc
            continue
        attrs.append(desc)

    if oid_desc is None:
        oid_desc = {'name': oid_field, 'fgft': C.FGFT_OBJECTID, 'length': 4,
                    'nullable': False, 'required': True, 'editable': False,
                    'alias': '', 'default': None}
    if geom_desc is not None:
        # 用户传入了几何字段:用它的名字(可能不是默认的 Shape)
        geom_desc['name'] = geom_desc['name'] or shape_field
    return attrs, oid_desc, geom_desc


def _serialize(root: ET.Element, indent: bool = True) -> str:
    """序列化并补上 XML 声明 ``<?xml version="1.0" encoding="UTF-8"?>``。"""
    if indent:
        try:
            ET.indent(root, space='  ')
        except AttributeError:  # pragma: no cover - py<3.9
            pass
    body = ET.tostring(root, encoding='unicode')
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + body


def make_feature_class_xml(name: str, fields: Any, spatial_ref: Any,
                           geometry_type: Any, has_z: bool = False,
                           has_m: bool = False, oid_field: str = 'OBJECTID',
                           shape_field: str = 'Shape', **kw: Any) -> str:
    """生成 ArcGIS 10.x 兼容的 ``DEFeatureClassInfo`` XML。

    结构对应 ``RefreshXMLDefinitionInMemory``
    (ogropenfilegdblayer_write.cpp:2616)与 ``XMLSerializeGeomFieldBase``
    (:159),并额外写出任务书要求的 ``<Fields>`` 块。

    :param name: 图层名。
    :param fields: 属性字段,元素为 :class:`GdbField` 或
        ``{'name','type','length','nullable','alias','default'}``。
        列表里若含 ``FGFT_OBJECTID``/``FGFT_GEOMETRY`` 字段,会按类型归位到
        OID/几何槽(名字以描述符为准);否则用 ``oid_field``/``shape_field`` 合成。
    :param spatial_ref: :class:`GdbSpatialRef`、含 ``wkt``/``wkid``/``latest_wkid``
        的 dict,或 ``None``(生成 UnknownCoordinateSystem)。
    :param geometry_type: ``'esriGeometryPolygon'`` / ``'polygon'`` / ``FGTGT_*``
        / ``ShapeType.*`` 均可。
    :param has_z: ``<HasZ>``。
    :param has_m: ``<HasM>``。
    :param oid_field: OID 字段名,默认 ``'OBJECTID'``。
    :param shape_field: 几何字段名,默认 ``'Shape'``。
    :param kw: 可选 ``catalog_path``、``alias_name``、``dsid``、``extent``、
        ``has_spatial_index``、``area_field_name``、``length_field_name``、
        ``shape_nullable``、``pro``(用 10.8 命名空间)、``is_time_in_utc``。
    """
    pro = bool(kw.get('pro', False))
    root = _make_root('DEFeatureClassInfo', pro=pro)

    catalog_path = kw.get('catalog_path') or ('\\' + name)
    alias_name = kw.get('alias_name', '')
    extent = kw.get('extent')

    attrs, oid_desc, geom_desc = _split_fields(fields, oid_field, shape_field)
    if geom_desc is None:
        geom_desc = {'name': shape_field, 'fgft': C.FGFT_GEOMETRY, 'length': 0,
                     'nullable': bool(kw.get('shape_nullable', True)),
                     'required': True, 'editable': True, 'alias': '',
                     'default': None}
    entries = [oid_desc, geom_desc] + attrs

    _append(root, 'CatalogPath', catalog_path)
    _append(root, 'Name', name)
    _append(root, 'ChildrenExpanded', 'false')
    _append(root, 'DatasetType', 'esriDTFeatureClass')
    if kw.get('dsid') is not None:
        _append(root, 'DSID', kw['dsid'])
    _append(root, 'Versioned', 'false')
    _append(root, 'CanVersion', 'false')
    if kw.get('configuration_keyword'):
        _append(root, 'ConfigurationKeyword', kw['configuration_keyword'])
    if pro:
        _append(root, 'RequiredGeodatabaseClientVersion', '13.2')
    _append(root, 'HasOID', 'true')
    _append(root, 'OIDFieldName', oid_desc['name'])

    _append_fields_block(root, entries)
    _append_gpfieldinfoexs(root, entries)

    _append(root, 'CLSID', _CLSID_FEATURE_CLASS)
    _append(root, 'EXTCLSID', '')
    if alias_name:
        _append(root, 'AliasName', alias_name)
    _append(root, 'ModelName', '')
    _append(root, 'HasGlobalID', 'false')
    _append(root, 'IsTimeInUTC',
            'true' if kw.get('is_time_in_utc', True) else 'false')

    _append(root, 'FeatureType', 'esriFTSimple')
    _append(root, 'ShapeType', _normalize_geometry_type(geometry_type))
    _append(root, 'ShapeFieldName', geom_desc['name'])
    _append(root, 'HasM', 'true' if has_m else 'false')
    _append(root, 'HasZ', 'true' if has_z else 'false')
    _append(root, 'HasSpatialIndex',
            'true' if kw.get('has_spatial_index', False) else 'false')
    _append(root, 'AreaFieldName', kw.get('area_field_name', ''))
    _append(root, 'LengthFieldName', kw.get('length_field_name', ''))

    parts = _spatial_ref_parts(spatial_ref)
    _append_extent(root, extent, parts)
    _append_spatial_reference(root, parts)
    _append(root, 'ChangeTracked', 'false')
    return _serialize(root)


def make_table_xml(name: str, fields: Any, oid_field: str = 'OBJECTID',
                   **kw: Any) -> str:
    """生成非空间表的 ``DETableInfo`` XML。

    与 :func:`make_feature_class_xml` 的区别(见
    ``RefreshXMLDefinitionInMemory``):根元素 ``typens:DETableInfo``,
    ``DatasetType`` 为 ``esriDTTable``,``CLSID`` 用
    ``{7A566981-C114-11D2-8A28-006097AFF44E}``,并且 **完全不含**
    ``FeatureType``/``ShapeType``/``ShapeFieldName``/``Extent``/
    ``SpatialReference`` 这一整块。
    """
    pro = bool(kw.get('pro', False))
    root = _make_root('DETableInfo', pro=pro)

    catalog_path = kw.get('catalog_path') or ('\\' + name)
    alias_name = kw.get('alias_name', '')

    attrs, oid_desc, _geom = _split_fields(fields, oid_field, '')
    entries = [oid_desc] + attrs

    _append(root, 'CatalogPath', catalog_path)
    _append(root, 'Name', name)
    _append(root, 'ChildrenExpanded', 'false')
    _append(root, 'DatasetType', 'esriDTTable')
    if kw.get('dsid') is not None:
        _append(root, 'DSID', kw['dsid'])
    _append(root, 'Versioned', 'false')
    _append(root, 'CanVersion', 'false')
    if kw.get('configuration_keyword'):
        _append(root, 'ConfigurationKeyword', kw['configuration_keyword'])
    if pro:
        _append(root, 'RequiredGeodatabaseClientVersion', '13.2')
    _append(root, 'HasOID', 'true')
    _append(root, 'OIDFieldName', oid_desc['name'])

    _append_fields_block(root, entries)
    _append_gpfieldinfoexs(root, entries)

    _append(root, 'CLSID', _CLSID_TABLE)
    _append(root, 'EXTCLSID', '')
    if alias_name:
        _append(root, 'AliasName', alias_name)
    _append(root, 'ModelName', '')
    _append(root, 'IsTimeInUTC',
            'true' if kw.get('is_time_in_utc', True) else 'false')
    return _serialize(root)


def make_workspace_xml(path: Any = '\\') -> str:
    """生成工作空间行用的 ``DEWorkspace`` XML。

    逐元素对应 ``CreateGDBItems`` 里写的常量串
    (ogropenfilegdbdatasource_write.cpp:920-937),包括 ``MajorVersion=3``/
    ``MinorVersion=0``/``BugfixVersion=0``。``GDB_Items`` 里那条 ``Path == '\\'``
    的记录就是它,GDAL 靠它拿到根组的 GUID(ogropenfilegdbdatasource.cpp:731)。

    :param path: ``<CatalogPath>``,默认 ``'\\'``(根)。
    """
    root = _make_root('DEWorkspace', pro=False)
    _append(root, 'CatalogPath', path if path is not None else '\\')
    _append(root, 'Name', None)
    _append(root, 'ChildrenExpanded', 'false')
    _append(root, 'WorkspaceType', 'esriLocalDatabaseWorkspace')
    _append(root, 'WorkspaceFactoryProgID', None)
    _append(root, 'ConnectionString', None)
    conn = ET.SubElement(root, 'ConnectionInfo')
    conn.set('xsi:nil', 'true')
    domains = ET.SubElement(root, 'Domains')
    domains.set('xsi:type', 'typens:ArrayOfDomain')
    _append(root, 'MajorVersion', '3')
    _append(root, 'MinorVersion', '0')
    _append(root, 'BugfixVersion', '0')
    return _serialize(root)
