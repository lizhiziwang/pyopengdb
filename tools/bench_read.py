#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""读路径基准测试。

用来定位"读一个图层要多久"到底花在哪。分段计时,从最便宜的 IO 一路量到
最贵的几何解码,好判断优化该往哪儿使劲。

用法::

    python tools/bench_read.py                                  # 默认样例
    python tools/bench_read.py D:/work/xxx.gdb 图层名
    python tools/bench_read.py D:/work/xxx.gdb 图层名 2000      # 只跑前 N 条

分段说明(每段都是独立的、可单独比较的):

    A  端到端:read_features() + 取一个属性      ← main.py 的那种用法
    B  端到端 + fields=[...] 投影
    C  只走 iter_rows()(解码整条记录)
    D  只 seek + read 记录体字节,完全不解码   ← IO 的理论下限
    E  连几何一起读(第一次访问 feat.geometry 才付这笔钱)
    F  bbox= 空间过滤(走几何 blob 自带的存储包围盒粗筛)

A 便宜是因为几何没被解 —— 只要循环里不碰 ``feat.geometry``,几百个县也
就几十毫秒。E 才是"真要几何"时的代价,D 给出其中 IO 的下限,两者之差
就是纯解码开销。F 用来量粗筛省了多少。
"""
from __future__ import annotations

import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
# tools/ 的上一级 = 仓库根(pyopenfilegdb 包在这儿);_HERE 留给同目录的 import。
sys.path[:0] = [os.path.dirname(_HERE), _HERE]

from pyopenfilegdb import OpenFileGDB  # noqa: E402


def _pick_layer(gdb, wanted=None):
    names = gdb.list_feature_classes()
    if not names:
        raise SystemExit('库里没有图层')
    name = wanted or names[0]
    if name not in names:
        raise SystemExit(f'没有图层 {name!r};可选: {names}')
    return gdb.get_layer(name)


def bench(gdb_path: str, layer_name: str = None, limit: int = 0) -> None:
    with OpenFileGDB.open(gdb_path) as gdb:
        ly = _pick_layer(gdb, layer_name)
        tab = ly.table
        total = ly.record_count
        cap = min(total, limit) if limit else total

        print(f'库   : {os.path.basename(gdb_path)}')
        print(f'图层 : {ly.name}  几何={ly.geometry_type}  '
              f'记录={total}  本次跑={cap}')
        print(f'字段 : {[f.name for f in ly.fields]}')
        print()

        attr = next((f.name for f in ly.fields
                     if f.field_type == 4 and f.length), None)

        # ---- A:main.py 的那种循环 ------------------------------------
        t0 = time.perf_counter()
        n = 0
        for feat in ly.read_features(limit=cap or None):
            if attr:
                feat.attributes[attr]
            n += 1
        ta = time.perf_counter() - t0
        print(f'A  read_features(只取属性)      {ta:8.3f}s  {n:>7} 条  '
              f'{_per(ta, n)}')

        # ---- B:加 fields 投影 ----------------------------------------
        t0 = time.perf_counter()
        n = 0
        for _ in ly.read_features(limit=cap or None,
                                  fields=[attr] if attr else None):
            n += 1
        tb = time.perf_counter() - t0
        print(f'B  read_features(fields=...)    {tb:8.3f}s  {n:>7} 条  '
              f'{_per(tb, n)}')

        # ---- C:只走 iter_rows ----------------------------------------
        t0 = time.perf_counter()
        n = 0
        verts = 0
        for _row, vals in tab.iter_rows():
            n += 1
            if n > cap:
                break
        tc = time.perf_counter() - t0
        print(f'C  iter_rows(解码整条)         {tc:8.3f}s  {n:>7} 条  '
              f'{_per(tc, n)}')

        # ---- D:只 seek+read,不解码 -----------------------------------
        t0 = time.perf_counter()
        n = 0
        size = 0
        with open(tab.path, 'rb') as fp:
            for row in range(cap or tab.tablx.record_count):
                off = tab.tablx.offset_for_row(row)
                if not off:
                    continue
                fp.seek(off)
                ln = int.from_bytes(fp.read(4), 'little')
                if ln & 0x80000000:
                    continue
                size += len(fp.read(ln))
                n += 1
        td = time.perf_counter() - t0
        print(f'D  只 seek+read(不解码)       {td:8.3f}s  {n:>7} 条  '
              f'{_per(td, n)}   {size / 1e6:.1f} MB')
        print()

        # ---- E:几何解码的真实代价(强制解出来) ----------------------
        t0 = time.perf_counter()
        verts = 0
        for feat in ly.read_features(limit=cap or None):
            g = feat.geometry          # ← 这一行才触发解码
            if g is None or g.is_empty:
                continue
            if g.kind == 'point':
                verts += 1
            elif g.kind == 'multipoint':
                verts += len(g.coordinates)
            else:
                verts += len(g.coordinates[1])
        te = time.perf_counter() - t0
        print(f'E  连几何一起读(强制解码)      {te:8.3f}s  {n:>7} 条  '
              f'{_per(te, n)}')
        print()

        # ---- F:bbox 过滤(走存储包围盒粗筛) --------------------------
        ext = ly.extent
        if ext:
            xmin, ymin, xmax, ymax = ext
            mid = (xmin + (xmax - xmin) * 0.45, ymin + (ymax - ymin) * 0.45,
                   xmin + (xmax - xmin) * 0.55, ymin + (ymax - ymin) * 0.55)
            t0 = time.perf_counter()
            hit = sum(1 for _ in ly.read_features(bbox=mid, limit=cap or None))
            tf = time.perf_counter() - t0
            print(f'F  bbox 过滤(存储包围盒粗筛)  {tf:8.3f}s  命中 {hit} 条')
            print()

        _verdict(ta, td, te, verts)


def _per(sec: float, n: int) -> str:
    if not n:
        return ''
    return f'{sec / n * 1e6:8.1f} µs/条'


def _verdict(ta: float, td: float, te: float, verts: int) -> None:
    """按"属性 vs 几何"两条路分别给结论。

    这里不再拿 A 和 C 比 —— A 走惰性几何、C 解整条记录,本来就该差一个
    数量级,比出来只会误导。真正要盯的是 **E**:一旦访问 ``feat.geometry``,
    省下的钱就都还回去了。
    """
    print('结论:')
    if te > 0:
        saved = (te - ta) / te * 100
        print(f'  · 只读属性 {ta:.3f}s,连几何一起读 {te:.3f}s —— '
              f'惰性几何省掉 {saved:.1f}%')
    if verts:
        dec = te - td
        print(f'  · 顶点 {verts:,} 个:几何解码 ≈ {dec:.3f}s,'
              f'折合 {dec / verts * 1e6:.3f} µs/顶点;'
              f'其中 IO 只占 {td / te * 100:.1f}%')
    print('  · 瓶颈是几何解码(纯 Python 逐顶点 varint),不在 IO。'
          '只读属性就别碰 feat.geometry;')
    print('    真要几何,一是按 bbox= 先粗筛(走存储包围盒,不解点数组),'
          '二是尽量别对同一条要素反复取几何(解出来会缓存)。')


def main() -> int:
    gdb_path = sys.argv[1] if len(sys.argv) > 1 else None
    layer = sys.argv[2] if len(sys.argv) > 2 else None
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    if gdb_path is None:
        import glob
        cands = sorted(glob.glob('D:/work/*.gdb'))
        if not cands:
            raise SystemExit('请把 .gdb 路径作为第一个参数传进来')
        gdb_path = cands[0]
    bench(gdb_path, layer, limit)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
