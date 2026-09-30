#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 XY 增量 varint 解码用 numpy 向量化,和纯 Python 版比。

⚠️ **这不是本库的实现,是可行性实测。** 原任务约束写明"不要任何 C 扩展库"
(numpy 正是 C 扩展),所以下面这段只能待在 `tools/` 里当参考,不能进
``pyopenfilegdb/``。

思路(为什么这条能向量化)
------------------------
纯 Python 逐字节慢,是因为每个字节都要过一次解释器。用 numpy 可以把
**除累加之外的全部**放进 C 层:

1. **定边界。** varint 的**最后一个**字节是 bit7 = 0 的那个(续字节 bit7 = 1)。
   于是 ``flatnonzero((arr & 0x80) == 0)`` 一次就拿到所有 varint 的**结束**
   位置,前一个结束 +1 就是下一个的开始 —— 不需要知道长度就能定界。
2. **拼值。** 第 j 个字节对第 i 个 varint 的贡献是
   ``(arr[starts_i + j] & 0x7F) << (6 + 7*(j-1))``,首字节低 6 位、bit6 是符号位。
   按 j 循环(实测最多 6 层),每层一次 gather + 位运算,全是整数组并行。
   短 varint 的位置用 ``j < lengths`` 掩掉,不影响别人。
3. **累加。** 增量累加是前缀和 ``np.cumsum`` —— 恰好是 numpy 的核心操作,
   一次 C 循环搞定,连 X/Y 分开累加都不用特意写(步长切片即可)。
4. **缩放。** ``dx / scale + ox`` 也是整组一次,顺手把那 23% 的浮点尾巴
   一起吃掉了。

⚠️ 关键正确性前提:三步都不能改运算顺序 —— 拼位用同一套 ``& 0x3F`` /
``& 0x40`` / ``& 0x7F`` / 起点 6 步进 7;缩放仍是 ``dx/scale + origin``
(不是 ``dx*(1/scale)``,那会差最后一位);累加器是 int64(对应 GDAL 的
``GIntBig``)。下面逐一与纯 Python 结果对拍。

用法::

    python tools/bench_numpy_varint.py
    python tools/bench_numpy_varint.py D:/work/xxx.gdb 村行政区划
"""
from __future__ import annotations

import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
# tools/ 的上一级 = 仓库根(pyopenfilegdb 包在这儿);_HERE 留给同目录的 import。
sys.path[:0] = [os.path.dirname(_HERE), _HERE]

from bench_varint_decoders import decode_loop, find_xy_region  # noqa: E402

try:
    import numpy as np
except ImportError:
    print('没装 numpy,本工具跳过。')
    raise SystemExit(0)


#: `村行政区划` 全量顶点数。⚠️ 每个顶点 **2** 个 varint(X、Y 各一),
#: 折算 varint 数时要乘 2 —— 差这个 2 倍会把结论吹大一倍。
_TOTAL_VERTICES = 44_967_709


def _find_ends(arr: np.ndarray, start: int, m: int) -> np.ndarray:
    """找到前 ``m`` 个 varint 的**末字节**下标。

    ⚠️ 不能一上来就 ``flatnonzero(arr[start:])`` —— 那是 O(剩余字节),
    而调用方(每个 part 一次)只要 O(实际消耗字节)。真实 part 才一千来个点,
    整块扫一遍会让每调用的固定开销高到把收益全吃掉(实测 1000 点时反而变慢)。

    所以按"估一个窗口、不够就翻倍"来扫。实测均值 ~3.2 B/varint,
    取 4 B 起步,期望只要扫实际用量的 ~1.25 倍。
    """
    win = max(256, m * 4)
    while True:
        hi = min(arr.size, start + win)
        ends = np.flatnonzero((arr[start:hi] & 0x80) == 0)
        if ends.size >= m:
            return ends[:m].astype(np.int64) + start
        if hi >= arr.size:
            raise ValueError('varint 数量不足')
        win *= 2


def np_decode(arr: np.ndarray, start: int, n_points: int, scale: float,
              ox: float, oy: float):
    """向量化解 ``n_points`` 个点(``2*n_points`` 个 varint)。

    :param arr: ``np.frombuffer(blob, np.uint8)``,必须是**整个** blob,
        因为要看 bit7 定边界。
    :returns: ``(list[(x, y)], 结束位置)``;结束位置是最后一个 varint 的
        末字节下标 + 1。
    """
    m = n_points * 2

    # ---- 1. 定边界:bit7 = 0 的字节是 varint 的末字节 ----
    ends = _find_ends(arr, start, m)

    starts = np.empty(m, np.int64)
    starts[0] = start
    np.add(ends[:-1], 1, out=starts[1:])         # 上一个结束 + 1
    lengths = ends - starts + 1
    max_len = int(lengths.max())

    # 尾部补零:j 循环会对短 varint 越界取到后面字节,靠掩码丢掉,
    # 但下标本身不能越界。
    need = int(starts[-1]) + max_len
    if need > arr.size:
        arr = np.concatenate([arr, np.zeros(need - arr.size, np.uint8)])

    # ---- 2. 拼值 ----
    b0 = arr[starts]
    w = (b0 & 0x3F).astype(np.int64)             # 首字节:低 6 位是幅度
    neg = (b0 & 0x40) != 0                       # bit6 是符号位
    for j in range(1, max_len):
        shift = 6 + 7 * (j - 1)                  # 与 GDAL 一致:6 起步进 7
        idx = starts + j
        contrib = np.where(j < lengths, arr[idx] & 0x7F, 0).astype(np.int64)
        w |= contrib << shift

    # ---- 3. 符号 + 前缀和(X/Y 各一条)----
    vals = np.where(neg, -w, w)
    dx = np.cumsum(vals[0::2])
    dy = np.cumsum(vals[1::2])

    # ---- 4. 缩放还原 ----
    xs = dx / scale + ox
    ys = dy / scale + oy
    return list(zip(xs.tolist(), ys.tolist())), int(ends[-1]) + 1


def main() -> int:
    gdb_path = sys.argv[1] if len(sys.argv) > 1 else 'D:/work/2024年国土行政区划.gdb'
    want = sys.argv[2] if len(sys.argv) > 2 else ''

    found = find_xy_region(gdb_path, want)
    if found is None:
        print('没找到顶点够多的要素,调小 min_pts')
        return 1
    name, n_pts, raw, start = found
    scale = 1e9          # 真实量化参数,由调用方传;这里用典型值
    print(f'{os.path.basename(gdb_path)} / {name}')
    print(f'基准字节流:{n_pts:,} 个点({n_pts * 2:,} 个 varint),'
          f'XY 数组从偏移 {start} 起\n')

    arr = np.frombuffer(raw, dtype=np.uint8)

    # ---- 正确性:逐位对拍 ----
    # 纯 Python 版返回整数累加值;两侧按同一公式 ``v / scale + origin`` 还原
    # 成浮点再比 —— 也就是 `_read_xy_array` 尾部那一趟干的事。
    t0 = time.perf_counter()
    ref_xs, ref_ys, pos_ref, _dx, _dy = decode_loop(raw, start, n_pts, 0, 0)
    t_py = time.perf_counter() - t0
    ref = [(x / scale + 0.0, y / scale + 0.0) for x, y in zip(ref_xs, ref_ys)]

    t0 = time.perf_counter()
    got, pos_np = np_decode(arr, start, n_pts, scale, 0.0, 0.0)
    t_np = time.perf_counter() - t0

    print(f'varint 消耗到偏移:纯 Python {pos_ref},numpy {pos_np} '
          f'{"✓" if pos_ref == pos_np else "✗ 不一致!"}')
    identical = ref == got
    print(f'结果逐位相同:{identical}'
          f'{"  ✓" if identical else "  ✗ 有差异!"}')
    if not identical:
        for i, (a, b) in enumerate(zip(ref, got)):
            if a != b:
                print(f'  第 {i} 个点:{a!r} vs {b!r}')
                break
        return 1
    del ref, got
    print(f'(单跑一次:纯 Python {t_py:.3f}s,numpy {t_np:.3f}s)\n')

    # ---- 计时 ----
    def bench(label, fn, reps=3):
        best = None
        for _ in range(reps):
            t = time.perf_counter()
            fn()
            dt = time.perf_counter() - t
            if best is None or dt < best:
                best = dt
        u = best / (n_pts * 2) * 1e6
        print(f'{label:34s} {best:8.3f}s   {u:6.3f} µs/varint')
        return best

    py = bench('纯 Python  while 循环(现状)', lambda: decode_loop(raw, start, n_pts, 0, 0))
    nv = bench('numpy 向量化', lambda: np_decode(arr, start, n_pts, scale, 0.0, 0.0))

    n_delta = _TOTAL_VERTICES * 2
    u_py = py / (n_pts * 2)
    u_np = nv / (n_pts * 2)
    print(f'\n单看 varint 循环:numpy 快 {py / nv:.1f}×')
    print(f'\n按 村行政区划 {_TOTAL_VERTICES:,} 顶点 = {n_delta:,} 个 varint 折算')
    print(f'(该图层实测完整解码 59.3 s):')
    print(f'  varint 循环  纯 Python {u_py * n_delta:6.1f}s  →  numpy {u_np * n_delta:6.1f}s')
    print(f'\n⚠️ 但换个解码器不等于整层变快。剩下的账(与解码器无关,'
          f'只能按顶点数折算):')
    print(f'  物化成 Python 元组(API 要 list[(float,float)])  ~6.6s / 44.95M 顶点')
    print(f'  头解析+部件表+环闭合+Geometry                    ~2.9s(0.064 µs/顶点)')
    print(f'  → numpy 版整层估计 {u_np * n_delta + 6.6 + 2.9:.0f}s,'
          f'相对现在 59.3s 约 {59.3 / (u_np * n_delta + 6.6 + 2.9):.1f}×')

    # ---- 小数组的每调用开销 ----
    print('\n--- 小数组(真实 part 常见量级)的每调用开销 ---')
    for small in (1000, 2000, 5000):
        if small > n_pts:
            continue
        def run_py():
            decode_loop(raw, start, small, 0, 0)
        def run_np():
            np_decode(arr, start, small, scale, 0.0, 0.0)
        bp = bench(f'  {small} 点 纯 Python', run_py, reps=5)
        bn = bench(f'  {small} 点 numpy', run_np, reps=5)
        print(f'    → 每调用 {bp * 1e6:7.1f} vs {bn * 1e6:7.1f} µs,'
              f'numpy 快 {bp / bn:.1f}×')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
