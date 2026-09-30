#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""量「跳过几何字节」到底省了多少 IO。

对比两条路径读同一个 .gdbtable:

* **老**:每条记录读 ``4 + 记录体长度`` 字节(等价于 GDAL 的
  ``FileGDBTable::SelectRow`` —— 它一次 ``VSIFReadL`` 把整条记录(含几何)
  读进来,不做任何跳过)。
* **新**:``iter_rows()`` -> ``_read_record_lazy``,只读几何 blob 前后两段。

用法::

    python tools/bench_geom_skip.py                      # 扫 D:/work/*.gdb
    python tools/bench_geom_skip.py D:/work/xxx.gdb
"""
from __future__ import annotations

import builtins
import glob
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
# tools/ 的上一级 = 仓库根(pyopenfilegdb 包在这儿);_HERE 留给同目录的 import。
sys.path[:0] = [os.path.dirname(_HERE), _HERE]

import pyopenfilegdb._gdbtable as _gt                       # noqa: E402
from pyopenfilegdb import OpenFileGDB                       # noqa: E402

_real_open = builtins.open


class _Counting:
    """给读路径的文件对象记字节数。"""

    def __init__(self, f) -> None:
        self._f = f
        self.n = 0

    def seek(self, *a):
        return self._f.seek(*a)

    def read(self, size=-1):
        b = self._f.read(size)
        self.n += len(b)
        return b

    def __getattr__(self, k):
        return getattr(self._f, k)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return self._f.__exit__(*a)


def old_bytes(tab):
    """老路径:整条读,4 + 长度。"""
    total = 0
    with _real_open(tab.path, 'rb') as f:
        for row in range(tab.tablx.record_count):
            off = tab.tablx.offset_for_row(row)
            if not off:
                continue
            f.seek(off)
            ln = int.from_bytes(f.read(4), 'little')
            if ln & 0x80000000:
                continue
            total += 4 + ln
    return total


def new_bytes(tab):
    """新路径:跑一遍 iter_rows,数它读了多少字节。"""
    holder = []

    def fake_open(*a, **k):
        holder.append(_Counting(_real_open(*a, **k)))
        return holder[-1]

    _gt.open = fake_open
    try:
        n = sum(1 for _ in tab.iter_rows())
    finally:
        _gt.open = _real_open
    return (holder[0].n if holder else 0), n


def main():
    cands = sys.argv[1:] or sorted(glob.glob('D:/work/*.gdb'))
    for gdb_path in cands:
        try:
            gdb = OpenFileGDB.open(gdb_path)
        except Exception as e:
            print(f'跳过 {os.path.basename(gdb_path)}: {e}')
            continue
        print(f'\n=== {os.path.basename(gdb_path)} ===')
        for name in gdb.list_feature_classes():
            ly = gdb.get_layer(name)
            tab = ly.table
            if tab is None or tab.tablx is None:
                continue
            t0 = time.perf_counter()
            nb, n = new_bytes(tab)
            dt = time.perf_counter() - t0
            ob = old_bytes(tab)
            ratio = f'{ob / nb:8.1f}x' if nb else '     n/a'
            print(f'  {name:14s} 记录={n:6d}  '
                  f'老 {ob / 1e6:8.1f} MB -> 新 {nb / 1e6:7.3f} MB  '
                  f'{ratio}   迭代 {dt:6.3f}s')
        gdb.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
