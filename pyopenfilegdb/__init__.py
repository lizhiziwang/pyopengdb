"""pyopenfilegdb —— 纯 Python 的 Esri File Geodatabase(``.gdb``)读写库。

* **不依赖** GDAL / fiona / pygdal / libgdal,也不调任何外部进程;
  只用标准库(``struct`` / ``io`` / ``pathlib`` / ``dataclasses`` / ``typing``
  / ``re`` / ``xml.etree``)。
* 有一个**可选**的 C 加速模块(:mod:`._gdbaccel`),只覆盖几何坐标数组的
  delta-varint 解码这一条热点路径。没编出来就自动回退到等价、逐位相同的
  纯 Python 实现 —— **回退是正常状态,不是错误**(见 :mod:`._accel`)。
  想知道当前跑的是哪一条,看 :data:`HAS_ACCEL`。
* 格式知识全部来自对 GDAL ``ogr/ogrsf_frmts/openfilegdb`` C++ 源码的
  逐行研读 —— FileGDB 没有公开规范,该目录是事实上的唯一权威文档。
  每个模块的 docstring 与关键函数都标了对应源文件与行号。

快速上手::

    from pyopenfilegdb import OpenFileGDB, GdbField, Geometry
    from pyopenfilegdb import FGFT_STRING, FGFT_DOUBLE

    # ---- 读 ----
    with OpenFileGDB.open(r'D:/data/sample.gdb') as gdb:
        print(gdb.list_feature_classes())          # ['道路', '地块', ...]
        fc = gdb.get_layer('道路')
        print(fc.fields, fc.spatial_ref.wkt[:60], fc.record_count)
        for feat in fc.read_features(where='WIDTH > 3.5', limit=10):
            print(feat.oid, feat['NAME'], feat.geometry.wkt()[:50])

    # ---- 建 + 写 ----
    with OpenFileGDB.create(r'D:/data/new.gdb') as gdb:
        fc = gdb.create_layer(
            '点位', geometry_type='point',
            fields=[GdbField('NAME', FGFT_STRING, length=64),
                    GdbField('HEIGHT', FGFT_DOUBLE)],
        )
        fc.write_feature({
            'NAME': 'P1', 'HEIGHT': 12.5,
            'Shape': Geometry.from_wkt('POINT (116.39 39.91)'),
        })

模块划分(与任务书一一对应)::

    _constants.py      常量:FGFT_*/FGTGT_*/ShapeType/魔数/头部尺寸
    _util.py           字节读写原语(varuint 等)
    _gdbtablx.py       .gdbtablx 索引
    _gdbtable.py       .gdbtable(读 + 写)
    geometry.py        Geometry:几何对象(存储 + 空间计算 + WKT/GeoJSON 导出)
    _geometry_ops.py   几何算法(只认 array('d'),不依赖本包任何模块)
    _esri_geometry.py  Esri 私有几何 blob 的编解码 + WKT / GeoJSON 互转
    _datatypes.py      GdbField / GdbGeomField / GdbFeature / GdbItem / ...
    _system_catalog.py 系统表与 GDB_Items 的 Definition XML
    _gdbindex.py       .gdbindexes / .atx / .spx(Tier 3,只读为主)
    _gdb_template.py   空库的 7 张系统表样板(从真实 ArcGIS 库提取)
    layer.py           GdbLayer:单个图层的读写接口
    core.py            OpenFileGDB:目录级门面

可选加速(纯 Python 实现始终保留,是回退口径)::

    _gdbaccel.c        几何 XY / Z / M 数组的 delta-varint 解码(C)
    _accel.py          探测上面那个模块 + PYOPENFILEGDB_NO_ACCEL 开关

支持范围见仓库根目录的 ``STATUS.md``,设计说明见 ``DESIGN.md``。
"""
from __future__ import annotations

from ._accel import HAS_ACCEL
from ._constants import (
    FGFT_BINARY,
    FGFT_BIGINT,
    FGFT_BLOB,
    FGFT_DATE,
    FGFT_DATEONLY,
    FGFT_DATETIME,
    FGFT_DOUBLE,
    FGFT_FLOAT32,
    FGFT_FLOAT64,
    FGFT_GEOMETRY,
    FGFT_GLOBALID,
    FGFT_GUID,
    FGFT_INT16,
    FGFT_INT32,
    FGFT_INT64,
    FGFT_LONG,
    FGFT_OBJECTID,
    FGFT_RASTER,
    FGFT_SHORT,
    FGFT_SINGLE,
    FGFT_STRING,
    FGFT_TEXT,
    FGFT_TIME,
    FGFT_TIMEONLY,
    FGFT_XML,
    FGTGT_LINE,
    FGTGT_MULTIPATCH,
    FGTGT_MULTIPOINT,
    FGTGT_NONE,
    FGTGT_POINT,
    FGTGT_POLYGON,
    ShapeType,
)
from ._datatypes import (
    GdbError,
    GdbFeature,
    GdbField,
    GdbFormatError,
    GdbGeomField,
    GdbItem,
    GdbNotFoundError,
    GdbSpatialRef,
    GdbVersionError,
    GdbWriteError,
    datetime_to_gdb_days,
    gdb_days_to_datetime,
)
from ._esri_geometry import (from_geojson, from_wkt, to_geojson,
                              to_wkt)
from ._gdbtable import GdbTable
from ._gdbtablx import GdbTablx
from .geometry import Geometry
from .core import OpenFileGDB
from .layer import GdbLayer

__version__ = '0.1.0'

#: FileGDB 格式版本常量(``.gdbtable`` 头部第 0 个 uint32)。
FGDB_VERSION_10 = 3   # ArcGIS 10.x —— 本项目读写的主要目标
FGDB_VERSION_11 = 4   # 较新的 ArcGIS Pro —— 只读

__all__ = [
    # 主入口
    'OpenFileGDB', 'GdbLayer', 'GdbTable', 'GdbTablx',
    # 数据类型
    'GdbField', 'GdbGeomField', 'GdbFeature', 'GdbItem',
    'GdbSpatialRef',
    # 几何
    'Geometry', 'ShapeType', 'to_wkt', 'from_wkt', 'to_geojson',
    'from_geojson',
    # 字段类型常量
    'FGFT_INT16', 'FGFT_INT32', 'FGFT_INT64', 'FGFT_FLOAT32', 'FGFT_FLOAT64',
    'FGFT_STRING', 'FGFT_DATETIME', 'FGFT_DATE', 'FGFT_TIME',
    'FGFT_OBJECTID', 'FGFT_GEOMETRY', 'FGFT_BINARY', 'FGFT_RASTER',
    'FGFT_GUID', 'FGFT_GLOBALID', 'FGFT_XML',
    # ESRI 叫法的别名
    'FGFT_SHORT', 'FGFT_LONG', 'FGFT_SINGLE', 'FGFT_DOUBLE', 'FGFT_TEXT',
    'FGFT_BLOB', 'FGFT_BIGINT', 'FGFT_DATEONLY', 'FGFT_TIMEONLY',
    # 表级几何类型常量
    'FGTGT_NONE', 'FGTGT_POINT', 'FGTGT_MULTIPOINT', 'FGTGT_LINE',
    'FGTGT_POLYGON', 'FGTGT_MULTIPATCH',
    # 错误
    'GdbError', 'GdbFormatError', 'GdbVersionError', 'GdbNotFoundError',
    'GdbWriteError',
    # 时间辅助
    'gdb_days_to_datetime', 'datetime_to_gdb_days',
    # 可选 C 加速:导入时是否可用(False 表示走纯 Python 回退)
    'HAS_ACCEL',
    # 版本
    '__version__', 'FGDB_VERSION_10', 'FGDB_VERSION_11',
]
