# STATUS —— pyopenfilegdb 实现进度

> 纯 Python 读写 Esri File Geodatabase(`.gdb`),**不依赖 GDAL / fiona /
> pygdal / libgdal**,不调任何外部二进制,**运行时依赖为零**。
>
> 两个 **可选** 加速器,都不改变功能、都能在运行时关掉:
> * `pyopenfilegdb/_gdbaccel.c`(几何坐标数组的 delta-varint 解码)——编出来
>   就用(**59.3 s → 6.3 s**),没编出来 / 换了解释器就自动回退,结果逐位相同。
>   `PYOPENFILEGDB_NO_ACCEL=1` 强制关。见 `DESIGN.md` §2.19.4。
> * **numpy**(面积 / 周长 / 质心的数组运算)——装上就用(大环 **×21~25**),
>   没装或 `PYOPENFILEGDB_NO_NUMPY=1` 就回退纯 Python。**它不在
>   `dependencies` 里**,只在 `[project.optional-dependencies] speed`。
>   见 `DESIGN.md` §2.21。
>
> 格式知识全部逆向自 GDAL `ogr/ogrsf_frmts/openfilegdb/` C++ 源码 +
> 对真实 ArcGIS 产出的 `.gdb` 的逐字节实测。**FileGDB 没有公开规范。**
> ⚠️ 唯一的例外是**拓扑谓词**:GDAL 在那里委托 GEOS,没有 C++ 可抄,本库按
> OGC DE-9IM 自实现(边界与鲁棒性口径见 `DESIGN.md` §2.21)。
>
> 设计说明见 [`DESIGN.md`](DESIGN.md),使用示例见
> [`examples/`](examples/)。

最后更新:2026-09-29

---

## 一、总览

| 项 | 状态 |
|---|---|
| 目标格式 | FileGDB **version 3**(ArcGIS 10.x)—— 读写 |
| version 4(ArcGIS Pro) | 只读识别;**写入主动抛 `GdbVersionError`** |
| version 9.x | 不支持,抛 `GdbVersionError` |
| Tier 1 读路径 | ✅ **完成** |
| Tier 2 写路径 | ✅ **完成** |
| Tier 3 几何空间计算 | ✅ **完成**(`is_valid` 为部分实现,见 §四) |
| Tier 4 高级特性 | 🔶 大部分为**桩**,见 §四 |
| 测试 | **155 用例全绿**,四配置:py3.13+numpy、`PYOPENFILEGDB_NO_NUMPY=1`、3.11 `.venv`、`PYOPENFILEGDB_NO_ACCEL=1` |
| 性能(只读属性) | **0.42 s / 21,217 条**(GDAL/OGR 同口径 **0.3~0.742 s**)—— 见 §五 |
| 性能(要几何) | 有 C 扩展 + flat 容器 **1.50 s**;纯 Python 回退 **65.4 s**(GDAL/OGR 同口径 **0.80 s**)—— 见 §五 |
| 代码量 | 库 **12,834 行** + 测试 **2,812 行** = 15,646 行(不含示例、工具与 `_gdbaccel.c`) |

---

## 二、模块清单

| 文件 | 行数 | 状态 | 说明 |
|---|---:|---|---|
| `__init__.py` | 156 | ✅ | 对外 API 转出 |
| `_constants.py` | 437 | ✅ | FGFT_* / FGTGT_* / ShapeType / 魔数 / 尺寸 |
| `_datatypes.py` | 449 | ✅ | `GdbField` `GdbGeomField` `GdbFeature` `GdbItem` `GdbSpatialRef` + 异常体系 |
| `geometry.py` | 911 | ✅ | `Geometry`(由 `GdbGeometry` 改名)+ `_FlatCoords` + `_LazyGeometry`;度量·描述·构造·DE-9IM 谓词·WKT/GeoJSON 出口 |
| `_geometry_ops.py` | 1759 | ✅ | 纯算法叶子模块(只认 `array('d')`,不 import 内部模块):鞋带·绕向·线段相交·DE-9IM 装配·谓词·凸包·简化·segmentize·**numpy 可选快路径** |
| `_util.py` | 352 | ✅ | 字节读写原语(varuint / 字符串 / 类型转换) |
| `_gdbtable.py` | 1756 | ✅ | `.gdbtable` 读 + 写:表头、字段描述区、记录编解码(解码计划 + 跳读几何) |
| `_gdbtablx.py` | 331 | ✅ | `.gdbtablx` 读 + 写:偏移表、base block map |
| `_esri_geometry.py` | 1852 | ✅ | Esri 几何 blob ↔ `Geometry`,WKT / GeoJSON 互转,存储包围盒速查 |
| `_system_catalog.py` | 1718 | ✅ | 七张系统表 + `GDB_Items` 的 Definition XML |
| `_gdbindex.py` | 450 | 🔶 | `.gdbindexes` / `.freelist` 解析 ✅;`.atx` / `.spx` 查询为桩 |
| `_gdb_template.py` | 319 | ✅ | 空白库的固化字节(由 `tools/gen_template.py` 生成) |
| `_accel.py` | 81 | ✅ | 可选 C 加速模块的探测 + `PYOPENFILEGDB_NO_ACCEL` 开关 + `use()` |
| `_gdbaccel.c` | 595 | ✅ | ⚠️ **唯一的 C 扩展**:几何 varint 数组解码,4 个入口 —— `decode_xy_flat` / `decode_scalar_flat`(库的热路径,**写 `array('d')`**)+ `decode_xy` / `decode_scalar`(只给 `tools/` 做容器形状对照,库里无调用方)。可选,没编译就回退 |
| `core.py` | 1279 | ✅ | `OpenFileGDB`:open / create / 图层增删 / WHERE |
| `layer.py` | 848 | ✅ | `GdbLayer`:读 / 写 / 改 / 删 / extent / spatial_ref |

> ⚠️ `build/lib.win-amd64-cpython-311/` 是 setuptools 留下的**过期副本**
> (里面还是 `GdbGeometry`),**不要**改它,也不要当成当前代码。

> ⚠️ **更正历史遗留描述**:早先的 STATUS 里写的 `_rle.py`、以及
> "记录区按块分组、块内 RLE 压缩"、"`.gdbtablx` 每页 512 条" —— **全是错的**。
> `.gdbtable` 的记录区是扁平的 `[u32 长度][记录体]` 序列,**没有块、没有压缩**;
> `.gdbtablx` 的分页单位是 **1024** 条。详见 `DESIGN.md` §2.2 / §2.6。

---

## 三、API 一览

```python
from pyopenfilegdb import OpenFileGDB, GdbLayer, GdbFeature, Geometry, GdbField

# ---- 读 ----
gdb = OpenFileGDB.open('D:/data/x.gdb')          # 只读
gdb = OpenFileGDB.open('D:/data/x.gdb', update=True)   # 可写
gdb.list_feature_classes() -> List[str]
gdb.get_layer('道路') -> GdbLayer                # 也接受物理名或 '\道路'
gdb.close()                                       # 支持 with

layer.feature_class_name / .name / .physical_name
layer.fields -> List[GdbField]
layer.spatial_ref -> GdbSpatialRef
layer.geometry_type -> str                        # point/polyline/polygon/...
layer.has_z / .has_m
layer.record_count -> int                         # 有效记录数
layer.extent -> (xmin, ymin, xmax, ymax)          # ⚠️ 上界,非真实范围
layer.read_features(where=, bbox=, limit=, offset=, fields=) -> Iterator[GdbFeature]
layer.read_feature(oid) -> Optional[GdbFeature]

# ---- 建 + 写 ----
gdb = OpenFileGDB.create('D:/data/new.gdb')       # 路径必须不存在
layer = gdb.create_layer('点位', geometry_type='point', fields=[...])
layer.write_feature({...}) -> int                 # 返回新 OID
layer.update_feature(feature)                     # 就地改一条
layer.delete_feature(oid)
gdb.delete_layer('点位')

# ---- 数据类型 ----
GdbFeature(oid, attributes, geometry)             # attributes 不含几何与 OBJECTID
Geometry(shape_type, coordinates, has_z, has_m)   # 也支持 .wkt() / .from_wkt()
GdbField(name, field_type, length=, nullable=, required=, editable=, alias=)
```

### 几何对象(`Geometry`,原来叫 `GdbGeometry`)

```python
g = feature.geometry

# 描述(近零成本,不物化坐标)
g.kind / .is_empty / .dimension / .point_count / .part_count
g.is_ring / .is_clockwise

# 度量(都只读 xy_parts,不物化坐标)
g.envelope() -> (xmin, ymin, xmax, ymax) | None   # ⚠️ 顺序与 OGR 不同
g.area() / .length() -> float                     # 非面/线恒为 0.0
g.centroid() -> (x, y) | None                     # ⚠️ 元组,不是点对象
g.distance(other) -> float | None                 # ⚠️ O(n·m),无 bbox 预筛

# 构造(返回新的 Geometry)
g.convex_hull() / .simplify(tolerance) / .segmentize(max_len)

# 拓扑判定(全部 -> bool);relate() 返回 9 字符 DE-9IM 矩阵
g.relate(other) -> '212101212'
g.intersects / .disjoint / .contains / .within / .covers / .covered_by
g.touches / .crosses / .overlaps
g.equals(other)          # 拓扑等价(OGC T*F**FFF*)
g.exactly_equals(other)  # 结构等价(同 GDAL OGRGeometry::Equals)
g.is_valid() / .is_simple()   # ⚠️ 部分实现,见 §六

# 文本
g.wkt() / Geometry.from_wkt(s)                 # 多壳面 -> MULTIPOLYGON
g.to_geojson() / g.__geo_interface__           # M 在 JSON 里没有位置,导出即丢
from pyopenfilegdb import to_geojson, from_geojson
```

⚠️ `Geometry()` 的 Python 构造签名与 GDAL 的 `OGRGeometry` 不同,但**名字改成
`Geometry` 是硬改名,没有 `GdbGeometry` 别名**。下游代码需要一起改。

`read_features` 的 `where` 支持三种写法:`None` / 可调用谓词
`f(feature) -> bool` / SQL 子集字符串。SQL 子集按**三值逻辑**求值
(`True` / `False` / `None`),只有 `True` 命中。

⚠️ **`feature.geometry` 是惰性的。** 从 `read_features()` / `iter_rows()`
出来的要素只带几何 blob 的**未解码引用**,第一次访问 `feat.geometry` 时才
真正解析,解完缓存。所以"只读属性"的循环一行几何的代价都不用付 ——
这正是 GDAL `SetIgnoredFields` 省掉的那一段。详见 §五。

`bbox=` 过滤会先用几何 blob 自带的**存储包围盒**做粗筛(读几个 varuint,
不解点数组),粗筛通过才逐点精确判 —— 大多边形图层上能省掉绝大部分解码。

---

## 四、Tier 标注

### Tier 1 —— 读:**完成 ✅**

* `.gdbtable` 头部 + 字段描述区(**字段描述区位置读 `+32`,不假设是 40**)
* 全部 FGFT 字段类型(含 INT64 / DATE / TIME / DATETIME_WITH_OFFSET / BINARY / RASTER)
* `.gdbtablx` 偏移表,`offset_size` 4/5/6 全支持,含 base block map 压实换算
* Esri 几何解码:POINT / ARC / POLYGON / MULTIPOINT 的 XY / Z / M 各档
* 环闭合归一化(盘上闭合 → 内部不闭合)、环方向
* `GDB_SystemCatalog` / `GDB_Items` / `GDB_ItemTypes` / `GDB_ItemRelationships`
* 空间参考(WKT + WKID + LatestWKID)
* WHERE 子句(`= <> < <= > >= AND OR NOT IS [NOT] NULL IN LIKE`),SQL 三值逻辑
* 完整读链路:`open → list_feature_classes → get_layer → read_features`

### Tier 2 —— 写:**完成 ✅**

* 记录序列化(空值位图 + 定长/变长字段 + 几何)
* `create()` 建空库 —— 与真实 ArcGIS 空白库**逐段字节一致**(除 `+4`/`+8`/`+24`
  三个计数/大小字段,它们随写入变化)
* `create_layer()` + 四重登记(SystemCatalog / GDB_Items / ItemRelationships / XML)
* `write_feature()` 追加、`update_feature()` 就地改、`delete_feature()` 负长度标记
* 表级包围盒维护(**只放大不缩小**,与 ArcGIS 一致)
* 几何编码,环方向按 Esri 约定(首环 CW,其余 CCW)自动调整
* 默认量化参数 WGS84 `1e6`(ZM `1e4`),与 GDAL `CreateGDBItems` 完全一致
* 版本选择:有 INT64/DATE/TIME 时 XML 命名空间升到 `10.8`

### Tier 3 —— 几何空间计算:**完成 ✅**(口径与边界见 `DESIGN.md` §2.21)

| 能力 | 状态 |
|---|---|
| 描述:`kind` / `is_empty` / `dimension` / `point_count` / `part_count` / `is_ring` / `is_clockwise` | ✅ 完成 |
| 度量:`envelope()` / `area()` / `length()` / `centroid()` / `distance()` | ✅ 完成 |
| 构造:`convex_hull()` / `simplify()` / `segmentize()` | ✅ 完成 |
| 拓扑:`relate()` + 九个 DE-9IM 谓词 | ✅ 完成(**自实现 OGC DE-9IM**) |
| `equals()` 拓扑等价 / `exactly_equals()` 结构比较 | ✅ 完成 |
| WKT 出口(`wkt()` / `from_wkt()`) | ✅ 完成(⚠️ 见 §六:本轮修了两个非法输出) |
| GeoJSON(`to_geojson()` / `from_geojson()` / `__geo_interface__`) | ✅ 完成 |
| `is_valid()` / `is_simple()` | ⚠️ **部分实现**(只查 4 条,见 §六) |
| `buffer` / `union` / `intersection` / `difference` / `sym_difference` | ❌ **不做**(需要 GEOS 级鲁棒性) |
| multipatch 的谓词与 `relate` | ❌ 抛 `NotImplementedError`(描述与度量仍可用) |
| numpy 加速 | ✅ **可选**(不装功能不变,只是慢) |
| C 扩展接入几何计算 | ❌ 不加(仍是 4 个入口,本轮不开新口子) |

⚠️ **这一档没有 GDAL 的 C++ 可抄。** `OGRGeometry::Intersects` / `Contains` /
`Touches` … 在 `ogrgeometry.cpp` 里**全部转手给 GEOS**,GDAL 自己只加了一句
"包围盒先快速排除"。所以拓扑是**按 OGC DE-9IM 规范自实现**的,口径与已知边界
(无 snap-rounding、退化构型可能判错、不做 overlay)都写在 `DESIGN.md` §2.21。

✅ **对拍跑起来了**(本机有 shapely 2.1.2 / GEOS 3.13.1,以及 QGIS 4.2 自带的
GDAL 3.13.3 —— 本文档此前写"本机没有 GDAL"是**错的**)。`tools/verify_topology.py`
四层全跑:`--features 8 --pairs 400` 比了 1,290 对 / 14,190 项判定,合成 1/2/3 层
0/2/0 处不符、真实语料 **0** 处。

⚠️ **它抓出了两个真 bug —— 同一个根因:"重算出来的点不能拿去问浮点"**
(`point_location` 判"点在不在线上"用的是 `orient(...) == 0` 的**精确**比较,
而 `relate_of` 的探针点一半是算出来的,一般不精确落在线段上):

| # | 症状 | 最小复现 | 治它的返回值 |
|---|---|---|---|
| 1 | 交点不可精确表示 → `II`/`IB`/`BI` 整格塌成 `F` | `LINESTRING(-0.0000003 0.0000004, 10.0000002 9.9999998)` × `LINESTRING(0 10, 10 0)`,老答 `FF1FF0102`,GEOS `0F1FF0102` | `hit` |
| 2 | 共线重叠子段的中点被算到线外 → `IB`/`IE`/`BE` 全错 | 三角形 `POLYGON((0 0, 0.1 0.3, 0.4 0.1, 0 0))` **与它自己**比,老答 `2F2F11212`,正确 `2FFF1FFF2` | `on_obstacle` |

⚠️ **bug 2 更值钱**:共线重叠在真实数据里到处都是(相邻行政区共享界线)。它在
`D:/work/2024年国土行政区划.gdb` 上把 `乡行政区划#75` 与 `村行政区划#92`
(两条**完全相同**的面)判成"部分重叠"。而且它是**修完 bug 1 之后**、把语料抽样
从"8 条要素 / 400 对"放大到"150 条要素 / 900 对"才撞出来的 ——
**"对拍跑过且绿"这句话必须带上参数,否则它没有意义。** 回归用例分别是
`TestNonExactIntersectionPoints` 与 `TestRecomputedMidpoints`,各自带一条
"把修复还原回去"的路径来证明**自己有牙**。

⚠️ **第三个发现:`tools/fake_ogr.py` 曾经被绕过。** `pick_oracle()` **优先挑
shapely**,而本机装了 shapely —— 注入的假 `osgeo` 压根没被用到,它一直在跟
**真的 GEOS** 对比却自称在跑假管道。**这与那个"假绿"是同一个失败模式:守卫悄悄
失效,而它看上去还在守。** 现在 `install()` 两个都注入,并加硬断言(拿到的必须
都是假的,否则 `RuntimeError`)。

**残留 2 处**是同一个构型,属精度地板而非 bug(1e-9 薄片平移到 UTM 后只有
2.1 ULP 高,穿越它的子线段比一个 ULP 还短,双精度网格上没有内部点可采样),
**看到这 2 处不必当成回归**。


### Tier 4 —— 高级特性

| 能力 | 状态 |
|---|---|
| `.gdbindexes` 描述符解析 | ✅ 完成 |
| `.freelist` 读取 | ✅ 完成(只读,不参与写路径) |
| `.atx` 属性索引 B 树查询 | 🔶 **桩**,`GdbBTreeIndex.lookup()` 抛 `GdbWriteError` |
| `.spx` 空间索引包围盒查询 | 🔶 **桩**,`GdbSpatialIndex.query_bbox()` 抛 `GdbWriteError` |
| 生成 `.gdbindexes` / `.atx` / `.spx` | ❌ 不写(刻意的取舍,见下) |
| 生成 `.freelist` | ❌ 不写(文件空洞不回收,只单调增长) |
| `Shape_Area` / `Shape_Length` 自动字段 | ❌ 未实现(需要字段默认值的 `FILEGEODATABASE_SHAPE_AREA` magic marker) |
| 域(Domains) | 🔶 **只读**:字段级 `DomainName` 能解析,建库时不生成 |
| 注释(Annotation)要素类 | ❌ 未实现 |
| 附件(Attachments) | ❌ 未实现 |
| version 4 写入 | ❌ 主动拒绝(GDAL 也不支持对 v4 做 update) |

**为什么索引一个都不写?** 它们是**可选**的纯加速结构:ArcGIS 打开时若
`.gdbindexes` 声明了某个索引而文件不存在会报错,但**完全不看 `.gdbindexes`
也能读全表**(GDAL 就是"有就用、没有就线性扫")。本库的查询一律走全表扫描
—— 结果与用索引**完全相同**,只是大表上慢一些。与其写出一个半对的空间索引
害人,不如一个都不写,让 ArcGIS 自己按需重建。

---

## 五、性能

基线取自 `main.py` 用的那个图层:`D:/work/2024年国土行政区划.gdb` 的
`村行政区划`,**21,217 条面要素、4,494 万个顶点**。同口径(只遍历、只取
一个属性字段,不解几何)GDAL/OGR 用时 **742 ms**(用户后来报的是 0.3 s)。

⚠️ **性能必须在终端里测,不能按 PyCharm 的 Debug 跑。** PyCharm 的调试器
是 pydevd,它给每个 Python 帧装 line tracer,每条字节码都回调一次 —— 本库
是纯 Python,全额上税;GDAL 是 C 扩展,那 0.3 s 是 C 时间,税率 0。用
`tools/bench_trace.py` 装一个最朴素的 line tracer 复现这个机制:本库的属性
遍历 **0.073 s → 0.172 s(2.4×)**,等量的纯 C `utf-16-le` 解码
0.036 s → 0.041 s(1.1×,≈噪声)。**在调试器里比"纯 Python 实现 vs C 实现",
量到的是 tracer 的税率差,不是算法差。**

| 场景 | 本库 | 说明 |
|---|---:|---|
| 只读属性(`main.py` 的循环) | **0.42 s** | 与 GDAL 的 0.3~0.74 s 同量级 |
| 只 seek + read 记录体,不解码 | 0.163 s | IO 理论下限,261.7 MB |
| 全量包围盒(`peek_envelope` × 21,217) | 0.892 s | 只要包围盒时的正解 |
| `bbox=` 小范围过滤(命中 245/21217) | 1.96 s | 存储包围盒粗筛 |
| **连几何一起读**(**有 C 扩展 + flat 容器**) | **1.50 s** | 对 GDAL 的 0.80 s 是 **1.9×** |
| **连几何一起读**(强制纯 Python) | **65.4 s** | 对 GDAL 是 82× |
| 单条几何的空间计算(`area()` / `length()` / `centroid()`) | 与上面无关 | 只在**单条**几何上跑,不在遍历循环里;大环上 numpy 快 **×21~25**(见下) |

复现:`python tools/bench_read.py "D:/work/2024年国土行政区划.gdb" 村行政区划`
(分段基准)、`python tools/profile_read.py`(cProfile)、
`python tools/bench_trace.py`(调试器税率)、
`python tools/bench_geom_split.py`(几何解码三档拆账,强制纯 Python)、
`python tools/bench_cext_varint.py`(C 扩展对照)、
`python tools/verify_accel.py`(C 与纯 Python 逐位对拍的差分闸门)、
`python tools/verify_numpy.py`(numpy 快路径的差分 + 精度标定 + 阈值体检)、
`python tools/verify_topology.py`(⚠️ **可选**的 GDAL/GEOS 谓词对拍,**没装 osgeo 就 skip**)、
`python tools/fake_ogr.py`(本机没 GDAL 时,用**假的** osgeo 把上面那个工具的管道跑通 —— 不是真值)。

**为什么 0.42 s 会变成 65 s。** 循环里加一句 `g = feature.geometry`,就从
"一个顶点没解"变成"4,494 万个顶点全解"(159×)。三档拆账
(`tools/bench_geom_split.py`,真实 blob,不开 profiler,**下面是 flat 容器之前
的口径**):varint 逐字节解码 **0.947 µs/顶点(72%)**、尾部浮点缩放+拼元组
**0.308(23%)**、头解析+部件表+环闭合+几何构造 **0.064(5%)**,合计
1.319 µs/顶点 × 44.95 M = 59.3 s。**第 7 步把前两档连容器一起拆了**,现在
纯 Python 是 65.4 s(逐点建 Python 对象的成本本来就该付),有 C 扩展是 1.50 s。

去 GDAL master 核过:`ReadVarIntAndAddNoCheck`(`filegdbtable.cpp`
1474–1517)只有**一个 1 字节早退**,没有 2/3 字节特化;`ReadXYArray`
(3451–3479)就是普通模板循环,每点一次边界检查,换算式正是
`dxLocal / GetXYScale() + GetXOrigin()`;**整个驱动没有定宽/压缩坐标的
快路径**。所以这段循环**就是 GDAL 的算法**,60 s 是 CPython 跑在同一个
循环上的成本,不是算法差、也不是 IO 差。实测能挤的只有:尾部换 listcomp
(−3.7 s / 6%)、varint 3 字节展开(−8 s / 13%),合计约 1.21×,都是对
GDAL 自己都不优化的循环做微优化,按"逻辑严格跟 GDAL"的口径**不做**。

**做了什么。** 八步:1–2 照 GDAL 的口径来,3–5 是在 GDAL 之上多走的,
6–7 是换执行器与换容器,8 只服务于空间计算不在读链路上。

1. **数组解码去函数调用。** 把 varint 循环内联进 `_read_xy_array`,循环体
   只做整数运算,缩放整批甩给 `map` 跑在 C 层。**2.049 → 1.444 µs/顶点**。
2. **惰性几何。** `_LazyGeometry` + `GdbFeature.geometry` property,
   `iter_rows(lazy_geom=True)`。**5.833 s → 0.013 s(449×)**。
3. **`bbox=` 走存储包围盒粗筛。** `peek_envelope()` 只读 blob 头部几步就
   返回一个放宽一个量化步长的包围盒(真实范围的**超集**,不会错杀),
   过了才 `_geometry_bbox()` 精确判。
4. **连几何的字节都不读**(GDAL **不**这么做 —— 它 `SelectRow` 一次把整条
   记录读进来)。`村行政区划` 记录体 261.7 MB 里几何占 **259.3 MB(99.1%)**,
   `_read_record_lazy` 先读 32 B 探针探出几何位置,再只读几何前后两段,
   中间那段留个文件偏移给 `_LazyGeometry.deferred()`;判据是"跳读读得更少
   才跳",所以**读的字节数永不超过优化前**。17 个图层实测:村 85.5×、
   乡 217×、县 2346×,最差 1.2×。
   ⚠️ 配套把 `open()` 的缓冲从默认 8 KB 降到 **512 B**
   (`_TABLE_READ_BUFFER`):记录不连续、每条都要 `seek`,而 `seek` 作废
   缓冲区,8 KB 预读会让逻辑上 3 MB 的读变成 OS 层 348 MB。
5. **字段解码编译成计划。** IO 不再是瓶颈后,剩下全是 Python 簿记:一次
   遍历里 `is_oid` 属性求值 30.9 万次、`_decode_one_field` 调用 22.4 万次、
   `need()` 24.5 万次、`remaining` 26.6 万次 —— **269 万次函数调用**。
   `_build_decode_plan()` 把字段定义预编译成
   `(名字, 种类, 位图字节下标, 位图掩码, Struct, 字段)`,主循环只做整数
   比较 + `Struct.unpack_from`,空值判断缩成一次下标 + 一次与,游标摊成局部
   整数、**不用 ByteReader**;少见类型挪进 `_decode_other()`。
   **函数调用 269 万 → 104 万(2.6×),终端 0.70 s → 0.42 s。**
6. **几何 varint 数组换执行器(可选 C 扩展)。** 前五步之后算法已经和 GDAL
   逐位对齐,剩下的 60 s 纯粹是"CPython 跑在同一个循环上"—— 于是把**同一个
   算法**用 C 再写一遍(`pyopenfilegdb/_gdbaccel.c`,只包
   `_read_xy_array` / `_read_scalar_array` 这两处)。varint 循环
   **336.8 → 2.9 ns/个(116×)**,含浮点缩放+拼元组的全路径 **446.9 →
   37.9 ns/个(11.7×)**,`村行政区划` 整层 **59.3 → 4.80 s**。
   ⚠️ 这是**可选**的:没编译、换了解释器、ABI 不匹配都自动回退纯 Python。
   **结果逐位相同**(`tools/verify_accel.py`:8 个库 11,885 条 / 6,335,486 顶点
   逐位对拍 + 18/18 定向损坏用例 + 4000 轮 fuzz)。只读属性那条路**没动**,
   仍是 0.42 s。详见 `DESIGN.md` §2.19.4。
7. **坐标容器换成 flat(把"物化地板"拆了)。** 第 6 步之后 C 侧算得飞快,
   却在**把 double 装箱成 Python 元组**这一步漏回去:实测过路费
   **37.4 ns/值**。于是把几何的主存储从 `list[(float, float)]` 换成
   `_FlatCoords` —— 每个 part 一块 `array('d')`,XY **交错**存放;
   `.coordinates` **行为一字未变**,但退化成**惰性物化的派生视图**(首次访问
   物化并缓存 `_coords`)。C 侧两个新入口 `decode_xy_flat` /
   `decode_scalar_flat` 直接 `PyObject_GetBuffer` 拿可写指针往 `double*` 里写
   (`array('d', memoryview)` 那条直觉路**更慢**,60.2 ns/varint,因为它走
   迭代器协议逐元素装箱)。整条路径 **41.5 → 4.0 ns/varint**,
   `村行政区划` **4.80 → 1.50 s(3.2×)**,累计 **65.4 s → 1.50 s = 44×**
   (对 GDAL 的 0.80 s 是 1.9×)。验证热循环里**一次都没有**提前物化:
   21,217 条里 `_coords is not None` 的是 **0** 条。
   ⚠️ **收益有前提**:下游若**逐点**在 Python 里遍历坐标
   (`for x, y in g.coordinates:`),那 1.7 s 会原样回来 —— 换容器省掉的正是
   "逐点建 Python 对象"。所以按"会做空间计算"(用户确认)这个前提落地。
   ⚠️ **写路径仍然走 `.coordinates`**(`_encode_parts` 等),读写往返多一次
   物化,读热路径不受影响 —— 这是"老代码一行不改"的代价。详见 `DESIGN.md` §2.19.5。
8. **空间计算的数组运算走 numpy(可选)。** 这一条**不在**上面的读链路里 ——
   它只作用于 `Geometry.area()` / `length()` / `centroid()` 这几个**单条几何**
   的算子,不影响 §五 表里任何一行。N=2000、UTM 量级坐标实测:长度
   **689 → 27 µs(×25)**、面积 **351 → 17 µs(×21)**、质心 **494 → 53 µs(×9.4)**。
   包围盒**刻意不加速**(纯 Python 的 `min(a[0::2])` 已经在 C 层跑,实测 ×1.0,
   加了反而多一层)。
   ⚠️ **两个下限不是同一个数**:面积 `_NP_MIN_AREA2 = 128`,质心
   `_NP_MIN_CENTROID = 256` —— 质心路径要多两次 `np.roll`(整数组拷贝),
   在 128 顶点上**反而慢 40%**,要到 192 才打平。这个 bug 是
   `tools/verify_numpy.py --bench` 抓出来的(那张表现在会打出"这一行到底走没走
   numpy",走错方向直接非零退出)。
   ⚠️ **符号敏感的路径一律不走 numpy**:numpy 的 `sum` 用成对求和,近零面积的
   环上符号可能翻转 —— 那是**写路径的绕向判定**,不是性能问题。所以
   `ring_signed_area2` 保持纯顺序实现,`_signed_area2` 才是分派版。
   零硬依赖:没装 numpy 或 `PYOPENFILEGDB_NO_NUMPY=1` 就自动回退,结果只在
   1e-14 相对量级上有差(`tools/verify_numpy.py`:面积 1.89e-14、周长
   1.20e-14、质心 1.72e-11 尺度归一、**0 次符号翻转**)。见 `DESIGN.md` §2.21。

顺带否掉一个假设:"多数增量是单字节,加个快路径"。全量扫 6 个样例库
(6,106 万个顶点),单字节命中率 **0.0%**。**先量再写**,没写废码。
(第五步对**长度前缀**内联的单字节快路径是另一回事,那里命中率很高。)

**残留差距(如实说)。** 两块,都还在,但第一块已经有了出口。

一是**几何解码**。纯 Python 那条路一旦真的需要几何且量很大,就是比 C++ 慢
近百倍 —— 这是解释器逐顶点循环 + 逐点建 Python 对象的固有成本,不是算法差异
(算法上已与 GDAL 对齐,它连 varint 快路径都没有;IO 上还比它多走了一步)。
**第 6、7 步把这块从 65.4 s 压到 1.50 s**,但那是**可选**的:约束放宽到
"可以有 C 扩展"(原话:"可以使用C拓展,不能依赖GDAL相关库"),而
**"不依赖 GDAL 相关库"没有放宽**。所以装不上编译产物时,回退仍是几十秒 ——
这时**凡是不需要坐标的活,别碰 `feat.geometry`**:只比包围盒用
`peek_envelope`(全量 21,217 条 0.892 s),要空间过滤传 `bbox=`。

二是**属性解码**,0.42 s vs 0.3 s 的 1.4 倍,来自"每个字段一次 Python 字节码"
这个下限。⚠️ 这一块 **C 扩展没覆盖**(边界只圈了几何 varint 数组),
按实测口径搬进 C 还能再省约 0.3 s,但本轮**刻意没有蔓延过去**
(用户只选了"几何 varint 数组"),数字留在这里备查。

---

## 六、已知限制(诚实清单)

1. **`extent` 不是真实范围。** ArcGIS 维护的是一个单调增长的**上界**,删除
   要素后不会回缩;实测有样例偏差达 5.3°。要真实范围请自己扫几何
   (`examples/read_example.py` 里有现成写法)。
2. **MULTIPOLYGON 会被压成 POLYGON。** Esri 几何类型没有"多面"这一档,
   多个面只能丢进同一个 POLYGON 的 parts。读回来无法还原"哪几个环属于同一个面"。
   这是格式本身的局限。
3. **不能线性扫描记录区。** 记录不连续、可以排在字段描述区之前、tablx 偏移
   不单调。遍历**必须**走 `.gdbtablx`。
4. **`.gdbtable` 文件只增不减。** 删除只标记不回收,更新变长的记录会追加到末尾。
5. **写入必须显式 `update=True`。** 默认打开的句柄是只读的。
6. **`create()` 的路径必须不存在。**
7. **声明了 Z/M 就不降级。** 声明 `has_m=True` 却只给 (x, y) 时,缺的分量会
   填成 Esri 的 NaN(**Esri 的 NaN 位型是 `0x7FF8000000000001`**,不是默认的
   `0x7FF8...000`),而不是静默把这条要素写成无 M 的几何。
8. **POINT → MULTIPOINT 是单向放宽**:点可写进多点图层,多点写进点图层报错。
9. **几何解码的硬成本,以及 C 扩展的可选性。** 惰性化之后"不碰几何"很快,
   但真要把几千万顶点全解出来,**纯 Python 是几十秒**(`村行政区划`
   4,494 万顶点 = 65.4 s,见 §五)。这是解释器循环 + 逐点建 Python 对象的
   固有代价:GDAL 的 `ReadVarIntAndAddNoCheck` 连 2/3 字节快路径都没有,
   我们这段循环就是它的逐位复刻。第 6、7 步的 **C 扩展 + flat 容器把它压到
   1.50 s**(对 GDAL 的 0.80 s 是 1.9×),但那套东西**是可选的** —— 没编译、
   换了解释器(仓库里那个 3.11.9 的 `.venv`)、ABI 不匹配,都会**自动回退**
   纯 Python,功能一样、结果逐位相同,只是慢。
   要判断当前进程走哪条路看 `pyopenfilegdb.HAS_ACCEL`;
   要强制回退设 `PYOPENFILEGDB_NO_ACCEL=1`。
10. **C 扩展只覆盖几何 varint 数组。** 属性记录解码(0.42 s)、逐要素簿记
    (约 2.9 s)都**没有**搬进 C —— 这是本轮刻意划的边界,不是遗漏。
    ⚠️ **更正一条过时描述**:早先这里写"坐标容器形状 `list[(float, float)]`,
    约 6.6 s 的物化地板,搬不进 C" —— 那句在**扁平容器**(`_FlatCoords`,
    `DESIGN.md` §2.19.5)之后**不成立了**。现在 `Geometry` 内部存的是按 part
    交错的 `array('d')`,元组那一站根本不存在,`.coordinates` 退化成**惰性
    派生视图**(访问一次 caches 到 `_coords`)。C 侧直接往 `array('d')` 里写,
    实测该段 **0.36 s**(原来是 6.6 s)。所以地板不是"绕过去"的,是**拆掉**的。
    推论(对所有新代码都成立):**只读 `xy_parts` / `z_parts` / `m_parts`,
    绝不碰 `.coordinates`** —— 一碰就等于把刚省下的 6.6 s 又花回去。见 `DESIGN.md` §2.19.4 / §2.19.5。
11. **`is_valid()` / `is_simple()` 是部分实现,`True` 不等于 OGC 有效。**
    只查四条:(1) 环是否闭合、(2) 顶点数是否满足类型下限、(3) **单个环**是否
    自交、(4) 洞是否落在壳内。**没做**:洞与洞的重叠、环的自切、
    multipoint 的点重合、壳与壳的相交。`is_simple()` 同理,只做折线自交。
    没做的那几条逐条写在 docstring 里,不含糊成"检测几何有效性"。
12. **不做 overlay。** `buffer` / `union` / `intersection` / `difference` /
    `sym_difference` **不提供** —— 那需要 GEOS 级的 precision model 与
    snap-rounding,是本轮明确划出边界的部分(用户选的是"度量与描述 + 拓扑判定")。
13. **`distance()` 是 `O(n·m)`。** 直接遍历两边的线段对取最小线段-线段距离,
    **不做线段包围盒预筛**。两条几万顶点的线之间会明显慢。先比包围盒不相交的
    快路径有(`intersects` 那一层),但"包围盒相交却实际很远"的构型全价付费。
14. **拓扑判定没有鲁棒性保证(要如实说)。** 全部基于浮点方向判定
    (叉积符号),**没有 snap-rounding、没有精确算术**。次 ULP 量级的退化构型
    —— 两点在量化网格上相距不到一个 ULP、环边几乎共线 —— **可能判错**。
    GEOS 在引入 precision model 之前有同样的毛病。这是 §2.21 里唯一真正的
    风险点,也是"缩小承诺范围"而不是"硬凑都对"的原因。

    这条已经**具体到一处**,而且是量出来的:与 GEOS 对拍时,唯一残留的不符就是
    一个 10 × **1e-9** 的直角薄片平移到 UTM 量级之后 —— 它只有 **2.1 ULP 高**,
    直线穿过它的那一段**比一个 ULP of x(7.45e-9)还短**,于是那段子线段的
    **中点在双精度里根本不存在**(四舍五入回切点本身),"取中点判内外"必然失败。
    任何采样式实现都过不了这一关,GEOS 是因为走**组合式**拓扑图(edge-end 分类)
    而不是采样才答对的。**这是精度地板,不是待修的 bug。**
    除这一处外,判定全部与 GEOS 一致(真实语料 0 处不符):默认口径 1,290 对 /
    14,190 项;放大到 `--features 150 --max-verts 60 --pairs 900` 是 2,438 对 /
    **26,818** 项,`PYOPENFILEGDB_NO_NUMPY=1` 同样口径也一致;`--oracle gdal`
    合成层 1,540 对 / 12,320 项。
    另:那两个**真 bug**——交点坐标不可精确表示、共线重叠子段的中点算到线外——
    **已修**(见 `DESIGN.md` §2.21 与 `TestNonExactIntersectionPoints` /
    `TestRecomputedMidpoints`)。两者同一根因:**重算出来的点不能拿去问浮点。**
15. **GeoJSON 不按 RFC 7946 重绕环。** RFC 7946 §3.1.6 要求外环 CCW、内环 CW,
    而 Esri(以及 GDAL 的默认导出)是**外环 CW、内环 CCW**。本库照原样导出,
    与 `OGR_G_ExportToJson` 一致,不做隐形改写 —— 需要合规的消费者请自行重绕
    (GDAL 也只在 GeoJSON 驱动的 `RFC7946=YES` 选项下才重绕)。
16. **M 维在 GeoJSON 里丢失。** JSON 没有 M 的位置(RFC 7946 只认 x/y/z),
    所以 `to_geojson()` **丢弃 M**,而 `wkt()` 保留(`LINESTRING M (...)` 的
    维度后缀必须写出,否则读回来会变成 Z)。推论:**带 M 的几何 GeoJSON 往返
    不回来** —— `__eq__` 比 `has_m`。`from_geojson` 留了 `has_m=False` 参数,
    让调用方能显式要回一个 M 维几何。
17. **写回不是逐位幂等的(本轮新测)。** 全语料 21,217 条做"读→写→读"往返,
    **152 条**(0.7%)的字节与原文不同 —— 原因是环顺序/绕向的规范化加上
    ≤1 个量化步长的舍入。**面积在全部 21,217 条上一致**,所以是表示差异不是
    几何差异。要逐位等价的场景(哈希去重)不能拿本库的输出当原文。

---

## 七、测试

**155 个用例**(`test_create_write.py` 51 + `test_geometry.py` 75 +
`test_read.py` 29),四配置全绿。改用例数不等于覆盖率 —— 见下面那条教训。

```bash
# 读测试(需要样例库;设 PYOPENFILEGDB_TEST_GDB 或扫 D:/work/*.gdb)
PYTHONIOENCODING=utf-8 python -m unittest discover -s tests -p 'test_read.py'
# -> Ran 29 tests ... OK   (约 14 秒;惰性几何之前要 316 秒)

# 写测试(自给自足,不依赖外部数据)
PYTHONIOENCODING=utf-8 python -m unittest discover -s tests -p 'test_create_write.py'
# -> Ran 51 tests ... OK

# 几何空间计算 + WKT/GeoJSON 出口(自给自足)
PYTHONIOENCODING=utf-8 python -m unittest discover -s tests -p 'test_geometry.py'
# -> Ran 75 tests ... OK
```

**四配置**(改一个全局名字最容易漏掉某个引用点,所以每条都要过):

```bash
P="D:/zsh/app/py_3.13.1/python"      # 项目解释器,带 numpy
V="E:/code/pyopenfilegdb/.venv/Scripts/python.exe"   # 3.11.9,无 numpy

# 1) py3.13 + numpy(C 扩展 + numpy 快路径都在)
PYTHONIOENCODING=utf-8 $P -m unittest discover -s tests          # 155 OK   33.9 s
# 2) 强制回退 numpy(5 个 numpy 对比用例 skip)
PYOPENFILEGDB_NO_NUMPY=1 PYTHONIOENCODING=utf-8 $P -m unittest discover -s tests
#                                                    # 155 OK (skipped=5)  34.0 s
# 3) 另一个解释器,没有 numpy 也没有 cp311 的 C 产物
PYTHONIOENCODING=utf-8 $V -m unittest discover -s tests          # 155 OK   33.7 s
# 4) 强制回退 C 扩展(6 个加速对比用例 skip,慢一倍多)
PYOPENFILEGDB_NO_ACCEL=1 PYTHONIOENCODING=utf-8 $P -m unittest discover -s tests
#                                                    # 155 OK (skipped=6)  80.9 s
```

```bash
# C 扩展编译(可选;不编也能跑,只是慢)
python setup.py build_ext --inplace          # 或 pip install -e .
PYTHONIOENCODING=utf-8 python -c "import pyopenfilegdb; print(pyopenfilegdb.HAS_ACCEL)"

# C 路径 vs 纯 Python 路径的差分闸门(逐位相同才算过)
python tools/verify_accel.py                 # 8 个样例库 + 定向损坏用例 + fuzz
python tools/bench_cext_varint.py            # 复现 116× / 11.7× / 9.4×

# numpy 快路径的差分闸门(不是逐位,是真值标定 —— 见工具 docstring)
python tools/verify_numpy.py                 # 真实语料差分 + Fraction 真值 + 符号检查
python tools/verify_numpy.py --bench         # 阈值体检:走 numpy 的行必须更快

# GDAL/GEOS 谓词对拍(本机装得上:shapely 2.1.2/GEOS 3.13.1,QGIS 的 GDAL 3.13.3)
# ⚠️ 两个 oracle 都没装是**正常状态**:脚本 skip 并 exit 0
python tools/verify_topology.py --synthetic-only         # 只跑合成三层
python tools/verify_topology.py --features 8 --pairs 400 # 加真实语料(第 4 层)
python tools/verify_topology.py --oracle gdal            # 强制走 GDAL(比不了 relate)
# ⚠️ --pairs 预算的是**对数**不是工作量,relate 是 O(顶点²) —— 大语料要配
#    --max-verts(默认 400)。**语料抽样强度不够会放过 bug**:bug 2 就是"8 条要素 /
#    400 对"全绿、放大到"150 条要素 / 900 对 + --max-verts 60"才撞出来的。
python tools/verify_topology.py --features 150 --max-verts 60 --pairs 900

# 全语料 WKT 出口体检(每条几何 wkt() -> from_wkt 往返 + 幂等,~700 s,不是单元测试)
python tools/verify_wkt_roundtrip.py                     # 扫 D:/work/*.gdb
python tools/verify_wkt_roundtrip.py --limit 3000        # 冒烟
# ⚠️ 它断言的不只是"能不能读回来":kind / has_z / has_m / point_count 都不许变,
#    且再导一次文本必须逐位相同。`M` 静默变 `Z` 这种事只有这套断言抓得住。

# 用**假的** osgeo 把上面那个工具的管道跑通 —— 守"这段验证代码不是废的"(不是真值)
python tools/fake_ogr.py --synthetic-only    # 1540 对 / 16,940 项,exit 0
FLIP="contains touches" python tools/fake_ogr.py --synthetic-only
#                                            # 故意答反 -> 必须报不符并 exit 1
```

> ✅ **本机有 oracle,对拍真跑过。** `--features 8 --pairs 400`:1,290 对 / 14,190
> 项判定,合成三层 0/2/0 处不符、真实语料 **0** 处;放大到
> `--features 150 --max-verts 60 --pairs 900` 是 2,438 对 / 26,818 项,同样只有
> 那 2 处已知薄片。**它一共抓出两个真 bug**(交点不可精确表示 / 共线重叠子段的
> 中点),同一根因,已修并补了回归用例,见 `DESIGN.md` §2.21。
> **残留那 2 处是精度地板,不是回归。**
> ⚠️ 这条工具有两段"守卫悄悄失效"的历史,都值得记住:
> (a) **假绿** —— 原版调 `a.relate(b)`,而 GDAL 的 Python 绑定没有 `relate`,
> `AttributeError` 被宽泛的 `except` 吞成"跳过",于是它打印"逐对一致 ✓"、
> `exit 0`,**而实际比了 0 对**。现在 `pairs == 0` 是硬失败。
> (b) **换了 oracle** —— `fake_ogr.py` 注入的假 `osgeo` 因为 shapely 优先而
> 从未被用到,它一直在跟真 GEOS 比却自称在跑假管道。现在两个都注入 + 硬断言。
> 两段合起来一句话:**不变量要么被断言,要么不存在;注释不算。**
>
> ⚠️ **两个工具都带 UTF-8 stdout 兜底**(`sys.stdout.reconfigure`)。Windows 默认
> 控制台是 GBK,编码不了 `⚠️`/`✓`,而崩在"打印报告"那一步最难查 —— 一路设了
> `PYTHONIOENCODING=utf-8` 的开发机碰不到。

> ⚠️ **`.pyd` 绑解释器版本,必须用"跑代码的那个解释器"编。** 本机同时有
> 项目解释器 3.13 和仓库里 `.venv` 的 3.11.9,两份产物 tag 不同
> (`cp313` / `cp311`)、可同目录共存。只编了一份却用另一个解释器跑,会
> `HAS_ACCEL = False` 静默回退 —— 实测 `main.py` 因此是 **56.5 s** 而不是 4.4 s。
> **完整的编译 / 打包 / 排错说明见 [`ACCEL.md`](ACCEL.md)。**

```bash
# 示例
python examples/read_example.py                    # 自动找样例库
python examples/read_example.py D:/work/xxx.gdb 图层名
python examples/create_write_example.py            # 写到临时目录
python examples/create_write_example.py D:/tmp/demo.gdb
```

验证手段:

* **与真实 ArcGIS 空白库逐段字节比对**(`test_system_table_contents_match_arcgis`)
* **写 → 关 → 重开 → 读** 的闭环,而不是只看内存对象
* **不变量断言**:读测试不假设记录区布局,断言 tablx 驱动下必然成立的性质
* **往返测试**:WKT 往返、环闭合往返、Z/M 声明往返、GeoJSON 往返
* **教科书真值表**:9 条 DE-9IM 矩阵手推值 + 单位正方形面积/周长/质心、
  带洞面积、`LINESTRING(0 0, 3 4)` 长度 5 —— 这些不依赖实现
* **谓词不变量**:`disjoint == not intersects`、`contains(a,b) == within(b,a)`、
  `covers(a,b) == covered_by(b,a)`、每个谓词与 `relate()` 矩阵自洽
* **独立真值**(numpy 那条路):用 `fractions.Fraction` 算精确有理数,两个实现
  各自离真值多远 —— 这是"numpy 的数还准不准"唯一能回答的方式

> ⚠️ **用例数不是覆盖率的证明。** 本轮就是这个教训:库带着**两个真实
> bug**(所有线与面的 `wkt()` 产出非法文本;`POINT Z` 的维度后缀被吞掉、
> `M` 静默变成 `Z`)跑过了 134 个绿色的用例。原因是
> `test_read.py` 的往返用例里有一句多余的 `break`,每个图层只测了第一条
> 要素(恰好都是面),而 `test_geometry.py` 当时**根本没有 WKT 出口的断言**。
> 往返测试还有一个结构性弱点:**用自己的宽容解析器读回自己吐的文本**,
> 非法格式只要解析器肯收就发现不了。发现它的是 `tools/fake_ogr.py` ——
> 它注入一个假的 `osgeo.ogr`,于是 `verify_topology.py` 里的 `to_gdal()` 真的
> 把 WKT 递给了"GDAL"去解析,非法文本当场暴露。(`break` 已删,`TestWkt`
> 已补 13 条精确串断言,`fake_ogr.py` 已从临时脚本转正进 `tools/`。)

---

## 八、进度清单

- [x] Tier 1:`.gdbtable` / `.gdbtablx` / Esri 几何 / 系统表 / 完整读链路
- [x] Tier 2:记录序列化 / 索引维护 / 建库 / 建图层 / 写要素
- [x] Tier 2:`update_feature` / `delete_feature` 完整实现
- [x] Tier 3:度量与描述(`envelope` / `area` / `length` / `centroid` / `distance`)
- [x] Tier 3:构造(`convex_hull` / `simplify` / `segmentize`)
- [x] Tier 3:DE-9IM 矩阵 + 九个拓扑谓词 + `equals` / `exactly_equals`
- [x] Tier 3:`is_valid` / `is_simple`(⚠️ **部分实现**,见 §六 第 11 条)
- [x] Tier 3:WKT 出口修正(非法括号 + 丢失的 Z/M 后缀,见 §七 的教训)
- [x] Tier 3:GeoJSON 导出 / `__geo_interface__` / `from_geojson` 往返
- [x] Tier 3:可选 numpy 加速(面积 / 周长 / 质心,×21~25,零硬依赖)
- [x] Tier 4:`.gdbindexes` 解析 + `.freelist` 读取 + `.atx` / `.spx` / 注释 /
      附件 / 域的桩与注释
- [x] 完整设计说明文档([`DESIGN.md`](DESIGN.md))
- [x] 使用示例(read / write,见 [`examples/`](examples/))
- [x] Tier 标注(完整 / 有风险 / 桩,见 §四)
- [x] 性能:惰性几何 + 数组解码内联 + `bbox=` 存储包围盒粗筛(见 §五)
- [x] 性能:可选 C 扩展(`_gdbaccel.c`)加速几何 varint 数组,59.3 s → 6.3 s;
      差分验证逐位相同,没编译自动回退(见 §五 第 6 步 / `DESIGN.md` §2.19.4)
- [x] 验证:GDAL/GEOS 谓词对拍工具(`tools/verify_topology.py`),**已真跑**:
      1,290 对 / 14,190 项判定(放大口径 2,438 对 / 26,818 项),真实语料 0 处
      不符。它抓出并修掉了 `relate()` 的**两个**真 bug(交点不可精确表示、
      共线重叠子段的中点算到线外),同一根因;并连带修掉了 `tools/fake_ogr.py`
      被 shapely 抢走 oracle 的问题(守卫悄悄失效,第二次)。残留 2 处为精度地板,
      见 `DESIGN.md` §2.21
- [ ] (未做,已说明理由)`Shape_Area` / `Shape_Length` 自动字段
- [ ] (未做,已说明理由)`.atx` / `.spx` 查询与生成
- [ ] (未做,已说明理由)注释 / 附件 / 域的写入
- [ ] (未做,刻意划界)属性记录解码 / 逐要素簿记搬进 C(见 §六 第 10 条)
- [ ] (未做,刻意划界)overlay(`buffer` / `union` / `intersection` / …),
      需要 GEOS 级精度模型(见 §六 第 12 条)
