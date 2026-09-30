#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""读示例 —— 打开一个已有的 ``.gdb``,打印结构信息并过滤要素。

用法::

    python examples/read_example.py                     # 自动找样例库
    python examples/read_example.py D:/work/xxx.gdb     # 指定一个库
    python examples/read_example.py D:/work/xxx.gdb 图层名

只用标准库 —— 本示例不需要 GDAL / fiona / pygdal。
"""
from __future__ import annotations

import glob
import os
import sys

# 让脚本在没装包的情况下也能直接从仓库根目录跑起来
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pyopenfilegdb import (  # noqa: E402
    OpenFileGDB, GdbError, FGFT_STRING, FGFT_INT16, FGFT_INT32,
    FGFT_FLOAT32, FGFT_FLOAT64, FGFT_INT64,
)


def find_sample() -> str:
    """按 ``PYOPENFILEGDB_TEST_GDB`` -> ``D:/work/*.gdb`` 的顺序找一个样例库。"""
    env = os.environ.get('PYOPENFILEGDB_TEST_GDB')
    if env:
        for path in env.split(os.pathsep):
            if os.path.isdir(path):
                return path
        raise SystemExit(f'PYOPENFILEGDB_TEST_GDB 里的路径都不存在: {env}')
    for path in sorted(glob.glob('D:/work/*.gdb')):
        if os.path.isdir(path):
            return path
    raise SystemExit(
        '找不到样例 .gdb。请把库路径作为第一个参数传进来,'
        '或设置 PYOPENFILEGDB_TEST_GDB 环境变量。')


def dump_schema(layer) -> None:
    """打印一个图层的字段清单、坐标系、几何类型和范围。"""
    print(f'  逻辑名        : {layer.name}')
    print(f'  要素类名      : {layer.feature_class_name}')
    print(f'  物理名        : {layer.physical_name}      '
          f'({layer.physical_name}.gdbtable)')
    print(f'  几何类型      : {layer.geometry_type}'
          f'   Z={layer.has_z} M={layer.has_m}')
    print(f'  记录数        : {layer.record_count}')

    sr = layer.spatial_ref
    if sr:
        print(f'  坐标系        : WKID={sr.effective_wkid}  {sr.name}')
        # WKT 很长,只打前 72 个字符
        print(f'    WKT         : {sr.wkt[:72]}...')
    else:
        print('  坐标系        : (无 —— 非空间表)')

    # ⚠️ extent 是 ArcGIS 维护的**单调上界**,删除要素后不会回缩,
    #    所以要得到真实范围得自己扫几何。这里照实打印并标注。
    ext = layer.extent
    if ext:
        print(f'  存储范围(上界): ({ext[0]:.6f}, {ext[1]:.6f}) - '
              f'({ext[2]:.6f}, {ext[3]:.6f})')

    print('  字段:')
    print(f'    {"名字":<24}{"类型":<10}{"可空":<6}{"必填":<6}别名')
    for f in layer.fields:
        print(f'    {f.name:<24}FGFT_{f.field_type:<6}'
              f'{"是" if f.nullable else "否":<6}'
              f'{"是" if f.required else "否":<6}{f.alias}')


def actual_bounds(layer) -> None:
    """扫全表算出**真实**几何范围,和上面的存储上界对照。"""
    xmin = ymin = float('inf')
    xmax = ymax = float('-inf')
    n = 0
    for feat in layer.read_features():
        geom = feat.geometry
        if geom is None or geom.is_empty:
            continue
        coords = geom.coordinates
        # coordinates 的形态随 kind 变化,统一摊平成一个点序列:
        #   point      -> (x, y, ...)              单个元组
        #   multipoint -> [(x, y, ...), ...]       点列表
        #   polyline/polygon -> (part_starts, points)  取 points
        if geom.kind == 'point':
            pts = (coords,)
        elif geom.kind == 'multipoint':
            pts = coords
        else:
            pts = coords[1]
        for p in pts:
            xmin = min(xmin, p[0]); xmax = max(xmax, p[0])
            ymin = min(ymin, p[1]); ymax = max(ymax, p[1])
            n += 1
    if n:
        print(f'  真实范围       : ({xmin:.6f}, {ymin:.6f}) - '
              f'({xmax:.6f}, {ymax:.6f})   [{n} 个顶点]')
    else:
        print('  真实范围       : (没有非空几何)')


def demo_where(layer) -> None:
    """演示 WHERE 子句的三种写法 + SQL 三值逻辑。"""
    # 挑一个字符串字段和一个数值字段做演示;没有就跳过。
    str_field = next((f for f in layer.fields
                      if f.field_type == FGFT_STRING and f.length), None)
    num_field = next((f for f in layer.fields if f.field_type in (
        FGFT_INT16, FGFT_INT32, FGFT_INT64, FGFT_FLOAT32, FGFT_FLOAT64)), None)
    if str_field is None:
        print('  (没有字符串字段,跳过 WHERE 演示)')
        return

    print(f'  字符串字段 = {str_field.name!r},数值字段 = '
          f'{num_field.name if num_field else "(无)"}')

    # 1) 字符串形式的 SQL 子集
    rows = list(layer.read_features(where=f'{str_field.name} IS NOT NULL',
                                    limit=3,
                                    fields=[str_field.name]))
    print(f'  WHERE {str_field.name} IS NOT NULL  LIMIT 3  -> '
          f'{[r.attributes.get(str_field.name) for r in rows]}')

    # 2) 可调用谓词 —— 想怎么写就怎么写
    rows = list(layer.read_features(
        where=lambda f: f.oid % 2 == 1, limit=5, fields=[]))
    print(f'  where=lambda f: f.oid % 2 == 1  LIMIT 5     -> '
          f'{[r.oid for r in rows]}')

    # 3) 三值逻辑:谓词返回 True / False / None(None 即 SQL 的 UNKNOWN),
    #    只有 True 才会命中。所以 `NOT (x > 0)` 捞不出 x IS NULL 的行 ——
    #    NULL 参与比较的结果是 UNKNOWN,取反还是 UNKNOWN,不是 True。
    if num_field:
        name = num_field.name
        n_true = sum(1 for _ in layer.read_features(where=f'{name} > 0'))
        n_not = sum(1 for _ in layer.read_features(where=f'NOT ({name} > 0)'))
        n_null = sum(1 for _ in layer.read_features(where=f'{name} IS NULL'))
        n_all = layer.record_count
        print(f'  {name} > 0       -> {n_true} 条')
        print(f'  NOT ({name} > 0) -> {n_not} 条')
        print(f'  {name} IS NULL  -> {n_null} 条')
        print(f'  三条加起来 = {n_true + n_not + n_null},全表 = {n_all}'
              f'  {"✓ 对得上" if n_true + n_not + n_null == n_all else "✗ 对不上"}'
              f'  —— 之所以要单独数 NULL,就是三值逻辑:'
              f'NULL 落在 True 和 False 之外')


def main() -> int:
    gdb_path = sys.argv[1] if len(sys.argv) > 1 else find_sample()
    wanted = sys.argv[2] if len(sys.argv) > 2 else None

    print(f'打开: {gdb_path}')
    try:
        with OpenFileGDB.open(gdb_path) as gdb:
            print(f'  FileGDB 格式版本 : {gdb.version} '
                  f'({"ArcGIS 10.x" if gdb.version == 3 else "ArcGIS Pro"})')

            names = gdb.list_feature_classes()
            print(f'  图层/表({len(names)} 个): {names}\n')

            if not names:
                print('这个库是空的。')
                return 0

            target = wanted or names[0]
            if target not in names:
                print(f'库里没有 {target!r};可选: {names}')
                return 1

            layer = gdb.get_layer(target)
            print(f'--- 图层 {target!r} 的结构 ---')
            dump_schema(layer)
            print('--- 真实范围 ---')
            actual_bounds(layer)
            print('--- WHERE 子句 ---')
            demo_where(layer)

            print(f'--- 前 3 条要素 ---')
            for feat in layer.read_features(limit=3):
                geom = feat.geometry
                wkt = geom.wkt() if geom is not None else '(无几何)'
                if len(wkt) > 70:
                    wkt = wkt[:70] + '...'
                print(f'  OID={feat.oid}  {wkt}')
                for k, v in list(feat.attributes.items())[:6]:
                    print(f'      {k} = {v!r}')
    except GdbError as exc:
        print(f'失败: {type(exc).__name__}: {exc}')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
