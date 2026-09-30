#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""量 XY 增量 varint 的**字节长度分布**,用来决定怎么优化 ``_read_xy_array``。

背景:``feat.geometry`` 一旦被访问,几何就得逐顶点解 varint —— 实测占
几何解码总耗时的 72%。想提速就得先知道这些 varint 到底几个字节:

* 如果 **2 字节**占绝对多数,可以预建一张 ``65536`` 项的
  ``TAB[(b0<<8)|b1] -> delta*4 + 长度`` 表,一次下标取值解掉整个 varint,
  把内层 ``while`` 循环整个消掉;
* 如果 3 字节及以上占多数,这条表就白建了,只能退而求其次做循环展开。

所以先把分布量出来(本项目的老规矩:先量再写)。

用法::

    python tools/bench_varint.py                    # 扫 D:/work/*.gdb
    python tools/bench_varint.py D:/work/xxx.gdb 村行政区划
"""
from __future__ import annotations

import glob
import os
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
# tools/ 的上一级 = 仓库根(pyopenfilegdb 包在这儿);_HERE 留给同目录的 import。
sys.path[:0] = [os.path.dirname(_HERE), _HERE]

from pyopenfilegdb import OpenFileGDB                       # noqa: E402
from pyopenfilegdb._datatypes import _LazyGeometry          # noqa: E402


def scan_blob(blob: bytes, dist: Counter, hist: Counter) -> int:
    """扫一个几何 blob,统计每个 XY 增量 varint 的字节长度。

    复用 ``_esri_geometry`` 的头解析走到 XY 数组起点,然后自己走一遍
    varint —— 只判边界,不做累加,免得把要优化的代码抄第二遍。
    """
    from pyopenfilegdb import _esri_geometry as G
    from pyopenfilegdb._util import read_varuint

    n_pts = 0
    try:
        shape, pos = read_varuint(blob, 0)
    except Exception:
        return 0
    kind = shape & 0xFF
    # 只处理有 XY 数组的类型:POINT 不是数组,跳过
    if kind in (0, 1, 8, 9, 11, 21, 25, 31):        # NULL / POINT 家族
        return 0
    try:
        n_pts, pos = read_varuint(blob, pos)
        n_parts, pos = read_varuint(blob, pos)
        for _ in range(4):                           # 包围盒 4 个 varuint
            _, pos = read_varuint(blob, pos)
        if kind in (10, 13, 23, 28, 30):             # 带 nCurves 的类型
            _, pos = read_varuint(blob, pos)
        del n_parts
    except Exception:
        return 0

    end = len(blob)
    count = 0
    try:
        for _ in range(n_pts * 2):                   # X、Y 各 n_pts 个
            ln = 1
            while blob[pos] & 0x80:
                pos += 1
                ln += 1
                if ln > 9:
                    raise ValueError
            pos += 1
            dist[ln] += 1
            count += 1
    except (IndexError, ValueError):
        pass
    hist[min(count // 2, 12)] += 1
    return count


def main() -> int:
    cands = sys.argv[1:2] or sorted(glob.glob('D:/work/*.gdb'))
    only = sys.argv[2] if len(sys.argv) > 2 else None

    dist: Counter = Counter()
    total = 0
    for gdb_path in cands:
        try:
            gdb = OpenFileGDB.open(gdb_path)
        except Exception as e:
            print(f'跳过 {os.path.basename(gdb_path)}: {e}')
            continue
        for name in gdb.list_feature_classes():
            if only and name != only:
                continue
            ly = gdb.get_layer(name)
            if ly.table is None:
                continue
            n_here = 0
            for feat in ly.read_features():
                g = feat._geometry
                if not isinstance(g, _LazyGeometry):
                    continue
                blob = g._raw
                if blob is None:
                    blob = g._read_bytes(g._len)
                    g._raw = blob
                n_here += scan_blob(blob, dist, Counter())
            if n_here:
                print(f'  {name[:48]:48s} {n_here // 2:>9} 个 XY 增量')
            total += n_here
        gdb.close()
        if sys.argv[1:2]:
            break

    if not dist:
        print('没扫到几何')
        return 1

    n = sum(dist.values())
    print(f'\n共 {n:,} 个 XY 增量 varint(约 {total // 2:,} 个顶点)\n')
    print(f'{"字节数":>6} {"个数":>14} {"占比":>8}   累计')
    cum = 0
    for ln in sorted(dist):
        cum += dist[ln]
        bar = '#' * int(dist[ln] / n * 50)
        print(f'{ln:>6} {dist[ln]:>14,} {dist[ln] / n * 100:7.2f}%  '
              f'{cum / n * 100:7.2f}%  {bar}')
    print()
    le2 = (dist[1] + dist[2]) / n * 100
    le3 = (dist[1] + dist[2] + dist[3]) / n * 100
    print(f'≤2 字节占比 {le2:.2f}%   ≤3 字节占比 {le3:.2f}%')
    if le2 > 80:
        print('→ 2 字节查表能把绝大多数 varint 一次解掉,值得建表。')
    elif le3 > 80:
        print('→ 2 字节查表覆盖不住,得按 3 字节展开。')
    else:
        print('→ 长度分散,只能做循环展开,收益有限。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
