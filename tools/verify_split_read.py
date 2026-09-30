#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""对照验证:_read_record_lazy(跳过几何字节)vs 整条读,结果必须完全一致。

老路径:整条记录读进来 -> ``_decode_row_blob(lazy_geom=True)``
新路径:``iter_rows()`` -> ``_read_record_lazy``(只读几何前后两段)

比对口径:

* **属性**:逐条逐字段比(便宜,全量比)。
* **几何 blob 长度**:逐条比 —— 偏移算错的话这里第一个就露馅。
* **几何 WKT**:抽样比(解几何是 1.35 µs/顶点,全量比要几分钟),默认前
  200 条 + 之后每 500 条抽 1 条。

顺带统计新路径每条记录实际从盘上读了多少字节,和老路径对比。
"""
from __future__ import annotations

import builtins
import glob
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
# tools/ 的上一级 = 仓库根(pyopenfilegdb 包在这儿);_HERE 留给同目录的 import。
sys.path[:0] = [os.path.dirname(_HERE), _HERE]

import pyopenfilegdb._gdbtable as _gt                       # noqa: E402
from pyopenfilegdb import OpenFileGDB                       # noqa: E402
from pyopenfilegdb._datatypes import _LazyGeometry          # noqa: E402

SAMPLE_HEAD = 200       # 前 N 条一定比几何
SAMPLE_EVERY = 500      # 之后每 N 条抽 1 条



_real_open = builtins.open


class _Counting:
    """给读路径的文件对象记字节数。"""

    __slots__ = ('_f', 'n')

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


def reference_rows(tab):
    """老路径:整条读进来再解。"""
    with _real_open(tab.path, 'rb') as f:
        for row in range(tab.tablx.record_count):
            off = tab.tablx.offset_for_row(row)
            if not off:
                continue
            f.seek(off)
            ln = int.from_bytes(f.read(4), 'little')
            if ln & 0x80000000:
                continue
            blob = f.read(ln)
            if len(blob) < ln:
                break
            yield row, tab._decode_row_blob(blob, row, lazy_geom=True)


def blob_len(v):
    if isinstance(v, _LazyGeometry):
        return v._len
    return -1


def geom_wkt(v):
    if not isinstance(v, _LazyGeometry):
        return None
    g = v.resolve()
    return None if g is None or g.is_empty else g.wkt()


def check_layer(ly):
    tab = ly.table
    n = 0
    bad = 0
    new_bytes = 0
    seen_holder = _Counting(None)

    # --- 新路径,顺便数字节 ---
    counting = []
    _gt.open = lambda *a, **k: counting.append(_Counting(_real_open(*a, **k))) or counting[-1]
    try:
        new_iter = tab.iter_rows()
        for row, v in new_iter:
            seen_holder = counting[-1] if counting else seen_holder
            n += 1
            if n >= 400:      # 数够 400 条就够了,不用跑完
                break
    finally:
        _gt.open = _real_open
    new_bytes = seen_holder.n if seen_holder is not None and seen_holder._f else 0

    # --- 正式逐条比对 ---
    n = 0
    bad = 0
    for (r1, v1), (r2, v2) in zip(reference_rows(tab), tab.iter_rows()):
        if r1 != r2:
            print(f'  ✗ 行号不一致 {r1} != {r2}')
            bad += 1
            break
        ka, kb = set(v1), set(v2)
        if ka != kb:
            print(f'  ✗ 行 {r1} 字段集不同: 多={ka - kb} 少={kb - ka}')
            bad += 1
            break
        for k in ka:
            a, b = v1[k], v2[k]
            if isinstance(a, _LazyGeometry) or isinstance(b, _LazyGeometry):
                if blob_len(a) != blob_len(b):
                    print(f'  ✗ 行 {r1} 字段 {k} 几何长度不同: '
                          f'{blob_len(a)} != {blob_len(b)}')
                    bad += 1
                    break
                if n < SAMPLE_HEAD or n % SAMPLE_EVERY == 0:
                    wa, wb = geom_wkt(a), geom_wkt(b)
                    if wa != wb:
                        print(f'  ✗ 行 {r1} 字段 {k} 几何不同')
                        print(f'      老: {str(wa)[:100]}')
                        print(f'      新: {str(wb)[:100]}')
                        bad += 1
                        break
            elif a != b and not (a != a and b != b):   # NaN 视为相等
                print(f'  ✗ 行 {r1} 字段 {k} 不同: {a!r} != {b!r}')
                bad += 1
                break
        if bad:
            break
        n += 1

    old_bytes = 0
    with _real_open(tab.path, 'rb') as f:
        for row in range(min(n, 400)):
            off = tab.tablx.offset_for_row(row)
            if not off:
                continue
            f.seek(off)
            ln = int.from_bytes(f.read(4), 'little')
            if ln & 0x80000000:
                continue
            old_bytes += 4 + ln

    ratio = (f'{old_bytes / new_bytes:.1f}x' if new_bytes else '-')
    status = '✓' if not bad else '✗'
    print(f'  {status} {ly.name:16s} 比对 {n:6d} 条   前400条 IO: '
          f'老 {old_bytes / 1e6:7.3f} MB -> 新 {new_bytes / 1e6:6.3f} MB  ({ratio})')
    return bad


def main():
    cands = sys.argv[1:] or sorted(glob.glob('D:/work/*.gdb'))
    total_bad = 0
    for gdb_path in cands:
        try:
            gdb = OpenFileGDB.open(gdb_path)
        except Exception as e:
            print(f'跳过 {os.path.basename(gdb_path)}: {e}')
            continue
        print(f'\n=== {os.path.basename(gdb_path)} ===')
        for name in gdb.list_feature_classes():
            ly = gdb.get_layer(name)
            if ly.table is None or ly.table.tablx is None:
                continue
            total_bad += check_layer(ly)
        gdb.close()

    print(f'\n{"全部一致" if not total_bad else f"有 {total_bad} 处不一致"}')
    return 1 if total_bad else 0


if __name__ == '__main__':
    raise SystemExit(main())
