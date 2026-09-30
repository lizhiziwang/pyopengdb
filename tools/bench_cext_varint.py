#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""C 加速模块 vs 纯 Python:几何 XY 数组解码的实测对照。

四个入口(``pyopenfilegdb._gdbaccel``)分两组:

* ``decode_xy_flat`` / ``decode_scalar_flat`` —— **库的热路径**走的就是它们,
  结果直接落进 ``array('d')``,不建任何逐点的 Python 对象。
* ``decode_xy`` / ``decode_scalar`` —— 建元组列表的老版本。**库里没有调用方**,
  留着纯粹是为了在这张表里把"建元组"和"填 array"两笔账分开量。

四个都必须与纯 Python 结果**逐位相同**才看耗时(正确性的正式闸门是
``tools/verify_accel.py``,这里只做一次快速对拍)。

⚠️ "varint 循环本身"这一笔账本来靠原型里的 ``decode_xy_ints``(只跑循环、
不建对象)量,那个入口**没有进库**。现在用差值替代:
``t_cf - t_flat`` 就是"把 double 包成 Python 对象"的过路费 —— 也正是
换 flat 容器的理由(见 DESIGN.md §2.19.5)。

用法::

    python tools/bench_cext_varint.py
    python tools/bench_cext_varint.py D:/work/xxx.gdb 村行政区划
"""
from __future__ import annotations

import os
import sys
import time
from array import array
from itertools import chain

_HERE = os.path.dirname(os.path.abspath(__file__))
# tools/ 的上一级 = 仓库根(pyopenfilegdb 包在这儿);_HERE 留给同目录的 import。
sys.path[:0] = [os.path.dirname(_HERE), _HERE]

from bench_varint_decoders import decode_loop, find_xy_region  # noqa: E402

from pyopenfilegdb import _gdbaccel                          # noqa: E402

#: `村行政区划` 全量顶点数。每个顶点 2 个 varint。
_TOTAL_VERTICES = 44_967_709
#: 该图层实测完整几何解码的耗时(见 DESIGN.md §2.19.2)。
_FULL_DECODE_S = 59.3
#: 整层里"与解码器无关"的那部分(头解析/部件表/环闭合/Geometry)。
_PY_BOOKKEEPING_S = 2.9


def bench(label, fn, reps=3):
    best = None
    for _ in range(reps):
        t = time.perf_counter()
        r = fn()
        dt = time.perf_counter() - t
        if best is None or dt < best:
            best, out = dt, r
    return best, out


def main() -> int:
    gdb_path = sys.argv[1] if len(sys.argv) > 1 else 'D:/work/2024年国土行政区划.gdb'
    want = sys.argv[2] if len(sys.argv) > 2 else '村行政区划'

    found = find_xy_region(gdb_path, want)
    if found is None:
        print('没找到顶点够多的要素,调小 min_pts')
        return 1
    name, n_pts, raw, start = found
    scale = 1e9
    ox = oy = 0.0
    print(f'{os.path.basename(gdb_path)} / {name}')
    print(f'基准字节流:{n_pts:,} 个点({n_pts * 2:,} 个 varint),'
          f'XY 数组从偏移 {start} 起\n')

    # ---------------- 正确性:逐位对拍 ----------------
    ref_xs, ref_ys, ref_pos, _, _ = decode_loop(raw, start, n_pts, 0, 0)
    ref_pts = [(x / scale + ox, y / scale + oy)
               for x, y in zip(ref_xs, ref_ys)]
    ref_flat = array('d', chain.from_iterable(ref_pts))

    got_pts, pos_c, dx_c, dy_c = _gdbaccel.decode_xy(
        raw, start, n_pts, scale, ox, oy, 0, 0)
    ok = (got_pts == ref_pts and pos_c == ref_pos
          and dx_c == (ref_xs[-1] if ref_xs else 0)
          and dy_c == (ref_ys[-1] if ref_ys else 0))
    print(f'[decode_xy] 结束位置 {pos_c} vs {ref_pos} '
          f'{"✓" if pos_c == ref_pos else "✗"}   元组列表 '
          f'{"逐位相同 ✓" if got_pts == ref_pts else "✗ 有差异!"}')
    if not ok:
        for i, (a, b) in enumerate(zip(ref_pts, got_pts)):
            if a != b:
                print(f'  第 {i} 个点:{a!r} vs {b!r}')
                break
        return 1

    got_flat, pos_f, dx_f, dy_f = _gdbaccel.decode_xy_flat(
        raw, start, n_pts, scale, ox, oy, 0, 0)
    ok_f = (isinstance(got_flat, array) and got_flat == ref_flat
            and pos_f == ref_pos and dx_f == dx_c and dy_f == dy_c)
    print(f'[decode_xy_flat] 结束位置 {pos_f} vs {ref_pos} '
          f'{"✓" if pos_f == ref_pos else "✗"}   array(\'d\') 交错 '
          f'{"逐位相同 ✓" if got_flat == ref_flat else "✗ 有差异!"}')
    if not ok_f:
        for i, (a, b) in enumerate(zip(ref_flat, got_flat)):
            if a != b:
                print(f'  第 {i} 个值:{a!r} vs {b!r}')
                break
        return 1
    del ref_xs, ref_ys, got_pts, ref_flat, got_flat

    # ---------------- 计时 ----------------
    print()
    t_py, _ = bench('纯 Python  while 循环 + 浮点/拼元组',
                    lambda: decode_loop(raw, start, n_pts, 0, 0))
    t_cf, _ = bench('C  decode_xy(含浮点/拼元组)',
                    lambda: _gdbaccel.decode_xy(
                        raw, start, n_pts, scale, ox, oy, 0, 0))
    t_fl, _ = bench('C  decode_xy_flat(含浮点,不建对象)',
                    lambda: _gdbaccel.decode_xy_flat(
                        raw, start, n_pts, scale, ox, oy, 0, 0))
    # Python 那版的浮点/拼元组尾巴,单独量
    xs, ys, _, _, _ = decode_loop(raw, start, n_pts, 0, 0)
    t_tail, _ = bench('  └ 其中 Python 浮点/拼元组尾巴',
                      lambda: [(x / scale + ox, y / scale + oy)
                               for x, y in zip(xs, ys)])

    nv = n_pts * 2
    for label, dt in (('纯 Python 循环(不含尾巴)', t_py - t_tail),
                      ('Python 浮点/拼元组尾巴', t_tail),
                      ('纯 Python 合计', t_py),
                      ('C 含浮点/拼元组(旧路径)', t_cf),
                      ('C 含浮点/填 array(热路径)', t_fl)):
        print(f'  {label:28s} {dt:8.4f}s  {dt / nv * 1e9:7.1f} ns/varint')

    print(f'\n纯 Python / 热路径 = {t_py / t_fl:.1f}×')
    print(f'建 Python 对象的过路费(C 两条路之差)= {t_cf - t_fl:.4f}s'
          f'  ({(t_cf - t_fl) / nv * 1e9:.1f} ns/值)')

    n_all = _TOTAL_VERTICES * 2
    print(f'\n按 村行政区划 {_TOTAL_VERTICES:,} 顶点 = {n_all:,} 个 varint 折算:')
    print(f'  解码(两条路各自)  纯 Python {t_py / nv * n_all:7.1f}s'
          f'  →  热路径 {t_fl / nv * n_all:6.2f}s')
    print(f'  与解码器无关的那部分(头解析/部件表/环闭合/Geometry)'
          f'  {_PY_BOOKKEEPING_S:.1f}s')

    # ---------------- 小数组:真实 part 的量级 ----------------
    print('\n--- 小数组(真实 part 常见量级)的每调用开销 ---')
    for small in (200, 1000, 2000, 5000):
        if small > n_pts:
            continue
        bp, _ = bench(f'  {small} 点 纯 Python', lambda: decode_loop(
            raw, start, small, 0, 0), reps=5)
        bc, _ = bench(f'  {small} 点 C', lambda: _gdbaccel.decode_xy_flat(
            raw, start, small, scale, ox, oy, 0, 0), reps=5)
        print(f'  {small:>5} 点:{bp * 1e6:9.1f} vs {bc * 1e6:8.1f} µs'
              f'   快 {bp / bc:5.1f}×')

    total_c = t_fl / nv * n_all + _PY_BOOKKEEPING_S
    print(f'\n整层估计:开着加速约 {total_c:.1f}s,'
          f'相对纯 Python 的 {_FULL_DECODE_S}s 快 {_FULL_DECODE_S / total_c:.1f}×')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
