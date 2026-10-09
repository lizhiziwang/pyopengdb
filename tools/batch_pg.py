import sys
from datetime import datetime, timedelta, timezone

import psycopg
from psycopg import sql

from pyopenfilegdb import OpenFileGDB
from pyopenfilegdb import _constants as C

connect_param = {
    'host': 'localhost',
    'port': 54321,
    'user': 'postgres',
    'password': 'Aa@123456',
    'database': 'postgres',
    'connect_timeout': 60
}

def init_db_connection(core_size=5, time_out=60,time_unit=timedelta(seconds=1)):
    return psycopg.connect(
        host=connect_param['host'],
        port=connect_param['port'],
        dbname=connect_param['database'],
        user=connect_param['user'],
        password=connect_param['password']
    )


def check_table_exist(con, table_name: str, schema: str = "public") -> bool:
    with con.cursor() as cur:
        cur.execute(
            """select exists(select 1 from information_schema.tables 
               where table_schema=%s and table_name=%s)""",
            (schema, table_name)
        )
        return cur.fetchone()[0]


#: FGFT_* -> PostgreSQL 类型。STRING / OBJECTID 在 _pg_type() 里单独处理。
_PG_TYPE = {
    C.FGFT_INT16: 'smallint',
    C.FGFT_INT32: 'integer',
    C.FGFT_INT64: 'bigint',
    C.FGFT_FLOAT32: 'real',
    C.FGFT_FLOAT64: 'double precision',
    C.FGFT_DATETIME: 'timestamp',
    C.FGFT_DATE: 'date',
    C.FGFT_TIME: 'time',
    C.FGFT_DATETIME_WITH_OFFSET: 'timestamptz',
    C.FGFT_GUID: 'uuid',
    C.FGFT_GLOBALID: 'uuid',
    C.FGFT_BINARY: 'bytea',
    C.FGFT_RASTER: 'bytea',
    C.FGFT_XML: 'text',
}

#: FileGDB 表级几何类型 -> PostGIS 类型名。见 create_table_from_layer 里的说明:
#: 线/面在图层的**同一列**里可以同时有单体和多体(shape type 3/11、5/15),
#: 而 PostGIS 的 typmod 是单一类型,所以这两个只能用泛型 GEOMETRY。
_PG_GEOM_TYPE = {
    'point': 'Point',
    'multipoint': 'MultiPoint',
    'polyline': 'Geometry',
    'polygon': 'Geometry',
    'multipatch': 'Geometry',
}


#: PostgreSQL 的 varchar 长度上限。FileGDB 用 2147483647(INT32_MAX)表示"不限长",
#: 照抄过去 PG 会直接拒绝(length for type varchar cannot exceed 10485760),
#: 所以超过上限的一律退成 text。
_PG_VARCHAR_MAX = 10485760


def _pg_dim_suffix(has_z: bool, has_m: bool) -> str:
    """表级 ``hasZ`` / ``hasM`` -> PostGIS 类型修饰符的维度后缀。

    ⚠️ PostGIS 的**两参数** typmod(``geometry(Point, 4326)``)是**二维**的,
    往里面塞 ``POINT Z`` 会报 ``Geometry has Z dimension but column does not``。
    所以 Z / M 图层必须显式写成 ``PointZ`` / ``PointZM``,不能只写 ``Point``。
    """
    if has_z and has_m:
        return 'ZM'
    if has_z:
        return 'Z'
    return 'M' if has_m else ''


def _pg_type(field) -> str:
    if field.is_oid:
        return 'integer'
    if field.field_type == C.FGFT_STRING:
        if 0 < field.length <= _PG_VARCHAR_MAX:
            return f'varchar({field.length})'
        return 'text'
    try:
        return _PG_TYPE[field.field_type]
    except KeyError:
        raise ValueError(
            f'字段 {field.name!r}: FGFT {field.field_type} 没有对应的 PostgreSQL 类型'
        ) from None


def create_table_from_layer(con, layer, table_name=None, schema='public',
                            if_exists='error', with_geometry=True,
                            geom_type=None, spatial_index=True, comments=True,
                            commit=True) -> bool:
    """按 ``layer`` 的字段定义在 PostgreSQL 里建一张表。

    :param con: psycopg 连接。
    :param layer: :class:`pyopenfilegdb.GdbLayer`。
    :param table_name: 表名;默认用 ``layer.name``(中文名直接 quote 成标识符)。
    :param if_exists: ``'error'`` 报错 / ``'skip'`` 返回 False / ``'replace'`` 先 DROP。
    :param with_geometry: 为要素类加一列 PostGIS ``geometry``;**需要目标库装了
        PostGIS**,没装会报 ``type "geometry" does not exist``。非空间表自动跳过。
    :param geom_type: 覆盖几何列的类型名,给的是**完整**名字(要 Z 就自己写
        ``'MultiPolygonZ'``,这里不再拼维度后缀)。
    :param spatial_index: 给几何列建 GiST 空间索引(PostGIS 的标准做法),没有
        几何列时自动跳过。**要一次性灌几十万条的话可以传 False** —— 空表上建
        索引几乎不要钱,但"先灌完再建"比"边灌边维护 GiST"快得多,灌完自己跑
        ``CREATE INDEX ON t USING GIST ("Shape")`` 即可。
    :param comments: 把图层名/字段别名(ArcGIS 里的中文名)写成 COMMENT。
    :param commit: 建完就提交。批量建表时传 ``False`` 自己控事务。
    :return: 建了表返回 True;``if_exists='skip'`` 且已存在返回 False。

    字段类型映射(FGFT_* -> PostgreSQL)::

        INT16                smallint
        INT32                integer
        INT64                bigint              ArcGIS Pro 3.2+
        FLOAT32              real
        FLOAT64              double precision
        STRING               varchar(n) / text   n 取 length(字符数);0 或 2147483647
                                                 (FileGDB 的"不限长")都给 text
        DATETIME             timestamp           读出是 datetime
        DATE                 date                读出是 datetime,入库前 .date()
        TIME                 time                读出是 datetime,入库前 .time()
        DATETIME_WITH_OFFSET timestamptz         读出是 (datetime, 分钟偏移) 元组
        OBJECTID             integer PRIMARY KEY OID 天然唯一
        GUID / GLOBALID      uuid                读出是 {..} 形式,PG 认这种输入
        BINARY               bytea
        RASTER               bytea               本库读栅格一律给 None
        XML                  text                跟 GDAL 一致(OFTString),不用 PG 的 xml

    ⚠️ **几何列的 typmod**:点/多点图层就是 ``Point`` / ``MultiPoint``;线、面、
    multipatch 用泛型 ``Geometry`` —— FileGDB 不区分单体/多体,一个"面"图层里
    ``POLYGON`` 和 ``MULTIPOLYGON`` 可以混着存,而 ``geometry(Polygon, ...)`` 会
    把 ``MULTIPOLYGON`` 挡在门外。知道自己的数据是纯多体,就用
    ``geom_type='MultiPolygon'`` 收紧。
    **Z / M 图层会按表级 ``hasZ``/``hasM`` 拼上维度后缀**(``PointZ`` /
    ``GeometryZM``)—— PostGIS 的两参数 typmod 是二维的,写成 ``geometry(Point,
    4326)`` 再灌 ``POINT Z`` 会被拒。
    SRID 取 ``layer.spatial_ref.effective_wkid``,拿不到就是 0(未知)。

    ⚠️ **时间字段**读出来都是 ``datetime``(FileGDB 存的是"1899-12-30 起的天数"),
    ``DATE`` / ``TIME`` 列入库前要自己 ``.date()`` / ``.time()``;
    ``DATETIME_WITH_OFFSET`` 是 ``(本地壁钟, 分钟偏移)`` 的元组,要减掉偏移才是
    真正的时间点。这一层只管建表,不做类型转换。

    不做的事:不建 schema、不搬数据、不 ANALYZE。空间索引默认建,
    要自己控制就 ``spatial_index=False``。
    """
    if if_exists not in ('error', 'skip', 'replace'):
        raise ValueError("if_exists 只能是 'error' / 'skip' / 'replace'")

    name = table_name or layer.name
    if check_table_exist(con, table_name=name, schema=schema):
        if if_exists == 'skip':
            return False
        if if_exists == 'error':
            raise ValueError(
                f'表 {schema}.{name} 已存在(要覆盖就 if_exists="replace")')
        with con.cursor() as cur:
            cur.execute(sql.SQL('DROP TABLE {}.{}').format(
                sql.Identifier(schema), sql.Identifier(name)))

    geom_field = layer.geometry_field if with_geometry else None

    cols = []
    for field in layer.fields:
        parts = [sql.Identifier(field.name), sql.SQL(_pg_type(field))]
        if field.is_oid:
            parts.append(sql.SQL('PRIMARY KEY'))
        elif field.required or not field.nullable:
            parts.append(sql.SQL('NOT NULL'))
        cols.append(sql.SQL(' ').join(parts))

    if geom_field is not None:
        if geom_type:
            gtype = geom_type      # 给了就是完整名字,要 Z 自己写 'MultiPolygonZ'
        else:
            gtype = (_PG_GEOM_TYPE.get(layer.geometry_type, 'Geometry')
                     + _pg_dim_suffix(layer.has_z, layer.has_m))
        srid = layer.spatial_ref.effective_wkid or 0
        cols.append(sql.SQL('{} geometry({}, {})').format(
            sql.Identifier(geom_field.name), sql.SQL(gtype), sql.Literal(srid)))

    with con.cursor() as cur:
        cur.execute(sql.SQL('CREATE TABLE {}.{} ({})').format(
            sql.Identifier(schema), sql.Identifier(name), sql.SQL(', ').join(cols)))

        if comments:
            cur.execute(sql.SQL('COMMENT ON TABLE {}.{} IS {}').format(
                sql.Identifier(schema), sql.Identifier(name),
                sql.Literal(layer.name)))
            all_fields = list(layer.fields) + ([geom_field] if geom_field else [])
            for field in all_fields:
                if field.alias and field.alias != field.name:
                    cur.execute(sql.SQL('COMMENT ON COLUMN {}.{}.{} IS {}').format(
                        sql.Identifier(schema), sql.Identifier(name),
                        sql.Identifier(field.name), sql.Literal(field.alias)))

        if geom_field is not None and spatial_index:
            # 索引名交给 PG 自己取(表名_列名_idx);中文长表名手拼会被截到
            # 63 字节,有撞名风险,PG 自动命名会自己加后缀去重。
            cur.execute(sql.SQL('CREATE INDEX ON {}.{} USING GIST ({})').format(
                sql.Identifier(schema), sql.Identifier(name),
                sql.Identifier(geom_field.name)))

    if commit:
        con.commit()
    return True


def _table_columns(con, schema: str, table: str):
    """目标表的 ``(列名, PG 类型, udt_name)``,按建表顺序。"""
    with con.cursor() as cur:
        cur.execute(
            """select column_name, data_type, udt_name
               from information_schema.columns
               where table_schema=%s and table_name=%s
               order by ordinal_position""",
            (schema, table))
        return cur.fetchall()


def _coerce(value, data_type: str):
    """按**目标列**的 PG 类型做最小适配。理由见 :func:`insert_features`。"""
    if value is None:
        return None
    if isinstance(value, tuple):        # DATETIME_WITH_OFFSET:(壁钟, 分钟偏移)
        dt, offset = value
        if dt is None:
            return None
        # 带偏移就是"瞬时",换算成 UTC 再交给 timestamptz,免得按会话 TimeZone 解释。
        return (dt - timedelta(minutes=offset)).replace(tzinfo=timezone.utc)
    if isinstance(value, datetime):
        # PG 的 date/time 输入函数不吃带时间的串,按目标列裁掉多余的部分。
        # ⚠️ 必须逐字比,不能用 ``startswith('time')`` ——
        # 'timestamp without time zone' 也是以 'time' 开头的,那样会把
        # datetime 裁成 ``08:09:10`` 塞进 timestamp 列,PG 直接报语法错。
        if data_type == 'date':
            return value.date()
        if data_type in ('time without time zone', 'time with time zone'):
            return value.time()
    return value


def insert_features(con, layer, table_name=None, schema='public',
                    where=None, limit=None, truncate=False,
                    verbose=False, commit=True) -> int:
    """把 ``layer`` 的要素批量灌进**已经建好的** PostgreSQL 表。

    :param con: psycopg 连接。
    :param layer: :class:`pyopenfilegdb.GdbLayer`。
    :param table_name: 目标表名;默认 ``layer.name``。
    :param schema: 目标 schema。
    :param where: 只灌满足条件的要素(语法见 ``GdbLayer.read_features``)。
    :param limit: 最多灌多少条。
    :param truncate: 灌之前先 ``TRUNCATE``(重跑用 —— 不清空会撞主键)。
    :param verbose: 每 1000 条打一行进度。
    :param commit: 灌完就提交;批量时传 ``False`` 自己控事务。
    :return: 实际插入的行数。

    **几何走 EWKT 文本**,SRID 从图层读(``layer.spatial_ref.effective_wkid``,
    拿不到就是 0),写成 ``SRID=4527;POLYGON((...))`` —— PostGIS 的
    ``geometry_in`` 自己认 ``SRID=`` 前缀,不用再套一层 ``ST_GeomFromText``。
    **这一步是无损的**:本库的 WKT 写出用 ``repr()``(Python 的最短往返表示),
    PostGIS 那头用 ``strtod``,双精度原样过去。

    **真瓶颈是几何文本本身,不是数据库这一侧,更不是批的大小。** 实测
    (``村行政区划`` 取 2000 条,几何文本共 75 MB / 37.5 KB 一条,
    psycopg 3.3 + 本机 PG 12 + PostGIS 3.2,详见 DESIGN.md §2.19.6/§2.19.7):

    * 整条循环 **3.95 s(506 条/s)**;目标表**没有几何列**时只要 0.14 s
      (14,650 条/s)—— 96% 花在几何上;
    * ⚠️ **"生成"和"灌进去"这两段不是相加关系。** 拆开量:``wkt()`` 生成
      2.4 s;把同样 75 MB 文本(预先算好)灌进去 3.3 s。相加是 5.7 s,实测全程
      3.95 s —— COPY 是流式的,客户端的生成与服务端的解析/装载**同时在跑**。
      所以别把 ``wkt()`` 单独跑的秒数当成"灌库要多久",那是客户端这一条腿;
      也正因如此,wkt() 从 3.9~4.3 s 降到 2.4 s,全程只跟着从 5.24 s 降到
      3.95 s(省 1.29 s),**省不满**那 1.6 s。现在**服务端那条腿(3.3 s)比
      客户端还长**,再压 wkt() 的边际收益被它卡着;
    * ``wkt()`` 的逐坐标格式化**已经走 C 拓展**(``wkt_seq``,``_gdbaccel.c``)。
      **没有**那个扩展时这一步是 3.9 ~ 4.3 s、整条循环 5.24 s —— 那是纯 Python
      的数,别拿它们当"现在有多快"(改前后的对照见 DESIGN.md §2.19.7);
    * **分批买不到任何东西**:预先算好 WKT 再单独灌,一次 ``COPY`` 3.28 s ——
      改成每 1000 条一个 ``COPY`` 3.31 s、每 1000 条再加 commit 3.29 s、
      每 100 条加 commit 3.22 s,**全是 ±3% 噪声**。psycopg 的 ``write_row``
      本来就按 ``BUFFER_SIZE`` 缓冲(``psycopg._copy_base.TextFormatter``),
      "一行一次系统调用"这件事不存在;
    * GiST 空间索引同样不是瓶颈:带索引 5.15 s、去掉索引 5.23 s,2000 行灌完
      再建索引只要 0.01 s。索引成本随行数走,百万行时才需要"先灌完再建"那套
      (所以 ``create_table_from_layer`` 才留了 ``spatial_index=False``)。

    ⚠️ 换二进制(hex EWKB)那条路**试过,只快 2.5×**(2.21 s),没采用:
    EWKB 二进制 36.8 MB,hex 编码后又回到 73.5 MB —— 和 WKT 的 75 MB 一样大,
    省下的只有生成那一头,而生成已经不是长边了(见上)。别再照着"二进制一定
    更快"重来一遍,数在 §2.19.6。

    ⚠️ 还剩多少可挖:客户端那 2.4 s 里**一半是 CPython 自己的 ``repr``**(每个
    坐标约 0.25 µs 的 dtoa),搬进 C 省不掉它 —— 要再快只能自己写一个最短往返
    格式化器(Ryu 那一类),而且受上一条限制,端到端拿不到全额。DESIGN.md
    §2.19.7 末尾记了为什么没做。另一条路在**调用侧**(按 OID 分区开进程)。

    ⚠️ **"无损"是几何意义上的,不是"环的顺序也一样"。** 多环面过一趟 WKT
    回来,环按"外环 + 它的洞"重新编排(本库的写出口就是这么分组的),而盘上
    解码出来的是 FileGDB 自己的平铺顺序。拿 ``exactly_equals`` 直接比
    "解码态 vs 回读态",2000 条里会有 **28 条**不等(实测,全是多环的);
    两边**都先过一遍 WKT** 再比就是 **0 条** —— 差的只是顺序,几何本身没变。

    ⚠️ COPY 中途出错只说"第 N 行"(N 是本次遍历的第几条,**不是 OID**);只有几何
    转 WKT 失败(``multipatch``)那一步能带上 OID,因为那时还没进 COPY。
    同一事务里要么全进要么全不进。

    目标表的列**现查**(``information_schema.columns``),按列名对号入座:

    * 列名 = 图层的 OBJECTID 字段名 -> ``feat.oid``;
    * ``udt_name='geometry'`` 的那一列 -> 几何(有且只能有一列,多了直接报错);
    * 其余列 -> ``feat.attributes.get(列名)``,**对不上就是 NULL**(这样目标表
      多几列、或者建表时 ``with_geometry=False``,都不用改调用)。

    几何按 **EWKT** 文本给(``SRID=4527;POLYGON((...))``):PostGIS 的
    ``geometry_in`` 自己认 ``SRID=`` 前缀,不用再套一层 ``ST_GeomFromText``。
    这一步是**无损的** —— 本库的 WKT 写出用 ``repr()``(Python 的最短往返表示),
    PostGIS 那头用 ``strtod``,双精度原样过去。

    ⚠️ FileGDB 的 ``DATETIME`` 字段**不带时区信息**,目标列若是 ``timestamptz``
    会按会话的 ``TimeZone`` 解释;要固定口径就自己先 ``SET TimeZone``。
    带偏移的 ``DATETIME_WITH_OFFSET`` 例外,它换算成 UTC 瞬时。
    ``DATE`` / ``TIME`` 读出来是 ``datetime``,这里按目标列的类型自动裁成
    ``date`` / ``time``。

    ⚠️ ``multipatch`` 图层灌不了:WKT 没有对应的几何类型,``Geometry.wkt()``
    会抛 ``NotImplementedError``(消息里带 OID)。见 DESIGN.md §2.21。
    """
    name = table_name or layer.name
    cols = _table_columns(con, schema, name)
    if not cols:
        raise ValueError(f'表 {schema}.{name} 不存在(先 create_table_from_layer)')

    col_names = [c for c, _t, _u in cols]
    oid_col = layer.oid_field_name if layer.oid_field_name in col_names else None
    geom_cols = [c for c, _t, udt in cols if udt == 'geometry']
    if len(geom_cols) > 1:
        raise ValueError(
            f'表 {schema}.{name} 有 {len(geom_cols)} 个 geometry 列,分不清写哪个')
    geom_col = geom_cols[0] if geom_cols and layer.geometry_field else None
    srid = layer.spatial_ref.effective_wkid or 0

    if truncate:
        with con.cursor() as cur:
            cur.execute(sql.SQL('TRUNCATE {}.{}').format(
                sql.Identifier(schema), sql.Identifier(name)))

    def value_of(feat, col, data_type):
        if col == geom_col:
            geom = feat.geometry
            return None if geom is None else f'SRID={srid};{geom.wkt()}'
        if col == oid_col:
            return feat.oid
        return _coerce(feat.attributes.get(col), data_type)

    stmt = sql.SQL('COPY {}.{} ({}) FROM STDIN').format(
        sql.Identifier(schema), sql.Identifier(name),
        sql.SQL(', ').join(sql.Identifier(c) for c in col_names))

    inserted = 0
    with con.cursor() as cur:
        with cur.copy(stmt) as copy:
            for feat in layer.read_features(where=where, limit=limit):
                try:
                    copy.write_row([value_of(feat, c, t) for c, t, _u in cols])
                except NotImplementedError as e:
                    raise NotImplementedError(f'OID {feat.oid}: {e}') from e
                inserted += 1
                if verbose and inserted % 1000 == 0:
                    print(f'  ... 已插入 {inserted} 条')

    if commit:
        con.commit()
    return inserted


if __name__ == '__main__':
    # 用法: python tools/batch_pg.py [xxx.gdb [图层名 ...]]   —— 不传图层名就整库建
    con = init_db_connection()
    print(check_table_exist(con, table_name="村行政区划", schema="sde"))

    ds = OpenFileGDB(r'D:\work\2024年国土行政区划.gdb')
    layer = ds.get_layer('村行政区划')

    var1 = create_table_from_layer(con, layer,schema='sde')
    print(var1)

    # 灌数据:去掉 limit 才是全量(村行政区划 21,217 条,几何很大,要几分钟)
    var2 = insert_features(con, layer, schema='sde', truncate=True,
                           verbose=True)
    print('插入', var2, '条')
    con.close()



