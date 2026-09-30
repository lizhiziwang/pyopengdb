# pyopenfilegdb

纯 Python 读写 **Esri File Geodatabase(`.gdb` 文件夹数据库)**。

**不依赖 GDAL / fiona / pygdal / libgdal**,不调任何外部二进制,**运行时依赖为零** ——
只用标准库。

FileGDB 没有公开格式规范。本库的全部格式知识逆向自 GDAL 的
[`ogr/ogrsf_frmts/openfilegdb/`](https://github.com/OSGeo/gdal/tree/master/ogr/ogrsf_frmts/openfilegdb)
C++ 源码,以及对真实 ArcGIS 产出的 `.gdb` 的逐字节实测;每一处格式细节在代码注释里
都标了对应的 GDAL 出处。

```python
from pyopenfilegdb import OpenFileGDB

with OpenFileGDB.open('D:/data/2024年国土行政区划.gdb') as gdb:
    layer = gdb.get_layer('村行政区划')
    print(layer.record_count)               # 21217
    print([f.name for f in layer.fields])   # ['OBJECTID', 'BSM', 'YSDM', 'ZLDWDM', ...]

    for feat in layer.read_features(limit=3):
        print(feat.oid, feat.attributes['BSM'], feat.geometry.area())
```

（字段名随数据而定,先看一眼 `layer.fields`。）

---

## 目录

- [安装](#安装)
- [快速上手](#快速上手)
- [功能](#功能)
- [性能](#性能)
- [两个可选加速器](#两个可选加速器)
- [与 GDAL 的关系、以及刻意的边界](#与-gdal-的关系以及刻意的边界)
- [验证](#验证)
- [仓库结构](#仓库结构)
---

## 安装

```bash
pip install .                       # 纯 Python,零运行时依赖
pip install .[speed]                # 附带 numpy(可选加速器,见下)
pip install .[crs]                  # 附带 pyproj(坐标转换,见下)
pip install .[all]                  # 两个都要
```

要求 Python ≥ 3.9。**可选 C 扩展不编也能跑**,只是慢(见[性能](#性能));
编译方式见[两个可选加速器](#两个可选加速器)。

也可以不安装,直接把仓库根目录加进 `sys.path`。

---

## 快速上手

### 读

```python
from pyopenfilegdb import OpenFileGDB

gdb = OpenFileGDB.open('D:/data/x.gdb')                 # 只读
gdb = OpenFileGDB.open('D:/data/x.gdb', update=True)    # 可写

gdb.version                              # 3 = ArcGIS 10.x
gdb.list_feature_classes()               # ['村行政区划', '乡行政区划', ...]

layer = gdb.get_layer('村行政区划')        # 也接受物理名或 '\村行政区划'
layer.geometry_type                      # 'polygon'
layer.has_z, layer.has_m                 # False, False
layer.record_count                       # 有效记录数(已扣删除)
layer.extent                             # ⚠️ 存储的单调上界,不是真实范围
layer.fields                             # List[GdbField]
layer.spatial_ref                        # GdbSpatialRef(WKT + WKID)

# 三种 where;SQL 子集按三值逻辑求值,NULL 落在 True/False 之外
layer.read_features(where="县名 = '朝阳区' AND 面积 > 100")
layer.read_features(where=lambda f: f.oid % 2 == 1)
layer.read_features(bbox=(116.2, 39.8, 116.6, 40.1))    # 走存储包围盒粗筛
layer.read_features(limit=100, offset=50, fields=['县名'])
layer.read_feature(42)
```

### 写

```python
from pyopenfilegdb import OpenFileGDB, GdbField, GdbFeature, Geometry
from pyopenfilegdb import FGFT_STRING, FGFT_DOUBLE, FGFT_INT32

gdb = OpenFileGDB.create('D:/data/new.gdb')          # 路径必须不存在
layer = gdb.create_layer(
    '监测点',
    geometry_type='point',                           # point/multipoint/polyline/polygon/null
    fields=[
        GdbField('NAME', FGFT_STRING, length=64, nullable=False),
        GdbField('HEIGHT', FGFT_DOUBLE, alias='高程(m)'),
        GdbField('POP', FGFT_INT32),
    ],
)

oid = layer.write_feature({                          # 返回新 OID(从 1 开始)
    'NAME': 'A1', 'HEIGHT': 12.5, 'POP': 300,
    'Shape': Geometry.from_wkt('POINT(116.4 39.9)'),
})

feat = layer.read_feature(oid)                       # 读 → 改 → 写回
feat.attributes['POP'] = 301
layer.update_feature(feat)

layer.delete_feature(oid)
gdb.close()
```

改要素走 **读→改→写回**:`update_feature()` 是**整条记录替换**(对应 GDAL
`ISetFeature`),`attributes` 里没提到的字段会按字段定义落默认值/空值 —— 所以
只改一两个字段要先 `read_feature(oid)` 拿回完整的一条(它自带 `oid`)。

⚠️ **OID 在 `feature.oid` 上,不在 `attributes` 里**(等价于 OGR 的 FID,
读回来的 `attributes` 本来就不含 `OBJECTID`)。传 `GdbFeature` 给
`write_feature()` 时新 OID 会**写回** `feature.oid`(照 GDAL
`ICreateFeature` 结尾的 `poFeature->SetFID()`),所以 `write_feature(feat)`
之后可以直接 `update_feature(feat)`;传 dict 则取返回值。字段定义里**没有**的
键会被**静默忽略**,写错名字不会报错 —— 只会发现值没进去。

建出来的库**可以直接用 ArcGIS / QGIS 打开** —— 七张系统表与真实 ArcGIS 空白库
逐段字节一致(有测试守着)。

### 几何对象

`Geometry` 提供与 `OGRGeometry` 对应的度量、构造与拓扑判定,命名用 snake_case。

```python
g = feature.geometry

# 描述(近零成本:不物化坐标)
g.kind / .is_empty / .dimension / .point_count / .part_count
g.is_ring / .is_clockwise

# 度量
g.envelope()          # (xmin, ymin, xmax, ymax) | None
g.area() / .length()  # 非面/线恒为 0.0
g.centroid()          # (x, y) | None
g.distance(other)     # O(n·m),无 bbox 预筛

# 构造(返回新的 Geometry)
g.convex_hull() / .simplify(tolerance) / .segmentize(max_len)

# 缓冲区 —— 按 JTS/GEOS 算法自实现(结果恒 2D、Esri 绕向)
g.buffer(50)                       # 圆角圆帽,quad_segs=8
g.buffer(50, cap='flat')           # round / flat / square(也收 1/2/3)
g.buffer(50, join='mitre', mitre_limit=5.0)   # round / mitre / bevel
g.buffer(-20)                      # 负数 = 侵蚀
g.buffer(50, single_sided=True)    # 单侧(⚠️ 闭合输入上与 GEOS 不同,见下)

# overlay 四算子 —— 同样按 JTS/GEOS 自实现(结果恒 2D、Esri 绕向)
a.difference(b) / .union(b) / .intersection(b)
a.symmetric_difference(b)          # 别名 a.sym_difference(b)
# 混维度结果会给出 GEOMETRYCOLLECTION(纯内存类型,⚠️ 写不进 .gdb)

# 拓扑判定 —— relate() 直接给 9 位 DE-9IM 矩阵
g.relate(other)                                       # '212101212'
g.intersects / .disjoint / .contains / .within
g.covers / .covered_by / .touches / .crosses / .overlaps
g.equals(other)          # 拓扑等价(OGC T*F**FFF*)  ← 与 GEOS 一致
g.exactly_equals(other)  # 结构等价(同 OGRGeometry::Equals)
g.is_valid() / .is_simple()                           # ⚠️ 部分实现,见下

# 文本
g.wkt() / Geometry.from_wkt(s)                        # 多壳面 → MULTIPOLYGON
g.to_geojson() / g.__geo_interface__                  # 可直接喂 geopandas/folium
```

> **`equals()` vs `exactly_equals()`** —— 这两个不是同一个问题:
> ```python
> A = Geometry.from_wkt('POLYGON((0 0,10 0,10 10,0 10,0 0))')
> B = Geometry.from_wkt('POLYGON((0 0,5 0,10 0,10 10,0 10,0 0))')  # 顶点 (5,0) 在边上
> A.equals(B)          # True   —— 占同一块地(拓扑等价)
> A.exactly_equals(B)  # False  —— 顶点数不同(结构比较,同 GDAL)
> ```
> GDAL 的 Python 绑定把这两个都叫 `Equals`,是结构比较;GEOS 的 `equals` 才是拓扑。
> 本库用两个名字分开,免得踩坑。

### 坐标转换(可选,需要 pyproj)

`geometry.to_crs()` 转**一条**几何,`transform_layer()` 转**一个图层**。
走 [pyproj](https://pyproj4.github.io/pyproj/)(PROJ 的绑定),不碰 GDAL。

```python
from pyopenfilegdb import OpenFileGDB, Geometry
from pyopenfilegdb.coordinates_system import (create_transformed_layer,
                                              transform_layer,
                                              transformer_description,
                                              write_transformed)

# 一条几何:源坐标系必须显式给 —— 几何自己不知道自己是什么坐标系
g = Geometry.from_wkt('POINT(39394370.79 3179362.10)')     # CGCS2000 3 度带 zone 39
g.to_crs(4326, src=4527).wkt()
# 'POINT (115.91881971302487 28.725838734163123)'          # 经度, 纬度
g.to_crs(4326, src=4527).coordinates      # (115.91881971302487, 28.725838734163123)

# 一个图层:逐条产出**新**要素,不写盘,惰性解码
with OpenFileGDB.open('D:/data/x.gdb') as gdb:
    for feat in transform_layer(gdb.get_layer('村行政区划'), 4326):
        ...                       # feat.geometry 已经是经纬度

# 落盘:建同构图层 + 逐条写
with OpenFileGDB.open('D:/data/x.gdb') as src_db, \
     OpenFileGDB.create('D:/data/x_wgs84.gdb') as dst_db:
    src = src_db.get_layer('村行政区划')
    dst = create_transformed_layer(src, dst_db, 4326)
    write_transformed(src, dst, 4326)      # 返回写入条数
```

也可以当命令行脚本跑:

```bash
python -m pyopenfilegdb.coordinates_system 源.gdb 村行政区划 目标.gdb 4326 --limit 1000
```

**四个必须知道的行为**(都写在模块 docstring 里,都有测试守着):

| | |
|---|---|
| **轴序固定 lon/lat** | 一律 `always_xy=True`。去掉它之后 EPSG:4326 会按(纬度, 经度)的权威轴序解释,实测**静默**返回 `(inf, inf)` |
| **`errcheck` 拦不住域外点** | 实测把域外坐标喂进去,`errcheck=True` **不报错**,只给你一个有限但完全错误的数。所以本库自己做**量级体检**:源是地理坐标系却给出 ±180 以外的坐标(或反之)就发 `CoordTransformWarning`。这是**告警不是异常**,`check=False` 可关 |
| **返回新几何,不就地改** | ⚠️ 与 `OGRGeometry::Transform()` **相反**。本库的要素把解码后的几何缓存在 `feat.geometry` 上,就地改会静默污染那个缓存(盘上没变,再取一次却是改过的) |
| **可能只是 "Ballpark"** | CGCS2000 → WGS84 之间没装改化格网时,pyproj 会如实写明 `Ballpark geographic offset`。同一对连线的描述可以打出来看一眼:`transformer_description(layer, 4326)` |

几何语义:Z 一并转(**二维转换器对 Z 是原样透传**,交给 pyproj 判断)、M 原样保留、
multipatch 允许转(逐点操作,不依赖"环"模型,没有谓词那种语义鸿沟)、
环在内存里本就不闭合所以也不用补点。

---

## 功能

### 读 ✅

| | |
|---|---|
| 表结构 | `.gdbtable` 头 + 字段描述区、`.gdbtablx` 偏移表(`offset_size` 4/5/6) |
| 字段类型 | 全部 FGFT 类型 —— INT16/32/64、FLOAT32/64、STRING、DATE、TIME、DATETIME、`DATETIME_WITH_OFFSET`、GUID、BINARY、XML、RASTER |
| 几何 | POINT / MULTIPOINT / ARC(折线)/ POLYGON 的 XY / Z / M 各档;环闭合归一化、环方向 |
| 坐标系 | WKT + WKID + LatestWKID |
| 查询 | `=` `<>` `<` `<=` `>` `>=` `AND` `OR` `NOT` `IS [NOT] NULL` `IN` `LIKE`,SQL 三值逻辑 |
| 索引 | `.gdbindexes` / `.freelist` 解析 |

支持 FileGDB **version 3(ArcGIS 10.x)**。version 4(ArcGIS Pro)可读;
更老的 9.x 主动抛 `GdbVersionError`,不做兼容。

### 写 ✅

`create` 建库、`create_layer` / `delete_layer` 增删图层、`write_feature` /
`update_feature` / `delete_feature` 增删改要素。写入会做四重登记
(`GDB_SystemCatalog` / `GDB_Items` / `GDB_ItemRelationships` / Definition XML)。

### 几何空间计算 ✅

度量与描述、构造型(凸包 / Douglas-Peucker 简化 / segmentize / **buffer** /
**overlay 四算子**)、DE-9IM 拓扑判定、WKT + GeoJSON 出口。`buffer` 与
`difference` / `union` / `intersection` / `symmetric_difference`(别名
`sym_difference`)都按 GEOS 的上游 JTS 逐段复刻,并分别与 GEOS 3.13.1 对拍留数
(见[边界](#与-gdal-的关系以及刻意的边界))。

### 坐标转换 ✅(可选,需要 pyproj)

`Geometry.to_crs()` / `transform_layer()` / `create_transformed_layer()` +
`write_transformed()` + 命令行。见[快速上手](#坐标转换可选需要-pyproj)。

---

## 性能

基线取 `D:/work/2024年国土行政区划.gdb` 的 `村行政区划` 图层:
**21,217 条面要素、4,494 万个顶点、记录体 261.7 MB**。
同口径的 GDAL/OGR(只遍历、只取一个属性字段、不解几何)是 **742 ms**。

| 场景 | 本库 | 对照 |
|---|---:|---|
| 只读属性(不解几何) | **0.42 s** | GDAL/OGR 同口径 **0.3 ~ 0.742 s** |
| 只 seek + read 记录体,不解码 | 0.163 s | IO 理论下限 |
| 全量包围盒 | 0.892 s | 只要包围盒时的正解 |
| `bbox=` 小范围过滤(命中 245/21217) | 1.96 s | 存储包围盒粗筛 |
| **连几何一起读** | **1.50 s** | GDAL/OGR 同口径 **0.80 s** → **1.9×** |
| 连几何一起读(强制纯 Python,不开 C 扩展) | 65.4 s | 82× |

**三个关键设计:**

1. **惰性几何。** 从 `read_features()` 出来的要素只带几何 blob 的未解码引用,
   第一次访问 `feat.geometry` 才解析并缓存。所以"只读属性"的循环**一个顶点都不解** ——
   这正是 GDAL `SetIgnoredFields` 省掉的那一段。加一句 `g = feat.geometry`,
   代价就从 0.42 s 变成"4,494 万个顶点全解"。
2. **连几何的字节都不读。** 几何占记录体的 99.1%;`peek_envelope()` 先读 32 字节探针
   探出几何位置,再只读几何前后两段,中间那段留个文件偏移给惰性解析。判据是
   "跳读读得更少才跳",所以**读的字节数永不超过优化前**。17 个图层实测省 1.2× ~ 2346×。
3. **数组运算可交给 numpy**(可选)。面积 / 周长 / 质心在大环上快 **×21~25**,
   小几何仍走纯 Python(低于阈值数组化的固定开销划不来)。

**单条几何的空间计算不在遍历循环里**:`area()` / `centroid()` 这类只对一条几何跑,
大环上 numpy 快 ×21~25,小几何用纯 Python。

⚠️ **性能必须在终端里测,不能按 PyCharm 的 Debug 跑。** PyCharm 的调试器给每个
Python 帧装 line tracer,每条字节码回调一次 —— 本库是纯 Python,全额上税;
GDAL 是 C 扩展,税率近乎为零。在调试器里比较"纯 Python 实现 vs C 实现",
量到的是 **tracer 的税率差,不是算法差**(`tools/bench_trace.py` 可以复现这个机制)。

---

## 两个可选加速器

两个都**不改变功能、不改变结果**,都能在运行时关掉,都有差分闸门守着。

### 1. C 扩展 `pyopenfilegdb/_gdbaccel.c`

只包几何坐标数组的 delta-varint 解码(4 个入口),源码随包发布。

| | 纯 Python | 有 C 扩展 |
|---|---:|---:|
| varint 循环 | 336.8 ns/值 | **2.9 ns/值(116×)** |
| 含浮点缩放 + 拼数组的全路径 | 446.9 ns/值 | **37.9 ns/值(11.7×)** |
| `村行政区划` 整层 | 59.3 s | **~4.8 s** |

没编译、换了解释器、ABI 不匹配,都**自动回退纯 Python**,不用改一行代码。
`PYOPENFILEGDB_NO_ACCEL=1` 强制关。

> ⚠️ `.pyd` 绑解释器版本(`cp311` / `cp313` 各一份),**必须用跑代码的那个解释器编**,
> 否则会静默回退 —— 实测因此从 4.4 s 变成 56.5 s。

### 2. numpy

只用于**几何的数组运算**(面积 / 周长 / 质心的鞋带公式),大环上快 ×21~25。

numpy **不在 `dependencies` 里**,只在 `[project.optional-dependencies] speed`。
代码里是 `try: import numpy`,绝不硬 import;`PYOPENFILEGDB_NO_NUMPY=1` 强制关。
没有它,库的全部功能仍在。

⚠️ **依赖符号的路径一律不走 numpy** —— 环方向判定用成对求和还是顺序累加,
在近零面积环上符号可能翻,而那是**写盘正确性**,不是性能问题。

### 3. 还有 pyproj —— 但那个不是"加速器"

坐标转换靠 pyproj(见[快速上手](#坐标转换可选需要-pyproj))。它和前两个**性质不同**:
numpy 与 C 扩展是"有就快些、没有照样全功能",pyproj 是**"没有就没有这个功能"**。
所以它单独占一个 extra(`[crs]`),没装不影响读写,只是 `to_crs()` 会用不了并给出
一句带安装命令的 `ImportError`;CLI 则直接 `exit 2` 并说明原因。

---

## 与 GDAL 的关系、以及刻意的边界

| | |
|---|---|
| **格式读写** | 严格按 GDAL `openfilegdb` 实现,包括它那些反直觉的地方 |
| **拓扑谓词** | ⚠️ **例外。** `OGRGeometry::Intersects` / `Contains` / `Touches`… 在 GDAL 里**全部转手给 GEOS**,没有纯 C++ 可抄。本库按 OGC DE-9IM 规范自实现 |
| **`buffer`** | ⚠️ **同一个例外。** `OGRGeometry::Buffer` 也是一句转发给 GEOS 的话,GDAL 自己一行算法都没有。本库按 **GEOS 的上游 JTS**(`operation/buffer/*`)逐段复刻,并在上游分岔处以 **GEOS 为准**(共 5 处,见 DESIGN.md §2.23) |
| **overlay 四算子** | ⚠️ **还是这个例外。** `Difference` / `Union` / `Intersection` / `SymmetricDifference` 在 GDAL 里同样只是转发给 GEOS,所以按 **JTS `operation/overlayng/*`** 复刻。**算法上与 `buffer` 共用一套平面图**(`_planar.py`),差异都记在 DESIGN.md §2.24 |
| **坐标转换** | 与 GDAL 同一个来源:GDAL 的 `OGRCoordinateTransformation` 内部调的也是 **PROJ**,本库通过 pyproj 调同一个 PROJ。⚠️ 但这是**可选**功能,且不做 GDAL 那套 `OGRCreateCoordinateTransformation` 的坐标系推测(本库要求你显式给出源坐标系) |

这一档的诚实边界:

- **无 snap-rounding、无精确算术。** 用浮点方向判定,次 ULP 的退化构型可能判错。
  已知的一处:1e-9 高的薄片平移到 UTM 量级后只有 2.1 ULP 高,穿过它的子线段比一个
  ULP 还短,双精度网格上没有内部点可采样。GEOS 靠**组合式**拓扑图躲过,采样式实现躲不过。
- **`buffer` 与 overlay 四个算子都已实现,但都不是"GEOS 级"。** `buffer(distance,
  quad_segs=8, cap=..., join=..., mitre_limit=5.0, single_sided=False)`;
  overlay 是 `difference` / `union` / `intersection` / `symmetric_difference`
  (`sym_difference` 别名)。两者都按 GEOS 的上游 **JTS** 逐段复刻
  (`operation/buffer/*` 与 `operation/overlayng/*`),并各自与 GEOS 3.13.1 对拍留数
  (`tools/verify_buffer.py` / `tools/verify_overlay.py`)。
  `buffer` 自己的边界:① **无 snap-rounding** —— 只有靠它才能救回来的退化构型,
  GEOS 给结果而本库给空多边形;② 结果**恒为 2D**(Z/M 丢弃);③ 输出环是 **Esri
  绕向**(外壳 CW、洞 CCW);④ `multipatch` 输入抛 `NotImplementedError`;
  ⑤ **闭合输入(面 / 闭合折线)上的 `single_sided` 与 GDAL/GEOS 不同** —— GEOS 之后
  还有一步 `OverlayNG` + `Polygonizer`(取最大面),本库按 JTS 返回单侧缓冲。
  overlay 自己的边界:① 同样**无 snap-rounding**;② 与 GEOS **不逐位相同**(算出来的
  交点可能差 1 ulp),判定用"对称差面积 + 两结果总长之差 + Hausdorff"三条腿,不用
  对称差线长(GEOS 对近重合输入的对称差自身不稳);③ 结果同为 **Esri 绕向**;
  ④ 线结果**逐条节点边**输出(JTS 口径),GEOS 会合并成最长链,故对拍里那 44 对
  **列出原因与条数**排除;⑤ `GEOMETRYCOLLECTION` 是**纯内存**的结果类型,**写不进
  `.gdb`**;⑥ `multipatch` 与"输入含 GC"的处理各有明说的边界,见 `DESIGN.md` §2.24。
  与 GEOS 的对拍:`tools/verify_buffer.py` **1096 对**,逐位完全相同 **1015 对(92.6%)**,
  **0 对超差**,44 对按上一条(闭合输入的单侧缓冲)列明原因后排除;
  `tools/verify_overlay.py` **1264 对**,集合与结构全同 **1220 对**(其中对称差严格为空
  1205 对),**0 对超差**,44 对按"线结果逐条节点边输出"列明原因后排除。
  实测性能:`buffer` 在行政区语料取前 200 条(约 2.5 万顶点)、`d=50 m` 下约
  **74–109 ms/条**(见 DESIGN §2.23);overlay 在同一个语料上约 **5~6 ms/算子**。
- **`is_valid()` / `is_simple()` 是部分实现。** 只查:环是否闭合、顶点数是否满足类型
  下限、**单环自交**、洞是否落在壳内。**没查**的:洞之间的互不包含、multipoint 互不重合等。
  所以 `is_valid() == True` **不等于** OGC 有效 —— 这条写在 docstring 里,别当成全量校验。
- **`distance()` 是 `O(n·m)`**,没有线段包围盒预筛,大几何之间会明显慢。
- **GeoJSON 不按 RFC 7946 重绕环**(与 `OGR_G_ExportToJson` 默认一致 —— Esri 的绕向与
  RFC 7946 恰好相反),**且丢弃 M**(JSON 里没有 M 的位置)。
- **`extent` 是单调上界**,不是真实范围 —— 这是 ArcGIS 自己的语义,删要素后不会回缩。
- **坐标转换的两个边界。** (1) 目标图层的**量化参数是本库推导的**,不是从 GDAL
  抄的 —— GDAL 那边由创建选项或 ArcGIS 决定,没有纯 C++ 规则可抄。本库的规则
  复现了参照语料里 ArcGIS 自己写的值(投影档 `xy_scale=20000`/`xy_tolerance=1e-4`),
  但不敢说与 ArcGIS 在所有数据上都选得一样;差别只在文件体积与末位精度。
  (2) 精度受**目标坐标系**的默认量化限制:转到地理坐标系是 `2e-6` 度(≈ 0.2 m),
  这是 FileGDB + ArcGIS 的默认值,不是本库选的;要更细用 `quantization=` 覆盖。
  另外 **不做基准面改化** —— 没装 PROJ 的格网文件时就是 `Ballpark`,见上文。
- **不写 `.atx` / `.spx` / `.freelist`**,`.atx`/`.spx` 查询是桩。不写也能正常读。
- 写回**不是逐位幂等**的:全语料 21,217 条做"读→写→读",152 条(0.7%)字节不同,
  来自环顺序/绕向规范化加 ≤1 个量化步长的舍入。**面积全部一致**,是表示差异不是几何差异。
  要逐位等价的场景(哈希去重)不能拿本库的输出当原文。

---

## 验证

**383 个用例,四配置全绿**(改一个全局名字最容易漏掉某个引用点,所以每条都过):

```bash
P="D:/zsh/app/py_3.13.1/python"                      # 带 numpy + pyproj
V="E:/code/pyopenfilegdb/.venv/Scripts/python.exe"   # 3.11.9
PYTHONIOENCODING=utf-8 $P -m unittest discover -s tests        # 383 OK      33.9 s
PYOPENFILEGDB_NO_NUMPY=1 PYTHONIOENCODING=utf-8 $P -m unittest discover -s tests
#                                                              # 383 OK(skipped=5)  33.4 s
PYTHONIOENCODING=utf-8 $V -m unittest discover -s tests        # 383 OK      33.2 s
PYOPENFILEGDB_NO_ACCEL=1 PYTHONIOENCODING=utf-8 $P -m unittest discover -s tests
#                                                              # 383 OK(skipped=6)  74.9 s
```

⚠️ **做变异测试(或任何"改一行源码看测试红不红")时,前后都要
`rm -rf pyopenfilegdb/__pycache__`。** 变异前后**字节数相同**时,CPython 的
`(源文件 mtime, 源文件大小)` 失效判断会失灵,还原源码后旧字节码还在跑 ——
症状是"源码改回来了,行为却没变",很容易误判成源码里还有第二处 bug。

`tests/test_read.py` 与 `tests/test_coordinates_system.py` 的一部分需要真实样例库
(设 `PYOPENFILEGDB_TEST_GDB`,或放 `D:/work/*.gdb`);找不到就 skip。其余自给自足。
`test_coordinates_system.py` 一共 43 个用例,**没装 pyproj 时整组 43 个 skip**,
其中的 `TestTransformRealCorpus`(5 个)没有样例库时各自 skip —— pyproj 与样例库
都是可选/外部资源,缺了不等于代码错。

这个新测试文件里有一批用例是**守着具体陷阱**的,不是凑覆盖率:轴序必须 `always_xy`
(去掉就静默出 `inf`)、量级体检必须报、`origin` 必须严格小于一切坐标(否则写盘直接抛)、
目标图层 `wkid` 不能是 0、以及**转换不许就地改原几何**。最后一条尤其值得说:它最初
写成"读一次自己的坐标再比",结果**抓不住变异体** —— 读取本身就把派生视图物化进缓存了。
改成和独立参照几何比、并且 **point(走 `_coords`)与 polygon(走 `_flat`)两条存储路径
都覆盖**之后才真的能拦住。这批守卫全部用变异测试确认过会红。

除单元测试外,`tools/` 下有几个**差分闸门**(拿两个实现互相对拍,逐位相同才算过):

```bash
python tools/verify_accel.py     # C 路径 vs 纯 Python:8 个库 11,885 条 / 6,335,486 顶点
python tools/verify_numpy.py     # numpy 快路径 vs 顺序路径 + 拿 Fraction 当精确解校准
python tools/verify_wkt_roundtrip.py   # 全语料 WKT 出口体检(往返 + 幂等 + 维度不变)
python tools/verify_buffer.py    # buffer vs GEOS:1096 对,逐位相同 1015,超差 0
python tools/verify_overlay.py   # 四个 overlay 算子 vs GEOS:1264 对,全同 1220,超差 0
python tools/bench_read.py "D:/work/2024年国土行政区划.gdb" 村行政区划   # 复现上面的数字
```

**`tools/verify_topology.py`** 拿 GEOS 逐对比 `relate()` 与十个谓词,
**`tools/verify_buffer.py`** 逐对比 `buffer()`,**`tools/verify_overlay.py`** 逐对比四个
overlay 算子 —— `import shapely` / `import osgeo` **只出现在 `tools/` 里**,不进包、
不进 `tests/`、不进 `pyproject.toml`。没装就 skip 并 `exit 0`(那是正常状态,不是失败)。

> 它**真的抓出过两个真 bug**,同一根因:**重算出来的点不能拿去问浮点。**
> `relate()` 里的探针点有一半是算出来的(交点、中点),它们一般不精确落在对方线段上,
> 于是"在线上"被判成"在内部/外部",整块 DE-9IM 矩阵跟着塌。两个最小复现:
> ```python
> # 1) 交点坐标除不尽 —— 这不是退化构型,是极常见的情形
> LINESTRING(-0.0000003 0.0000004, 10.0000002 9.9999998).relate(LINESTRING(0 10, 10 0))
> # 老答 FF1FF0102,GEOS 答 0F1FF0102
>
> # 2) 共线重叠子段的中点 —— 与它自己比!
> POLYGON((0 0, 0.1 0.3, 0.4 0.1, 0 0)).relate(自己)
> # 老答 2F2F11212,正确 2FFF1FFF2
> ```
> 这两个都躲过了当时全部的手推真值表和不变量测试 —— 真值表用的全是整数坐标,
> 重算出来的点恰好逐位精确。**只有跑真参照实现才抓得到。**
> bug 2 在真实数据上会把两条**完全相同**的行政区面判成"部分重叠"。

> overlay 那一轮又补了一条教训:**判定用的度量本身也可能是错的。**
> 最初拿"两个结果的对称差的线长"当集合是否相同的主判据,结果 920 对里报了 15 对
> "集合不同",逐个查下来全是**次 ULP 的伪差**:同一个环把一个顶点的 y 动 **1 ulp**
> (5.68e-14),两个环的面积差 5.03e-14、**周长差 6.75e-14**,而 GEOS 自己给出的
> `symmetric_difference` 却是一个**周长 4.75** 的退化环 —— 那是 GEOS 对近重合输入
> 自身的数值不稳。于是换成三条腿:**对称差面积** + **两个结果各自总长之差** +
> **Hausdorff 距离**。换完还实测了每条腿各管什么:点被挪 0.4 只有 Hausdorff 抓得住
> (面积、长度都是恒 0)、线多一根 0.4 的刺由长度差抓、面多出一块 0.16 由面积差抓。
> 用 GDAL 当真参照时**没有 Hausdorff**(C 层没暴露),被挪开的点会**静默通过** ——
> 所以那个 oracle 会明说自己少了这条腿,而不是假装验过了。

---

## 仓库结构

```
pyopenfilegdb/     库本体
  geometry.py        Geometry —— 度量·描述·构造·DE-9IM 谓词·buffer·overlay·WKT/GeoJSON
  _geometry_ops.py   纯算法叶子模块(只认 array('d'),不 import 内部模块)
  _planar.py         平面图:节点化 / 面深度 / 结果环装配(buffer 与 overlay 共用)
  _buffer_ops.py     缓冲区算法(JTS/GEOS 逐段复刻;算法在 _planar 里)
  _overlay_ops.py    overlay 四算子(JTS operation/overlayng 逐段复刻)
  coordinates_system.py  坐标转换(靠 pyproj,可选;也可当命令行脚本跑)
  _gdbtable.py       .gdbtable 读 + 写
  _gdbtablx.py       .gdbtablx 读 + 写
  _esri_geometry.py  Esri 几何 blob ↔ Geometry
  _system_catalog.py 七张系统表 + GDB_Items
  core.py            OpenFileGDB
  layer.py           GdbLayer
  _gdbaccel.c        可选 C 扩展
tests/             383 个用例(test_read.py / test_coordinates_system.py 需样例库)
examples/          read / create_write 两个可运行示例
tools/             基准与差分验证脚本(不是库的一部分)
main.py            最小读示例(性能对照口径的来源)
```

库本体 **18,935 行**(包内 18,340 行 `.py` + 可选 C 扩展 `_gdbaccel.c` 的 595 行),测试 **5,927 行**。

---

## 许可

MIT
