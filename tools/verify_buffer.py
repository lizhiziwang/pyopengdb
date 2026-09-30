#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""**可选**的权威对拍:本库的 ``Geometry.buffer()`` 与 GEOS 的 ``buffer`` 比。

⚠️ shapely / GDAL **不在这个文件里出现** —— 它们在 ``tools/_oracle.py``。
本脚本只 ``from _oracle import pick_oracle``。``pyopenfilegdb`` 包、``tests/``、
``pyproject.toml`` 都不含它们。

为什么这一档的非 oracle 自检特别要紧
------------------------------------
谓词那一档至少有 OGC 规范当"真值来源"。buffer 没有:它的结果是一个**近似**
(圆角被离散成折线),所以"实现和理解一起错"是可能的 —— 比如把 ``quad_segs``
当成了"半圆的段数",或者把端帽画到了错误的一侧,圆还是圆的,看起来都对。

所以本脚本有两部分,**互相独立**:

1. **手推真值表**(不需要任何 oracle,任何机器上都跑)。全部用**闭式解**:
   圆的 ``4q`` 边形面积 ``2q·sin(π/2q)``、四分之一圆盘 ``q/2·sin(π/2q)``、
   矩形 ``10×2``、斜接角补满的 ``4×1`` …… 这些数与实现细节无关,只由定义决定。
   改坏任何一处(``quad_segs`` 的方向、端帽的侧别、斜接/倒角的面积增量、
   负距离的侵蚀方向)都会在这里变红。
2. **GEOS 差分**(需要 shapely)。参数矩阵抽样 + 真实语料 + UTM 量级坐标,
   逐顶点判距离、逐面判面积。

判定与"假绿"闸门(照 ``verify_topology.py`` 的老规矩)
------------------------------------------------------
* **不承诺逐位相同** —— 交点用普通双精度而非自适应 DD,偏移端点的末位可能差
  一位。判定分两层:逐位相同(归一化环序之后)与容差内一致。
* 但**逐位相同的用例数要打出来**。这个数字是信息量:简单用例(凸多边形 + 圆角)
  预期逐位相同;这个数掉到 0 就说明"容差判定"其实在替某个真 bug 打掩护。
* **一个 oracle 都没装**是正常状态 → 跑完真值表后 ``exit 0``(skip)。
* **装了 oracle 却一对都比不了 → ``exit 1``**(`pairs == 0` 是失败,不是成功)。
  GDAL 绑定的 ``Buffer`` 只收 ``(distance, quadsegs)``,比不了参数矩阵,这时也会
  显式报出来并失败 —— 不能让它看起来像"通过"。

用法::

    python tools/verify_buffer.py                     # 真值表 + 扫 D:/work/*.gdb
    python tools/verify_buffer.py --synthetic-only
    python tools/verify_buffer.py --oracle shapely
    python tools/verify_buffer.py --features 30 --max-verts 400
"""
from __future__ import annotations

import argparse
import glob
import math
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _oracle import pick_oracle  # noqa: E402


# --------------------------------------------------------------------------
# 手推真值表(不依赖 oracle)
# --------------------------------------------------------------------------
#: 四分之一圆盘被离散成 ``q`` 段时的面积,乘方的系数:``q/2 * sin(pi/(2q))``
def quarter_disc_area(q: int) -> float:
    return 0.5 * q * math.sin(math.pi / (2.0 * q))


#: 整圆被离散成 ``4q`` 段时的面积:``2q * sin(pi/(2q))``
def disc_area(q: int) -> float:
    return 2.0 * q * math.sin(math.pi / (2.0 * q))


_SQ10 = 'POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0))'
_LN10 = 'LINESTRING (0 0, 10 0)'
_RING10 = 'LINESTRING (0 0, 10 0, 10 10, 0 10, 0 0)'
_HOLE = ('POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0),'
         ' (3 3, 7 3, 7 7, 3 7, 3 3))')


def _truth(wkt: str, distance: float, area, env=None, empty=False, **kw):
    return (wkt, distance, area, env, empty, kw)


def truth_table(q: int = 8):
    """返回 ``(名字, wkt, distance, 期望面积, 期望包围盒, 期望是否为空, 参数)``。

    面积一律**闭式解**,与实现无关:

    * ``POINT.buffer(d)`` —— ``4q`` 边形内接于半径 ``d`` 的圆 → ``2q·sin(π/2q)·d²``;
    * ``LINESTRING(0 0,10 0).buffer(d)`` 圆帽 —— 矩形 ``20d`` + 整圆;
    * 平头帽 —— 纯矩形 ``20d``;方头帽 —— ``(10+2d)·2d``;
    * ``10×10`` 方框 ``buffer(d)`` 圆角 —— ``100 + 40d + 4·(四分之一圆盘)``;
    * 斜接 —— 四个角各补一个 ``d×d`` 方块,倒角 —— 各补半个。
    """
    d = 1.0
    out = [
        ('disc_q8', 'POINT (0 0)', 1.0, disc_area(8), (-1, -1, 1, 1), False,
         dict(quad_segs=8)),
        ('disc_q16', 'POINT (0 0)', 1.0, disc_area(16), (-1, -1, 1, 1), False,
         dict(quad_segs=16)),
        ('disc_q1', 'POINT (0 0)', 1.0, disc_area(1), (-1, -1, 1, 1), False,
         dict(quad_segs=1)),
        ('pt_square_cap', 'POINT (0 0)', 1.0, 4.0, (-1, -1, 1, 1), False,
         dict(cap='square')),
        ('pt_neg', 'POINT (0 0)', -1.0, 0.0, None, True, {}),
        ('pt_zero', 'POINT (0 0)', 0.0, 0.0, None, True, {}),
        ('line_round', _LN10, 1.0, 20.0 + disc_area(8), (-1, -1, 11, 1), False,
         dict(quad_segs=8)),
        ('line_flat', _LN10, 1.0, 20.0, (0, -1, 10, 1), False,
         dict(cap='flat', quad_segs=8)),
        ('line_square', _LN10, 1.0, 24.0, (-1, -1, 11, 1), False,
         dict(cap='square', quad_segs=8)),
        ('line_neg', _LN10, -1.0, 0.0, None, True, {}),
        ('sq_round', _SQ10, d, 100.0 + 40.0 + 4 * quarter_disc_area(8),
         (-1, -1, 11, 11), False, dict(quad_segs=8)),
        ('sq_mitre', _SQ10, d, 144.0, (-1, -1, 11, 11), False, dict(join='mitre')),
        ('sq_bevel', _SQ10, d, 142.0, (-1, -1, 11, 11), False, dict(join='bevel')),
        # 负距离:内缩 1,四个内凹角是**内角**(尖的),不补圆弧
        ('sq_neg1', _SQ10, -1.0, 64.0, (1, 1, 9, 9), False, {}),
        ('sq_neg6', _SQ10, -6.0, 0.0, None, True, {}),
        ('sq_zero', _SQ10, 0.0, 100.0, (0, 0, 10, 10), False, {}),
        # 闭合折线当成环:外侧 10×10 长出来的圆角方框,里面留 8×8 的洞
        ('ring_closed', _RING10, 1.0,
         100.0 + 40.0 + 4 * quarter_disc_area(8) - 64.0,
         (-1, -1, 11, 11), False, dict(quad_segs=8)),
        # 大洞缩 1:洞是 4×4,内缩成 2×2,角是内角 → 面积 4
        ('hole_in', _HOLE, 1.0,
         100.0 + 40.0 + 4 * quarter_disc_area(8) - 4.0,
         (-1, -1, 11, 11), False, dict(quad_segs=8)),
    ]
    return out


def run_truth_table(verbose: bool = True) -> int:
    """跑真值表。返回不符条数。"""
    from pyopenfilegdb.geometry import Geometry

    bad = 0
    for name, wkt, dist, area, env, empty, kw in truth_table():
        g = Geometry.from_wkt(wkt)
        r = g.buffer(dist, **kw)
        if r.is_empty:
            got_area, got_env = 0.0, None
        else:
            got_area, got_env = r.area(), r.envelope()
        tol = 1e-9 * max(1.0, abs(area))
        ok = (abs(got_area - area) <= tol) and (r.is_empty == empty)
        if ok and env is not None:
            ok = (got_env is not None
                  and max(abs(a - b) for a, b in zip(got_env, env)) <= 1e-9)
        if not ok:
            bad += 1
            print(f'  ✗ {name}: 面积 {got_area!r} (期望 {area!r}) '
                  f'包围盒 {got_env} (期望 {env}) 空={r.is_empty} (期望 {empty})')
        elif verbose:
            print(f'  ✓ {name}: area={got_area:.12f} env={got_env}')
    return bad


# --------------------------------------------------------------------------
# 环的规范化(用于"逐位相同"这条判定)
# --------------------------------------------------------------------------
def _signed_area2(pts) -> float:
    a = 0.0
    n = len(pts)
    for i in range(n):
        x0, y0 = pts[i]
        x1, y1 = pts[(i + 1) % n]
        a += x0 * y1 - x1 * y0
    return a


def _canon_ring(pts):
    """环 -> 可逐位比较的规范形:统一成逆时针,再旋转到字典序最小的顶点。"""
    if not pts:
        return ()
    pts = list(pts)
    while len(pts) > 1 and pts[0] == pts[-1]:
        pts.pop()
    if _signed_area2(pts) < 0.0:
        pts.reverse()
    k = min(range(len(pts)), key=lambda i: pts[i])
    return tuple(pts[k:] + pts[:k])


def _canon(parts):
    """``[(shell, holes)]`` -> 与环序、面序、起点、绕向都无关的规范形。"""
    out = []
    for shell, holes in parts:
        out.append((_canon_ring(shell),
                    tuple(sorted(_canon_ring(h) for h in holes))))
    return tuple(sorted(out))


# --------------------------------------------------------------------------
# 几何度量(本脚本自己实现,不用被检方的距离代码)
# --------------------------------------------------------------------------
def _pt_seg_dist(p, a, b) -> float:
    dx = b[0] - a[0]
    dy = b[1] - a[1]
    s2 = dx * dx + dy * dy
    if s2 <= 0.0:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / s2
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return math.hypot(a[0] + t * dx - p[0], a[1] + t * dy - p[1])


def _pt_boundary_dist(p, parts) -> float:
    """点到一组多边形(含所有环)边界的最短距离。"""
    best = float('inf')
    for shell, holes in parts:
        for ring in (shell, *holes):
            n = len(ring)
            if n < 2:
                continue
            for i in range(n):
                d = _pt_seg_dist(p, ring[i], ring[(i + 1) % n])
                if d < best:
                    best = d
    return best


def _all_verts(parts):
    for shell, holes in parts:
        for v in shell:
            yield v
        for h in holes:
            for v in h:
                yield v


def _max_vertex_dev(a_parts, b_parts) -> float:
    """双向最大顶点偏差:我方每个顶点到对方边界的距离的最大值(两个方向取大)。

    这个量比"对称差面积"更严 —— 面积能对上但边界错位的构型它抓得住。
    """
    if not a_parts or not b_parts:
        return 0.0 if (not a_parts and not b_parts) else float('inf')
    worst = 0.0
    for v in _all_verts(a_parts):
        d = _pt_boundary_dist(v, b_parts)
        if d > worst:
            worst = d
    for v in _all_verts(b_parts):
        d = _pt_boundary_dist(v, a_parts)
        if d > worst:
            worst = d
    return worst


def _parts_area(parts) -> float:
    total = 0.0
    for shell, holes in parts:
        total += abs(_signed_area2(shell)) / 2.0
        for h in holes:
            total -= abs(_signed_area2(h)) / 2.0
    return total


def _own_geometry(wkt: str):
    from pyopenfilegdb.geometry import Geometry
    return Geometry.from_wkt(wkt)


def _own_parts(wkt: str, distance: float, quad_segs: int, cap, join,
               mitre_limit: float, single_sided: bool):
    g = _own_geometry(wkt)
    r = g.buffer(distance, quad_segs=quad_segs, cap=cap, join=join,
                 mitre_limit=mitre_limit, single_sided=single_sided)
    if r.is_empty:
        return []
    parts = r.xy_parts
    shells = r._shells or [True] + [False] * (len(parts) - 1)
    groups = []
    cur = None
    for arr, is_shell in zip(parts, shells):
        pts = [(arr[i], arr[i + 1]) for i in range(0, len(arr), 2)]
        if is_shell:
            cur = [pts, []]
            groups.append(cur)
        elif cur is not None:
            cur[1].append(pts)
    return [(s, h) for s, h in groups]


# --------------------------------------------------------------------------
# 用例
# --------------------------------------------------------------------------
_SYNTHETIC = [
    ('pt', 'POINT (0 0)'),
    ('pt_utm', 'POINT (39393621 3179757)'),
    ('multipoint', 'MULTIPOINT ((0 0), (40 0), (-3 7))'),
    ('ln_h', 'LINESTRING (0 0, 10 0)'),
    ('ln_utm', 'LINESTRING (39393621 3179757, 39393631 3179757)'),
    ('ln_diag', 'LINESTRING (0 0, 10 10)'),
    ('ln_L', 'LINESTRING (0 0, 10 0, 10 10)'),
    ('ln_L_utm', 'LINESTRING (39393621 3179757, 39393631 3179757, '
                 '39393631 3179767)'),
    ('ln_zigzag', 'LINESTRING (0 0, 5 3, 10 0, 15 4, 20 0)'),
    ('ln_shallow', 'LINESTRING (0 0, 5 0.001, 10 0)'),
    ('ln_ring', 'LINESTRING (0 0, 10 0, 10 10, 0 10, 0 0)'),
    ('ln_thin', 'LINESTRING (0 0, 0.001 0)'),
    ('sq', _SQ10),
    ('sq_utm', 'POLYGON ((39393621 3179757, 39393631 3179757, '
               '39393631 3179767, 39393621 3179767, 39393621 3179757))'),
    ('sq_cw', 'POLYGON ((0 10, 10 10, 10 0, 0 0, 0 10))'),
    ('tri', 'POLYGON ((0 0, 10 0, 5 8, 0 0))'),
    ('sliver', 'POLYGON ((0 0, 10 0, 10 0.001, 0 0.001, 0 0))'),
    ('hole', _HOLE),
    ('hole_small', 'POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0),'
                   ' (4 4, 6 4, 6 6, 4 6, 4 4))'),
    ('two_sq', 'MULTIPOLYGON (((0 0, 10 0, 10 10, 0 10, 0 0)),'
               ' ((50 50, 60 50, 60 60, 50 60, 50 50)))'),
    ('nested', 'MULTIPOLYGON (((0 0, 20 0, 20 20, 0 20, 0 0)),'
               ' ((5 5, 8 5, 8 8, 5 8, 5 5)))'),
    ('bowtie', 'POLYGON ((0 0, 10 10, 10 0, 0 10, 0 0))'),
]

#: 参数矩阵抽样 —— 不做全笛卡尔积(4×3×3×4 = 144 太大),按固定种子抽
_MATRIX = {
    'quad_segs': (1, 2, 8, 16),
    'cap': ('round', 'flat', 'square'),
    'join': ('round', 'mitre', 'bevel'),
    'distance': (1.0, -1.0, 0.5, 5.0),
}


def _sample_params(rnd, n):
    out = [(8, 'round', 'round', 1.0, 5.0, False)]
    seen = set(out)
    guard = 0
    while len(out) < n and guard < n * 50:
        guard += 1
        c = (rnd.choice(_MATRIX['quad_segs']), rnd.choice(_MATRIX['cap']),
             rnd.choice(_MATRIX['join']), rnd.choice(_MATRIX['distance']),
             rnd.choice((0.5, 1.0, 5.0)), rnd.random() < 0.15)
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def collect_corpus(gdb_paths, n_feat, max_verts):
    """真实语料:用**本库读出来的**几何,保证两边输入逐位相同。"""
    from pyopenfilegdb import OpenFileGDB

    out = []
    for path in gdb_paths:
        try:
            db = OpenFileGDB.open(path)
        except Exception as exc:                            # noqa: BLE001
            print(f'  跳过语料 {path}: {exc}')
            continue
        for lname in db.list_layers():
            try:
                lyr = db.get_layer(lname)
            except Exception:                               # noqa: BLE001
                continue
            if lyr.geometry_field is None:
                continue
            got = skipped = 0
            for feat in lyr.read_features():
                g = feat.geometry
                if (g is None or g.is_empty or g.kind == 'multipatch'
                        or g.has_z or g.has_m):
                    continue
                if g.point_count > max_verts:
                    skipped += 1
                    continue
                try:
                    out.append((f'{lname}#{feat.oid}', g.wkt(), g.area()))
                except Exception:                           # noqa: BLE001
                    continue
                got += 1
                if got >= n_feat:
                    break
            if got:
                note = f'(跳过 {skipped} 条超过 {max_verts} 顶点的)' if skipped else ''
                print(f'  语料 {lname}: 取 {got} 条 {note}')
        db.close()
    return out


# --------------------------------------------------------------------------
# 差分
# --------------------------------------------------------------------------
#: ``single_sided`` 在**闭合输入**上本库与 GEOS 的已知差异 —— 显式列出来,
#: **不静默跳过**(照 ``verify_topology.py`` 立下的规矩)。
#:
#: GEOS 在跑完同一条单侧缓冲管线之后,还有一步 JTS 没有的后处理
#: (``BufferBuilder::buffer`` 结尾):拿 ``输入边界`` 与 ``结果的边界`` 做
#: ``OverlayNG::overlay(..., UNION)``,再 ``Polygonizer`` 出所有面,面多于一个时
#: **只留面积最大的那个**。对**开放折线**这一步是恒等的(只围得出一个面,原样返回);
#: 对**闭合环 / 面**它会换掉结果 —— 实测 10×10 方框 ``ss=1, d=1``:GEOS 给 100
#: (方框本身那块面),本库给 143.12(方框外那圈单侧带) —— 两者都"对",是两套
#: 语义。本库按 **JTS** 原样返回:⚠️ **2026-09-30 补注** —— overlay 那一档后来做了
#: (见 ``verify_overlay.py``),这一步却**仍旧不做**,因为还差一个 ``Polygonizer``,
#: 且"只留最大面"是**丢几何**的取舍。``DESIGN.md`` §2.23 第 5 条有记。
_SS_CLOSED_REASON = ('闭合输入(面/闭合折线)上的 single_sided:GEOS 多一步 '
                     'overlay+polygonize 后处理(取"输入边界∪缓冲边界"围出的最大面),'
                     '本库按 JTS 返回单侧缓冲')


def _single_sided_closed(wkt: str) -> bool:
    """输入是不是"闭合"的(面 / 闭合折线)—— 只有这一类 ``single_sided`` 才和 GEOS 分岔。"""
    from pyopenfilegdb.geometry import Geometry
    g = Geometry.from_wkt(wkt)
    if g.kind == 'polygon':
        return True
    if g.kind == 'polyline':
        for arr in g.xy_parts:
            if len(arr) >= 4 and arr[0] == arr[-2] and arr[1] == arr[-1]:
                return True
    return False


def compare(oracle, cases, rnd, tol_rel=1e-9):
    """逐用例比。返回 ``(总对数, 逐位相同数, 超差列表, 逐位相同列表, 排除统计)``。

    ``排除统计`` 是 ``{原因: 次数}``,由 :func:`main` 打印出来 —— **被排除的对数
    必须出现在报告里**,否则"排除"和"静默跳过"就没有区别了。
    """
    total = identical = 0
    bad = []
    same = []
    excluded = {}
    t0 = time.time()
    for name, wkt, (q, cap, join, dist, mlim, ss) in cases:
        if ss and _single_sided_closed(wkt):
            excluded[_SS_CLOSED_REASON] = excluded.get(_SS_CLOSED_REASON, 0) + 1
            continue
        try:
            mine = _own_parts(wkt, dist, q, cap, join, mlim, ss)
        except Exception as exc:                            # noqa: BLE001
            bad.append((name, f'本库抛异常 {exc!r}'))
            continue
        theirs = oracle.buffer_parts(wkt, dist, quad_segs=q, cap=cap,
                                     join=join, mitre_limit=mlim,
                                     single_sided=ss)
        if theirs is None:
            return total, identical, [('', 'oracle 不支持 buffer 参数矩阵')], same, excluded
        total += 1
        label = f'{name}|q={q} cap={cap} join={join} d={dist} ss={int(ss)}'
        if _canon(mine) == _canon(theirs):
            identical += 1
            same.append(label)
            continue
        tol = tol_rel * max(1.0, abs(dist))
        dev = _max_vertex_dev(mine, theirs)
        a_mine, a_theirs = _parts_area(mine), _parts_area(theirs)
        area_tol = tol_rel * max(1.0, abs(a_theirs))
        if dev > tol or abs(a_mine - a_theirs) > area_tol:
            bad.append((label, f'顶点偏差 {dev:.3e} (tol {tol:.3e}); '
                               f'面积 本库 {a_mine!r} 参照 {a_theirs!r}'))
    if total and (time.time() - t0) > 5.0:
        print(f'  (差分耗时 {time.time() - t0:.1f}s)')
    return total, identical, bad, same, excluded


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description='本库 buffer() 与 GEOS/GDAL 的差分对拍')
    ap.add_argument('--oracle', default='auto',
                    choices=('auto', 'shapely', 'gdal'))
    ap.add_argument('--gdb', action='append', default=[],
                    help='语料 .gdb 路径,可重复;默认扫 D:/work/*.gdb')
    ap.add_argument('--synthetic-only', action='store_true')
    ap.add_argument('--features', type=int, default=8,
                    help='每层取多少条要素(默认 8)')
    ap.add_argument('--max-verts', type=int, default=600,
                    help='超过这么多顶点的要素跳过(默认 600)')
    ap.add_argument('--params', type=int, default=40,
                    help='参数矩阵抽多少个组合(默认 40)')
    ap.add_argument('--seed', type=int, default=20260930)
    ap.add_argument('--no-truth', action='store_true',
                    help='跳过手推真值表')
    args = ap.parse_args(argv)

    rc = 0

    # ---- 第 1 部分:手推真值(不需要 oracle,任何机器都跑) ----
    if not args.no_truth:
        print('== 手推真值表(闭式解,不依赖任何 oracle)==')
        bad_truth = run_truth_table(verbose=False)
        if bad_truth:
            print(f'✗ 手推真值表有 {bad_truth} 条不符 —— 这跟 oracle 有没有装无关,'
                  f'是实打实的错。')
            rc = 1
        else:
            print(f'  ✓ {len(truth_table())} 条全过')

    oracle = pick_oracle(args.oracle)
    if oracle is None:
        print('\n== GEOS 差分 ==')
        print('  没有装 shapely 也没有 osgeo —— **跳过差分**(这不算失败)。')
        print('  要跑差分:装一份 shapely,或用 QGIS 自带的 Python 跑本脚本。')
        return rc

    print(f'\n== GEOS 差分(oracle: {oracle.name};{oracle.describe()})==')
    if not oracle.supports_buffer_params:
        print(f'  ✗ {oracle.name} 的 Buffer 只收 (distance, quadsegs),'
              f'比不了 cap/join/mitre_limit/single_sided —— 本脚本无可比项。')
        print('    这**不是通过**,是没比成。请装 shapely 再跑。')
        return 1

    rnd = random.Random(args.seed)
    params = _sample_params(rnd, args.params)
    cases = [(n, w, p) for n, w in _SYNTHETIC for p in params]
    print(f'  合成用例 {len(_SYNTHETIC)} 个几何 × {len(params)} 组参数 = '
          f'{len(cases)} 对')

    total, identical, bad, same, excluded = compare(oracle, cases, rnd)
    print(f'  合成:比了 {total} 对,逐位相同 {identical},超差 {len(bad)}')
    for label, why in bad[:12]:
        print(f'    ✗ {label}: {why}')
    if bad:
        rc = 1

    # ---- 真实语料 ----
    if not args.synthetic_only:
        gdb_paths = args.gdb or sorted(glob.glob('D:/work/*.gdb'))
        if gdb_paths:
            print('\n== 真实语料 ==')
            corpus = collect_corpus(gdb_paths, args.features, args.max_verts)
            if corpus:
                # 语料只跑常用参数(全矩阵对上千顶点的环太贵)
                cparams = [(8, 'round', 'round', 1.0, 5.0, False),
                           (4, 'round', 'round', 2.0, 5.0, False),
                           (8, 'flat', 'bevel', 1.0, 5.0, False),
                           (12, 'square', 'mitre', -1.0, 5.0, False)]
                ccases = [(n, w, p) for n, w, _a in corpus for p in cparams]
                total2, id2, bad2, _same2, ex2 = compare(oracle, ccases, rnd)
                print(f'  语料:比了 {total2} 对,逐位相同 {id2},超差 {len(bad2)}')
                for label, why in bad2[:12]:
                    print(f'    ✗ {label}: {why}')
                total += total2
                identical += id2
                for k, v in ex2.items():
                    excluded[k] = excluded.get(k, 0) + v
                if bad2:
                    rc = 1
        else:
            print('\n== 真实语料 ==\n  (D:/work 下没有 *.gdb —— 跳过)')

    print('\n' + '=' * 60)
    if excluded:
        print('⚠️ **显式排除**(不是静默跳过 —— 原因与条数都列在这里):')
        for why, n in sorted(excluded.items(), key=lambda kv: -kv[1]):
            print(f'   {n} 对  {why}')
    if total == 0:
        print('✗ 一对都没比 —— 这是失败,不是通过。')
        return 1
    print(f'比了 {total} 对;逐位完全相同 {identical} 对 '
          f'({100.0 * identical / total:.1f}%),其余都在容差内'
          f'({"无超差" if not rc else "有超差"})')
    if identical == 0:
        print('⚠️ 逐位相同的用例是 0 —— 容差判定可能在替真 bug 打掩护,'
              '先把一个简单用例(凸多边形 + 圆角)对到逐位相同再谈别的。')
    return rc


if __name__ == '__main__':
    sys.exit(main())
