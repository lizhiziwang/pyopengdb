#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 ``feat.geometry`` 的单顶点成本拆开:varint 循环 vs 其余。

问题背景:循环体里加一句 ``g = feature.geometry``,`村行政区划` 从 0.42 s
涨到 59 s。几何是惰性解码的,碰一下就得逐顶点解 varint。这里在**真实 blob**
上量两档(``time.perf_counter`` 计时,不开 profiler):

A. 只解 XY 数组的 varint 循环 —— **元组版** ``_read_xy_array`` 的逐字复刻,
   **含**尾部 ``map/zip`` 浮点缩放与拼元组,但**不含**头解析/部件表/
   ``_strip_part_closures`` / ``Geometry`` 构造;
D. 完整 ``GdbTable._decode_geometry``。

D−A 就是"除了逐顶点 varint 之外"的全部开销,再看 cProfile 把它拆到函数。

⚠️ **本工具强制走纯 Python 路径**,理由见下。

关于 A 档测的是"元组版"循环
---------------------------
``_read_xy_array`` 现在填的是 ``array('d')``(§2.19.5 的 flat 容器),不再建
元组。A 档**故意**留着元组版的循环 —— 它量的是"换容器之前纯 Python 路径的
成本结构",§2.19.2 那张分解表讲的就是这件事。所以本工具报的 A 档**高于**
现在真实的纯 Python 回退;``bench_cext_varint.py`` 量的才是当前两条路。

关于 C 加速
----------
D 档调的是真身 ``tab._decode_geometry``,而 A 档的 ``xy_loop`` 是本文件里
**手抄的一份循环**。有 C 加速模块时 D 会被加速、A 不会,``D − A`` 直接变负,
"其余开销"那一行就成了垃圾数字。所以这里在**导入 pyopenfilegdb 之前**把
``PYOPENFILEGDB_NO_ACCEL`` 置上(``_accel`` 是导入期读环境变量),让 A/D 这
一对拆分保持它本来的含义 —— DESIGN.md §2.19.2 引的就是这套数。

C 那条路的收益归 ``tools/bench_cext_varint.py`` 管。

用法::

    python tools/bench_geom_split.py [gdb] [layer] [要素数]
"""
from __future__ import annotations

import cProfile
import io
import os
import pstats
import sys
import time
from itertools import repeat
from operator import add, truediv

# ⚠️ 必须在 import pyopenfilegdb 之前 —— _accel 在导入期读这个变量。
os.environ['PYOPENFILEGDB_NO_ACCEL'] = '1'

_HERE = os.path.dirname(os.path.abspath(__file__))
# tools/ 的上一级 = 仓库根(pyopenfilegdb 包在这儿);_HERE 留给同目录的 import。
sys.path[:0] = [os.path.dirname(_HERE), _HERE]

from pyopenfilegdb import OpenFileGDB                       # noqa: E402
from pyopenfilegdb import _esri_geometry as G               # noqa: E402
from pyopenfilegdb._datatypes import _LazyGeometry          # noqa: E402
from pyopenfilegdb._util import read_varuint                # noqa: E402
from pyopenfilegdb._constants import ShapeType as ST        # noqa: E402


def xy_loop(blob: bytes, pos: int, n_points: int, q, dx: int, dy: int,
            floats: bool = True):
    """``_read_xy_array`` 的逐字复刻(仅把 ``dr`` 换成裸 ``pos``)。

    ``floats=False`` 时只返回整数累加结果,用来量尾部 ``map/zip``
    浮点缩放 + 拼元组那一档的占比。
    """
    xs = []
    ys = []
    xa = xs.append
    ya = ys.append
    for _ in range(n_points):
        b = blob[pos]
        val = b & 0x3F
        neg = b & 0x40
        if b & 0x80:
            pos += 1
            shift = 6
            while True:
                b = blob[pos]
                pos += 1
                val |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
        else:
            pos += 1
        dx = dx - val if neg else dx + val
        xa(dx)

        b = blob[pos]
        val = b & 0x3F
        neg = b & 0x40
        if b & 0x80:
            pos += 1
            shift = 6
            while True:
                b = blob[pos]
                pos += 1
                val |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
        else:
            pos += 1
        dy = dy - val if neg else dy + val
        ya(dy)

    scale = q.xy_scale
    if not floats:
        return xs, ys
    return list(zip(map(add, map(truediv, xs, repeat(scale)), repeat(q.x_origin)),
                    map(add, map(truediv, ys, repeat(scale)), repeat(q.y_origin))))


def head(raw):
    """走到 XY 数组起点,返回 ``(n_points, x_pos)``;不适用则 ``None``。

    注意 part 的点数是分开的,这里只取第一个 part 的对齐信息,
    所以本工具对多 part 要素只近似 —— 用于定性拆分足够。
    """
    shape, pos = read_varuint(raw, 0)
    kind = shape & 0xFF
    if kind in (0, 1, 8, 9, 11, 21, 25, 31):
        return None
    n_pts, pos = read_varuint(raw, pos)
    if kind in ST._MULTIPOINT_ALL:
        for _ in range(4):
            _, pos = read_varuint(raw, pos)
        return n_pts, pos
    n_parts, pos = read_varuint(raw, pos)
    if kind in (10, 13, 23, 28, 30):
        _, pos = read_varuint(raw, pos)
    for _ in range(4):
        _, pos = read_varuint(raw, pos)
    first = n_pts
    for i in range(n_parts - 1):
        cnt, pos = read_varuint(raw, pos)
        if i == 0:
            first = cnt
    return first, pos


def main() -> int:
    gdb_path = sys.argv[1] if len(sys.argv) > 1 else 'D:/work/2024年国土行政区划.gdb'
    want = sys.argv[2] if len(sys.argv) > 2 else '村行政区划'
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else 3000

    ds = OpenFileGDB.open(gdb_path)
    ly = ds.get_layer(want)
    tab = ly.table

    blobs = []
    for feat in ly.read_features():
        g = feat._geometry
        if not isinstance(g, _LazyGeometry):
            continue
        blobs.append(g._raw if g._raw is not None else g._read_bytes(g._len))
        if len(blobs) >= limit:
            break
    ds.close()

    q = tab.geom_field and G._Quantizer(tab.geom_field)
    mb = sum(len(b) for b in blobs) / 1e6
    total_pts = 0
    for raw in blobs[:len(blobs)]:
        h = head(raw)
        if h:
            total_pts += h[0]
    print(f'{os.path.basename(gdb_path)} / {want}:{len(blobs)} 个 blob,{mb:.1f} MB')
    print(f'合计 {total_pts:,} 个顶点(多 part 要素这里计入偏少)\n')

    def bench(label, fn):
        best = None
        for _ in range(2):
            t0 = time.perf_counter()
            fn()
            dt = time.perf_counter() - t0
            if best is None or dt < best:
                best = dt
        print(f'{label:32s} {best:8.3f}s   {best / total_pts * 1e6:6.3f} µs/顶点')
        return best

    def run_a():
        for raw in blobs:
            h = head(raw)
            if h is None:
                continue
            n_pts, p = h
            xy_loop(raw, p, n_pts, q, 0, 0)

    def run_a_ints():
        for raw in blobs:
            h = head(raw)
            if h is None:
                continue
            n_pts, p = h
            xy_loop(raw, p, n_pts, q, 0, 0, floats=False)

    def run_d():
        for raw in blobs:
            tab._decode_geometry(raw)

    a_int = bench('A0 varint 循环(只累加整数)', run_a_ints)
    a = bench('A1 varint 循环(含浮点/拼元组)', run_a)
    d = bench('D  完整 _decode_geometry', run_d)

    pr = cProfile.Profile()
    pr.enable()
    run_d()
    pr.disable()
    s = io.StringIO()
    pstats.Stats(pr, stream=s).sort_stats('tottime').print_stats(10)
    print('\n--- D 的 cProfile ---')
    print(s.getvalue())

    print(f'varint 逐字节解码(A0)占完整解码 {a_int / d * 100:.0f}%;'
          f'尾部浮点缩放+拼元组(A1−A0) {a - a_int:.3f}s'
          f'({(a - a_int) / total_pts * 1e6:.3f} µs/顶点);')
    print(f'其余 {d - a:.3f}s({(d - a) / total_pts * 1e6:.3f} µs/顶点)'
          f' = 头解析 + 部件表 + 环闭合 + Geometry 构造')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
