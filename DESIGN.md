# pyopenfilegdb 设计说明

> **纯 Python** 实现的 Esri File Geodatabase(下称 FileGDB / `.gdb`)读写库
> —— 唯一的例外是一个**可选**的 C 加速模块(几何坐标数组的 delta-varint 解码,
> 见 §2.19.4):没编译出来就自动回退纯 Python,**功能完全一样**。
> 全部格式知识来自对 GDAL `ogr/ogrsf_frmts/openfilegdb/` C++ 源码的阅读
> 与对真实 ArcGIS 产出的 `.gdb` 的逐字节实测。

---

## 0. 定位与硬约束

| 约束 | 落实方式 |
|---|---|
| 不依赖 GDAL / fiona / pygdal / libgdal | `import` 图里只有标准库 + 本项目自己的 `_gdbaccel` |
| 只用标准库 + 纯 Python 第三方 | 仅用 `struct` / `io` / `os` / `re` / `xml.etree` / `dataclasses` / `typing` / `math` / `contextlib`。**没有 numpy** —— 见 §2.19.3 |
| 不调用任何外部二进制 | 没有 `subprocess`,没有 `ctypes` |
| C 扩展**可选**,且永远是回退之外的加速 | 只有一个 `pyopenfilegdb/_gdbaccel.c`(几何坐标数组的 delta-varint 解码),**没编译出来就跑纯 Python,功能完全一样**。见 §2.19.4 |
| 格式版本 | 支持 version **3**(ArcGIS 10.x),version 4(11.x / Pro)只读识别并拒绝写入;9.x 抛 `GdbVersionError` |
| 无公开规范 | 每个非平凡常量的注释里都写了对应的 GDAL 函数名与文件 |

> **这条约束在本轮被放宽过一次。** 原口径是"不要任何 C 扩展库",用户后来
> 明确改成"可以使用 C 拓展,不能依赖 GDAL 相关库"。"不依赖 GDAL"这条
> **没有**放宽,也仍然是最硬的一条;放宽的只是"能不能有 C"。
> §2.19.3(为什么当初用 numpy 的方案被否)与 §2.19.4(C 加速的落地与实测)
> 记录了这个转向的完整理由,§2.19.5 记的是随后"换结果容器"那一步
> (端到端 4.80 s → 1.50 s)。

**没有网络、没有 GDAL 二进制可供对拍。** 因此本文中所有"实测"标注的数字,
都来自对随项目提供的真实 `.gdb` 样例逐字节统计得出,统计脚本已删除,
结论固化在代码注释与 `tests/test_read.py` 的不变量断言里。

---

## 1. 磁盘结构总览

一个 `.gdb` 是一个**目录**,扩展名是 `.gdb`。内部是一堆编号文件:

```
xxx.gdb/
├── g                      8 字节魔数 b'\x05\x00\x00\x00\xDE\xAD\xBE\xEF'
├── timestamps             400 字节,全 0xFF
├── a00000001.gdbtable     GDB_SystemCatalog    —— 表清单,决定物理编号
├── a00000001.gdbtablx
├── a00000002.gdbtable     GDB_DBTune
├── a00000003.gdbtable     GDB_SpatialRefs
├── a00000004.gdbtable     GDB_Items           —— 每个要素类的 XML 定义
├── a00000005.gdbtable     GDB_ItemTypes
├── a00000006.gdbtable     GDB_ItemRelationships
├── a00000007.gdbtable     GDB_ItemRelationshipTypes
├── a00000009.gdbtable     <第一个用户要素类>
├── a00000009.gdbtablx
├── a00000009.gdbindexes   (可选)索引清单
├── a00000009.atx          (可选)属性索引
└── a00000009.spx          (可选)空间索引
```

三件套的分工:

* `.gdbtable` —— 表头 + 字段描述 + **记录体区**。扁平的,没有页、没有压缩。
* `.gdbtablx` —— "行号 → `.gdbtable` 内绝对偏移" 的索引,**也是记录是否存在的唯一依据**。
* `.gdbindexes` —— 只是一个描述符列表,声明"我有哪些 `.atx` / `.spx`"。

`.gdbtable` 和 `.gdbtablx` 是一对,缺一不可;**读路径永远是 tablx 驱动的**。

---

## 2. 逐项难点

下面按"踩到才知道"的顺序排列。每一条都对应代码里的一段注释。

### 2.1 表头 40 字节,而"40 字节处"并不总是字段描述区

`.gdbtable` 头部:

```
+0   uint32  version              3 (ArcGIS 10.x) / 4 (ArcGIS Pro)
+4   uint32  nValidRecordCount    当前有效记录数(不含已删除槽位)
+8   uint32  nHeaderBufferMaxSize 头部区预留大小
+12  uint32  magic1 = 5
+16  uint32  magic2 = 0
+20  uint32  magic3 = 0
+24  uint64  nFileSize            文件总字节数,必须等于实际长度
+32  uint64  nOffsetFieldDesc     字段描述区起点
------------------------------------------------ 共 40 字节
```

关键点:**`+32` 才告诉你字段描述区在哪,不要假设它等于 40。**

GDAL 写文件时会在 `+40` 处塞一段创建者字符串 `"GDAL <release>"`
(`filegdbtable_write.cpp:159`)。而**真实 ArcGIS 产出的文件没有这段**,
于是 `offset_field_desc == 40` 恰好成立 —— 这是一个极易形成的错误假设:
拿 ArcGIS 样例写代码,`offset_field_desc == 40` 永远为真,一旦遇到 GDAL
写的文件就全错。本库一律读 `+32`,并在 `test_system_table_contents_match_arcgis`
里断言两种来源的 `offset_field_desc` 都是 40(仅作为样例事实,不作为代码假设)。

同理,`+24` 的文件大小和 `+4`/`+8` 的计数在写入过程中会变,所以
`tests/test_create_write.py::_normalized` 在做"与模板逐字节比对"时会
**跳过这三个字段**,只比 `version` / 三个 magic / 字段描述区 / 记录体。

### 2.2 记录区:**不连续、无序、可以有"幽灵区"**

这是本项目最大的一个坑,也是最初写出来的测试大面积失败的原因。

文档级直觉是:"记录区从字段描述区之后开始,一条接一条地排下去"。**错。**

实测(74 张真实表 / 33959 个 tablx 偏移)推翻了三条假设:

1. **记录不是紧挨着排的。** `新建文件地理数据库.gdb/a000000a` 里,字段描述区
   与第一条记录之间有 705 字节的空洞;另一张表里记录 1 结束于偏移 3253,
   记录 2 却开始于 5126 —— 中间是垃圾。
2. **字段描述区不一定在记录前面。** `上栗县…/a00000009` 的字段描述区在
   **81704**,而它全部 159 条记录都在它**之前**,最早一条在偏移 40。
3. **tablx 偏移不单调。** 槽位 1 在 74018,槽位 2 在 63077 —— 删除后重用时
   会回填到更早的空洞里。

结论:**线性扫描记录区在这个格式上是不可行的**。GDAL 从不线性扫描,
永远走 `.gdbtablx`。本库照做:唯一合法的遍历方式是"读 tablx 拿偏移 →
逐个 seek → 读一条"。

`tests/test_read.py::test_gdbtable_header_self_consistent` 因此被重写成
**tablx 驱动的不变量检查**,而不是顺序遍历 —— 它的 docstring 里完整记录了
上面这三条被推翻的假设。

### 2.3 删除标记是**负数长度**,不是"高位标志"

记录体开头是 4 字节长度。删除一条记录时,ArcGIS 把这个长度**取负**写回:

```
活记录: 0x00000058  → 长度 88
已删除: 0xFFFFFFF8  → 长度 -8   (二的补码,原记录体 8 字节)
```

很容易误读成"最高位置 1 = 删除标志,低 31 位是长度"。区别在于
`0x80000004` 这种值:按标志位解释是"长度 4",按补码解释是"长度 -2147483644",
而实测中只出现前者语义下的负数。本库按**补码**处理:

* 读:`length = int32(offset)`,小于 0 即删除槽位(`_gdbtable.py` 读取路径)。
* 写:`((-old_len) & 0xFFFFFFFF).to_bytes(4, 'little')`,并 `tablx.set_offset(row, 0)`。

实测 33959 个偏移中,**活记录长度无一为负,也无一处越界**,该判据成立。

### 2.4 OID 不占记录体字节

`OBJECTID` 字段在字段描述区里有定义,但**记录体里一个字节都不占**。
它的值 = **行号 + 1**(行号即 tablx 槽位下标,从 0 起)。

后果:

* 空值位图里没有 OID 的位置(`fixed_size == 0`,见 `GdbField.fixed_size`)。
* 不能靠"记录体里有没有 OID"来校验。`GDB_DBTune`(`a00000002`)压根**没有
  OID 字段** —— 没有 OID 字段的表是合法的,本库必须容忍
  (`test_read.py` 里一条 OID 字段断言因此被删掉)。
* 读完一条记录后,必须把 OID **从属性字典里剔除**,否则 `set(feat.attributes)`
  会多出一个 `'OBJECTID'` 键,与用户按字段清单构造的属性集对不上
  (这正是当年 73 个读测试失败的根因)。修法是
  `GdbTable.oid_field_name` + `GdbTable._values_to_feature()` 统一出口,
  `GdbLayer.read_feature()` 也走同一个函数。

### 2.5 空值位图只数 **nullable** 字段,而且是"从全 1 往 0 清"

记录体布局:

```
+0   [空值位图]  ceil(n_nullable / 8) 字节
+a   [各字段值]  按字段定义顺序紧密排列,定长字段占固定字节,变长字段前置 4 字节长度
+b   [几何]      varuint 长度 + 几何 blob   (若该表有几何字段)
```

位图规则:

* **只有 `nullable=True` 的字段占 1 bit**,`required` 字段不占位。
  按字段声明顺序,LSB first 排列。
* 位 = **1 表示 NULL**,0 表示有值。
* 写入时 GDAL 把整个缓冲区初始化成 `0xFF`(全 NULL),每写一个值就
  **清掉对应位**:`m_abyBuffer[...] &= ~(1 << ...)`。本库照此实现。

顺带一个反直觉的事实:同一张表里,几何字段可能是 nullable 也可能不是;
`GdbGeomField` 继承 `GdbField`,所以 `nullable` 标志对它同样生效。

### 2.6 `.gdbtablx`:1024 槽一页,偏移宽度 5 字节

```
+0   uint32  version             必须与 .gdbtable 相同
+4   uint32  n1024BlocksPresent  已分配的索引页数
+8   int32   nTotalRecordCount   槽位总数(含空洞)
+12  uint32  nTablxOffsetSize    每个偏移占几字节:4 / 5 / 6
+16          偏移表,n1024BlocksPresent * 1024 项,每项 nTablxOffsetSize 字节
尾部 16 字节 trailer:
    uint32 nBitmapInt32Words
    uint32 nBitsForBlockMap
    uint32 n1024BlocksBis      冗余,必须等于 +4
    uint32 nLeadingNonZero32BitWords
    若 nBitmapInt32Words != 0,后跟位图本体
```

要点:

* **一页 = 1024 条记录**,不是 512。(`TABLX_RECORDS_PER_BLOCK = 1024`)。
  这里的 "block" 是**索引分页单位,与压缩无关**。
* ArcGIS 用 `offset_size = 5`(3 字节就够但现在文件会超 16MB,
  留一个字节余量)。读的时候必须按实际宽度解,不能写死 4。
* 偏移 == 0 表示空槽(从未写过或已删除)。**这是判断记录存在与否的唯一权威**,
  `.gdbtable` 里的 `nValidRecordCount` 只是个计数。
* 尾部位图(base block map):删除后 GDAL 可以选择压实偏移表,用位图记录
  哪些页还非空。查找时要把"逻辑行号"先换算成"压实后的物理下标":
  `iCorrected = popcount(位图前 iBlock 项) * 1024 + row % 1024`。
  本库实现了这套换算,但**写路径不产生位图**(见 §2.15)。

### 2.7 Esri 几何 blob 的四个层次

一个几何 blob 从外到内:

```
varuint  总长度           ← 在记录体里,是"varuint 长度 + blob"的前缀
u32      shape type       见 §2.8
--- 以下随类型变化 ---
POINT:   u32 计数(=1) + 量化 x,y [+ z [+ m]]
ARRAY:   u32 点数 + 量化 x[] + y[] [+ z[] [+ m[]]
ARC/POLYGON:
         u32 环数 + u32 点数
       + u32 每环起点下标[环数]      ← 注意:是**下标**,不是长度
       + 量化 x[] + y[]
       + (若 hasZ) u8 z_range + f64 zmin/zmax + 量化 z[]
       + (若 hasM) u8 m_range + f64 mmin/mmax + 量化 m[]
```

注意几个 GDAL 的取舍:

* **环起点存的是"起点在点数组里的下标"**,不是"每环的点数"。差分时容易写反。
* Z/M 数组前有个 `u8` 范围标志。取 `0x42` 表示"该数组**整体为空**",
  此时后面**不跟** min/max 和数组本体 —— 这是最常见的"有 M 字段但这条
  要素没写 M 值"的表达。
* **点坐标是"先列所有 X、再列所有 Y"**,不是交替的 (x,y,x,y)。做差分编码时
  必须按分量分列。

### 2.8 GDAL 私有的 shape type 编号(与 shapefile 公开规范不一致)

照公开的 ESRI shapefile 规范写 3D+M 几何,GDAL 会读错。因为
`ogr/ogrpgeogeometry.h` 用的是**另一套编号**,而且 **ZM 档恰好占用了
shapefile 里 Z 档的编号**:

| 类型 | GDAL 编号 | shapefile 公开值 |
|---|---|---|
| POINTZ | 9 | 11 |
| POINTZM | 11 | (无) |
| POLYGONZ | 19 | 15 |
| POLYGONZM | 15 | (无) |
| MULTIPOINTZ | 20 | 18 |
| MULTIPOINTZM | 18 | (无) |
| ARCZ (POLYLINEZ) | 10 | 13 |
| ARCZM | 13 | (无) |

`POINTM=21` / `POLYLINEM=23` / `POLYGONM=25` / `MULTIPOINTM=28` /
`MULTIPATCH=32`,以及 `GENERAL*` 系列 50–54 也一并定义在
`_constants.ShapeType`。100+ 行注释里逐条标了出处。

### 2.9 量化:不对称的 `+1`,以及 `0` 表示 NaN

坐标在盘上是定点整数:

```
POINT 单点:  q = round((v - origin) * scale) + 1
数组(array): q = round((v - origin) * scale)
反解:        v = (q - 1) / scale + origin        (POINT)
             v =  q      / scale + origin        (数组)
```

**单点带 +1、数组不带。** 为什么?因为量化后的 `0` 被征用为 **NaN 的占位符**
—— 单点若不加 1,原点处的点就会被解码成"空几何"。数组不需要这个偏移,
因为数组用 `u8 范围标志 = 0x42` 表示整列缺失,不需要逐点占位。

GDAL 在 `filegdbtable.cpp` 的 `ReadPoint` / `ReadArray` 两组函数里分别写的
这两套公式,注释见 `_esri_geometry.py`。

### 2.10 ESRI 的 NaN 有自己的位型

IEEE754 的 quiet NaN 是 `0x7FF8000000000000`。**Esri 用的是
`0x7FF8000000000001`,最低有效位额外置 1。** GDAL 为此专门写了
`getESRI_NAN()`。本库:

```python
ESRI_NAN_BITS = 0x7FF8000000000001
ESRI_NAN = struct.unpack('<d', struct.pack('<Q', ESRI_NAN_BITS))[0]
```

写 Z/M 缺失值时按位写 Esri NaN,不能写 `float('nan')` 的默认位型。

### 2.11 环:盘上是**闭合**的,内存里是**不闭合**的

实测:**样例里 1746/1746 个多边形环的首尾点都相同**(盘上闭合)。
而 `GdbGeometry` 的对外表示、`from_wkt` / `wkt()` 的表示都是**不闭合**的
——WKT 按 OGC 要求必须写闭合环,`from_wkt` 解析时会先把重复的首点削掉,
所以 `POLYGON((0 0,1 0,1 1,0 0))` 存进 `GdbGeometry` 的是 **3** 个顶点。

这是"表示一致性"问题:如果两处不一致,`test_geometry_wkt_round_trip` 就会
凭空多/少一个点。本库用**两阶段归一化**解决:

* 解码末尾:`_strip_part_closures(part_starts, points)` 去掉每个环末尾那个
  重复首点(只比 XY)。
* 编码开头:`_close_parts(part_starts, points)` 补回。

于是**外面看到的一律是不闭合的环**,盘上写出的却仍是闭合的 —— 两边都正确。
`_esri_geometry.py` 里那条曾经写着"Esri 存的是不重复的顶点序列"的注释
已被更正,并注明 1746/1746 这个实测依据。

### 2.12 环方向:首环 CW,其余 CCW

`ProcessSurface` 的判据是 `bReverseOrder = bFirstRing != bIsClockwise`
(`filegdbtable_write.cpp:936-955`),化简后:**外环(第 0 环)顺时针,
其余环逆时针**。这既是 Esri 的约定,也是 OGC 的约定,但顺带要注意:
**输入方向是自由的**,编码时要主动算有向面积来翻转,不能假设调用方给对了。
`_orient_rings(counts, points)` 干这件事。

**一个伴随的损失:MULTIPOLYGON 会被压成 POLYGON。** Esri 几何类型里没有
"多面"这一档 —— 多个面的所有环丢进同一个 `POLYGON` 的 parts 数组即可,
GDAL 在 `wkbPolygon → wkbMultiPolygon` 那步做的也是同类归并。代价是
**读回来时无法还原"哪几个环属于同一个面"**,外环/内环的配对关系丢失。
这是格式本身的局限,不是实现缺陷;`_esri_geometry.py` 的编码注释里写明了
这一点。

### 2.13 字段描述区尾部有 `DE AD BE EF`

字段描述区的结构大致是:

```
varuint  字段数
对每个字段:
    u8  名称长度 + 名称(UTF-8)
    u8  别名长度 + 别名
    u8  字段类型 (FGFT_*)
    u32 flags     (MASK_NULLABLE / REQUIRED / EDITABLE ...)
    [若可编辑] 默认值: u8 长度 + 值   ← 也可能是 magic marker
    [若是 STRING] u32 最大宽度
几何字段额外跟: WKT 长度 + WKT + 量化参数 + 全表包围盒 + 格网分辨率
--------------------------------------------------
u32 0xDEADBEEF   ← 结束标记
```

`DE AD BE EF` 在 FileGDB 里是通用的"段结束"标记 —— `g` 魔数里也有它
(`b'\x05\x00\x00\x00\xDE\xAD\xBE\xEF'`)。字段描述区的长度 = 从
`offset_field_desc` 到该标记的**含标记**长度(标记本身占 4 字节)。

字段默认值有两种形态:直接的内联值,或一个 **magic marker**
(如 `FILEGEODATABASE_SHAPE_AREA`,表示"这个值由几何算出来,别当真")。
本库的 `_write_default()` 只处理内联值 —— **这是 `Shape_Area` /
`Shape_Length` 自动字段尚未实现的原因**(见 §4)。

### 2.14 系统表的四重登记 + 物理编号

新建一张要素类,要在**四个地方**同时登记,少一处 ArcGIS 就打不开:

1. `a00000001.gdbtable`(GDB_SystemCatalog)—— 加一行"物理名 + 文件格式"
2. `a00000004.gdbtable`(GDB_Items)—— 加一行,`Definition` 列放**完整 XML**
3. `a00000006.gdbtable`(GDB_ItemRelationships)—— 加"数据集 ↔ 要素类"关系
4. `a00000002/3/5/7` 一般不动,但**编号要接着它们排**

物理编号的算法是 `n_table = 1 + GDB_SystemCatalog.GetTotalRecordCount()`,
格式化成 `a%08x`。**新库的 SystemCatalog 有 8 条记录**(含 `a00000008`
GDB_ReplicaLog 的那一行),所以**第一个用户图层是 `a00000009`**。

`core.py::create()` 只写 `a00000001`..`a00000007` 这七个物理文件(不写
`a00000008` 的实体文件)—— 但 catalog 里那第 8 行的记录**必须保留**,
否则编号会错位。`_gdb_template.py` 把"一张空白库应有的全部字节"固化成
模块级常量,由 `tools/gen_template.py` 从真实空白库生成,可重新生成。

### 2.15 写路径:原地更新 vs 追加

写路径的难点不在"写一条记录"(那只是 §2.5 的逆运算),而在**索引维护**:

* **追加**:新记录写到文件末尾(`nFileSize` 处),`tablx.set_offset(row, pos)`,
  同步 `+4` 计数、`+24` 文件大小、`.gdbtablx` 的 `nTotalRecordCount`。
* **原地更新**:如果新记录体**不比旧的长**,可以直接写回原偏移,不用动 tablx
  —— 这是唯一省事的更新路径。
* **删除**:写负数长度 + `set_offset(row, 0)`,并把 `+4` 计数减 1。
  文件**不回收**(不写 freelist)。
* **变长字段导致新记录比旧的长**:只能"标记旧的为删除 + 末尾追加新的",
  等价于"删除 + 插入",OID 不变(tablx 槽位不变)。

**`.gdbindexes` / `.atx` / `.spx` / `.freelist` 一律不写。** 理由:
它们是**可选**的纯加速结构 —— ArcGIS 打开时若发现 `.gdbindexes` 里声明的
索引文件不存在会报错,但**不去查 `.gdbindexes` 也完全能读**(GDAL 就是
"有就用,没有就线性扫")。与其写一个半对的空间索引害人,不如一个都不写,
让 ArcGIS 自己按需重建。这是个明确的取舍,不是遗漏。

### 2.16 全表包围盒:只增不减的**上界**,不是真实范围

字段描述区里的 `xmin/ymin/xmax/ymax` 是 ArcGIS 维护的**单调增长的上界**:
删除要素时**不会**回缩。实测有一张表的存储范围与真实几何范围差了 **5.3°**。

后果:

* `GdbLayer.extent` 的 docstring 里加了 `.. warning::`,明说这是上界。
* `tests/test_read.py::test_extent_matches_geometry_bounds` 断言的是
  **包含关系**,容差 `eps = 4.0 / max(abs(xy_scale), 1.0)`(一个量化步长的量级),
  而不是相等。
* 写入时:**只放大,不缩小** —— 与 ArcGIS 行为一致。

### 2.17 XML 命名空间 10.3 vs 10.8

`GDB_Items.Definition` 里的 XML 默认用 `.../ArcGIS/10.3` 命名空间;
当字段里出现 `INT64` / `DATE` / `TIME` / `DATETIME_WITH_OFFSET`,或显式要求
`TARGET_ARCGIS_VERSION=ARCGIS_PRO_3_2_OR_LATER` 时,升级到 `.../10.8`
(`ogropenfilegblayer.cpp:90`, `:641-653`)。写库时必须选对,否则 ArcGIS
可能拒绝打开或误读字段类型。

### 2.18 `PhysicalName` 的一处分歧

新建 item 时 `PhysicalName` 写什么,ArcGIS 与 GDAL 不完全一致(ArcGIS 写
`a00000009` 这种物理名,GDAL 在某些路径下留空由 ArcGIS 回填)。
本库按 **ArcGIS 的实际产物**写物理名,并在注释中标注这处与 GDAL 的差异,
以免日后对拍时误判为 bug。

### 2.19 纯 Python 的性能账:**几何必须惰性**

这条不是格式坑,是**实现口径**的坑,但它决定了"能不能用"。

基线:`村行政区划` 图层(21,217 条记录),`main.py` 那种"只读字段、不解
几何"的循环,GDAL/OGR 要 **742 ms**,而本库初版要 **5.8 s** —— 差近 8 倍。
cProfile 一量,`_esri_geometry._read_xy_array` 一个人占 **84%**,IO 只占
0.1%。也就是说瓶颈完全不在"怎么读文件",而在"逐顶点解 varint"。

GDAL 为什么不吃这个亏:它的几何是 **C++ 惰性构造**的,
`OGROpenFileGDBLayer::SetIgnoredFields` 让属性查询根本不碰几何,而
`OGRFeature::GetGeomFieldRef` 第一次被调用时才建几何、建完缓存。本库照抄
这个口径,分五步落地(前两步管 CPU、第三、四步管 IO,第五步管字段解码):

**第一步:数组解码去函数调用。** 原来每读一个坐标要 2 次
`read_varint_delta()` 调用 + 3 次元组打包/解包。改成把 varint 循环
**内联**进 `_read_xy_array`,循环体里只做整数运算,`x / scale + origin`
这段浮点整批甩给 `map(add, map(truediv, xs, repeat(scale)), repeat(origin))`
跑在 C 层。实测 **2.049 → 1.444 µs/顶点(1.42×)**。

顺带否掉了一个看着很美的假设:"大部分增量是单字节,加个快路径"。把
6 个样例库全量扫了一遍(61,062,602 个顶点),单字节命中率 **0.0%** ——
ArcGIS 的量化坐标增量几乎总是多字节。**先量再写**,省下一堆废码。
(注意这条结论只对**顶点增量**成立,不对**长度前缀**成立 —— 见第五步。)

**第二步:惰性几何。** `_datatypes._LazyGeometry` 持有未解码的 blob,
`GdbFeature.geometry` 是个 property,第一次访问才 `resolve()`,结果缓存
回 `_geometry`;`iter_rows()` 传 `lazy_geom=True`,`read_row()` 保持 eager
(它的契约是"返回的字典里就是真几何")。效果:**5.833 s → 0.013 s,449×**,
比 GDAL 的 742 ms 还快 —— 因为本库连 `GdbFeature` 都省着没建。

注意 `_LazyGeometry` 只在 `_gdbtable` 内部流转,外面拿到的永远是
`GdbGeometry`;`GdbFeature.copy()` 也**不强解**,拷贝出来仍是惰性的。

**第三步:`bbox=` 走存储包围盒粗筛。** 几何 blob 自带一个
`[minX, minY, spanX, spanY]` 的 varuint 包围盒(见 §2.7),读它不用解点
数组。`_esri_geometry.peek_envelope()` 只读这几步就返回一个 **放宽一个
量化步长**(`pad = 4.0 / max(abs(scale), 1.0)`)的包围盒,保证是真实范围
的**超集** —— 粗筛淘汰掉的必定不相交,不会错杀。`read_features(bbox=...)`
先拿它粗筛,过了才 `_geometry_bbox()` 精确判。

**第四步:连几何的字节都不读。** 第二步只省了"解码",没省"读盘"。
量一下 `村行政区划`:记录体合计 **261.7 MB**,其中几何 blob 占
**259.3 MB(99.1%)**,非几何部分只有 **2.4 MB** —— 而几何字段在字段
顺序里排第 2(OBJECTID 不占字节),后面还跟着 14 个属性字段,所以既不能
"读到几何就收工",也不能整条读。

⚠️ **GDAL 不这么做。** `FileGDBTable::SelectRow` 是一次 `VSIFReadL`
把整条记录(含几何)读进 `m_abyBuffer` 的,几何字节照样从盘上过一遍,
只是后面不 `GetAsGeometry` 而已(见 `filegdbtable.cpp:1828`)。所以这一步
是在 GDAL 之上**多走一步**:不读那 99%,而不只是不解码。

做法(`_read_record_lazy`):先读一小段探针(起步 32 B,探不到按 ×4 放大)
试解一遍,几何字段的 varuint 长度前缀一定落在里面,顺手被记进
`geom_span`;拿到 `(几何起点, 长度)` 后在两种读法里**取读得少的那个**:

* **跳读**:读 `[0, 几何起点)` + `[几何末尾, 记录末尾)`,中间整段不进内存,
  只留个文件偏移交给 `_LazyGeometry.deferred()`;
* **整读**:几何太小的时候,跳过它反而要多读一遍前缀,不如老实读完。

判据是 `几何末尾 > 已读字节数` 才值得跳 —— 这样**读的字节数永远不会超过
优化前**,几何小的表最多打平。实测(`tools/bench_geom_skip.py`,17 个图层):
乡 76.5 → 0.352 MB(217×)、县 16.3 → 0.007 MB(2346×)、村 261.8 → 3.061 MB
(85.5×),最差的表 1.2×。

⚠️ **踩过的坑:`open()` 的默认缓冲会把这笔账全吃回去。** 默认给的是 8 KB
`BufferedReader`,而记录在 `.gdbtable` 里不连续、每条都要 `seek` ——
`seek` 会把缓冲区作废,于是每次 `read` 都实打实拖一个缓冲区。跳读之后我们
每次只要几十~几百字节,**逻辑上读 3 MB,OS 层面却读了 ~348 MB**,比优化前
的 262 MB 还差。`tools/bench_io_buffer.py` 把 0→8192 全试了一遍
(0.807 / 0.724 / 0.643 / **0.634(512)** / 0.697 / 0.656),取
`_TABLE_READ_BUFFER = 512`。缓冲不是越大越好,得跟"每条记录实际要读的
字节数"对齐。

**第五步:字段解码编译成计划。** 走完前四步,IO 已经不是瓶颈了,剩下的
全是**纯 Python 的簿记开销**。cProfile 一量(`村行政区划`,一次遍历):
`is_oid` 属性被求值 **30.9 万次**、`_decode_one_field` 被调 22.4 万次、
ByteReader 的 `need()` 24.5 万次 / `remaining` 26.6 万次 —— 一共
**269 万次函数调用**,而真正干活的只有几十万次 `varuint`。

`_build_decode_plan()` 把字段定义**预编译**成一张扁平表
`(名字, 种类, 位图字节下标, 位图掩码, Struct 解包器, 字段)`,只建一次:

* 主循环只做**整数比较**(`kind <= _K_I64` / `== _K_STRING` / …),不再访问
  `field.nullable` / `field.is_oid` / `field.field_type` 这三个 Python property;
* 空值判断从"求值 nullable → 累加位号 → 移位取位"缩成
  `blob[bbyte] & bmask` 一次下标 + 一次与;
* 定长数值直接 `Struct.unpack_from`,字节数用 `Struct.size`(不再查表);
* 游标摊成局部整数 `pos`、缓冲区摊成局部 `data`,**完全不用 ByteReader** ——
  每取一个值过 `need()` / `remaining` 两道方法调用是纯浪费;
* 长度前缀内联单字节快路径(`data[pos] < 0x80`)。⚠️ 这与第一步"单字节
  假设被否掉"不矛盾:那说的是**顶点增量**,这里说的是**字符串/几何的
  长度前缀**,后者绝大多数 < 128;
* 少见类型(GUID / BINARY / RASTER / DATETIME_WITH_OFFSET)全部挪进
  `_decode_other()`,不把热循环撑长;
* `_values_to_feature()` 的名字由计划预先算好,不再逐字段 `isinstance`
  一遍(14 字段 × 2.1 万条 = 28 万次类型判断)。

效果:**函数调用 269 万 → 104 万(2.6×)**;`main.py` 终端实测
**0.70 s → 0.42 s**。

**结果与残留差距:**

| 场景 | 本库 | GDAL | 说明 |
|---|---|---|---|
| 只读属性(`村行政区划`,21217 条) | **0.42 s** | 0.30 s | 纯 Python 逐字段解码 vs C |
| 只读属性(`乡行政区划`,2649 条) | 0.072 s | — | 27 µs/条 |
| `iter_rows`(只 seek+read,不解码) | 0.042 s | — | IO 已经不是瓶颈 |
| 连几何一起读(1.3 M 顶点) | 18.3 s | — | 纯 Python 逐顶点 varint 的硬成本 |
| `bbox=` 小范围(命中 31/2649) | 0.603 s | — | 存储包围盒粗筛 |

**剩下的差距如实说:** 一是**几何解码** —— 一旦真的需要几何、而且量还大,
纯 Python 就是比 C++ 慢一个数量级(1.38 µs/顶点),这是解释器逐字节循环的
固有成本,不是算法问题(算法上已经在跟 GDAL 对齐,甚至在 IO 上比它多走了
一步)。二是**属性解码** —— 0.42 s vs 0.30 s 的 1.4 倍,同样来自"每个字段
一次 Python 字节码"这个下限。

> **这两条当时的结论是"再快只能上 C 扩展,而硬约束不允许"。** 那条约束后来
> 放宽了(见 §0 的说明),几何那一半已经被 **§2.19.4 的 C 扩展**处理掉
> (59.3 s → 6.3 s),又被 **§2.19.5 的 flat 容器**压到 **1.50 s**;
> 属性解码那一半**刻意没动**,数字留在这里备查。

### 2.19.1 ⚠️ 别在 PyCharm 调试器里比性能

`main.py` 在终端里 0.42 s,在 PyCharm 里按"Debug"跑却是 **1.4~3 s**,
很容易被误读成"本库慢"。原因不是代码:PyCharm 的调试器是 **pydevd**,
它给每一个 Python 帧装 line tracer,**每条字节码都要回调一次调试器**。
于是:

* 整个 pyopenfilegdb 是纯 Python,**每一行解码语句都上税**;
* GDAL 是 C 扩展,它的 0.3 s 是 C 时间,tracer 根本进不去,税率为 0。

`tools/bench_trace.py` 用 `sys.settrace` 装一个最朴素的 line tracer 复现了
这个机制(不是 pydevd 本身,真实 pydevd 只会更重):

| 路径 | 不 trace | 装 line tracer | 税率 |
|---|---|---|---|
| 本库只读属性(纯 Python) | 0.073 s | 0.172 s | **2.4×** |
| 等量 `utf-16-le` decode(纯 C) | 0.036 s | 0.041 s | 1.1×(≈噪声) |

**结论:要比性能,两边都得在终端里跑、关掉调试器。** 在调试器里比"纯 Python
实现 vs C 实现",量的其实是 tracer 的税率差,不是算法差。

### 2.19.2 几何惰性一失效,代价是 59 秒 —— 这笔账怎么拆

`main.py` 里属性遍历 0.42 s。在循环里加一句 `g = feature.geometry`,立刻变
**59.06 s**(140×)。这不是退化:几何本来就是**惰性**的(§2.19 第二步),
0.42 s 那条路**一个顶点都没解**;碰一下 `.geometry`,`村行政区划` 的
**4,494 万**个顶点全得逐字节解 varint。

拆账工具:`tools/bench_geom_split.py`(取真实 blob,`time.perf_counter`
计时,不开 profiler):

| 档 | µs/顶点 | 占完整解码 |
|---|---|---|
| A0 varint 逐字节解码(只累加整数) | 0.947 | **72%** |
| A1−A0 尾部浮点缩放 + 拼元组 | 0.308 | **23%** |
| D−A1 头解析 + 部件表 + 环闭合 + `GdbGeometry` 构造 | 0.064 | 5% |
| **D 完整 `_decode_geometry`** | **1.319** | 100% |

1.319 µs/顶点 × 44.95 M = **59.3 s**,与用户实测的 59.06 s 对上。**IO 不是
瓶颈**:259.3 MB 几何 blob 全预读也只要 0.781 s(0.78 / 59.3 = 1.3%)。

> 本节的全部数字是**纯 Python 基线**,由 `tools/bench_geom_split.py` 量的 ——
> 那个工具在 `import pyopenfilegdb` 之前置上 `PYOPENFILEGDB_NO_ACCEL=1`,
> 否则 D 档会被 C 加速而 A 档(`xy_loop` 是本文件里手抄的循环)不会,
> `D − A` 直接变负,拆分就废了。
>
> 有 C 扩展时这笔账变成 **1.319 → 0.140 µs/顶点(59.3 s → 6.3 s)**,
> 再换掉结果容器(§2.19.5)之后端到端是 **1.50 s**。见 **§2.19.4**。
> 本节留下的是"为什么纯 Python 只能到这儿"的证据。

**为什么这 60 秒消不掉(在纯 Python 约束下)**

去 GDAL master 逐行核过(`ogr/ogrsf_frmts/openfilegdb/filegdbtable.cpp`):

* `ReadVarIntAndAddNoCheck`(1474–1517)只有**一个 1 字节早退**,
  **没有** 2 字节 / 3 字节特化 —— 其余一律走 `shift` 递增的续字节循环;
* `ReadXYArray`(3451–3479)就是个普通模板循环,每点一次
  `pabyCur >= pabyEnd` 边界检查,换算式正是
  `dxLocal / GetXYScale() + GetXOrigin()`;
* **整个驱动里没有定宽 / 压缩坐标的快路径**,坐标永远是增量 varint。
  GDAL 快的办法是编译期分派(`XYSetter` 模板函数),让 `store` 内联,
  外加去边界检查 —— 这是 C++ 编译器给的,不是算法给的。

也就是说:**我们这段循环就是 GDAL 的算法**,含符号位 `0x40`、低 6 位幅度、
续字节 `shift` 从 6 起步进 7,全部逐位对齐。60 s 是 CPython 解释器跑到
C++ 的同一个循环上的成本,不是算法差、也不是 IO 差 —— 既然算法已经对齐,
**剩下的唯一杠杆就是"把这同一个算法换个执行器"**,这正是 §2.19.4 做的
(约束放宽后)。

⚠️ **别把这笔账记到"模板内联"头上。** `XYSetter` 模板让 `store` 内联,
省掉的是**每个点一次函数调用**(不是每个字节),量级上是 C 侧再快 20~30%。
放在 1,319 ns/顶点的总差距里,大约值 **3 ns ≈ 0.2%**。真正吃掉 1,300 ns 的
是**每次操作的常数**,逐条量过(CPython 3.13,20 M 次循环取最好成绩):

| 循环里的一个动作 | 本库(纯 Python) | C 里的对应物 | 倍数 |
|---|---:|---|---:|
| `blob[pos]`(`bytes.__getitem__` + 装箱) | 66.1 ns | `movzbl (%rax),%ecx` ≈ 0.3 ns | ~200× |
| `x = x + 1`(int 加,要建新对象 + 引用计数) | 32.4 ns | `add` 到寄存器 ≈ 0.3 ns | ~100× |
| `xs.append(v)` | 80.6 ns | `*p++ = v` ≈ 0.3 ns | ~250× |
| `dx = dx-val if neg else dx+val` | 90.5 ns | `cmov`/`add` ≈ 0.6 ns | ~150× |

这条 3 字节 varint 要 12~15 个这样的动作,加起来 ≈428 ns(实测 473.5 ns,
差的是循环控制/分支)。**C 侧同样的活是 ~10 ns 量级,差 40~50 倍** —— 这就是
"解释器分派 + 对象模型"的成本,与内联无关。

而且**内联这一步我们早就做过了**:§2.19 第一步就是把
`read_varint_delta` 内联进 `_read_xy_array`,实测 **2.049 → 1.444 µs/顶点
(1.42×)**。这 1.42× 正是"内联"这个杠杆的真实刻度 —— 它管用,但它是 1.4 倍,
不是 150 倍。GDAL 的模板从它那份 ~10 ns 里再抠 20~30%,在 C 的尺度上值得,
在 473 ns 面前就没意义了。

**还能挤出多少(实测,结果逐位相同)**

| 改法 | 收益 | 折到 59 s |
|---|---|---|
| 尾部 `map` 链换成 listcomp(`[(x/s+ox, y/s+oy) for x,y in zip(xs,ys)]`) | 308.5 → 226.0 ns/顶点 | −3.7 s(6%) |
| varint 循环 3 字节展开(GDAL 无此特化) | 1.19×(只作用在 72% 上) | −8 s(13%) |
| 两者都上 | | ≈49 s(1.21×) |

两个都是**对 GDAL 自己都不优化的循环做微优化**,`x/s+ox` 那条还与原式
逐位相同、只是换了个 C 层写法。既然定的是"读写的逻辑严格按照 GDAL 来
实现",这两条**不做**,记在这里备查。

**真要靠得住的办法是别解**:只比包围盒就 `peek_envelope` —— 实测全量
**21,217** 个存储包围盒只要 **0.892 s**(59 s 的 1/66);要空间过滤就传
`bbox=`,让存储包围盒先筛掉绝大多数要素(§2.19 第三步)。

### 2.19.3 上 numpy 能快多少:实测约 2.8×,最终没采用

"用 numpy 向量化 varint" 是这次唯一有可能跨数量级的思路,所以动手量了
(`tools/bench_numpy_varint.py`,**逐位对拍通过**)。结论两条。

**一、技术上能,而且真的很优雅。** varint 流看起来是串行的,其实除了累加
全都能向量化:

1. **定边界不用知道长度** —— varint 的**末字节**是 bit7 = 0 的那个,于是
   `flatnonzero((arr & 0x80) == 0)` 一次拿到所有 varint 的结束位置,
   前一个结束 + 1 就是下一个开始;
2. **拼值按字节层循环** —— 第 j 字节的贡献是
   `(arr[starts+j] & 0x7F) << (6 + 7*(j-1))`,实测最多 6 层,每层一次
   gather + 位运算;短 varint 的位置靠 `j < lengths` 掩掉;
3. **累加就是 `np.cumsum`** —— 前缀和正好是 numpy 的核心操作,X/Y 分开
   累加只要步长切片 `vals[0::2]` / `vals[1::2]`;
4. **缩放顺手带走** —— `dx/scale + ox` 整组一次,连那 23% 的浮点尾
   (0.308 µs/顶点)一起省了。

实测(真实 72,001 点面,逐位相同):varint 循环 **0.480 → 0.145 µs/varint,
3.6×**;折算到 `村行政区划` 是 **42.7 s → 12.0 s**。

**二、但整层不会跟着快一个数量级。** 换掉解码器之后,**两项和它无关的
成本变成大头**(三行互不重叠,按 44.95 M 顶点折算):

| 项 | 纯 Python | numpy | 说明 |
|---|---:|---:|---|
| varint 循环(89.94 M 个,只累加整数) | 42.7 s | **12.0 s** | 换掉的就是这块 |
| 浮点缩放 + 拼成 Python 元组 | 13.9 s | **6.9 s** | 缩放 0.3 + 物化 6.6,见下 |
| 头解析 + 部件表 + 环闭合 + `GdbGeometry` | 2.9 s | **2.9 s** | 逐要素的 Python 簿记,与解码器无关 |
| **合计** | **59.3 s** | **≈21.8 s** | **2.8×** |

也就是说:**numpy 把 72% 的那块砍到 1/3.6,剩下那 28% 一分不动** ——
物化 4,495 万个 Python 元组本身就要 **6.6 s**(实测 `tolist()` 30 ns/顶点、
`list(zip(...))` 147 ns/顶点),这是 API 形状(`list[(float, float)]`)决定的,
不是解码算法决定的。除非把 `GdbGeometry.coordinates` 换成 numpy 数组
(那会改掉对外契约),否则这 6.6 s 是**任何"先出中间数组、再转元组"的解码器**
都绕不开的地板。

⚠️ 注意这个地板的措辞:"先出中间数组"的才撞得上。numpy 必然要
`tolist()`/`zip` 一次,所以撞;**C 不撞** —— 它一边解 varint 一边 `append`
元组,中间没有数组这一站,所以 §2.19.4 那条路的实测值是 **3.4 s**,在地板
下面。这也是当初选 C 而不是 numpy 的一个附带理由。

> 后来 §2.19.5 把容器也换掉了,连"元组"这一站都没了:C 侧直接往
> `array('d')` 里写,实测 **0.36 s**。也就是说这个"物化地板"最终不是绕过去的,
> 是**把地板拆了** —— 但拆的办法必须改 `GdbGeometry` 的存储形状(`coordinates`
> 退化成惰性派生视图),numpy 那条路当年没有考虑动这层。

**三、为什么最后没走这条路。** 当时的理由是约束:原任务硬约束原文是"只允许
标准库 + 可选纯 python 第三方库(…);如果需要字节处理只用纯 python,**不要
任何 C 扩展库**",而 numpy 就是 C 扩展,所以这段代码只能待在 `tools/` 里当
可行性实测,**不进 `pyopenfilegdb/`**。

后来用户**主动放宽**了这条约束(原话:"可以使用C拓展,不能依赖GDAL相关库,
有没有办法优化"),"不能依赖 GDAL 相关库"这半句没有放宽。放宽之后 numpy 技术上
可以进来了,但**最终仍然没有采用**,理由换成了三条更硬的:

1. **同一块循环 C 快 50 倍。** numpy 版 varint 循环折到 `村行政区划` 是
   **12.0 s**,C 版是 **0.26 s**(见 §2.19.4 的实测表)—— 差两个数量级;
2. **numpy 撞上面那 6.6 s 的物化地板,C 不撞**(它不建中间数组);
3. **为这一个循环拉一个几十 MB 的运行时依赖不划算。** C 版是自带源码、可选
   编译、编不出来就回退,**零外部依赖**;numpy 一旦进了 import 图就摘不掉。

也就是说:§2.19 到 §2.19.3 这几节量出来的"纯 Python 天花板",最后是靠
**§2.19.4 的 C 扩展**破的,不是靠 numpy。numpy 那份实测的价值在于它把
"哪些成本与解码器无关"这条账算清楚了(那张三行表),这个结论反过来决定了
C 扩展该放在哪个边界 —— 只包 varint 数组,**不碰**那 2.9 s 的逐要素簿记。

⚠️ 万一以后真要接 numpy,当年踩到的坑还在:定边界那步 `flatnonzero` 是
O(剩余字节),而调用方是每个 part 一次,整块扫会让小 part 的固定开销反噬
(实测 1000 点时反而比纯 Python 慢),必须按"估窗口、不够翻倍"来扫
(`_find_ends` 就是这么写的)。

### 2.19.4 落地的 C 扩展:几何 varint 数组(可选,有则用)

§2.19 量出来的"纯 Python 天花板"就是被这一节破的。产物只有一个文件
`pyopenfilegdb/_gdbaccel.c`(纯 C,不用 C++/不用 numpy C-API),由
`setup.py` 编成 `pyopenfilegdb._gdbaccel`。

**边界:只包那两个"真正的 varint 循环"。**

| C 入口 | 对应的 Python 函数 | 签名 |
|---|---|---|
| `decode_xy_flat` | `_esri_geometry._read_xy_array` | `(blob, pos, n, scale, ox, oy, dx, dy) -> (array('d'), end_pos, dx, dy)` |
| `decode_scalar_flat` | `_esri_geometry._read_scalar_array` | `(blob, pos, n, scale, origin, acc) -> (array('d'), end_pos, acc)` |

分派就写在**这两个函数体内**开头几行(`_accel.decode_xy_flat is not None` 就走
C),`_decode_point` / `_decode_parts` / `_decode_multipoint` 一行都不用改 —— 改动面
就这两处。

> 另有两个**只服务 `tools/` 的对照入口** `decode_xy` / `decode_scalar`(返回
> 元组列表),库里没有调用方。它们的来历与用途见 §2.19.5。

`dx`/`dy` 与 Z/M 各自的 `acc` 是**跨 part 连续**的线路语义,所以必须穿 C 边界
进、再穿出来,不能在 C 里重置;返回值里带了这两个累加器就是为了这个。

**明确没搬的东西**(用户当时只选了 varint 数组,这些数字留在 §2.19 备查):
逐要素簿记、属性记录解码 0.42 s 那条路。**其中"结果容器"这条后来被
§2.19.5 单独解决掉了** —— 那是本层最大的剩余成本。

**实测(C 侧那些数字见 §2.19.5,这里只记"循环本身"这一档)**

| 项 | 纯 Python | C 扩展 | 倍数 |
|---|---:|---:|---:|
| varint 循环(89.94 M 个,只累加整数) | 336.8 ns/个 → 40.2 s | **2.9 ns/个 → 0.26 s** | **116×** |
| 整层几何解码(`村行政区划`,C 元组版) | 59.3 s | 6.3 s | 9.4× |
| 属性遍历(不碰几何,对照) | 0.412 s | 0.405 s | 1.0×(**没动**) |

最后一行是**刻意保留的对照**:`feat.geometry` 一旦被访问才走加速,只用属性的
那条路径(`main.py` 原样)读数不变,证明加速没有误接到别处。

⚠️ 上面那行"6.3 s"是**只上 C 扩展、还没换容器**时的数 —— 它随后被 §2.19.5
的 flat 容器压到 **1.50 s**(端到端)。两节合起来才是现在库里的真实水平。

**`_MAX_SHIFT = 57`,不是 64。** 这是本模块唯一一处**故意与 GDAL 不同**的决定。
GDAL 的 `ReadVarIntAndAddNoCheck` 用 `GIntBig`(64 位)累加,续字节可以一路
shift 上去;`read_varint_delta`(:109)的上限是 `shift >= 64`。但 C 的
`unsigned long long` 在 `shift >= 64` 时是 UB/截断,而 Python 的 int 是多倍精度、
不截断 —— 两条路会在**畸形 blob** 上给出不同结果。取 57 之后:8 个续字节最多
用到 bit 61,两边的 64 位算术都**精确**,所以**接受域严格相同**,差分对拍才能
直接比"结果 + 异常类型"。代价:超过 8 个续字节的畸形 varint 从"算出一个大数"
变成"判非法" —— **这是对纯 Python 路径的一处有意行为变更**,只在损坏 blob 上
可见,所以 `_read_xy_array` / `_read_scalar_array` 里那两段内联循环也同时补上了
同一个上限(`_esri_geometry._MAX_SHIFT`,由注释互相钉住)。它扔的是
`IndexError`,不是 `GdbFormatError` —— 理由见下。

**异常契约:`IndexError` 做到底。** `iter_rows()`(`_gdbtable.py:931`)只
`except GdbFormatError`,而 `_decode_at`(`_esri_geometry.py:330-334`)把热点循环
漏出来的 `IndexError` 统一归一成 `GdbFormatError`。所以 **C 侧所有失败路径都
`PyErr_SetString(PyExc_IndexError, ...)`** —— C 不需要认识 `GdbFormatError`,
也不会因为抛别的异常类型而穿透 `iter_rows` 的守卫、把整层遍历带崩。三道闸:

1. **分配之前先验点数。** varuint 的长度是无界的,损坏的 blob 可以报出天文数字的
   `n_points`。原来的 `PyList_New(n_points)` 会去要一个巨大列表 → `MemoryError`
   漏出去,而纯 Python 会立刻 `IndexError`。现在用一个**必然成立的截断判据**
   卡住:每点至少 2 字节(x、y 各一个最短 varint),所以
   `n_points > (len - pos) // 2` 直接 `IndexError`;`decode_scalar` 每点至少
   1 字节。这既封了分配上限,又让异常类型与纯 Python 一致。
2. **参数校验不用 `ValueError`。** `pos < 0` / `n_points < 0` 一律 `IndexError`
   —— 没有任何调用方接 `ValueError`,真实路径也走不到,但差分 fuzz 会踩到。
   点数用 `PyLong_AsLongLongAndOverflow` 转,避免 `"n"` 那种 `OverflowError`。
3. **超长 varint 对齐**(上面 `_MAX_SHIFT` 那条)。

**构建与回退。** 完整的编译/打包/排错说明单独成文:**[`ACCEL.md`](ACCEL.md)**。
要点速览:`pyproject.toml` + `setup.py`(`ext_modules` 只这一个)。
MSVC 侧必须加 `/utf-8`:C 文件里有中文注释,MSVC 默认按 936 代码页读,
GBK 下某个 0x5C 会被当成行继续符,把注释接到下一行代码上 —— 这不是美观问题,
是**编译正确性**问题(`setup.py` 里按 `compiler_type == 'msvc'` 条件加)。

⚠️ **`.pyd` 绑解释器 + 绑 ABI,这是最常踩的一条。** 文件名带 tag
(`_gdbaccel.cp313-win_amd64.pyd`),只能被对应版本载入;不同 tag 可同目录共存。
所以**哪个解释器跑代码,就用哪个编**。本机实测:用项目解释器(3.13)编完后,
拿仓库里那个 `.venv`(3.11.9)跑 `main.py` 会 `HAS_ACCEL = False` 退回纯 Python
(**65.4 s**);给 3.11 再编一份后是 **1.51 s**。仓库里两份 `.pyd` 是**故意**
都留着的。

回退是**正常状态,不是错误**:没编译(`pip install` 没带编译器)、换了解释器
(仓库里那个 3.11.9 的 `.venv` 拿不到为 3.13 编的 `.pyd`)、ABI 不匹配,都会
自动走纯 Python,**功能完全一样,只是慢**。另给一个
`PYOPENFILEGDB_NO_ACCEL=1` 强制回退(差分测试与验证回退路径用),
`pyopenfilegdb.HAS_ACCEL` 是对外可见的布尔量。

**验证(唯一判据是"两条路逐位相同",见 `tools/verify_accel.py`):**

* **真实数据对拍** —— 8 个样例库全部图层,**11,885 条 / 6,335,486 顶点**,
  `shape_type` / `has_z` / `has_m` / `coordinates` / WKT / **flat 主存储
  (`xy_parts`/`z_parts`/`m_parts`)** / **容器类型**全部逐位相同,
  而且 `decode_geometry_ex` 返回的**消费位置也相同** —— 位置一致才说明 `dr.pos`
  的回写时机没写错(`dr.pos` 只在成功返回之后赋一次,失败即不动)。
  比 flat 主存储那一份不是凑数:`coordinates` 是从它物化出来的**派生视图**,
  只比派生视图的话,"主存储错一处、物化时又恰好错回来"这种双错会被放过。
* **定向损坏用例 18/18** —— 截断 blob、点数改写成 `2**40`、超过 8 个续字节的
  varint:两条路**抛同一种异常**且都是 `GdbFormatError`。
* **随机 fuzz 4000 轮 × 4 个入口** —— 随机字节流分别喂 `decode_xy` /
  `decode_xy_flat` / `decode_scalar` / `decode_scalar_flat`,与工具内一份把 C 语义
  (接受域上限、64 位绕回)写死的**参考实现**对拍:结果逐位相同、`end_pos`
  相同、**返回的容器类型也相同**。x/y 两个 origin 取**不同**的随机值 ——
  传同一个会把"第二个 origin 有没有接对"这件事变成永远验不到。

这三层覆盖的正是上面三道闸。另有一条**反空转**的测试
(`test_dispatch_reads_module_attribute_every_call`):分派处必须是**每次调用
重读模块属性**,不能在 import 期把函数抓走存成局部 —— 否则"关掉加速"的开关
会无声失效,而所有对比都变成"C 比 C",全部通过却毫无意义。

### 2.19.5 flat 坐标容器:把"把 double 包成 Python 对象"这笔钱省掉

§2.19.4 把 varint 循环搬进 C 之后,几何解码从 59.3 s 掉到 6.3 s —— 然后**卡住
了**。用户给的参照是 GDAL(Java 绑定)读同一个图层 **0.80 s**(那段脚本还多做了
一次 `OGR_G_Clone` 深拷贝,约 719 MB memcpy,所以这个参照对 GDAL 是偏宽的)。
6.3 s 对 0.8 s,差 8 倍,而且**改算法、改 IO 都够不着**,因为量出来的账是这样的:

| `_read_xy_array` 那 2.0 s 的构成 | 耗时 | 占比 |
|---|---:|---:|
| C 里的 varint 循环本身 | 0.33 s | 16% |
| `PyFloat_FromDouble` + `PyTuple_New`(4,496 万个 float + 2,121 万个元组) | **1.7 s** | **84%** |

GDAL 的 `OGRLineString` 就是一条 `double*`(它 `ReadXYArray` 的 `XYSetter`
直接往 `padfX` / `padfY` 里写),这笔钱一分不付。所以差距的根子是**结果容器**,
不是解码算法 —— 这也解释了 §2.19.3 那张表里"物化 6.6 s 是地板"那句话:
只要解码器先出中间数组再转元组,就撞得到这个地板;**C 不撞是因为它一边解
一边 append 元组,中间没有数组这一站**。要再往下走,只能反过来:**连元组也
不建**。

**做法:几何的主存储改成 flat,`.coordinates` 变成惰性物化的派生视图。**

* `GdbGeometry` 内部不再存 `list[(float, float)]`,改存 `_FlatCoords` ——
  **每个 part 一块 `array('d')`**,XY **交错**存放(`x0, y0, x1, y1, ...`),
  Z / M 各一块、顺序存放(不交错,因为 Z 和 M 是两个独立的数组语义);
* `.coordinates` 行为**一个字都没变**:第一次访问时按 `kind` 物化成
  `list[(float, float)]` 或 `(part_starts, points)` 并**缓存**,老调用方
  (写路径 `_esri_geometry.py:1315+`、`layer.py` 的空间算子、`_gdbtable.py`
  的 `bbox` 计算)一行不用改;
* 新增三个 flat 访问器:`xy_parts` / `z_parts` / `m_parts`,返回
  `List[array('d')]`(**每环一块**,不是一个整块 —— 空间计算要的是环列表,
  拼成一个整块还得自己切);没有 Z/M 时 `z_parts` / `m_parts` 返回 `None`;
* 多部件环闭合的削点也跟着换成 flat 版
  (`_strip_part_closures_flat`):原来是"重建两个新 list、逐点搬",现在是
  每块自己 `[:-2]` / `[:-1]` 切一刀,**逐点搬运整个没了**。

**C 侧多两个入口,四个入口的分工:**

| C 入口 | 结果容器 | 谁在用 |
|---|---|---|
| `decode_xy_flat` | `array('d')` 交错 | **库的热路径**(`_read_xy_array`) |
| `decode_scalar_flat` | `array('d')` | **库的热路径**(`_read_scalar_array`) |
| `decode_xy` | `list[(float, float)]` | **库里没有调用方** —— 只给 `tools/` 做 A/B 对照 |
| `decode_scalar` | `list[float]` | 同上 |

后两个**刻意留在库里**:它们是"同一段循环,只差容器形状"的对照物,`tools/bench_cext_varint.py`
靠 `t_tuple − t_flat` 量出那笔过路费的绝对值。留一个没有调用方的入口是有代价的
(得跟着一起维护、一起过差分闸门),但换来的是"这笔钱到底是多少"永远可复核。

⚠️ **纯 Python 回退也返回 `array('d')`。** 这不是"快版返回另一种东西" ——
两条路的**容器形状完全一样**,所以差分对拍可以把"容器类型"本身也钉进断言
(`tools/verify_accel.py` 的 `geom_tuple` 就是比对 `type(p).__name__`),
不等的情况会当场现形。

**⚠️ 一个必须记住的坑:`array('d', memoryview)` 比建元组还慢。**
直觉上"直接拿缓冲区构造 array 最快",实测是 **60.2 ns/varint —— 比建元组
还差**,因为它走的是**迭代器协议**(逐元素 `PyIter_Next` + 逐个装箱)。
正确的写法是绕过元素层:`array('d').frombytes(buf)` 直接按 `n * 8` 字节
memcpy(实测 **5.0 ns/varint**)—— 本模块在 C 侧的做法更直接:先
`array('d', [0.0]) * n` 把底存开出来,`PyObject_GetBuffer(..., PyBUF_WRITABLE)`
拿可写指针,**由 C 直接往 `double*` 里写**。

**实测(C 侧笔数,72,001 点的真实面 / 144,002 个 varint):**

| 项 | 纯 Python | C 元组版 | C flat |
|---|---:|---:|---:|
| 整条路径 | 475.9 ns/varint | 41.5 ns/varint | **4.0 ns/varint** |
| 折到 `村行政区划`(89,935,418 varint) | 42.8 s | 3.7 s | **0.36 s** |
| 小数组 200 / 5,000 点 | 166 / 4,439 µs | — | **2 / 25 µs**(83× / 175×) |

"把 double 包成 Python 对象"的过路费 = `t_tuple − t_flat` = **37.4 ns/值**,
纯 Python 相对热路径 = **118×**。

**端到端(`main.py` 的循环里加一句 `g = feature.geometry`,全层 21,217 要素 /
44,967,709 顶点 / 259.3 MB 几何 blob):**

| 路径 | 耗时 | 对 GDAL 0.80 s |
|---|---:|---:|
| 只读属性(`main.py` 原样,不碰几何) | 0.412 s | — |
| 纯 Python 回退 | 65.4 s | 82× |
| **有 C 扩展(flat 容器)** | **1.50 s** | **1.9×** |

改之前是 4.80 s,所以这一步单独拿到 **3.2×**;累计(含 §2.19.4 的 C 扩展)
**65.4 s → 1.50 s = 44×**。模型预测是 1.43 s(把 `_read_xy_array` 换成 flat
桩子得到 1.366 s,再加 `array('d')` 实例化的约 0.06 s),实测 1.50 s 落在
同一格。验证热循环里**一次都没有**提前物化:21,217 条里 `_coords is not None`
的是 **0** 条。

**⚠️ 什么时候这笔收益会退回去。** 如果下游**逐点**在 Python 里遍历坐标
(`for x, y in g.coordinates:`),那 1.7 s 会原样回来 —— 换容器省掉的正是
"逐点建 Python 对象",一旦你又要逐点,就没有可省的东西了。所以这一步的收益
前提是下游按**数组**用坐标(空间计算、喂 numpy、`array` 切片、`min`/`max`/比较
这类 C 层循环)。问过用户,答案是"会做空间计算",所以按这个前提落地。

**没搬的东西**照旧:逐要素簿记、属性记录解码、`_decode_parts` 的头解析。
另外**写路径仍然走 `.coordinates`**(`_encode_parts` 等),即"读进来是 flat、
要写出去时物化一次" —— 读写往返多一次物化,但读的热路径不受影响,这是
"老代码一行不改"的代价,记在这里。

### 2.20 空值位图和 `iter_rows` 的容错口径

顺手记一条读路径的约定:`iter_rows()` 是按 **"单条记录损坏不中断整表"**
写的,只 `except GdbFormatError`。所以为了提速把 `_read_xy_array` 的边界
检查去掉之后(截断的 blob 会直接抛 `IndexError`),必须在 `_decode_at` 的
分发处统一 `except IndexError → raise GdbFormatError`,否则一条坏记录会
把整个图层遍历带崩。这类"提速引入的异常类型漂移"很容易漏,记在这里。

---

### 2.21 `Geometry` 的空间计算:没有 GEOS 时的边界与鲁棒性口径

`GdbGeometry` 改名 `Geometry` 并抽到独立模块(`geometry.py`),补上度量、描述、
构造与拓扑判定。方法名用 **snake_case**,与 GDAL 的对应关系:

| 本库 | GDAL |
|---|---|
| `envelope()` | `OGRGeometry::getEnvelope`(⚠️ 顺序不同,见下) |
| `area()` / `length()` | `OGR_G_Area` / `OGR_G_Length` |
| `centroid()` | `OGR_G_Centroid`(⚠️ 返回元组,不是点对象) |
| `distance()` | `OGR_G_Distance` |
| `convex_hull()` | `OGR_G_ConvexHull` |
| `simplify(t)` | `OGR_G_Simplify`(⚠️ 不支持拓扑保持) |
| `segmentize(l)` | `OGRGeometry::segmentize`(**可逐字照抄的纯 C++**) |
| `relate()` + 九个谓词 | `OGRGeometry::relate` / `Intersects` … |
| `equals()` / `exactly_equals()` | 前者是 OGC 拓扑等价,后者才是 `OGRGeometry::Equals` |
| `to_geojson()` / `__geo_interface__` | `OGR_G_ExportToJson` |
| `wkt()` / `from_wkt()` | `OGR_G_ExportToWkt` / `OGR_G_CreateFromWkt` |

两处口径差要记住:`envelope()` 返回 **`(xmin, ymin, xmax, ymax)`**,而
`OGRGeometry::getEnvelope` 填的是 `(MinX, MaxX, MinY, MaxY)`;`area()` 对**每个
part 单独取 `abs()`**,所以在环方向写反的数据上比 GDAL 宽容(GDAL 会加出负数)。

#### ⚠️ 大坐标抵消:鞋带公式**必须先平移**(这条踩过,代价是 1 公里)

这是本节唯一一条"不这么做就是错的"的规定。UTM 量级的面积用不平移的鞋带公式,
中间量会涨到 ~1.5e15 而净结果只有 ~854 —— **12 位有效数字直接抵消掉**。在真实
语料(`D:/work/2024年国土行政区划.gdb`,21,217 个面)上实测:

| 写法 | 面积最大相对误差 | 质心最大偏差 |
|---|---|---|
| `Σ(x₁·y₂ − x₂·y₁)` | 8.6e-05 | **1135.84 m** |
| `Σ x(i)·(y(i+1) − y(i−1))`(GDAL 文档的写法) | 5.2e-10 | 0.0 m |
| **平移到首顶点再累加**(本库) | **1.4e-15** | **0.0 m** |

21,065 个要素里 **167 个**质心偏出 100 米以上。所以 `_geometry_ops` 里所有鞋带类
累加(面积、质心、顶点平均、绕向判定)**一律先减首顶点**。

用**首顶点**而不是包围盒左下角:首顶点不用额外扫一遍求极值,而且实测更准
(1.43e-15 vs 5.94e-15)。这也是 JTS `Area.ofRingSigned` 的做法。

**附带两个好处,所以这个修复反而更快**:平移后闭合项 `cross(p_{n−1}, p₀)` 恒等于
**精确的 0**,不用算;`i % n` 也随之消失 —— 实测 2000 顶点环 **×0.726**。

顺带修好了一个**写路径的 bug**:`_esri_geometry._is_clockwise`(决定写盘时环的
绕向)对 22,044 个环里有 **1 个**判反(oid 14883,3 顶点退化片,面积 4.88e-4)。
现在它与 `area()`/`is_clockwise()` **共用同一份** `ring_signed_area2`,分歧归零。
⚠️ 全库**只有**这一份鞋带实现,不要再写第二份。

#### numpy 是**可选加速器**,不是依赖

`_geometry_ops` 顶部的 `try: import numpy / except ImportError` 就是全部 ——
`pyproject.toml` 把它列在 `[project.optional-dependencies] speed`,**运行时的
`dependencies` 仍然是零**。`PYOPENFILEGDB_NO_NUMPY=1` 可在进程内强制关掉,
`use_numpy()` 上下文管理器给差分测试用(与 `_accel.use` 同一个套路)。

阈值是实测的交叉点,别凭感觉改小:

| 算子 | N=2000 加速比 | 阈值 | 交叉点实测 |
|---|---|---|---|
| `length` | ×25 | `_NP_MIN_LENGTH = 48` | 40~48 |
| `area` | ×21 | `_NP_MIN_AREA2 = 128` | 96~128 |
| `centroid` | ×9.4 | `_NP_MIN_CENTROID = 256` | ~176 |
| `envelope` | ×1.0 | **不加速** | — |

⚠️ **质心的阈值必须比面积高一个档,两者不是同一个数。** `_np_centroid_ring`
要靠两次 `np.roll` 拿 `(x[i+1], y[i+1])`,临时数组总量是面积路径的 3 倍,交叉点
因此高 ~1.4 倍。共用一个阈值时,**质心在 128 顶点这个最常见的尺寸上慢 40%** ——
这是 `tools/verify_numpy.py --bench` 抓出来的,它现在会打出"这一行 numpy 到底有没有
被分派",不输出这个列的话那张表会骗人。

**包围盒是刻意的反例**:纯 Python 的 `min(a[0::2])` 已经把活干在 C 层(切片和内建
`min` 都不建 Python 对象),numpy 那点向量优势抵不过 5~20 µs 的固定开销。所以
`envelope_of` 永远走纯 Python。

⚠️ **依赖符号或低位比特的路径永远不许走 numpy。** numpy 的 `sum` 用成对求和,舍入
顺序与顺序累加不同(实测相对误差:长度 ~3e-14、面积 ~1e-15)。所以 `_signed_area2`
是"给面积/质心用的分派版",而**绕向判定一律走 `ring_signed_area2` 的纯 Python 顺序
累加版** —— 近零面积的环上成对求和可能把符号翻过来,那是**写盘数据的正确性**问题。

#### 谓词没有 C++ 可抄,所以口径要说实话

`OGRGeometry::Intersects` / `Contains` / `Touches` … 在 `ogrgeometry.cpp` 里
**全部转手给 GEOS**,GDAL 自己只加了一句"包围盒先快速排除"。也就是说拓扑这一档
**没有纯 C++ 实现可以照抄**。本库按 OGC DE-9IM 规范自己实现,目标是与 GEOS 一致,
并明说边界:

* 用浮点方向判定,**没有 snap-rounding、没有精确算术**。次 ULP 量级的退化构型
  (量化网格上重合的两点、几乎共线的环边)可能判错。GEOS 引入 precision model
  之前有同样的毛病。
* `buffer` / `union` / `intersection` / `difference` / `sym_difference` **不做** ——
  那些需要 GEOS 级的鲁棒性,不做比做错好。
* `is_valid()` 是**部分实现**,只查四条:环是否闭合、顶点数是否满足类型下限、
  单个环是否自交、洞是否落在壳内。OGC 完整有效性还要洞与壳的包含关系、洞之间
  互不包含、multipoint 互不重合 —— **没做**。docstring 里逐条列了,不要理解成
  "`is_valid()` 为真就是 OGC 有效"。
* 只有 2D 参与计算;`has_z` / `has_m` 不参与任何度量与判定(OGR 亦然)。

**`equals()` 与 `exactly_equals()` 分工的判别式**(`test_geometry.py` 里有这条):

```python
A = 'POLYGON((0 0,10 0,10 10,0 10,0 0))'
B = 'POLYGON((0 0,5 0,10 0,10 10,0 10,0 0))'   # 顶点 (5,0) 落在边上,面积仍是 100
A.equals(B)          # True   —— OGC T*F**FFF*,拓扑等价
A.exactly_equals(B)  # False  —— 结构比较,顶点数不同
```
参照实现同口径:GEOS `equals` = True,GDAL `Equals` = False(后者是结构比较)。
⚠️ 写这个例子时踩过一次:**只有"新顶点落在原边上"才等价**。一个曾经被写进
草稿的 `POLYGON((0 0,10 0,5 10,0 10,0 0))` 面积是 **75** 不是 100,拿它当
"同一块地"是错的,`equals` 本来就该判 False。

#### ⚠️ **重算出来的点不能拿去问浮点** —— 同一根因的两个真 bug

对拍跑起来之后抓到的问题里,**最有价值的两个共享同一个根因**,所以合并成一条讲。
根因一句话:

> `point_location` 判"点在不在线上"用的是 `orient(...) == 0` 的**精确**比较。
> 而 `relate_of` 里有一半的探针点是**算出来的**(交点、中点),它们一般**不精确
> 落在**那条线上。于是"在线上"被判成"在外部 / 在内部",整块矩阵跟着塌。

两个 bug 是这句话的两个实例,分别由 `_split_parameters` 的两个返回值治住。

**bug 1 —— 交点(由 `hit` 治)。** 最小复现:

```python
A = 'LINESTRING(-0.0000003 0.0000004, 10.0000002 9.9999998)'
B = 'LINESTRING(0 10, 10 0)'
A.relate(B)   # 老实现 FF1FF0102(声称 II/IB/BI 全空);GEOS 0F1FF0102
```

老代码求出交点后**只留了参数 `t`**,把点重算成 `p1 + t·(p2−p1)` 再交给
`point_location`。实测那个重算点相对 B 的线段的 `orient` 是 **2.84e-14** 而不是 0
→ 判成外部 → `II` / `IB` / `BI` 三格一起塌。
**这不是次 ULP 的退化构型,而是"交点坐标除不尽"这个极常见的情形。**

**bug 2 —— 中点(由 `on_obstacle` 治)。** 第 2 块判"子段落在对方哪儿"取的是
子段中点,而中点同样是重算的。最小复现是**一个几何与它自己**做 `relate`:

```python
POLYGON((0 0, 0.1 0.3, 0.4 0.1, 0 0))   老实现 2F2F11212   正确 2FFF1FFF2
LINESTRING(0 0, 0.1 0.3, 0.4 0.1)       老实现 1F1F0F1F2   正确 1FFF0FFF2
```

实测该三角形第 1 条边相对**它自己**的中点,`orient` 是 **-3.5e-18** 而不是 0 ——
"与对方边界重合"被判成"在对方内部 / 外部",`IB` / `IE` / `BE` 全错。

⚠️ **bug 2 比 bug 1 值钱得多,因为共线重叠在真实数据里到处都是**(相邻行政区共享
界线、同一块地被登记两次)。实测它在 `D:/work/2024年国土行政区划.gdb` 上把
`乡行政区划#75` 与 `村行政区划#92`(两条**完全相同**的面)判成"部分重叠"而不是
"完全相等"。**而且它是修完 bug 1 之后才露出来的** —— 修 bug 1 时那 147 个用例、
以及"8 条要素 / 400 对"的语料抽样,都没碰到它;是把语料抽样放大到
"150 条要素 / 900 对"、并把 `--max-verts` 收到 60 之后才撞出来的。
**这是"抽样强度不够会放过 bug"的直接证据。**

**为什么两个都躲过了当时那批用例(147 → 151 个)。** 手推真值表用的全是**整数
坐标**(两条线正好交在 (5, 5)、正方形顶点都是整数),重算出来的点**恰好**逐位精确,
所以一次都没碰到。
`tools/fake_ogr.py` 也测不出 —— 见下,它当时根本没在跑它自称跑的那条路。

**修法**(都在 `_split_parameters`,两个返回值各治一个):

| 返回 | 治什么 | 怎么用 |
|---|---|---|
| `hit`(`[(t, hit), ...]`) | 交点 | 带 `hit` 的切点不再问浮点,用 `on_geometry_role()` 按类型直接数角色(面 → 边界;折线 → 看是否奇数次端点;点/多点 → 内部) |
| `on_obstacle`(`bool` / 子段) | 中点 | 与对方线段**共线重叠**的子段,`seg_seg_classify` 本来就报了 `SEG_OVERLAP` —— 把那段 t 区间记下来,落在里面的子段按构造算作与对方重合(`shared_segment_role()`:面 → 边界,折线 → 内部) |

顺带把第 3 块(2 维内部推断)改成**复用**第 2 块算好的子段分类,而不是把中点再算
一遍 —— 既消掉了一处重复,也保证两块的判据**永远一致**(以前它们各算各的,同一段
子线段在两块里得出不同结论是可能的)。

#### ⚠️ 第三个发现:`fake_ogr.py` 曾经**被绕过**(守卫悄悄失效)

`verify_topology.pick_oracle()` **优先挑 shapely**,而本机装了 shapely —— 于是
`tools/fake_ogr.py` 注入的假 `osgeo` **压根没被用到**,它一直在跟**真的 GEOS**
对比,却自称在跑"假的 osgeo 管道"。它当时报出的那 2 处不符其实是真 GEOS 的结果,
不是它自己那套"本库自比"的结果。

这与本仓库记过的那次**假绿**是同一个形状:**守卫悄悄失效,而它看上去还在守。**
当年是"比了 0 对却报一致",这次是"说的是一套,跑的是另一套"。
现在 `install()` 同时注入假 `shapely`,并在结尾加一条**硬断言**(装完之后
`import shapely` / `import osgeo` 必须都是假的,否则直接抛异常)——
**宁可它响,也不要它再默默换成别的 oracle。**

#### 残留一处:切点子段在**网格之下**时无解(精度地板,不是 bug)

四层里还剩 **2 处**,同一个构型:`pg_degenerate_sliver` 平移到 UTM 量级。
数值摊开看就清楚了:

| 量 | 值 |
|---|---|
| 直角片尺寸(平移后) | 10.0 宽 × **1e-9** 高 |
| `ULP(x)` @ 3.9e7 | **7.45e-9** |
| `ULP(y)` @ 3.18e6 | 4.66e-10 |
| 直线穿过它那一段的长度 | ~**4.7e-10**(< 1 ULP of x) |

也就是说,那一段子线段**在双精度网格上没有内部点可采样** —— 它的中点在坐标里
根本不存在,`p1 + t·(p2−p1)` 会原样四舍五入回切点本身。任何"取中点判内外"的方法
在这里都必然失败,这不是实现的缺陷。GEOS 能答对是因为它走的是**组合式**的拓扑图
(edge-end 分类),不是逐点采样。

这块薄片**本身就是照着网格设计的**:1e-9 高,搬到 3.9e7 量级后只有 2.1 ULP 高。
保留它、并把分歧记在这里,比删掉它诚实 —— 它就是"无 snap-rounding、无精确算术"
这条边界的具体位置。用 GDAL 那个弱一档的 oracle 时,同两个构型会**连带**在谓词上
露出来(`touches` / `crosses`),正好印证了分歧的传播路径:`II=F` vs `II=1`。

**最终总账**(真实 shapely 2.1.2 / GEOS 3.13.1):

| 配置 | 对数 / 判定数 | 不符 |
|---|---|---|
| `--features 8 --pairs 400`(默认口径) | 1,290 / 14,190 | 2(全是那两处薄片) |
| `--features 150 --max-verts 60 --pairs 900` | 2,438 / **26,818** | 2(同上) |
| 同上,`PYOPENFILEGDB_NO_NUMPY=1` | 2,438 / 26,818 | 2(同上) |
| `--oracle gdal --synthetic-only` | 1,540 / 12,320 | 4(同上两处传播到 `touches`/`crosses`) |

真实语料那一层**全部为 0**。

验证靠三层叠起来:**手推真值表**(`tests/test_geometry.py`,对着 DE-9IM 定义独立
推的矩阵;外加 `TestNonExactIntersectionPoints` 与 `TestRecomputedMidpoints` 两组
回归,它们各自带一条"把修复还原回去"的路径来证明**自己有牙**)、**不变量测试**
(转置对称、蕴含关系、自比必为单位矩阵、每个谓词必须与 `relate()` 的矩阵一致)、
以及**GEOS 对拍**(`tools/verify_topology.py`)。第三层是本仓库**唯一**允许
`import shapely` / `import osgeo` 的地方,两个都装不上就 skip。

#### 顺带修好的 WKT 出口

`to_wkt` 原先给**线和面的每个顶点**都套了括号,产出
`LINESTRING ((0 0), (1 1))` / `POLYGON (((0 0), ...))` —— 两者都是**非法 WKT**,
本库自己的 `from_wkt` 都读不回来,任何别的 GIS 工具也读不了。而且它**从不写
`Z`/`M` 维度后缀**,于是 `LINESTRING M (0 0 7, 1 1 8)` 导出成
`LINESTRING (0 0 7, 1 1 8)` —— 在 WKT1 里这被理解为 **Z**,带 M 的几何往返一次
维度就静默地变了。

这两个 bug 长期没被发现,原因是测试的**形状**不对:唯一的 WKT 测试是"读出来 →
`wkt()` → `from_wkt` → 比坐标"的**往返**,而本库的 `from_wkt` 是个宽容解析器,
非法文本它也照收;并且那个用例每个图层只跑第一条要素(`tests/test_read.py` 里
一句多余的 `break`),恰好都是面。现在 `TestWkt` **不往返,直接断言字符串** ——
合法与否是给别人看的,不是给自己解析的。

---

## 3. 模块地图

仓库整体布局:

```
pyopenfilegdb/     库本体(下表)
tests/             测试(test_read.py 需样例库;test_create_write.py 自给自足)
examples/          读写示例
tools/             基准/验证/生成脚本,**都不是库的一部分**(见 §5 第 5 条)
main.py            最小读示例(性能对照口径的来源)
DESIGN.md          本文
STATUS.md          进度与已知限制
ACCEL.md           可选 C 扩展的编译 / 打包 / 排错
pyproject.toml     打包元数据     setup.py  只有 ext_modules
```

库本体:

```
pyopenfilegdb/
├── __init__.py          对外 API 转出 + 版本号(140 行)
├── _constants.py        FGFT_* / ShapeType / 魔数 / 各种尺寸常量(437)
├── _datatypes.py        GdbField / GdbGeomField / GdbFeature
│                        / GdbSpatialRef / GdbItem / 异常体系
├── geometry.py          Geometry(由 GdbGeometry 改名,见 §2.21)
│                        / 度量·描述·构造·DE-9IM 谓词 / WKT·GeoJSON 出口
│                        / _FlatCoords(几何的 flat 主存储,见 §2.19.5)
│                        / _LazyGeometry(未解码几何占位)
├── _geometry_ops.py     纯算法叶子模块:只认 array('d') / 坐标对,
│                        不 import 任何内部模块(没有环)
│                        / 鞋带·绕向(唯一一份,§2.21)· 线段相交 · 点与环关系
│                        / DE-9IM 装配 · 谓词模式 · 面积/周长/质心/距离
│                        / 凸包 / DP 简化 / segmentize
│                        / numpy 可选快路径 + use_numpy() 开关(§2.21)
├── _util.py             字节读写帮手(get_uint32 / varuint / 字符串 ...)(352)
├── _gdbtable.py         .gdbtable 读 + 写:表头、字段描述区、记录编码解码
│                        / _build_decode_plan 预编译字段解码计划
│                        / _read_record_lazy 跳读几何字节(1756)
├── _gdbtablx.py         .gdbtablx 读 + 写:偏移表、位图换算(331)
├── _esri_geometry.py    Esri 几何 blob <-> Geometry,WKT 解析输出
│                        / peek_envelope 存储包围盒速查(1319)
├── _system_catalog.py   GDB_Items / GDB_SystemCatalog / GDB_ItemRelationships
│                        的解析与生成,XML 读写(1718)
├── _gdbindex.py         .gdbindexes / .freelist 描述符解析(450)
├── _gdb_template.py     空白库的固化字节(由 tools/gen_template.py 生成)(319)
├── _accel.py            可选 C 加速模块的探测 + 开关(没编出来就回退)
├── _gdbaccel.c          ⚠️ 唯一的 C 扩展:几何 varint 数组解码
│                        decode_xy_flat / decode_scalar_flat(热路径)
│                        + decode_xy / decode_scalar(只给基准做对照),见 §2.19.4
├── core.py              OpenFileGDB:open / create / 图层增删改查 / WHERE(1279)
└── layer.py             GdbLayer:read_features / write_feature / update
                         / delete / spatial_ref / extent(848)
```

⚠️ `build/lib.win-amd64-cpython-311/` 是 setuptools 留下的**过期副本**,里面的
`GdbGeometry` 等旧名字是正常的,**不要**去改它,也不要把它当成当前代码。

关键对照表(本库 ↔ GDAL):

| 本库 | GDAL `openfilegdb/` |
|---|---|
| `_gdbtable.GdbTable` | `filegdbtable.cpp` / `.h` |
| `_gdbtable.GdbTable.encode_feature` | `filegdbtable_write.cpp` |
| `_gdbtablx.GdbTablx` | `filegdbtable.cpp`(X 部分) |
| `_esri_geometry.decode_geometry` | `filegdbtable.cpp::ReadGeometry` + `ogrpgeogeometry.cpp` |
| `_esri_geometry.encode_geometry` | `filegdbtable_write.cpp::WriteGeometry` |
| `_gdbaccel.decode_xy` / `decode_scalar` | `filegdbtable.cpp::ReadVarIntAndAddNoCheck` + `ReadXYArray`/`ReadZArray`/`ReadMArray` 的 C 移植(算法逐位相同,只是换执行器,见 §2.19.4) |
| `_esri_geometry.peek_envelope` | `filegdbtable.cpp` 里的几何 blob 包围盒 |
| `_datatypes._LazyGeometry` | `OGROpenFileGDBLayer::SetIgnoredFields` 的省几何口径 |
| `_gdbtable.GdbTable._build_decode_plan` | `FileGDBField` 的 `m_eType`/`m_bNullable` 判据预编译(无直接对应,纯提速) |
| `_gdbtable.GdbTable._read_record_lazy` | ⚠️ **GDAL 没有对应物** —— `SelectRow` 一次读整条记录(见 §2.19 第四步) |
| `_system_catalog.GdbItems` | `ogr_openfilegdb.h` + `ogropenfilegdbdatasource.cpp` |
| `_gdbindex.GdbIndex` | `filegdbtable.cpp::ReadIndexes` |
| `geometry.Geometry` | `OGRGeometry` 及其子类(本库不建子类树,见 §2.21) |
| `_geometry_ops.organize_polygons` | `ogrgeometryfactory.cpp::OGRGeometryFactory::organizePolygons` |
| `_geometry_ops.ring_signed_area2` | `OGRLinearRing::get_Area` / `isClockwise`(⚠️ 必须先平移,§2.21) |
| `_geometry_ops.segmentize_part` | `OGRGeometry::segmentize` / `OGRLineString::segmentize` |
| `_geometry_ops.relate` 及九个谓词 | ⚠️ **GDAL 此处委托 GEOS,没有 C++ 可抄**;按 OGC DE-9IM 自实现(§2.21) |
| `_geometry_ops` 的 numpy 快路径 | ⚠️ **GDAL 没有对应物**(GDAL 是 C++,不需要);纯本库优化,§2.21 |
| `core.OpenFileGDB.create` | `ogropenfilegdbdatasource_write.cpp` |
| `layer.GdbLayer` | `ogropenfilegdblayer.cpp` |

---

## 4. Tier 标注

### Tier 1 —— 读路径:**完成**

| 能力 | 状态 | 说明 |
|---|---|---|
| `.gdbtable` 表头 / 字段描述区解析 | ✅ 完成 | 含 `offset_field_desc` 任意位置 |
| 字段类型全覆盖(FGFT_*) | ✅ 完成 | 含 INT64 / DATE / TIME / DATETIME / BINARY / RASTER |
| `.gdbtablx` 读(含位图压实) | ✅ 完成 | offset_size 4/5/6 都支持 |
| Esri 几何解码全类型 | ✅ 完成 | POINT/ARC/POLYGON/MULTIPOINT 的 XY/Z/M 各档 |
| 几何 varint 数组的 C 加速(**可选**) | ✅ 完成 | 编译了就用(59.3 s → 6.3 s),没编译/换解释器自动回退纯 Python,结果逐位相同。见 §2.19.4 |
| 环闭合 / 方向归一化 | ✅ 完成 | 1746/1746 实测支撑 |
| GDB_SystemCatalog / GDB_Items / ItemTypes / Relationships 解析 | ✅ 完成 | |
| 空间参考(WKT + WKID + LatestWKID) | ✅ 完成 | |
| WHERE 子句(**SQL 三值逻辑**) | ✅ 完成 | `= <> < <= > >= AND OR NOT IS [NOT] NULL IN LIKE`,返回 `True`/`False`/`None`,只有 `True` 命中 |
| 完整读链路 `open → list → get_layer → read_features` | ✅ 完成 | |
| `GdbLayer.extent` | ⚠️ 注意 | 是**上界**,不是真实范围(§2.16) |
| Z 缺失值 / M 缺失值 | ✅ 完成 | 按 Esri NaN 位型 |

### Tier 2 —— 写路径:**完成**

| 能力 | 状态 | 说明 |
|---|---|---|
| 记录序列化(位图 + 定长/变长 + 几何) | ✅ 完成 | |
| `create()` 建空库(7 张系统表) | ✅ 完成 | 与真实空白库**逐段字节一致**(除 `+4/+8/+24` 三个计数/大小字段) |
| `create_layer()` + 四重登记 | ✅ 完成 | SystemCatalog / Items / Relationships / XML |
| `write_feature()` 追加 | ✅ 完成 | 同步头部计数、文件大小、tablx |
| `update_feature()` | ✅ 完成 | 等长原地覆写;变长走"删旧 + 末尾追加" |
| `delete_feature()` | ✅ 完成 | 负长度 + 槽位置 0 |
| 表级包围盒维护 | ✅ 完成 | 只放大不缩小(与 ArcGIS 一致) |
| 几何编码 / 环方向 | ✅ 完成 | |
| 默认量化参数(WGS84 `1e6` / ZM `1e4`) | ✅ 完成 | 与 GDAL `CreateGDBItems` 完全一致 |
| 显式声明 Z/M 的几何 | ⚠️ 有风险 | **不静默降级** —— 声明了 `has_m` 就按 M 编,缺的分量填 Esri NaN。宁可写"空的 M"也不写"没有 M" |
| POINT → MULTIPOINT 提升 | ✅ 完成 | **单向放宽**:点可写进多点图层,多点写进点图层报错 |
| `Shape_Area` / `Shape_Length` 自动字段 | ❌ **未实现** | 需要字段默认值的 `FILEGEODATABASE_SHAPE_AREA` magic marker,`_write_default()` 目前只支持内联值 |
| `.gdbindexes` / `.atx` / `.spx` 生成 | ❌ **不写** | 刻意的取舍,见 §2.15 |
| `.freelist` 写入 | ❌ 不写 | 不回收文件空洞,只会单调增长 |

### Tier 3 —— 几何空间计算:**完成**(口径与边界见 §2.21)

| 能力 | 状态 | 说明 |
|---|---|---|
| 描述(`dimension` / `point_count` / `part_count` / `is_ring` / `is_clockwise`) | ✅ 完成 | ⚠️ `point_count` 是**所有** part 之和,与 `OGRPolygon::getNumPoints`(只数外环)不同 |
| 度量(`envelope` / `area` / `length` / `centroid` / `distance`) | ✅ 完成 | `distance` 是 `O(n·m)` 线段对,**没有包围盒预筛** |
| 构造(`convex_hull` / `simplify` / `segmentize`) | ✅ 完成 | `simplify` **不支持拓扑保持**,传 `preserve_topology=True` 抛 `NotImplementedError` |
| `relate()` + 九个 DE-9IM 谓词 | ✅ 完成 | 自实现 OGC DE-9IM;⚠️ 无 snap-rounding,退化构型可能判错 |
| `equals()` 拓扑等价 / `exactly_equals()` 结构比较 | ✅ 完成 | 前者是 OGC `T*F**FFF*`,后者才是 GDAL 的 `OGRGeometry::Equals` |
| WKT 出口(`wkt()` / `from_wkt`) | ✅ 完成 | 多壳面输出 `MULTIPOLYGON`;Z/M 带维度后缀;非空 multipatch 抛错 |
| GeoJSON(`to_geojson` / `from_geojson` / `__geo_interface__`) | ✅ 完成 | **不按 RFC 7946 重绕环**(与 GDAL 一致);**M 在 JSON 里没有位置,导出即丢** |
| `is_valid()` / `is_simple()` | ⚠️ **部分实现** | 只查环闭合 / 顶点数下限 / 环自交 / 洞在壳内;其余没做,docstring 逐条列了 |
| `buffer` / `union` / `intersection` / `difference` / `sym_difference` | ❌ **不做** | 需要 GEOS 级鲁棒性,不做比做错好 |
| multipatch 的谓词与 `relate` | ❌ 抛 `NotImplementedError` | 格式里是 triangle strip/fan,与 OGR 几何模型对不上;描述与度量仍可用 |
| numpy 快路径 | ✅ 可选 | 不装 numpy 功能不变,只是慢;见 §2.21 |

### Tier 4 —— **桩 / 未实现**

| 能力 | 状态 |
|---|---|
| `.atx` 属性索引 B 树查询(`GdbBTreeIndex.lookup`) | 🔶 桩,抛 `GdbWriteError` 并说明原因 |
| `.spx` 空间索引包围盒查询(`GdbSpatialIndex.query_bbox`) | 🔶 桩,同上 |
| `.gdbindexes` 描述符解析 / `.atx`/`.spx` 文件名推导 | ✅ 完成(**只解析,不查询**) |
| `.freelist` 读取 | ✅ 完成(读得出空洞列表,不参与写路径) |
| 注释(Annotation)要素类 | ❌ 未实现 |
| 附件(Attachments) | ❌ 未实现 |
| 域(Domains) | 🔶 **只读不写** —— 字段级 `DomainName` 能解析出来,建库时不生成 |
| 版本 4(ArcGIS Pro)写入 | ❌ 主动拒绝,抛 `GdbVersionError`(GDAL 本身也不支持对 v4 做 update) |
| 版本 9.x | ❌ 主动拒绝 |

---

## 5. 验证方法

没有 GDAL 对拍,怎么保证写对了?

1. **与真实 ArcGIS 空白库逐段比对。** `_gdb_template.py` 是从一张真实
   空白 `.gdb` 里固化出来的字节。`test_system_table_contents_match_arcgis`
   逐张系统表比较:版本、三个 magic、`offset_field_desc`、字段描述区、
   记录体,并额外断言两边的 `offset_field_desc` 都是 40。
2. **写出来的库能读回来。** 全部写测试都走"写 → 关 → 重开 → 读"的闭环,
   而不是只看内存对象。
3. **不变量断言。** 读测试不假设记录区布局,而是断言 tablx 驱动下必然成立的
   性质(偏移非 0、长度非负、不越界、文件大小自洽、字段描述区不空)。
4. **往返测试。** `Geometry` → WKT → `Geometry`、环闭合往返、
   Z/M 声明往返,都断言"值不变"。⚠️ **往返不是出口的证明**:宽容的 `from_wkt`
   会把非法文本照收,所以 `TestWkt` 改成**直接断言字符串**;全语料的
   "往返 + 幂等 + 维度不变"由 `tools/verify_wkt_roundtrip.py` 兜。
5. **性能基线可复现。** `tools/bench_read.py` 分段计时(A 端到端 / B 投影 /
   C 整条解码 / D 纯 IO / E 强制解几何 / F `bbox=` 粗筛),
   `tools/profile_read.py` 出 cProfile,`tools/bench_geom_skip.py` 数跳读
   几何省下的字节,`tools/bench_io_buffer.py` 挑读缓冲区,
   `tools/bench_trace.py` 量调试器税率(见 §2.19.1),
   `tools/bench_geom_split.py` 把几何解码拆成 varint / 浮点尾 / 其余三档
   (见 §2.19.2),`tools/bench_varint.py` 量增量 varint 的长度分布,
   `tools/bench_varint_decoders.py` 比几个 varint 解码器写法,
   `tools/bench_cext_varint.py` 量 C 扩展相对纯 Python 的倍数,并把
   "建元组 vs 填 array"两条路的差值(= 对象过路费)单独列出来(见 §2.19.4 /
   §2.19.5),
   `tools/verify_split_read.py` 逐条对照跳读路径与整读路径,
   `tools/mem_read.py` 量遍历循环的内存曲线(**看的是"平不平"**,不是绝对值
   —— 流式 + 惰性几何的口径下,当前占用必须不随要素条数涨,见 §2.19),
   `tools/verify_accel.py` **差分测试 C 路径与纯 Python 路径逐位相同**
   (真实数据 + 定向损坏用例 + 随机 fuzz 4 个入口三层,见 §2.19.4),
   `tools/verify_numpy.py` **差分测试 numpy 快路径与纯 Python 顺序路径**
   (真实语料对拍 + 拿 `Fraction` 当精确解校准精度 + 基准打"这一行有没有被
   分派",见 §2.21),
   `tools/verify_wkt_roundtrip.py` **全语料 WKT 出口体检**(每条几何
   `wkt()` → `from_wkt` 往返,并断言 `kind` / `has_z` / `has_m` / `point_count`
   一项不变、再导一次文本**逐位相同**;合成用例覆盖不到的"真实语料里到底有什么
   构型"由它来兜。~700 s,所以**不进 `tests/`**)。见 §2.19。
6. **回退路径也要过。** C 扩展和 numpy 都是**可选**的(§2.19.4 / §2.21),
   所以测试要跑四遍:`PYOPENFILEGDB_NO_ACCEL=1` 强制走纯 Python 解码、
   `PYOPENFILEGDB_NO_NUMPY=1` 强制走纯 Python 数组运算、3.11 的 `.venv`、
   以及默认配置。证明回退路径没有腐坏 —— 这既是验收条件,也是"两条实现
   并存"这个设计能成立的前提。
7. **对拍(可选,但本机装得上)。** `tools/verify_topology.py` 是本仓库**唯一**
   允许 `import osgeo` / `import shapely` 的地方:拿 GEOS 逐对比 `relate()` 与
   十个谓词(合成构型 / UTM 量级平移 / 抖动 / 真实语料四层)。
   GDAL **只在这一个文件里**,不进包、不进 `tests/`、不进 `pyproject.toml`。
   ⚠️ **更正一条旧记录。** 这里此前写的是"本机没有 GDAL,这个对拍**至今没有真正
   跑过**"。**那是错的,而且错得很贵。** 本机既有 QGIS 4.2 自带的 GDAL 3.13.3,
   也有 shapely 2.1.2 / GEOS 3.13.1(见下表)。而它一跑起来就抓出了一个真 bug:
   `relate()` 在交点坐标不可精确表示时整块矩阵塌掉(见 §2.21「交点不可精确表示」)。
   那个 bug 躲过了当时全部的 147 个用例,也躲过了 `fake_ogr.py` —— 假 oracle 的
   `relate` 恰好存在,那条路永远是绿的。

   参照实现按可得性自动挑:**优先 shapely**(它就是 GEOS 本体,`relate` 直接给
   DE-9IM 矩阵 —— 那是十个谓词的地基,比到矩阵就等于比到了根);**其次 GDAL**
   (它的 Python 绑定**不暴露** `relate` / `Covers` / `CoveredBy`,而且它的
   `Equals` 是**结构比较**,只能对着本库的 `exactly_equals()` 比)。两个都装不上
   就 skip 并退出 0 —— 那是**正常状态**,不是失败。

   | oracle | 提供什么 | 本机 |
   |---|---|---|
   | **shapely(GEOS)** | `relate` 矩阵 + 全部 10 个谓词 | ✅ 2.1.2 / GEOS 3.13.1 |
   | **GDAL / `osgeo`** | 7 个谓词 + `Equals`(结构) | ✅ 3.13.3(QGIS 4.2 自带 py3.12) |
   | 都没有 | — | skip,`exit 0` |

   ⚠️ **假绿是这个工具最大的历史教训。** 原版 `compare()` 调 `a.relate(b)`,而
   GDAL 的 Python 绑定**没有** `relate`,抛出的 `AttributeError` 被宽泛的
   `except Exception` 吞成"跳过",于是它打印"与 GDAL 逐对一致 ✓"、`exit 0` ——
   **而实际比较的对数是 0**。现在 `main()` 结尾有一条硬闸门:`pairs == 0` 直接
   `exit 1`。跑管道靠 `tools/fake_ogr.py`(注入转发给本库自己的假 `osgeo.ogr`);
   它**不产生真值**,只证明"这段验证代码不是废的" —— `FLIP="contains touches"`
   故意答反若干谓词,输出必须出现对应条数的不符,**一条都没有就说明它是个永远绿的
   摆设**。

   ⚠️ **同一个失败模式还犯过一次,更隐蔽:守卫悄悄失效,而它看上去还在守。**
   `pick_oracle()` **优先挑 shapely**,而本机装了 shapely —— 于是
   `fake_ogr.py` 注入的假 `osgeo` **压根没被用到**,它一直在跟**真的 GEOS** 对比,
   却自称在跑"假的 osgeo 管道"。症状是它开始报不符(自比本该逐对相同)——
   **一个永远绿的摆设有迹可循,一个换了 oracle 却仍报数的守卫没有。**
   现在 `install()` 同时注入假 `shapely`,并加一条**硬断言**:
   `import shapely` / `import osgeo` 拿到的必须都是假的,否则 `RuntimeError`。
   教训与上一条合并成一句:**不变量要么被断言,要么不存在;注释不算。**

当前:**155 个用例全部通过**(读 + 写 + 几何),四配置:py3.13 + numpy 33.9 s、
`PYOPENFILEGDB_NO_NUMPY=1`(34.0 s,skipped=5)、3.11.9 的 `.venv`(33.7 s)、
`PYOPENFILEGDB_NO_ACCEL=1`(80.9 s,skipped=6)。
惰性几何之前是 315.7 s。

⚠️ **测试的数量不是覆盖的证明。** §2.21 里那两个 WKT bug 就是在 134 个用例
全绿的情况下活着的:唯一的 WKT 用例是"往返",而宽容的 `from_wkt` 会把非法
文本照收,加上一句多余的 `break` 让它每个图层只跑第一条要素。**加用例之前先
看一眼"它能不能失败"** —— 拿一个已知错的实现塞回去,确认用例真的红了。
`tests/test_geometry.py::TestNonExactIntersectionPoints` 就是按这条写的:
它自带一个"把交点丢掉"的还原路径,能把老 bug 一字不差地复现出来(`FF1FF0102`);
`TestRecomputedMidpoints` 同样自带"把 `on_obstacle` 全置假"的还原路径,复现
`2F2F11212`。**这两条回归就是"用例有牙"的具体形状。**

⚠️ **更贵的一课在 `--pairs` 上。** §2.21 的 bug 2 是**修完 bug 1 之后**才出现的,
当时 147 个用例全绿、`--features 8 --pairs 400` 也全绿 —— 语料抽样**强度不够**
就碰不到共线重叠。放大到 `--features 150 --max-verts 60 --pairs 900` 才撞出来。
**所以"对拍跑过且绿"这句话必须带上参数,否则它没有意义。**

---

## 6. 一句话总结每个坑

| # | 坑 | 一句话 |
|---|---|---|
| 1 | 字段描述区位置 | 读 `+32`,别信 `40` |
| 2 | 记录区 | 不连续、无序、可在描述区之前;**只能走 tablx** |
| 3 | 删除标记 | 负长度(补码),不是标志位 |
| 4 | OID | 不占字节,值 = 行号 + 1 |
| 5 | 空值位图 | 只数 nullable 字段,1 = NULL,从 0xFF 往 0 清 |
| 6 | tablx | 1024 槽一页,偏移 5 字节,偏移 0 = 空槽 |
| 7 | 几何长度前缀 | `varuint 长度 + blob` |
| 8 | shape type | GDAL 编号 ≠ shapefile 公开编号,ZM 占了 Z 的号 |
| 9 | 量化 | POINT `+1`,数组不 `+1`;量化 0 = NaN |
| 10 | NaN | Esri 用 `0x7FF8000000000001` |
| 11 | 环闭合 | 盘上闭合,内存里不闭合 |
| 12 | 环方向 | 首环 CW,其余 CCW |
| 13 | 段结束 | `DE AD BE EF` |
| 14 | 登记 | 四张系统表都要写,编号 `1 + catalog 记录数` |
| 15 | 索引 | 可选,不写也能读 —— 本库选择不写 |
| 16 | 包围盒 | 单调上界,不是真实范围 |
| 17 | XML 命名空间 | 10.3,有 INT64/DATE 才升 10.8 |
| 18 | PhysicalName | ArcGIS 与 GDAL 有分歧,本库跟 ArcGIS |
| 19 | 性能 | 几何必须惰性;不碰 `feat.geometry` 就一行不付 |
| 20 | 坏记录 | 提速去掉边界检查后,`IndexError` 要转 `GdbFormatError` |
| 21 | 几何字节 | 占记录体 99.1%,只读属性时**连读都不该读**(GDAL 会读) |
| 22 | 读缓冲 | 记录不连续 + 每条 `seek` ⇒ 8 KB 预读全白读,降到 512 |
| 23 | 字段解码 | 编成计划,269 万次函数调用砍到 104 万 |
| 24 | 调试器 | PyCharm/pydevd 的 line tracer 只收纯 Python 的税,C 免检 |
| 25 | 几何惰性 | 0.42 s 那 4,494 万顶点一个没解;碰 `.geometry` 就得付 59 s |
| 26 | 几何上限 | GDAL 的 varint 也无快路径,60 s 是解释器成本不是算法差 |
| 27 | C 扩展的边界 | 只包 varint 数组两处,`dr.pos` **成功后才写一次**,失败即不动 |
| 28 | C 抛什么异常 | 一律 `IndexError` —— `iter_rows` 只接 `GdbFormatError`,漏别的就崩整层 |
| 29 | 结果容器 | varint 循环只占 16%,**84% 是把 double 包成 float+元组**(37.4 ns/值);换 flat 才拿得到那 8 倍 |
| 30 | `array('d', mv)` | 走迭代器协议,**比建元组还慢**(60.2 ns/varint);要 `frombytes` 或由 C 直接填 `double*`(5.0 ns) |
| 31 | 先验点数再分配 | varuint 无上界,损坏 blob 能报 `2**40` 个点;`PyList_New` 会甩 `MemoryError` |
| 32 | `_MAX_SHIFT = 57` | 不是 64 —— 让 C 的 64 位算术与 Python 大整数**接受域严格相同** |
| 33 | 分派要每次读属性 | import 期抓走函数 ⇒ `PYOPENFILEGDB_NO_ACCEL` 无声失效,对比变"C 比 C" |
| 34 | MSVC 要 `/utf-8` | C 文件的中文注释在 936 页下能把注释接到下一行代码上,不是美观问题 |
| 35 | **鞋带先平移** | UTM 量级下不平移会丢 12 位有效数字:实测质心偏出 **1135.84 m**、167/21065 个要素错 ≥100 m。平移到首顶点还**更快**(闭合项恒为 0,`i % n` 消失,×0.726)—— 见 §2.21 |
| 36 | 绕向判定共用一份 | 全库只有一份 `ring_signed_area2`;原先 `_is_clockwise` 自带一份不平移的,22,044 个环里判反 1 个(oid 14883) |
| 37 | **质心阈值 ≠ 面积阈值** | `np.roll` 让质心的交叉点高 1.4 倍;共用一个阈值时质心在 128 顶点(最常见尺寸)上**慢 40%**。`--bench` 必须打出"有没有被分派" |
| 38 | 依赖符号的路径不许走 numpy | 成对求和 vs 顺序累加,近零面积环上符号可能翻 —— 那是写盘正确性,不是性能 |
| 39 | 包围盒不加速 | 纯 Python 的 `min(a[0::2])` 已在 C 层完成,numpy 输给 5~20 µs 固定开销(×1.0) |
| 40 | 谓词没有 C++ 可抄 | GDAL 的 `Intersects`/`Contains`/… 全转手 GEOS;按 OGC DE-9IM 自实现,并明说"无 snap-rounding,退化构型可能判错" |
| 41 | `is_valid()` 是部分的 | 只查环闭合 / 顶点数下限 / 环自交 / 洞在壳内;docstring 逐条列了没做的,不要当成 OGC 有效 |
| 42 | **WKT 的点不带括号** | 线和环里每个顶点套括号会产出**非法 WKT**,本库自己的 `from_wkt` 都读不回来 |
| 43 | **WKT 要写 Z/M 后缀** | 光输出第三个分量不够:`LINESTRING (0 0 7, 1 1 8)` 在 WKT1 里是 **Z**;带 M 的几何往返一次维度就静默变了 |
| 44 | 往返测试测不出格式非法 | 本库 `from_wkt` 是宽容解析器,非法文本照收;WKT 必须**直接断言字符串** |
| 45 | 一句多余的 `break` | `test_read` 的 WKT 往返每个图层只跑第一条要素(恰好都是面),上面两个 bug 因此长期全绿 |
