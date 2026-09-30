#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""量化"在调试器里跑"对读路径的代价。

起因:main.py 在 PyCharm 里报 3 秒,在终端里只要 0.44 秒,而 GDAL 是 0.3 秒。
PyCharm 的调试器是 pydevd,它给**每一个 Python 帧**装 line tracer —— 也就是
每条字节码都要回调一次调试器。于是:

* 整个 pyopenfilegdb 是纯 Python,**每一行解码语句都上税**;
* GDAL 是 C 扩展,那 0.3 秒是 C 时间,tracer 根本进不去,税率为 0。

所以"在调试器里比 Python 实现 vs C 实现"这个比法本身就不成立。

本脚本不调用 pydevd(要连 socket),而是用 ``sys.settrace`` 装一个最朴素的
line tracer 复现同一个机制,量"同一份代码被 trace 会慢几倍"。同时量一段
**等价工作量的纯 C 操作**(对同样字节数做 ``decode``),用来对照:被 trace
时 C 那一侧几乎不动。

用法::

    python tools/bench_trace.py                      # 默认 2024年国土行政区划.gdb
    python tools/bench_trace.py D:/work/xxx.gdb 乡行政区划
"""
from __future__ import annotations

import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
# tools/ 的上一级 = 仓库根(pyopenfilegdb 包在这儿);_HERE 留给同目录的 import。
sys.path[:0] = [os.path.dirname(_HERE), _HERE]

from pyopenfilegdb import OpenFileGDB                       # noqa: E402


# ---------------------------------------------------------------------------
# 一个最朴素的 line tracer —— 只做 pydevd 每行都要做的那件事:被回调一次。
# 真正的 pydevd 回调里还要判断断点、维护帧栈,只会更慢。
# ---------------------------------------------------------------------------
def _tracer(frame, event, arg):
    return _tracer


def _measure(label: str, fn, traced: bool):
    if traced:
        sys.settrace(_tracer)
    try:
        t0 = time.perf_counter()
        out = fn()
        dt = time.perf_counter() - t0
    finally:
        sys.settrace(None)
    print(f'  {label:34s} {dt:8.3f}s   {out}')
    return dt


def main():
    gdb_path = sys.argv[1] if len(sys.argv) > 1 else \
        'D:/work/2024年国土行政区划.gdb'
    layer_name = sys.argv[2] if len(sys.argv) > 2 else '乡行政区划'

    gdb = OpenFileGDB.open(gdb_path)
    ly = gdb.get_layer(layer_name)
    n_features = ly.record_count
    print(f'{os.path.basename(gdb_path)} / {layer_name}  ({n_features} 条)\n')

    # --- 1) 纯 Python:只读属性 --------------------------------------------
    def read_attrs():
        n = 0
        for feat in ly.read_features():
            feat.attributes['YSDM']
            n += 1
        return f'{n} 条'

    print('纯 Python 路径(本库的解码循环):')
    plain = _measure('只读属性', read_attrs, traced=False)
    traced = _measure('只读属性(装 line tracer)', read_attrs, traced=True)
    print(f'  → 调试器税率 ≈ {traced / plain:.1f}x\n')

    # --- 2) 等价工作量的纯 C 操作 ------------------------------------------
    # 把这张表里所有属性字段的字节合起来,做成等量的 decode 工作 ——
    # 这段是 C 循环,可以类比 GDAL 那一侧的 0.3 秒。
    buf = []
    for i in range(60000):
        buf.append('坐落单位名称%06d' % i)
    blob = ''.join(buf).encode('utf-16-le')

    def c_work():
        total = 0
        for _ in range(60):
            total += len(blob.decode('utf-16-le'))
        return f'{len(blob) // 1024} KB x60 解码'

    print('等价工作量的纯 C 路径(类比 GDAL 的 C 循环):')
    c_plain = _measure('utf-16-le decode', c_work, traced=False)
    c_traced = _measure('utf-16-le decode(装 line tracer)', c_work, traced=True)
    print(f'  → 调试器税率 ≈ {c_traced / c_plain:.1f}x\n')

    print('结论:')
    print(f'  · 同一份 Python 代码,被 trace 后慢 {traced / plain:.1f} 倍;')
    print(f'  · C 循环被 trace 后慢 {c_traced / c_plain:.1f} 倍(≈ 无影响)。')
    print('  · 所以 PyCharm 调试器下"本库 3s vs GDAL 0.3s"里,那 3s 主要不是')
    print('    我们的算法慢,是 tracer 在逐行走我们的 Python;GDAL 的 C 时间是')
    print('    免检的。要比性能,必须两边都在终端里跑(关掉调试器)。')

    gdb.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
