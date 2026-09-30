#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""量 ``main.py`` 那种遍历循环到底吃多少内存,以及**有没有在涨**。

为什么不能只看任务管理器
----------------------
任务管理器给的是一个数(RSS),但它回答不了这个循环最重要的那个问题:
**随着要素一条条过去,占用是"平的"还是"一路涨"**。本库是流式 + 惰性几何
(见 DESIGN.md §2.19),循环体里 ``g = feature.geometry`` 每轮都被重新绑定,
所以**理论上是平的** —— 只有最后一条要素的几何活着。一旦实测是涨的,就说明
有东西把每一条的几何/记录体留住了(缓存、闭包、生成器没释放……),这是拿
任务管理器看不出来的。

所以本工具给两样东西:

1. **RSS 曲线**(进程真实占用,含 C 分配的 ``array('d')`` 缓冲区);
   开头 / 结尾 / 峰值,并且中途**抽样**,能看出趋势;
2. **``tracemalloc`` 的 Python 侧分配排行**,能量到"是谁占的、在哪一行"
   (RSS 只说"占了多少")。

Windows 上取 RSS 用 ``ctypes`` 直接调 ``psapi.GetProcessMemoryInfo`` —— 标准库,
不需要装 psutil(仓库的 3.11.9 ``.venv`` 里本来也没有 psutil),
也不用 ``resource``(那是 Unix-only,Windows 上 import 就报错)。

用法::

    python tools/mem_read.py                          # D:/work/2024年国土行政区划.gdb 村行政区划
    python tools/mem_read.py D:/work/xxx.gdb 图层名
    python tools/mem_read.py --no-geom                # 对照组:只读属性,不碰几何
    python tools/mem_read.py --trace                  # 额外开 tracemalloc(会明显变慢)

⚠️ ``--trace`` 是**诊断**模式:``tracemalloc`` 会给每次分配记账,遍历会慢好几倍,
而且它自己也要吃内存,读到的绝对值偏高。看**趋势**和**排行**可以,别拿它报绝对数。
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
# tools/ 的上一级 = 仓库根(pyopenfilegdb 包在这儿);_HERE 留给同目录的 import。
sys.path[:0] = [os.path.dirname(_HERE), _HERE]

DEFAULT_GDB = 'D:/work/2024年国土行政区划.gdb'
DEFAULT_LAYER = '村行政区划'
#: 每多少条要素采一次样。
SAMPLE_EVERY = 1000


# ----------------------------------------------------------------------
# 进程内存(标准库 ctypes -> psapi,只在 Windows 上有)
# ----------------------------------------------------------------------
class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ('cb', wt.DWORD),
        ('PageFaultCount', wt.DWORD),
        ('PeakWorkingSetSize', ctypes.c_size_t),
        ('WorkingSetSize', ctypes.c_size_t),
        ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
        ('QuotaPagedPoolUsage', ctypes.c_size_t),
        ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
        ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
        ('PagefileUsage', ctypes.c_size_t),
        ('PeakPagefileUsage', ctypes.c_size_t),
    ]


def _load_psapi():
    if sys.platform != 'win32':
        return None
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [
        wt.HANDLE, ctypes.POINTER(_PROCESS_MEMORY_COUNTERS), wt.DWORD]
    psapi.GetProcessMemoryInfo.restype = wt.BOOL
    kernel32.GetCurrentProcess.restype = wt.HANDLE
    return psapi, kernel32


_PSAPI = _load_psapi()


def rss() -> tuple:
    """返回 ``(当前工作集, 峰值工作集)``,单位字节。非 Windows 返回 ``(0, 0)``。

    "工作集"就是任务管理器那一列(``Working Set``):进程当前真正占着的物理
    内存。``PeakWorkingSetSize`` 是**进程启动至今**的历史峰值 —— 注意它不
    只覆盖这段循环,import 阶段也会算进去,所以它只会 >= 循环里的峰值。
    """
    if _PSAPI is None:
        return 0, 0
    psapi, kernel32 = _PSAPI
    c = _PROCESS_MEMORY_COUNTERS()
    c.cb = ctypes.sizeof(c)
    if not psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(),
                                      ctypes.byref(c), c.cb):
        return 0, 0
    return c.WorkingSetSize, c.PeakWorkingSetSize


def mb(n: int) -> str:
    return f'{n / 1048576:9.1f} MB'


# ----------------------------------------------------------------------
def run(gdb_path: str, layer_name: str, with_geom: bool, trace: bool) -> int:
    import gc

    if trace:
        import tracemalloc
        tracemalloc.start(10)          # 留 10 层栈,够定位到行

    from pyopenfilegdb import OpenFileGDB, HAS_ACCEL

    ds = OpenFileGDB(gdb_path)
    ly = ds.get_layer(layer_name)

    base, peak_at_start = rss()
    print(f'库:{os.path.basename(gdb_path)} / {layer_name}')
    print(f'加速:{HAS_ACCEL}   几何:{"开" if with_geom else "关(对照组)"}')
    print(f'\n打开之后(还没开始遍历)  当前 {mb(base)}   '
          f'进程历史峰值 {mb(peak_at_start)}')
    print(f'\n{"条数":>8}  {"当前":>12}  {"相对基线":>12}  {"耗时":>8}')
    print('-' * 48)

    n = 0
    peak_during = base
    t0 = time.perf_counter()
    for feature in ly.read_features():
        feature.attributes['ZLDWDM']
        if with_geom:
            g = feature.geometry       # 与 main.py 逐字一致
        n += 1
        if n % SAMPLE_EVERY == 0:
            cur, _ = rss()
            peak_during = max(peak_during, cur)
            print(f'{n:>8,}  {mb(cur)}  {mb(cur - base)}  '
                  f'{time.perf_counter() - t0:7.2f}s')
    dt = time.perf_counter() - t0

    cur, peak_hist = rss()
    peak_during = max(peak_during, cur)
    print('-' * 48)
    print(f'{n:>8,}  {mb(cur)}  {mb(cur - base)}  {dt:7.2f}s')

    print(f'\n=== 结论 ===')
    print(f'遍历中当前占用   {mb(cur)}')
    print(f'遍历中峰值       {mb(peak_during)}   (相对基线 '
          f'{mb(peak_during - base)})')
    print(f'进程历史峰值     {mb(peak_hist)}')
    print(f'收尾比峰值回落   {mb(peak_during - cur)}')
    # "平不平"看这一行:流式 + 惰性几何的口径下,当前占用应当很快稳定,
    # 不随要素条数线性增长。涨 = 有东西把历史要素留住了。
    if n and (cur - base) > 0:
        print(f'平均每条净增     {(cur - base) / n:9.1f} B/条'
              f'   (若几何真被留住,量级会是每个顶点几十字节)')

    if trace:
        snap = tracemalloc.take_snapshot()
        tracemalloc.stop()
        print(f'\n=== tracemalloc:Python 侧分配排行(前 12) ===')
        print('⚠️ 诊断模式,trace 自身有开销,绝对值偏高,看排行不看总数。')
        for stat in snap.statistics('lineno')[:12]:
            print(f'  {stat.size / 1048576:8.2f} MB  {stat.count:>9,} 块  '
                  f'{stat.traceback[0].filename}:{stat.traceback[0].lineno}')

    ds.close()
    return 0


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    with_geom = '--no-geom' not in sys.argv
    trace = '--trace' in sys.argv
    gdb_path = args[0] if args else DEFAULT_GDB
    layer_name = args[1] if len(args) > 1 else DEFAULT_LAYER
    if not os.path.exists(gdb_path):
        print(f'找不到 {gdb_path}')
        return 2
    return run(gdb_path, layer_name, with_geom, trace)


if __name__ == '__main__':
    raise SystemExit(main())
