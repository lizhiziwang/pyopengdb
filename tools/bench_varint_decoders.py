#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""在**真实字节流**上比几个 ``_read_xy_array`` 解码器变体。

由 ``tools/bench_varint.py`` 量到的分布驱动:XY 增量 varint 里
**85%+ 是 3 字节**、99.5% 不超过 3 字节。而现在的实现是一个
``while True`` 循环,每个续字节都要跑一遍循环控制 + 变长 ``shift``。
也就是说对 99.5% 的 varint,循环控制纯属白干。

这里把候选写法在同一段真实字节上各跑一遍,并**校验结果逐位相同**
(解不出来就说明写法有 bug,直接报出来,不看耗时)。

用法::

    python tools/bench_varint_decoders.py
    python tools/bench_varint_decoders.py D:/work/xxx.gdb 村行政区划
"""
from __future__ import annotations

import os
import struct
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
# tools/ 的上一级 = 仓库根(pyopenfilegdb 包在这儿);_HERE 留给同目录的 import。
sys.path[:0] = [os.path.dirname(_HERE), _HERE]

from pyopenfilegdb import OpenFileGDB                       # noqa: E402
from pyopenfilegdb._datatypes import _LazyGeometry          # noqa: E402
from pyopenfilegdb._util import read_varuint                # noqa: E402

_UNPACK_I32 = struct.Struct('<I').unpack_from


# ---------------------------------------------------------------------------
# 变体 A:现状 —— while 循环 + 变长 shift
# ---------------------------------------------------------------------------
def decode_loop(blob: bytes, pos: int, n: int, dx: int, dy: int):
    xs = []
    ys = []
    xa = xs.append
    ya = ys.append
    for _ in range(n):
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
    return xs, ys, pos, dx, dy


# ---------------------------------------------------------------------------
# 变体 B:直读下标 + 3 字节展开
#
# 把三档(1/2/3 字节)摊成直线代码,只在 4 字节以上才掉回循环。
# 好处:没有 while 控制、没有 shift 变量(位移量写成常量)。
# 代价:1 字节的情况白读了一个 blob[pos+1](实测只占 1%)。
# ---------------------------------------------------------------------------
def decode_flat3(blob: bytes, pos: int, n: int, dx: int, dy: int):
    xs = []
    ys = []
    xa = xs.append
    ya = ys.append
    for _ in range(n):
        # ---- X ----
        b0 = blob[pos]
        b1 = blob[pos + 1]
        if b0 & 0x80:
            if b1 & 0x80:
                b2 = blob[pos + 2]
                if b2 & 0x80:
                    val = b0 & 0x3F
                    neg = b0 & 0x40
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
                    val = (b0 & 0x3F) | ((b1 & 0x7F) << 6) | ((b2 & 0x7F) << 13)
                    neg = b0 & 0x40
                    pos += 3
            else:
                val = (b0 & 0x3F) | ((b1 & 0x7F) << 6)
                neg = b0 & 0x40
                pos += 2
        else:
            val = b0 & 0x3F
            neg = b0 & 0x40
            pos += 1
        dx = dx - val if neg else dx + val
        xa(dx)

        # ---- Y ----
        b0 = blob[pos]
        b1 = blob[pos + 1]
        if b0 & 0x80:
            if b1 & 0x80:
                b2 = blob[pos + 2]
                if b2 & 0x80:
                    val = b0 & 0x3F
                    neg = b0 & 0x40
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
                    val = (b0 & 0x3F) | ((b1 & 0x7F) << 6) | ((b2 & 0x7F) << 13)
                    neg = b0 & 0x40
                    pos += 3
            else:
                val = (b0 & 0x3F) | ((b1 & 0x7F) << 6)
                neg = b0 & 0x40
                pos += 2
        else:
            val = b0 & 0x3F
            neg = b0 & 0x40
            pos += 1
        dy = dy - val if neg else dy + val
        ya(dy)

    return xs, ys, pos, dx, dy


# ---------------------------------------------------------------------------
# 变体 C:``Struct.unpack_from`` 一次取 4 字节,再按位拼
#
# ``w = b0 | b1<<8 | b2<<16 | b3<<24``,于是:
#   * ``w & 0x80``     判 b0 的高位
#   * ``w & 0x8000``   判 b1
#   * ``w & 0x800000`` 判 b2
#   * ``(b1 & 0x7F) << 6``  == ``(w >> 2) & 0x1FC0``
#   * ``(b2 & 0x7F) << 13`` == ``(w >> 3) & 0xFE000``
# 一次 C 调用拿到 4 个字节,省掉 3 次下标;但要多建一个元组。
# ---------------------------------------------------------------------------
def decode_struct(blob: bytes, pos: int, n: int, dx: int, dy: int):
    xs = []
    ys = []
    xa = xs.append
    ya = ys.append
    unpack = _UNPACK_I32
    end4 = len(blob) - 4
    for _ in range(n):
        if pos < end4:
            w = unpack(blob, pos)[0]
        else:
            w = int.from_bytes(blob[pos:pos + 4], 'little')
        if w & 0x80:
            if w & 0x8000:
                if w & 0x800000:
                    val = w & 0x3F
                    neg = w & 0x40
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
                    val = (w & 0x3F) | ((w >> 2) & 0x1FC0) | ((w >> 3) & 0xFE000)
                    neg = w & 0x40
                    pos += 3
            else:
                val = (w & 0x3F) | ((w >> 2) & 0x1FC0)
                neg = w & 0x40
                pos += 2
        else:
            val = w & 0x3F
            neg = w & 0x40
            pos += 1
        dx = dx - val if neg else dx + val
        xa(dx)

        if pos < end4:
            w = unpack(blob, pos)[0]
        else:
            w = int.from_bytes(blob[pos:pos + 4], 'little')
        if w & 0x80:
            if w & 0x8000:
                if w & 0x800000:
                    val = w & 0x3F
                    neg = w & 0x40
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
                    val = (w & 0x3F) | ((w >> 2) & 0x1FC0) | ((w >> 3) & 0xFE000)
                    neg = w & 0x40
                    pos += 3
            else:
                val = (w & 0x3F) | ((w >> 2) & 0x1FC0)
                neg = w & 0x40
                pos += 2
        else:
            val = w & 0x3F
            neg = w & 0x40
            pos += 1
        dy = dy - val if neg else dy + val
        ya(dy)

    return xs, ys, pos, dx, dy


# ---------------------------------------------------------------------------
# 变体 D:3 字节展开,但**只**累加整数 —— 不做浮点缩放、不拼元组。
#
# 用来把"_read_xy_array 自身耗时"拆成"varint 循环"和"尾部 map/zip 批次转换"
# 两块。两者在 cProfile 里都算在 _read_xy_array 的 tottime 上。
# ---------------------------------------------------------------------------
def decode_flat3_ints(blob: bytes, pos: int, n: int, dx: int, dy: int):
    xs = []
    ys = []
    xa = xs.append
    ya = ys.append
    for _ in range(n):
        b0 = blob[pos]
        b1 = blob[pos + 1]
        if b0 & 0x80:
            if b1 & 0x80:
                b2 = blob[pos + 2]
                if b2 & 0x80:
                    val = b0 & 0x3F
                    neg = b0 & 0x40
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
                    val = (b0 & 0x3F) | ((b1 & 0x7F) << 6) | ((b2 & 0x7F) << 13)
                    neg = b0 & 0x40
                    pos += 3
            else:
                val = (b0 & 0x3F) | ((b1 & 0x7F) << 6)
                neg = b0 & 0x40
                pos += 2
        else:
            val = b0 & 0x3F
            neg = b0 & 0x40
            pos += 1
        dx = dx - val if neg else dx + val
        xa(dx)

        b0 = blob[pos]
        b1 = blob[pos + 1]
        if b0 & 0x80:
            if b1 & 0x80:
                b2 = blob[pos + 2]
                if b2 & 0x80:
                    val = b0 & 0x3F
                    neg = b0 & 0x40
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
                    val = (b0 & 0x3F) | ((b1 & 0x7F) << 6) | ((b2 & 0x7F) << 13)
                    neg = b0 & 0x40
                    pos += 3
            else:
                val = (b0 & 0x3F) | ((b1 & 0x7F) << 6)
                neg = b0 & 0x40
                pos += 2
        else:
            val = b0 & 0x3F
            neg = b0 & 0x40
            pos += 1
        dy = dy - val if neg else dy + val
        ya(dy)

    return xs, ys, pos, dx, dy


# ---------------------------------------------------------------------------
# 变体 E:3 字节展开 + 预分配列表、下标赋值。
#
# ``xs.append(v)`` 是 ``LOAD_METHOD`` + ``CALL``,实测 110 ns/次,而每读一个
# 顶点要 append 两次。改成 ``xs[i] = v; i += 1`` 看能不能省下来。
# ---------------------------------------------------------------------------
def decode_flat3_prealloc(blob: bytes, pos: int, n: int, dx: int, dy: int):
    xs = [0] * n
    ys = [0] * n
    i = 0
    for _ in range(n):
        b0 = blob[pos]
        b1 = blob[pos + 1]
        if b0 & 0x80:
            if b1 & 0x80:
                b2 = blob[pos + 2]
                if b2 & 0x80:
                    val = b0 & 0x3F
                    neg = b0 & 0x40
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
                    val = (b0 & 0x3F) | ((b1 & 0x7F) << 6) | ((b2 & 0x7F) << 13)
                    neg = b0 & 0x40
                    pos += 3
            else:
                val = (b0 & 0x3F) | ((b1 & 0x7F) << 6)
                neg = b0 & 0x40
                pos += 2
        else:
            val = b0 & 0x3F
            neg = b0 & 0x40
            pos += 1
        dx = dx - val if neg else dx + val
        xs[i] = dx

        b0 = blob[pos]
        b1 = blob[pos + 1]
        if b0 & 0x80:
            if b1 & 0x80:
                b2 = blob[pos + 2]
                if b2 & 0x80:
                    val = b0 & 0x3F
                    neg = b0 & 0x40
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
                    val = (b0 & 0x3F) | ((b1 & 0x7F) << 6) | ((b2 & 0x7F) << 13)
                    neg = b0 & 0x40
                    pos += 3
            else:
                val = (b0 & 0x3F) | ((b1 & 0x7F) << 6)
                neg = b0 & 0x40
                pos += 2
        else:
            val = b0 & 0x3F
            neg = b0 & 0x40
            pos += 1
        dy = dy - val if neg else dy + val
        ys[i] = dy
        i += 1

    return xs, ys, pos, dx, dy


VARIANTS = [('A 现状 while 循环', decode_loop),
            ('B 直读下标 + 3 字节展开', decode_flat3),
            ('C Struct.unpack_from + 位拼', decode_struct),
            ('D 只累加整数(不做浮点/拼元组)', decode_flat3_ints),
            ('E 预分配 + 下标赋值', decode_flat3_prealloc)]


# ---------------------------------------------------------------------------
def find_xy_region(gdb_path: str, want: str, min_pts: int = 40000):
    """找一条顶点够多的要素,把它的几何 blob 挖出来当作基准字节流。"""
    gdb = OpenFileGDB.open(gdb_path)
    best = None
    for name in gdb.list_feature_classes():
        ly = gdb.get_layer(name)
        if want and name != want:
            continue
        for feat in ly.read_features():
            geom = feat._geometry
            if not isinstance(geom, _LazyGeometry):
                continue
            raw = geom._raw
            if raw is None:
                raw = geom._read_bytes(geom._len)
            # 头:shape | nPoints | nParts | 4 个 varuint 包围盒
            shape, pos = read_varuint(raw, 0)
            kind = shape & 0xFF
            if kind in (0, 1, 8, 9, 11, 21, 25, 31):
                continue
            n_pts, pos = read_varuint(raw, pos)
            _, pos = read_varuint(raw, pos)
            for _ in range(4):
                _, pos = read_varuint(raw, pos)
            if kind in (10, 13, 23, 28, 30):
                _, pos = read_varuint(raw, pos)
            if n_pts >= min_pts and (best is None or n_pts > best[1]):
                best = (name, n_pts, raw, pos)
        if best is not None:
            break
    gdb.close()
    return best


def main() -> int:
    gdb_path = sys.argv[1] if len(sys.argv) > 1 else \
        'D:/work/2024年国土行政区划.gdb'
    want = sys.argv[2] if len(sys.argv) > 2 else ''

    found = find_xy_region(gdb_path, want)
    if found is None:
        print('没找到顶点够多的要素,调小 min_pts')
        return 1
    name, n_pts, raw, start = found
    print(f'{os.path.basename(gdb_path)} / {name}')
    print(f'基准字节流:{n_pts:,} 个点,XY 数组从偏移 {start} 起\n')

    ref = None
    results = []
    reps = 3
    for label, fn in VARIANTS:
        # 每个变体跑 reps 轮,取最好成绩(热缓存)
        best = None
        out = None
        for _ in range(reps):
            t0 = time.perf_counter()
            out = fn(raw, start, n_pts, 0, 0)
            dt = time.perf_counter() - t0
            if best is None or dt < best:
                best = dt
        xs, ys, pos, dx, dy = out
        u = best / (n_pts * 2) * 1e6
        results.append((label, best, u, pos, dx, dy))
        print(f'{label:28s} {best:8.3f}s   {u:6.3f} µs/varint   '
              f'结束 pos={pos} dx={dx} dy={dy}')
        if ref is None:
            ref = (xs, ys)
        elif (xs, ys) != ref:
            print(f'  ✗ 结果与变体 A 不一致!')
            return 1

    print('\n全部变体结果逐位相同 ✓')
    base = results[0][1]
    for label, dt, u, *_ in results[1:]:
        print(f'  {label}: {base / dt:.2f}x')
    print(f'\n按 村行政区划 44,967,709 个 XY 增量折算:')
    for label, dt, u, *_ in results:
        print(f'  {label:28s} {u * 44_967_709 / 1e6:6.1f}s')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
