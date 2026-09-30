#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""建库 + 写入示例 —— 从零建一个 ``.gdb``,加图层、写要素、改、删、再读回。

用法::

    python examples/create_write_example.py               # 写到临时目录
    python examples/create_write_example.py D:/tmp/out.gdb

只用标准库 —— 本示例不需要 GDAL / fiona / pygdal。
产出的库可以直接用 ArcGIS / QGIS 打开(**写完之后要 close()**,或者用
``with`` 退出 —— 写路径是懒落盘的,关句柄就是结账;急着让别人看见可以
中途 ``layer.sync()``)。
"""
from __future__ import annotations

import datetime
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pyopenfilegdb import (  # noqa: E402
    GdbFeature, GdbField, Geometry, OpenFileGDB,
    FGFT_DOUBLE, FGFT_INT32, FGFT_STRING, FGFT_DATETIME,
)


def build(gdb_path: str) -> None:
    """建库、加三个图层、写要素。"""
    # ------------------------------------------------------------------
    # 1. create() —— 建一个空库
    #    ⚠️ 路径不能已存在。内部会写出 a00000001..a00000007 七张系统表,
    #    与真实 ArcGIS 空白库逐段字节一致。
    # ------------------------------------------------------------------
    gdb = OpenFileGDB.create(gdb_path)
    print(f'建库: {gdb_path}  (FileGDB version={gdb.version})')

    # ------------------------------------------------------------------
    # 2. create_layer() —— 点图层
    #    fields 里的每个字段用 GdbField('名字', FGFT_类型, ...) 描述。
    #    函数内部会做四重登记:SystemCatalog / Items / ItemRelationships
    #    / Definition XML,并分配物理名(新库里第一个用户图层 = a00000009)。
    # ------------------------------------------------------------------
    points = gdb.create_layer(
        '监测点',
        geometry_type='point',          # point / multipoint / polyline / polygon / null
        fields=[
            GdbField('NAME', FGFT_STRING, length=64, nullable=False),
            GdbField('HEIGHT', FGFT_DOUBLE, alias='高程(m)'),
            GdbField('POP', FGFT_INT32, alias='人口'),
        ],
        spatial_ref=None,               # None -> 默认 WGS84(-180,-90,1e6 量化)
    )
    print(f'图层 {points.name!r} 物理名 = {points.physical_name}, '
          f'字段 = {[f.name for f in points.fields]}')

    # write_feature 接受 dict,也接受 GdbFeature。返回新 OID(从 1 开始)。
    # 几何字典的键用 geometry_field 的名字('Shape'),值必须是 Geometry;
    # 也可以像下面第 3 条那样直接传 GdbFeature(geometry 字段单独填)。
    oid1 = points.write_feature({
        'NAME': '北京',
        'HEIGHT': 43.5,
        'POP': 21_890_000,
        'Shape': Geometry.from_wkt('POINT (116.4074 39.9042)'),
    })
    oid2 = points.write_feature({
        'NAME': '上海',
        'HEIGHT': 4.0,
        'POP': 24_870_000,
        'Shape': Geometry.from_wkt('POINT (121.4737 31.2304)'),
    })
    # 空值:不写 HEIGHT 就落 NULL。可空字段在盘上由空值位图标记,不占字节。
    oid3 = points.write_feature(GdbFeature(
        attributes={'NAME': '某观测站'},            # HEIGHT / POP 都是 NULL
        geometry=Geometry.from_wkt('POINT (100.0 25.0)'),
    ))
    print(f'写入 3 个点,OID = {oid1}, {oid2}, {oid3}')

    # ------------------------------------------------------------------
    # 3. 折线图层 + 时间字段
    #    WKT 里的 Z/M 后缀与 has_z / has_m 声明要**一致**;
    #    声明了 has_m 就必须给 M 值(缺的分量会被填成 Esri 的 NaN,
    #    不会静默降级成"没有 M"的几何)。
    # ------------------------------------------------------------------
    lines = gdb.create_layer(
        '路线',
        geometry_type='polyline',
        fields=[
            GdbField('NAME', FGFT_STRING, length=64),
            GdbField('LENGTH_KM', FGFT_DOUBLE),
            GdbField('SURVEYED', FGFT_DATETIME),
        ],
    )
    lines.write_feature({
        'NAME': '示例路线',
        'LENGTH_KM': 42.195,
        'SURVEYED': datetime.datetime(2026, 9, 29, 8, 30, 0),
        'Shape': Geometry.from_wkt(
            'LINESTRING (116.0 39.0, 116.5 39.5, 117.2 40.1)'),
    })

    # ------------------------------------------------------------------
    # 4. 多边形图层
    #    WKT 按 OGC 要求写闭合环,库里存的是**不闭合**的环(首尾点不重复),
    #    写盘时会自动补回闭合点;外环逆时针/顺时针都行,编码时统一按
    #    Esri 约定(首环 CW、其余 CCW)调整方向。
    # ------------------------------------------------------------------
    polys = gdb.create_layer(
        '地块',
        geometry_type='polygon',
        fields=[GdbField('NAME', FGFT_STRING, length=32)],
    )
    polys.write_feature({
        'NAME': 'A 区',
        # 一个带内环(洞)的多边形:两个环写在同一个 POLYGON 里
        'Shape': Geometry.from_wkt(
            'POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0),'
            '         (2 2, 4 2, 4 4, 2 4, 2 2))'),
    })
    polys.write_feature({
        'NAME': 'B 区',
        'Shape': Geometry.from_wkt('POLYGON ((20 20, 30 20, 30 30, 20 20))'),
    })
    print(f'已建图层: {gdb.list_feature_classes()}')

    # ⚠️ 写是懒的(照 GDAL 的 FileGDBTable::CreateFeature):每条 write_feature
    # 只写记录体和它那一行的索引(O(1)),头部计数 / 包围盒 / 索引尾部攒到
    # 落盘点 —— close()、with 退出、或显式 gdb.sync()/layer.sync()。
    # 所以**不 close 就退出**会留下一个半成品库;批量写完想立刻用另一个句柄
    # (或 ArcGIS)读,也要先 sync()。
    gdb.close()


def mutate(gdb_path: str) -> None:
    """用 update=True 重开,演示 update / delete。"""
    print()
    print('--- 更新与删除 ---')
    # 写操作必须在打开时声明 update=True,否则拿到的是只读句柄。
    with OpenFileGDB.open(gdb_path, update=True) as gdb:
        points = gdb.get_layer('监测点')

        # update_feature:读 -> 改 -> 写回。
        # 新记录体若与旧记录等长就原地覆写;变长(例如把短字符串改长)
        # 会自动走"删旧 + 末尾追加",OID 保持不变。
        feat = points.read_feature(1)
        feat['HEIGHT'] = 44.0
        feat['NAME'] = '北京市'          # 3 字符 -> 3 字符,本例是等长覆写
        points.update_feature(feat)
        print(f'OID 1 更新后: {points.read_feature(1).attributes}')

        # delete_feature:盘上把长度写成负数、tablx 槽位置 0。
        # 文件不会回收这块空间,也**不会**缩小 extent(见 DESIGN.md §2.16)。
        points.delete_feature(3)
        print(f'删除 OID 3 后 record_count = {points.record_count}')
        print(f'再读 OID 3 -> {points.read_feature(3)}')


def verify(gdb_path: str) -> None:
    """重新以只读方式打开,验证写进去的东西读得回来。"""
    print()
    print('--- 回读验证 ---')
    with OpenFileGDB.open(gdb_path) as gdb:
        for name in gdb.list_feature_classes():
            layer = gdb.get_layer(name)
            print(f'[{name}] 几何={layer.geometry_type} '
                  f'记录数={layer.record_count} '
                  f'字段={[f.name for f in layer.fields]}')
            for feat in layer.read_features():
                wkt = feat.geometry.wkt() if feat.geometry else '(无)'
                if len(wkt) > 60:
                    wkt = wkt[:60] + '...'
                print(f'    OID={feat.oid}  {wkt}')
                for k, v in feat.attributes.items():
                    print(f'        {k} = {v!r}')

        # WHERE 过滤器(字符串写的是 SQL 子集,注意 NULL 走三值逻辑)
        pts = gdb.get_layer('监测点')
        hit = list(pts.read_features(where="HEIGHT IS NOT NULL", fields=['NAME']))
        print(f'\nWHERE HEIGHT IS NOT NULL -> '
              f'{[f.attributes["NAME"] for f in hit]}')

        # 按几何包围盒过滤
        boxed = list(pts.read_features(bbox=(110, 20, 130, 45)))
        print(f'bbox (110,20,130,45) -> {[f["NAME"] for f in boxed]}')


def main() -> int:
    if len(sys.argv) > 1:
        gdb_path = sys.argv[1]
        if os.path.exists(gdb_path):
            shutil.rmtree(gdb_path)      # create() 要求路径不存在
        parent = os.path.dirname(os.path.abspath(gdb_path))
        os.makedirs(parent, exist_ok=True)
    else:
        tmp = tempfile.mkdtemp(prefix='pyopenfilegdb_demo_')
        gdb_path = os.path.join(tmp, 'demo.gdb')
        print(f'(未指定路径,写到临时目录;用 python examples/create_write_example.py '
              f'D:/tmp/demo.gdb 可以自己选)\n')

    build(gdb_path)
    mutate(gdb_path)
    verify(gdb_path)

    print()
    print(f'完成。可以用 ArcGIS / QGIS 打开: {gdb_path}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
