#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""读路径热点剖析(cProfile 包装)。见 tools/bench_read.py 的分段计时。

用法::

    python tools/profile_read.py D:/work/xxx.gdb 图层名 [条数] [显示前N行]
"""
from __future__ import annotations

import cProfile
import glob
import os
import pstats
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
# tools/ 的上一级 = 仓库根(pyopenfilegdb 包在这儿);_HERE 留给同目录的 import。
sys.path[:0] = [os.path.dirname(_HERE), _HERE]

from pyopenfilegdb import OpenFileGDB  # noqa: E402


def main() -> int:
    gdb_path = sys.argv[1] if len(sys.argv) > 1 else None
    layer = sys.argv[2] if len(sys.argv) > 2 else None
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    top = int(sys.argv[4]) if len(sys.argv) > 4 else 25
    if gdb_path is None:
        cands = sorted(glob.glob('D:/work/*.gdb'))
        if not cands:
            raise SystemExit('请把 .gdb 路径作为第一个参数传进来')
        gdb_path = cands[0]

    with OpenFileGDB.open(gdb_path) as gdb:
        name = layer or gdb.list_feature_classes()[0]
        ly = gdb.get_layer(name)
        cap = limit or ly.record_count

        def run() -> None:
            n = 0
            for feat in ly.read_features(limit=cap):
                g = feat.geometry          # 强制惰性几何解出来
                if g is not None:
                    g.coordinates
                n += 1
            print(f'跑了 {n} 条')

        prof = cProfile.Profile()
        prof.enable()
        run()
        prof.disable()

    st = pstats.Stats(prof)
    st.sort_stats('tottime')
    print(f'--- 按自身耗时(tottime)前 {top} ---')
    st.print_stats(top)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
