#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""差分验证:几何算法的 **numpy 快路径 vs 纯 Python 顺序路径**,外加精度校准。

numpy 在本库里**只是加速器,不是依赖**(见 :mod:`pyopenfilegdb._geometry_ops`
开头)。两条路径并存,只有能证明等价才有意义 —— 与 ``tools/verify_accel.py``
对 C 扩展的验收条件同一个道理。

三层证据
--------
1. **真实数据对拍**。遍历样例库的要素,每条几何分别用两条路算
   ``area`` / ``length`` / ``centroid`` / ``ring_signed_area2``,要求一致。
   容差不是拍脑袋定的:numpy 的 ``sum`` 用**成对求和**,与顺序累加的舍入不同,
   实测相对误差在 1e-14 量级(面积/长度),质心按坐标尺度算 1e-9。
   **符号**必须完全相同 —— 写路径的绕向判定靠它。

2. **精度校准(对着有理数)**。用 ``fractions.Fraction`` 在**同样的双精度
   输入**上算精确解,量两条路各自离真值多远。这一层专门盯「大坐标抵消」:
   修之前,真实语料上质心能偏出 1135 米、面积相对误差 8.6e-5;修之后应当
   落在浮点 epsilon 量级。**这条是这份工具存在的主要理由** —— 它能在换
   机器、换 numpy 版本、有人"顺手优化"掉那次平移时立刻报警。

3. **基准**。报两条路在同一条几何上的耗时,用于核对
   ``_NP_MIN_AREA2`` / ``_NP_MIN_LENGTH`` 两个阈值有没有失效。

用法::

    python tools/verify_numpy.py                    # 扫 D:/work/*.gdb
    python tools/verify_numpy.py D:/work/xxx.gdb
    python tools/verify_numpy.py --all              # 关掉要素预算
    python tools/verify_numpy.py --bench            # 只跑基准
"""
from __future__ import annotations

import argparse
import glob
import math
import os
import sys
import time
from array import array
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pyopenfilegdb as _pkg                            # noqa: E402
from pyopenfilegdb import OpenFileGDB                   # noqa: E402
from pyopenfilegdb import _geometry_ops as G            # noqa: E402

#: 面积 / 长度允许的相对误差(numpy 成对求和的实测量级是 1e-14)。
TOL_REL = 1e-12

#: 质心按**坐标尺度**归一后的允许误差。
TOL_CENTROID = 1e-9

#: 精度层:实现离有理数真值的允许相对误差。平移到首顶点之后应当 ~1e-15。
TOL_EXACT = 1e-9

#: 默认最多对拍多少条要素(纯 Python 慢,全量要几分钟)。
DEFAULT_BUDGET = 3000


def find_gdbs(argv):
    if argv:
        return argv
    env = os.environ.get('PYOPENFILEGDB_TEST_GDB', '')
    if env:
        return [d for d in env.split(os.pathsep) if d and os.path.isdir(d)]
    return sorted(glob.glob('D:/work/*.gdb'))


def scale_of(parts):
    """几何的尺度(所有 part 包围盒对角线),用来归一坐标误差。"""
    lo_x = lo_y = math.inf
    hi_x = hi_y = -math.inf
    for a in parts:
        for v in a[0::2]:
            lo_x = min(lo_x, v)
            hi_x = max(hi_x, v)
        for v in a[1::2]:
            lo_y = min(lo_y, v)
            hi_y = max(hi_y, v)
    if lo_x is math.inf:
        return 1.0
    s = math.hypot(hi_x - lo_x, hi_y - lo_y)
    return s if s > 0 else 1.0


def exact_area_centroid(parts, shells):
    """有理数精确解 —— 作用在浮点的精确二进制值上,即"真值"。"""
    roles = G.ring_roles(parts, shells)
    ta = Fraction(0)
    cx = Fraction(0)
    cy = Fraction(0)
    for a, is_shell in zip(parts, roles):
        n = len(a) // 2
        if n < 3:
            continue
        a2 = Fraction(0)
        rx = Fraction(0)
        ry = Fraction(0)
        for i in range(n):
            j = (i + 1) % n
            x1, y1 = Fraction(a[2 * i]), Fraction(a[2 * i + 1])
            x2, y2 = Fraction(a[2 * j]), Fraction(a[2 * j + 1])
            cr = x1 * y2 - x2 * y1
            a2 += cr
            rx += (x1 + x2) * cr
            ry += (y1 + y2) * cr
        if a2 == 0:
            continue
        w = abs(a2) / 2
        if not is_shell:
            w = -w
        ta += w
        cx += w * (rx / (3 * a2))
        cy += w * (ry / (3 * a2))
    if ta == 0:
        return None, None
    return abs(float(ta)), (float(cx / ta), float(cy / ta))


def verify_parity(gdb_path, budget, exact_budget=200):
    """两条路对拍 + 对有理数校准精度。"""
    gdb = OpenFileGDB.open(gdb_path)
    n_feat = 0
    n_exact = 0
    worst = {'area': 0.0, 'length': 0.0, 'centroid': 0.0}
    worst_exact = {'area': 0.0, 'centroid': 0.0}
    worst_exact_tag = ('', '')
    sign_flips = 0
    bad = []

    for lyr in gdb.list_layers():
        try:
            layer = gdb.get_layer(lyr)
        except Exception:
            continue
        if layer.geometry_field is None:
            continue
        t0 = time.perf_counter()
        for feat in layer.read_features():
            geom = feat.geometry
            if geom is None or geom.is_empty or geom.kind == 'multipatch':
                continue
            parts = geom.xy_parts
            if not parts:
                continue

            with G.use_numpy(False):
                a_p = G.area_of(geom.kind, parts, geom._shells)
                l_p = G.length_of(geom.kind, parts)
                c_p = G.centroid_of(geom.kind, parts, geom._shells)
                s_p = [G.ring_signed_area2(a) for a in parts]
            with G.use_numpy(True):
                a_n = G.area_of(geom.kind, parts, geom._shells)
                l_n = G.length_of(geom.kind, parts)
                c_n = G.centroid_of(geom.kind, parts, geom._shells)
                s_n = [G._np_signed_area2(a) if len(a) // 2 >= 2 else 0.0
                       for a in parts]

            if a_p:
                worst['area'] = max(worst['area'], abs(a_p - a_n) / abs(a_p))
                if abs(a_p - a_n) / abs(a_p) > TOL_REL:
                    bad.append((feat.oid, 'area', a_p, a_n))
            if l_p:
                worst['length'] = max(worst['length'],
                                      abs(l_p - l_n) / abs(l_p))
                if abs(l_p - l_n) / abs(l_p) > TOL_REL:
                    bad.append((feat.oid, 'length', l_p, l_n))
            if c_p and c_n:
                d = math.hypot(c_p[0] - c_n[0], c_p[1] - c_n[1])
                worst['centroid'] = max(worst['centroid'],
                                        d / scale_of(parts))
                if d / scale_of(parts) > TOL_CENTROID:
                    bad.append((feat.oid, 'centroid', c_p, c_n))
            for sp, sn in zip(s_p, s_n):
                if sp != 0.0 and sn != 0.0 and (sp < 0) != (sn < 0):
                    sign_flips += 1
                    bad.append((feat.oid, 'SIGN-FLIP', sp, sn))

            # --- 精度层:只对小环做有理数,否则太慢 ---
            if (n_exact < exact_budget
                    and all(len(a) // 2 <= 64 for a in parts)
                    and abs(a_p) > 1e-6):
                ea, ec = exact_area_centroid(parts, geom._shells)
                if ea:
                    ra = abs(a_p - ea) / ea
                    if ra > worst_exact['area']:
                        worst_exact['area'] = ra
                        worst_exact_tag = (f'{lyr}/{feat.oid}', 'area')
                    if ec:
                        dc = math.hypot(c_p[0] - ec[0], c_p[1] - ec[1])
                        rc = dc / scale_of(parts)
                        if rc > worst_exact['centroid']:
                            worst_exact['centroid'] = rc
                            worst_exact_tag = (f'{lyr}/{feat.oid}', 'centroid')
                    n_exact += 1

            n_feat += 1
            if budget and n_feat >= budget:
                break
        dt = time.perf_counter() - t0
        print(f'  {lyr}: 累积 {n_feat} 条, 本层 {dt:.1f}s')
        if budget and n_feat >= budget:
            break

    gdb.close()
    return n_feat, n_exact, worst, worst_exact, worst_exact_tag, sign_flips, bad


def bench(sizes=(48, 96, 128, 160, 192, 256, 512, 2000)):
    """两条路的耗时 **+ 分派决策** —— 用来核对阈值有没有失效。

    ⚠️ **必须打出"这一行的 numpy 到底有没有被分派"**,否则这张表会骗人:
    阈值以上才走 numpy,阈值以下比的是"纯 Python 对纯 Python"(恒为 1.0×),
    而**刚好在阈值上但 numpy 其实更慢**的那一档会被 1.1× 这种数字盖过去。
    质心的阈值 128 → 256 就是这么发现的(见 ``_geometry_ops`` 里
    ``_NP_MIN_CENTROID`` 的注释)。判据不是"numpy 快不快",而是
    **"分派了的地方 numpy 必须明显快,没分派的地方不算数"**。
    """

    def ring(n, base=39393621.0, r=1000.0):
        a = array('d')
        for i in range(n):
            t = 2.0 * math.pi * i / n
            a.append(base + r * math.cos(t))
            a.append(base + r * math.sin(t))
        return a

    import timeit
    # (名字, 调用, 阈值) —— 阈值 None = 永远不走 numpy
    ops = (('area', lambda a: G.area_of('polygon', [a]), G._NP_MIN_AREA2),
           ('length', lambda a: G.length_of('polygon', [a]), G._NP_MIN_LENGTH),
           ('centroid', lambda a: G.centroid_of('polygon', [a]),
            G._NP_MIN_CENTROID),
           ('envelope', lambda a: G.envelope_of([a]), None))

    print(f'{"算子":10s} {"N":>6s} {"纯 Python":>11s} {"numpy":>11s} '
          f'{"加速比":>8s}  分派')
    print('-' * 62)
    regress = []
    for n in sizes:
        a = ring(n)
        for name, fn, thr in ops:
            num = max(80, 40000 // n)
            with G.use_numpy(False):
                tp = min(timeit.repeat(lambda: fn(a), number=num, repeat=3)) / num
            with G.use_numpy(True):
                tn = min(timeit.repeat(lambda: fn(a), number=num, repeat=3)) / num
            used = thr is not None and n >= thr
            if used:
                note = 'numpy' if tp / tn >= 1.0 else 'numpy **慢**'
                if tp / tn < 1.0:
                    regress.append((name, n, tp / tn))
            elif thr is None:
                note = '— 不走'
            else:
                note = f'未达标(<{thr})'
            print(f'{name:10s} {n:6d} {tp*1e6:9.1f}µs {tn*1e6:9.1f}µs '
                  f'{tp/tn:7.2f}×  {note}')
    if regress:
        print('\n!! 分派到 numpy 却更慢 —— 阈值定低了:')
        for name, n, r in regress:
            print(f'   {name} @ N={n}: {r:.2f}×')
    else:
        print('\n所有分派点 numpy 都更快 ✓')
    return regress


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('gdb', nargs='*', help='样例库目录(默认扫 D:/work/*.gdb)')
    ap.add_argument('--all', action='store_true', help='关掉要素预算')
    ap.add_argument('--bench', action='store_true', help='只跑基准')
    args = ap.parse_args()

    print(f'pyopenfilegdb {_pkg.__version__}  '
          f'HAS_NUMPY={G.HAS_NUMPY}  HAS_ACCEL={_pkg.HAS_ACCEL}')

    if args.bench:
        return 1 if bench() else 0

    if not G.HAS_NUMPY:
        print('没装 numpy —— 没有第二条路可对拍。')
        print('(这是**正常状态**,不是错误:numpy 在本库里只是可选加速器。)')
        bench()
        return 0

    gdbs = find_gdbs(args.gdb)
    if not gdbs:
        print('找不到样例库。给个路径,或设 PYOPENFILEGDB_TEST_GDB。')
        return 2

    budget = 0 if args.all else DEFAULT_BUDGET
    total_bad = []
    for path in gdbs:
        print(f'\n=== {path} ===')
        try:
            n, ne, worst, wex, tag, flips, bad = verify_parity(path, budget)
        except Exception as exc:                       # noqa: BLE001
            print(f'  跳过({type(exc).__name__}: {exc})')
            continue
        print(f'  对拍要素 {n} 条,其中 {ne} 条做了有理数校准')
        print(f'  两条路最大相对差: 面积 {worst["area"]:.2e}  '
              f'长度 {worst["length"]:.2e}  '
              f'质心(按尺度) {worst["centroid"]:.2e}')
        print(f'  符号翻转: {flips}')
        print(f'  离有理数真值: 面积 {wex["area"]:.2e}  '
              f'质心(按尺度) {wex["centroid"]:.2e}   '
              f'最差 @ {tag[0]} [{tag[1]}]')
        total_bad.extend(bad)
        if wex['area'] > TOL_EXACT or wex['centroid'] > TOL_EXACT:
            print('  !! 精度超限 —— 鞋带公式的「先平移到首顶点」是不是被改掉了?')
            print('     见 pyopenfilegdb/_geometry_ops.py 开头的「大坐标抵消」。')

    print()
    if total_bad:
        print(f'不一致 {len(total_bad)} 处(前 10):')
        for b in total_bad[:10]:
            print('   ', b)
        return 1
    print('两条路一致 ✓')
    return 0


if __name__ == '__main__':
    sys.exit(main())
