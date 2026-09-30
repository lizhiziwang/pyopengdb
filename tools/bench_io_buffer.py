#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""选 .gdbtable 迭代时的缓冲区大小。

为什么要专门挑:``open()`` 默认给的是 ``BufferedReader``(8 KB)。记录在
``.gdbtable`` 里不连续,每条都要 ``seek`` —— 而 ``seek`` 会把缓冲区作废,
于是每次 ``read`` 都会真实地拖一个缓冲区那么大的一坨。跳读几何之后我们
每次只要几十~几百字节,8 KB 的预读就全成了白读。

所以缓冲区**不是越大越好**,得跟"每条记录实际要读的字节数"对得上。
这个脚本就是把每个候选值都跑一遍,看耗时。
"""
from __future__ import annotations

import builtins
import io
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
# tools/ 的上一级 = 仓库根(pyopenfilegdb 包在这儿);_HERE 留给同目录的 import。
sys.path[:0] = [os.path.dirname(_HERE), _HERE]

import pyopenfilegdb._gdbtable as _gt                       # noqa: E402
from pyopenfilegdb import OpenFileGDB                       # noqa: E402

_real_open = builtins.open


class _Raw:
    """``buffering=0`` 的裸文件;``read(n)`` 是真 syscall,可能短读,补足。"""

    def __init__(self, f) -> None:
        self._f = f
        self.n = 0

    def seek(self, *a):
        return self._f.seek(*a)

    def read(self, size=-1):
        if size < 0:
            size = 1 << 30
        out = b''
        while len(out) < size:
            b = self._f.read(size - len(out))
            if not b:
                break
            out += b
        self.n += len(out)
        return out

    def __getattr__(self, k):
        return getattr(self._f, k)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return self._f.__exit__(*a)


def main():
    gdb_path = sys.argv[1] if len(sys.argv) > 1 else \
        'D:/work/2024年国土行政区划.gdb'
    layer = sys.argv[2] if len(sys.argv) > 2 else '村行政区划'

    gdb = OpenFileGDB.open(gdb_path)
    tab = gdb.get_layer(layer).table
    rows = tab.tablx.record_count
    print(f'{os.path.basename(gdb_path)} / {layer}  ({rows} 条)\n')
    print(f'{"buffering":>10} {"耗时":>10}   备注')

    for bs in (0, 64, 128, 256, 512, 1024, 2048, 8192):
        if bs == 0:
            def fake_open(p, *a, **k):
                return _Raw(io.FileIO(p, 'rb'))
            note = '裸读,无预读(每次 read 都是 syscall)'
        else:
            def fake_open(p, *a, _b=bs, **k):
                return _real_open(p, 'rb', buffering=_b)
            note = '默认' if bs == 8192 else f'预读 {bs} B/次'

        _gt.open = fake_open
        try:
            t0 = time.perf_counter()
            n = sum(1 for _ in tab.iter_rows())
            dt = time.perf_counter() - t0
        finally:
            _gt.open = _real_open
        print(f'{bs:>10} {dt:10.3f}s   {note}   (n={n})')

    gdb.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
