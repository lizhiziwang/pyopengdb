#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""WKT 出口的 A/B:`wkt_seq`(C) vs 纯 Python 的 ``seq_of``。

量的就是 ``Geometry.wkt()`` 这一步 —— 它是"把几何导出去"(灌 PostgreSQL、
导 CSV)时最贵的一段,见 DESIGN.md §2.19.6。

用法::

    python tools/bench_wkt.py                     # 扫 D:/work/*.gdb
    python tools/bench_wkt.py D:/work/xxx.gdb 图层名
    python tools/bench_wkt.py D:/work/xxx.gdb 图层名 2000

⚠️ 每次计时都**重新读一遍**。``Geometry._ring_groups()`` 会缓存环组装,
拿同一批对象跑两遍,第二遍就白拿那笔时间(§2.19.6 记过这个坑)。
"""
from __future__ import annotations

import glob
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.dirname(_HERE), _HERE]

from pyopenfilegdb import OpenFileGDB               # noqa: E402
from pyopenfilegdb import _accel                    # noqa: E402

#: 每个 (图层, 配置) 跑几遍取最好的一遍。
REPEATS = 3


def one(layer, limit: int, enabled: bool) -> tuple:
    """返回 ``(wkt 秒数, 文本字节数, 读+解码秒数, 顶点数)``。

    ⚠️ **解码始终开着**,只把 WKT 出口切到 ``enabled`` —— 要证明的是"这个
    出口快了多少",解码那一头必须两边一模一样,否则量的是两件事之和。
    """
    with _accel.use(True):
        t0 = time.perf_counter()
        geoms = [f.geometry for f in layer.read_features(limit=limit)]
        t_read = time.perf_counter() - t0

    t0 = time.perf_counter()
    nbytes = 0
    with _accel.use(enabled):
        for g in geoms:
            if g is not None and not g.is_empty:
                nbytes += len(g.wkt())
    t_wkt = time.perf_counter() - t0

    nv = sum(sum(len(p) for p in g.xy_parts)
             for g in geoms if g is not None) // 2
    return t_wkt, nbytes, t_read, nv


def bench(layer, limit: int) -> None:
    # ⚠️ 只切 wkt_seq,不切解码:要对比的是"这个出口",别的都得一样。
    res = {}
    for tag, enabled in (('纯 Python', False), ('C (wkt_seq)', True)):
        best = None
        for _ in range(REPEATS):
            r = one(layer, limit, enabled)
            if best is None or r[0] < best[0]:
                best = r
        res[tag] = best
        t, nbytes, t_read, nv = best
        print(f'    {tag:12s} wkt() {t:7.3f} s   ({nv:,} 顶点, '
              f'{nbytes / 1e6:.1f} MB 文本, 读+解码 {t_read:.3f} s)')
    slow, fast = res['纯 Python'], res['C (wkt_seq)']
    print(f'    -> 快 {slow[0] / fast[0]:.2f}×  '
          f'(省 {slow[0] - fast[0]:.2f} s / {slow[0]:.1f} s)')


def main() -> int:
    args = sys.argv[1:]
    limit = int(args[2]) if len(args) > 2 else 2000
    cands = [args[0]] if args else sorted(glob.glob('D:/work/*.gdb'))
    if not _accel.HAS_ACCEL:
        print('⚠️ 没有 C 加速模块,两条路一样快。先跑 '
              '`python setup.py build_ext --inplace`。')
        return 2

    want = args[1] if len(args) > 1 else None
    for path in cands:
        try:
            gdb = OpenFileGDB.open(path)
        except Exception as e:                      # noqa: BLE001
            print(f'跳过 {os.path.basename(path)}: {e}')
            continue
        try:
            for name in gdb.list_feature_classes():
                if want and name != want:
                    continue
                ly = gdb.get_layer(name)
                if ly.table is None or ly.table.geom_field is None:
                    continue
                print(f'\n--- {os.path.basename(path)} / {name} '
                      f'(前 {limit} 条) ---')
                bench(ly, limit)
        finally:
            gdb.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
