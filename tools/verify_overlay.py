#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""**可选**的权威对拍:本库的 overlay(``difference`` / ``union`` / ``intersection``
/ ``symmetric_difference``)与 GEOS 比。

⚠️ shapely / GDAL **不在这个文件里出现** —— 它们在 ``tools/_oracle.py``。
本脚本只 ``from _oracle import pick_oracle``。``pyopenfilegdb`` 包、``tests/``、
``pyproject.toml`` 都不含它们。

为什么这一档的非 oracle 自检特别要紧
------------------------------------
overlay 有 GDAL 这件事反而**更难**:``OGRGeometry::Difference`` / ``Union`` /
``Intersection`` / ``SymmetricDifference`` 在 ``ogrgeometry.cpp`` 里**一律是一句
转发给 GEOS**,GDAL 自己一行算法都没有 —— 也就是说,这一档连"照抄 GDAL"这条
退路都没有,只有 JTS ``operation/overlayng/``。而"实现和理解一起错"在这里完全
可能:``POLYGON ∖ 横穿的 LINESTRING`` 若把线当成了面的边界,会给出两个 50 面积的
碎多边形 —— 看起来是个正常的答案。

所以本脚本有两部分,**互相独立**:

1. **手推真值表**(不需要任何 oracle,任何机器上都跑)。全部用**闭式解**:重叠
   方块的 25/175/75/150、``A ⊂ B`` 的空、共边降维成线、共点降维成点、横穿的线
   不切面 …… 改坏任何一处都会在这里变红。
2. **GEOS 差分**(需要 shapely)。合成用例 + 真实语料 + 固定种子随机几何。

判定与"假绿"闸门(照 ``verify_buffer.py`` / ``verify_topology.py`` 的老规矩)
----------------------------------------------------------------------------
判定**一个字都不依赖本库自己的谓词**(否则是自证)。两条判据都由 oracle 算:

* **集合**:``|本库结果 △ GEOS结果|`` 的面积与线长都要 ≤ 容差;
* **结构**:两边的**顶层成员表**要一模一样 —— ``[(类型, 面积, 线长, 顶点数)]``。
  ⚠️ 这一条不能省:``GC(面, 线, 线)`` 与 ``GC(面, MULTILINESTRING)`` 在集合意义
  下**完全相同**(对称差面积 0),只比集合会把 GC 装配的差异全放过。本库曾经就是
  后者,是这条判据把它抓出来的。

另外两条老闸门照样在:

* **一个 oracle 都没装**是正常状态 → 跑完真值表后 ``exit 0``(skip)。
* **装了 oracle 却一对都比不了 → ``exit 1``**(`pairs == 0` 是失败,不是成功)。
* **被排除的对数必须打出来**(GDAL 的 GC 面积恒为 0;含 GC 的输入本库明说不支持)。
  排除与静默跳过的区别就在这个报告里。

用法::

    python tools/verify_overlay.py                      # 真值表 + 扫 D:/work/*.gdb
    python tools/verify_overlay.py --synthetic-only
    python tools/verify_overlay.py --oracle shapely
    python tools/verify_overlay.py --features 30 --max-pairs 400
"""
from __future__ import annotations

import argparse
import glob
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _oracle import OVERLAY_OPS, pick_oracle  # noqa: E402


# --------------------------------------------------------------------------
# 手推真值表(不依赖 oracle)
# --------------------------------------------------------------------------
#: 两个 5×5 重叠的方块之间的一切都是闭式解。
_A = 'POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0))'
_B = 'POLYGON ((5 5, 15 5, 15 15, 5 15, 5 5))'
_FAR = 'POLYGON ((100 100, 110 100, 110 110, 100 110, 100 100))'
_SMALL = 'POLYGON ((2 2, 4 2, 4 4, 2 4, 2 2))'          # 完全在 _A 内
_HOLED = ('POLYGON ((0 0, 10 0, 10 10, 0 10, 0 0),'
          ' (3 3, 7 3, 7 7, 3 7, 3 3))')                # _A 带 4×4 的洞
_PLUG = 'POLYGON ((3 3, 7 3, 7 7, 3 7, 3 3))'           # 正好填上那个洞
_EDGE_B = 'POLYGON ((10 0, 20 0, 20 10, 10 10, 10 0))'  # 与 _A 共一条边
_CORNER_B = 'POLYGON ((10 10, 20 10, 20 20, 10 20, 10 10))'   # 只共一个角
_CROSS = 'LINESTRING (-5 5, 15 5)'                      # 横穿 _A
_INSIDE = 'LINESTRING (2 5, 8 5)'                       # 完全在 _A 内
_OUT_LINE = 'LINESTRING (20 20, 30 30)'
_PT_IN = 'POINT (5 5)'
_PT_OUT = 'POINT (99 99)'
_LX = 'LINESTRING (0 0, 10 0)'
_LY = 'LINESTRING (5 -5, 5 5)'
_LCOLL = 'LINESTRING (5 0, 15 0)'


def truth_table():
    """``(名字, wkt_a, wkt_b, 算子, 期望面积, 期望线长, 期望空)``。

    每一条都是**闭式解**,不看实现。`None` = 这一项不断言(例如"面 × 线"的并集
    线长含面的周长,闭式解写得出来但没有信息量)。
    """
    sq, cross = 100.0, 20.0
    return [
        # --- 面 × 面:四个算子给出四个不同的面积 ------------------------
        ('area_overlap_inter', _A, _B, 'intersection', 25.0, None, False),
        ('area_overlap_union', _A, _B, 'union', 175.0, None, False),
        ('area_overlap_diff', _A, _B, 'difference', 75.0, None, False),
        ('area_overlap_sym', _A, _B, 'symmetric_difference', 150.0, None, False),
        # --- 相离 -------------------------------------------------------
        ('far_inter', _A, _FAR, 'intersection', 0.0, 0.0, True),
        ('far_union', _A, _FAR, 'union', 200.0, None, False),
        ('far_diff', _A, _FAR, 'difference', 100.0, None, False),
        ('far_sym', _A, _FAR, 'symmetric_difference', 200.0, None, False),
        # --- A ⊂ B:差值必须为空(否掉"逐边看标签"那个朴素做法)----------
        ('subset_diff', _SMALL, _A, 'difference', 0.0, 0.0, True),
        ('subset_inter', _SMALL, _A, 'intersection', 4.0, None, False),
        ('subset_union', _SMALL, _A, 'union', 100.0, None, False),
        ('subset_sym', _SMALL, _A, 'symmetric_difference', 96.0, None, False),
        # --- 完全相同 ---------------------------------------------------
        ('same_diff', _A, _A, 'difference', 0.0, 0.0, True),
        ('same_sym', _A, _A, 'symmetric_difference', 0.0, 0.0, True),
        ('same_union', _A, _A, 'union', 100.0, None, False),
        # --- 只共一条边 → intersection 降维成线 --------------------------
        ('edge_inter', _A, _EDGE_B, 'intersection', 0.0, 10.0, False),
        ('edge_union', _A, _EDGE_B, 'union', 200.0, None, False),
        ('edge_diff', _A, _EDGE_B, 'difference', 100.0, None, False),
        # --- 只共一个角 → intersection 降维成点 --------------------------
        ('corner_inter', _A, _CORNER_B, 'intersection', 0.0, 0.0, False),
        ('corner_union', _A, _CORNER_B, 'union', 200.0, None, False),
        # --- 洞被填平 ---------------------------------------------------
        ('hole_filled', _HOLED, _PLUG, 'union', 100.0, None, False),
        # ⚠️ 交集**不是空的**:洞的边界与 plug 的边界**逐段重合**,按"两输入边界
        #    重合即降维成线"那条(与共边同一规则)结果是那四条边 —— 实为
        #    ``MULTILINESTRING``,总长 16(4×4 的周长)。实测 GEOS 同款。这里用
        #    线长断言而不写死的 WKT:下一条断言的是"洞确实被填平"。
        ('hole_inter', _HOLED, _PLUG, 'intersection', 0.0, 16.0, False),
        ('hole_sym', _HOLED, _PLUG, 'symmetric_difference', 100.0, None, False),
        # --- 面 × 线:线**不是**面的边界(delta 恒为 0)-----------------
        ('line_cross_inter', _A, _CROSS, 'intersection', 0.0, 10.0, False),
        ('line_cross_diff', _A, _CROSS, 'difference', 100.0, None, False),
        ('line_inside_inter', _A, _INSIDE, 'intersection', 0.0, 6.0, False),
        ('line_inside_union', _A, _INSIDE, 'union', 100.0, None, False),
        ('line_inside_diff', _A, _INSIDE, 'difference', 100.0, None, False),
        ('line_inside_sym', _A, _INSIDE, 'symmetric_difference', 100.0, None,
         False),
        ('line_out_inter', _A, _OUT_LINE, 'intersection', 0.0, 0.0, True),
        # --- 点 × 面 ----------------------------------------------------
        ('pt_in_union', _A, _PT_IN, 'union', 100.0, None, False),
        ('pt_out_union', _A, _PT_OUT, 'union', 100.0, None, False),
        ('pt_out_inter', _A, _PT_OUT, 'intersection', 0.0, 0.0, True),
        ('pt_in_diff', _A, _PT_IN, 'difference', 100.0, None, False),
        ('pt_out_diff', _A, _PT_OUT, 'difference', 100.0, None, False),
        # --- 线 × 线 ----------------------------------------------------
        ('lx_ly_inter', _LX, _LY, 'intersection', 0.0, 0.0, False),
        ('lx_ly_union', _LX, _LY, 'union', 0.0, 20.0, False),
        ('lx_coll_inter', _LX, _LCOLL, 'intersection', 0.0, 5.0, False),
        ('lx_coll_diff', _LX, _LCOLL, 'difference', 0.0, 5.0, False),
        ('lx_coll_union', _LX, _LCOLL, 'union', 0.0, 15.0, False),
        # --- 点 × 点 ----------------------------------------------------
        ('pt_same_inter', _PT_IN, _PT_IN, 'intersection', 0.0, 0.0, False),
        ('pt_same_diff', _PT_IN, _PT_IN, 'difference', 0.0, 0.0, True),
        ('pt_apart_inter', _PT_IN, _PT_OUT, 'intersection', 0.0, 0.0, True),
    ]


def run_truth_table(verbose=True):
    """跑手推真值表,返回不符的条数。**不需要 oracle。**"""
    from pyopenfilegdb.geometry import Geometry

    bad = 0
    for name, wa, wb, op, want_area, want_len, want_empty in truth_table():
        try:
            a, b = Geometry.from_wkt(wa), Geometry.from_wkt(wb)
            r = getattr(a, op)(b)
        except Exception as exc:                            # noqa: BLE001
            if verbose:
                print(f'  ✗ {name}: 抛异常 {exc!r}')
            bad += 1
            continue
        why = []
        if bool(r.is_empty) != want_empty:
            why.append(f'空 {r.is_empty} != {want_empty}')
        elif not want_empty:
            tol = 1e-9 * max(1.0, want_area)
            if abs(r.area() - want_area) > tol:
                why.append(f'面积 {r.area()!r} != {want_area!r}')
            if want_len is not None and abs(r.length() - want_len) > 1e-9:
                why.append(f'线长 {r.length()!r} != {want_len!r}')
        if why:
            bad += 1
            if verbose:
                print(f'  ✗ {name} ({op}): ' + '; '.join(why))
                print(f'      {r.wkt()}')
    return bad


# --------------------------------------------------------------------------
# 用例池:合成 + 固定种子随机 + 真实语料
# --------------------------------------------------------------------------
#: 合成几何按**维度**分组,好按"维度组合"抽样而不是全笛卡尔积。
_SYNTHETIC = {
    2: [
        _A, _B, _FAR, _SMALL, _HOLED, _PLUG, _EDGE_B, _CORNER_B,
        'POLYGON ((0 0, 4 0, 4 4, 0 4, 0 0))',
        'POLYGON ((0 0, 4 0, 4 4, 0 4, 0 0), (1 1, 3 1, 3 3, 1 3, 1 1))',
        'MULTIPOLYGON (((0 0, 4 0, 4 4, 0 4, 0 0)), ((10 10, 14 10, 14 14, 10 14, 10 10)))',
        'POLYGON ((2 -2, 4 -2, 4 2, 2 2, 2 -2))',       # 与 _A 部分交于下边
    ],
    1: [
        _CROSS, _INSIDE, _OUT_LINE, _LX, _LY, _LCOLL,
        'LINESTRING (0 0, 10 10)', 'LINESTRING (10 0, 0 10)',
        'LINESTRING (0 0, 10 0, 10 10, 0 10, 0 0)',
        'MULTILINESTRING ((0 0, 10 0), (10 0, 10 10))',
        'LINESTRING (2 5, 8 5, 8 8)',
    ],
    0: [
        _PT_IN, _PT_OUT, 'POINT (0 0)', 'POINT (10 10)',
        'MULTIPOINT ((1 2), (3 4))', 'MULTIPOINT ((5 5), (99 99))',
    ],
}

#: 位置关系:每个关系一条"怎么造第二个几何"的规则(固定种子下随机化)。
_RELATION = ('intersect', 'disjoint', 'contained', 'edge', 'same')


def _rand_ring(rnd, x, y, w, h, n):
    """一个简单的凸-ish 环:绕中心按角度铺点再收尾闭合。"""
    import math
    cx, cy = x + w / 2.0, y + h / 2.0
    pts = []
    for i in range(n):
        ang = 2.0 * math.pi * i / n
        rr = 0.5 + rnd.uniform(-0.12, 0.12)
        px = cx + rr * w * math.cos(ang)
        py = cy + rr * h * math.sin(ang)
        pts.append('%.6f %.6f' % (px, py))
    pts.append(pts[0])
    return 'POLYGON ((%s))' % ', '.join(pts)


def _random_pairs(rnd, n, max_verts=24):
    """固定种子的随机对:一个基准环 + 一个按 :data:`_RELATION` 造出来的环/线/点。"""
    out = []
    for _ in range(n):
        x = rnd.uniform(-1000.0, 1000.0)
        y = rnd.uniform(-1000.0, 1000.0)
        w = rnd.uniform(1.0, 100.0)
        h = rnd.uniform(1.0, 100.0)
        nv = rnd.randint(3, max_verts)
        a = _rand_ring(rnd, x, y, w, h, nv)
        rel = rnd.choice(_RELATION)
        if rel == 'same':
            b = a
        elif rel == 'contained':
            b = _rand_ring(rnd, x + w * 0.25, y + h * 0.25, w * 0.5, h * 0.5,
                           max(3, nv // 2))
        elif rel == 'disjoint':
            b = _rand_ring(rnd, x + 5 * w, y + 5 * h, w, h, nv)
        else:                                   # intersect / edge:重叠一半
            b = _rand_ring(rnd, x + w * 0.5, y + h * 0.5, w, h, nv)
        out.append(('rand_%s_%d' % (rel, nv), a, b))
    return out


def _random_other_dim(rnd, n):
    """线 / 点输入:拿随机环造横穿的线、面内的线、面外的点、面内的点。"""
    out = []
    for i in range(n):
        x = rnd.uniform(-100.0, 100.0)
        y = rnd.uniform(-100.0, 100.0)
        w = rnd.uniform(1.0, 50.0)
        h = rnd.uniform(1.0, 50.0)
        a = _rand_ring(rnd, x, y, w, h, rnd.randint(3, 12))
        mx, my = x + w / 2.0, y + h / 2.0
        kind = i % 4
        if kind == 0:                           # 横穿(两端都在面外)
            b = 'LINESTRING (%.6f %.6f, %.6f %.6f)' % (
                x - w, my, x + 2 * w, my)
        elif kind == 1:                         # 完全在面内
            b = 'LINESTRING (%.6f %.6f, %.6f %.6f)' % (mx, my, mx + 1e-6, my)
        elif kind == 2:                         # 面外的点
            b = 'POINT (%.6f %.6f)' % (x + 20 * w, y + 20 * h)
        else:                                   # 面内的点
            b = 'POINT (%.6f %.6f)' % (mx, my)
        out.append(('randdim_%d' % kind, a, b))
    return out


def collect_corpus(gdb_paths, n_feat, max_verts):
    """从 ``.gdb`` 里抽相邻 / 相交的要素对 —— 真实行政区**大量共边**,最有价值。

    几何用**本库读出来的**(照 ``verify_buffer.py`` 的规矩),这样喂给 oracle 的
    两个输入与本库算的是逐位同一份。只取无 Z/M 的 ``polygon``:四个算子对线与点
    也实现,但真实语料里"相邻/共边"这块价值最高的就是面。
    """
    from pyopenfilegdb import OpenFileGDB

    out = []
    for path in gdb_paths:
        try:
            db = OpenFileGDB.open(path)
        except Exception as exc:                            # noqa: BLE001
            print(f'  ({os.path.basename(path)}: 打不开 {exc!r})')
            continue
        for lname in db.list_layers():
            try:
                lyr = db.get_layer(lname)
            except Exception:                               # noqa: BLE001
                continue
            if lyr.geometry_field is None:
                continue
            feats = []
            try:
                for feat in lyr.read_features():
                    g = feat.geometry
                    if (g is None or g.is_empty or g.kind != 'polygon'
                            or g.has_z or g.has_m):
                        continue
                    if g.point_count > max_verts:
                        continue
                    feats.append(g)
                    if len(feats) >= n_feat:
                        break
            except Exception as exc:                        # noqa: BLE001
                print(f'  ({lname}: 读不动 {exc!r})')
                continue
            if feats:
                print(f'  语料 {lname}: 取 {len(feats)} 条'
                      f'(各 ≤{max_verts} 顶点,无 Z/M)')
            # 相邻 = 包围盒相交。真共边的对就在这里面。
            for i in range(len(feats)):
                for j in range(i + 1, len(feats)):
                    ga, gb = feats[i], feats[j]
                    ea, eb = ga.envelope(), gb.envelope()
                    if ea is None or eb is None:
                        continue
                    if (ea[2] < eb[0] or eb[2] < ea[0]
                            or ea[3] < eb[1] or eb[3] < ea[1]):
                        continue
                    out.append((f'{os.path.basename(path)}:'
                                f'{lname}[{i},{j}]',
                                ga.wkt(), gb.wkt()))
        db.close()
    return out


# --------------------------------------------------------------------------
# 差分
# --------------------------------------------------------------------------
_TOL_REL = 1e-9


def _struct_key(stats):
    """成员表 -> 可比对的不变量(面积/线长按容差取整,顶点数原样)。"""
    return tuple((round(a, 6), round(length, 6), npts)
                 for _t, a, length, npts in stats['members'])


#: JTS 与 GEOS 的一处**既有分歧**:结果里的**线**要不要按节点合并成更长的链。
#: JTS `LineBuilder.addResultLines()`(源码 225-234 行)是**逐条节点边**输出,
#: 而合并版 `addResultLinesMerged()` 上面明写着
#: ``NOT USED currently. Instead the raw noded edges are output. This matches the
#: original overlay semantics. It is also faster.`` —— GEOS 那边把合并打开了。
#: 本库照 JTS(计划 §二:GDAL 这四个算子只是一句转发给 GEOS,没有 C++ 可抄,
#: 所以上游取 JTS 的 ``operation/overlayng``)。同一套线于是被切成更多段:
#: **集合完全相同**(对称差那一关已经过了),只有段数不同。
#: ⚠️ 这条要**显式列出来**,不许静默放过 —— 见 :func:`compare`。
_LINE_SPLIT_REASON = ('线结果的分段:JTS 逐条节点边输出(LineBuilder.addResultLines),'
                      'GEOS 合并成最长的链;集合相同,只是线被切成几段不同')

#: **"对称差的线长"不能当判据** —— 这是 :func:`compare` 里那条腿的由来,别再改回去。
#:
#: 最小复现(与两边实现都无关,纯 GEOS):取同一个环,把一个顶点的 y 动 **1 ulp**
#: (5.684e-14)。两个环的面积差 5.03e-14、**周长差 6.75e-14**,而 GEOS 自己的
#: ``symmetric_difference`` 给回来的是**周长 4.75** 的退化环::
#:
#:     ring1[2] = (353.14645366986321, 401.35687455505717)
#:     ring2[2] = (353.14645366986321, 401.35687455505712)   # 只有 1 ulp
#:     ring1.length - ring2.length = 6.75e-14
#:     symmetric_difference(ring1, ring2).length = 4.753e+00   ← 差了两个数量级
#:
#: 原因是 GEOS 对**近重合**输入的 overlay 自身数值不稳:它把两个几乎同一个顶点
#: 的节点化成两个节点,还回来一个"出去再原路退回"的退化环 —— 面积 0,周长却是
#: 那条尖刺的两倍。所以拿它当"集合是否相同"的判据,会把**同一集合、顶点表逐个
#: 相同、只有一个坐标差 1 ulp**的两个结果判成不同。
#:
#: 于是改成三条腿(全部由 oracle 算,不碰本库的谓词):**对称差面积** +
#: **两个结果总长之差** + **Hausdorff 距离**。前两条抓"少一块/多一块/多一根尖刺",
#: 第三条抓"顶点被挪开但面积线长都不变"(点结果)。三条腿一起,真 bug 依然逃不掉:
#: 少一段(线长差)、多一个洞/环(面积差)、多一根尖刺(线长差)、顶点挪位
#: (Hausdorff),各有各的腿接着。
_XOR_LENGTH_ARTIFACT = ('对称差线长不参与判定:GEOS 对近重合输入的对称差自身不稳'
                        '(差 1 ulp 的两个环能差出周长 4.75 的退化环),改用'
                        ' 面积 + 两者总长之差 + Hausdorff 三条腿')


def _member_class(type_name):
    """按类型名归到 **面 / 线 / 点** 三档(两种 oracle 的大小写不同,不敏感比)。"""
    upper = type_name.upper()
    if 'POLYGON' in upper:
        return 2
    if 'LINE' in upper:
        return 1
    return 0


def _only_line_split_differs(st_mine, st_theirs):
    """结构确实不同 —— 但不同之处**只在"线结果被切成几段"**吗?

    判据是"除去线成员的分段以外,别的都对得上":

    * 维度、**面积**、**总长度**三项相等(总长度把面的周长也算进去,所以它同时
      钉住了面的周长);
    * 面成员的**个数与各自的 (面积, 线长)** 逐一相等;
    * 点成员的个数相等(点的位置由"集合"那一关负责 —— 对称差已经为 0);
    * 两边都**确实有**线成员,且线成员的**总长**相等。

    所以这不是"宽松判定":真出 bug 的结构差异(比如 GC 里该摊开的面没摊开、
    带洞的面被装配成两块)会先在成员个数 / 面积上露出来,一律拦在这之前。
    这里放过的是"同一套线、只是段数不同"。
    """
    if st_mine['dim'] != st_theirs['dim']:
        return False
    for key in ('area', 'length'):
        if abs(st_mine[key] - st_theirs[key]) > 1e-6:
            return False
    area_m = [m for m in st_mine['members'] if _member_class(m[0]) == 2]
    area_t = [m for m in st_theirs['members'] if _member_class(m[0]) == 2]
    pt_m = [m for m in st_mine['members'] if _member_class(m[0]) == 0]
    pt_t = [m for m in st_theirs['members'] if _member_class(m[0]) == 0]
    line_m = [m for m in st_mine['members'] if _member_class(m[0]) == 1]
    line_t = [m for m in st_theirs['members'] if _member_class(m[0]) == 1]
    if len(area_m) != len(area_t) or len(pt_m) != len(pt_t):
        return False
    if [(round(m[1], 6), round(m[2], 6)) for m in area_m] != \
       [(round(m[1], 6), round(m[2], 6)) for m in area_t]:
        return False
    if not line_m and not line_t:
        return False                    # 两边一条线都没有,那不属于这一类
    return (abs(sum(m[2] for m in line_m) - sum(m[2] for m in line_t)) <= 1e-6)


def compare(oracle, cases, tol_rel=_TOL_REL):
    """逐对逐算子比。

    返回 ``(总对数, 全同对数, 超差列表, 排除统计, 对称差严格为空的对数)``。
    最后一项是"全同"里最硬的那一档 —— 容差内的相等与逐位相同不是一回事,
    报告里要把两个数分开写(见 :func:`main`)。
    """
    from pyopenfilegdb.geometry import Geometry

    total = identical = exact = 0
    bad = []
    excluded = {}
    t0 = time.time()
    for name, wa, wb, op in cases:
        try:
            mine = getattr(Geometry.from_wkt(wa), op)(Geometry.from_wkt(wb))
        except NotImplementedError as exc:
            excluded[f'本库明说不支持:{exc}'] = \
                excluded.get(f'本库明说不支持:{exc}', 0) + 1
            continue
        except Exception as exc:                            # noqa: BLE001
            bad.append((f'{name}|{op}', f'本库抛异常 {exc!r}'))
            continue
        try:
            theirs = oracle.overlay(wa, wb, op)
        except Exception as exc:                            # noqa: BLE001
            bad.append((f'{name}|{op}', f'oracle 抛异常 {exc!r}'))
            continue
        if theirs is None:
            return total, identical, [('', 'oracle 不支持 overlay')], excluded, exact
        total += 1
        strict_set = False
        label = f'{name}|{op}'
        mine_wkt = mine.wkt()
        st_mine = oracle.stats(mine_wkt)
        st_theirs = oracle.stats(theirs)
        if not oracle.gc_area_ok and (st_mine['type'].startswith('GEOMETRY')
                                      or st_theirs['type'].startswith('GEOMETRY')):
            total -= 1
            excluded['oracle 的 GC 面积恒为 0,比不了集合与成员面积'
                     '(GDAL 的 get_Area())'] = \
                excluded.get('oracle 的 GC 面积恒为 0,比不了集合与成员面积'
                             '(GDAL 的 get_Area())', 0) + 1
            continue
        # ① 集合:三条腿都要在容差内 —— 见 _XOR_LENGTH_ARTIFACT
        xor = oracle.xor_stats(mine_wkt, theirs)
        diff_a = max(0.0, xor['area'])
        xor_len = max(0.0, xor['length'])
        scale = max(1.0, abs(st_theirs['area']), abs(st_theirs['length']))
        tol = tol_rel * scale + 1e-9
        # 腿一:对称差面积。任何"真少一块 / 真多一块"都在这里露。
        # 腿二:**两个结果各自的总长之差**。⚠️ 不是对称差的线长(理由见常量)。
        # 腿三:Hausdorff。抓"顶点挪开但面积线长都不变"的那种(点结果)。
        diff_len = abs(st_mine['length'] - st_theirs['length'])
        haus = oracle.hausdorff(mine_wkt, theirs)
        if haus is None:                # oracle 没这条腿 -> 退回更松的对称差线长
            len_bad = xor_len > tol
        else:
            len_bad = diff_len > tol or haus > tol
        if diff_a > tol or len_bad:
            detail = (f'对称差 面积 {diff_a:.3e} 线长 {xor_len:.3e};'
                      f'两者总长差 {diff_len:.3e};'
                      + ('Hausdorff 比不了' if haus is None
                         else f'Hausdorff {haus:.3e}')
                      + f' (tol {tol:.3e})')
            bad.append((label, f'集合不同:{detail};本库 {mine_wkt[:90]};'
                               f'参照 {st_theirs["type"]}'))
            continue
        if xor['empty']:
            strict_set = True
        # ② 结构:顶层成员表必须一样(专治 GC 装配)
        if _struct_key(st_mine) != _struct_key(st_theirs):
            if _only_line_split_differs(st_mine, st_theirs):
                excluded[_LINE_SPLIT_REASON] = \
                    excluded.get(_LINE_SPLIT_REASON, 0) + 1
                continue
            bad.append((label, f'结构不同:本库成员表 {st_mine["members"]};'
                               f'参照 {st_theirs["members"]}'))
            continue
        identical += 1
        if strict_set:
            exact += 1
    if total and (time.time() - t0) > 5.0:
        print(f'  (差分耗时 {time.time() - t0:.1f}s)')
    return total, identical, bad, excluded, exact


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def _build_cases(args, rnd):
    """按"维度组合 × 位置关系"抽样,不做全笛卡尔积。"""
    cases = []
    # 合成:同维两两 + 跨维,取前若干个组合
    dims = sorted(_SYNTHETIC, reverse=True)
    for i, da in enumerate(dims):
        for db in dims[i:]:
            for wa in _SYNTHETIC[da]:
                for wb in _SYNTHETIC[db]:
                    for op in OVERLAY_OPS:
                        cases.append(('syn_%d%d' % (da, db), wa, wb, op))
    rnd.shuffle(cases)
    cases = cases[:args.max_pairs]
    # 随机
    for name, wa, wb in _random_pairs(rnd, args.random, args.max_verts):
        for op in OVERLAY_OPS:
            cases.append((name, wa, wb, op))
    for name, wa, wb in _random_other_dim(rnd, args.random):
        for op in OVERLAY_OPS:
            cases.append((name, wa, wb, op))
    return cases


def perf_report(corpus, n=12, ops=OVERLAY_OPS):
    """前 ``n`` 对相邻要素各跑一遍四个算子,报 ms/次(计划 §六.4:留一个实测数)。

    ⚠️ 必须**连输入规模一起报**(顶点数)。单说"ms/次"没有意义:noder 的候选对
    扫描、面深度的分量划分都与顶点数强相关,真实行政区一个面几百到几千个顶点,
    与合成用例那几个顶点不是一回事。解析(WKT)的时间不计入 —— 量的是算子本身。
    """
    from pyopenfilegdb.geometry import Geometry

    pairs = corpus[:n]
    if not pairs:
        return
    geoms, sizes = [], []
    for _name, wa, wb in pairs:
        ga, gb = Geometry.from_wkt(wa), Geometry.from_wkt(wb)
        geoms.append((ga, gb))
        sizes.append(ga.point_count + gb.point_count)
    t0 = time.perf_counter()
    calls = 0
    for ga, gb in geoms:
        for op in ops:
            getattr(ga, op)(gb)
            calls += 1
    dt = time.perf_counter() - t0
    sizes.sort()
    per_op = 1000.0 * dt / calls
    print(f'  性能:前 {len(geoms)} 对相邻要素 × {len(ops)} 算子 = {calls} 次,'
          f'**{per_op:.1f} ms/次**(= {per_op * len(ops):.1f} ms/对;'
          f'耗时合计 {dt:.2f}s;输入顶点数中位数 {sizes[len(sizes) // 2]},'
          f'两端 {sizes[0]}~{sizes[-1]})')


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description='本库 overlay 与 GEOS/GDAL 的差分对拍')
    ap.add_argument('--oracle', default='auto',
                    choices=('auto', 'shapely', 'gdal'))
    ap.add_argument('--gdb', action='append', default=[],
                    help='语料 .gdb 路径,可重复;默认扫 D:/work/*.gdb')
    ap.add_argument('--synthetic-only', action='store_true')
    ap.add_argument('--features', type=int, default=12,
                    help='每层取多少条要素(默认 12)')
    ap.add_argument('--max-verts', type=int, default=600)
    ap.add_argument('--max-pairs', type=int, default=600,
                    help='合成用例最多几对(默认 600,不做全笛卡尔积)')
    ap.add_argument('--random', type=int, default=40,
                    help='随机几何各造几对(默认 40)')
    ap.add_argument('--seed', type=int, default=20260930)
    ap.add_argument('--perf', type=int, default=12,
                    help='真实语料里取前几对跑性能数(默认 12;0 = 不跑)')
    ap.add_argument('--no-truth', action='store_true',
                    help='跳过手推真值表')
    args = ap.parse_args(argv)

    rc = 0

    # ---- 第 1 部分:手推真值(不需要 oracle,任何机器都跑) ----
    if not args.no_truth:
        print('== 手推真值表(闭式解,不依赖任何 oracle)==')
        bad_truth = run_truth_table(verbose=True)
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
    if not getattr(oracle, 'supports_overlay', False):
        print(f'  ✗ {oracle.name} 没有 overlay —— 本脚本无可比项。'
              f'这**不是通过**,是没比成。')
        return 1

    rnd = random.Random(args.seed)
    cases = _build_cases(args, rnd)
    print(f'  合成 + 随机:{len(cases)} 对')

    total, identical, bad, excluded, exact = compare(oracle, cases)
    print(f'  合成 + 随机:比了 {total} 对,集合与结构全同 {identical}'
          f'(其中对称差严格为空 {exact}),超差 {len(bad)}')
    for label, why in bad[:12]:
        print(f'    ✗ {label}: {why}')
    if bad:
        rc = 1

    # ---- 真实语料 ----
    if not args.synthetic_only:
        gdb_paths = args.gdb or sorted(glob.glob('D:/work/*.gdb'))
        if gdb_paths:
            print('\n== 真实语料(相邻要素对,共边最有价值)==')
            corpus = collect_corpus(gdb_paths, args.features, args.max_verts)
            if corpus:
                ccases = [(n, a, b, op) for n, a, b in corpus
                          for op in OVERLAY_OPS]
                print(f'  相邻对 {len(corpus)} × 4 算子 = {len(ccases)} 对')
                total2, id2, bad2, ex2, exact2 = compare(oracle, ccases)
                print(f'  语料:比了 {total2} 对,集合与结构全同 {id2}'
                      f'(其中对称差严格为空 {exact2}),超差 {len(bad2)}')
                for label, why in bad2[:12]:
                    print(f'    ✗ {label}: {why}')
                total += total2
                identical += id2
                exact += exact2
                for k, v in ex2.items():
                    excluded[k] = excluded.get(k, 0) + v
                if bad2:
                    rc = 1
                perf_report(corpus, args.perf)
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
    print(f'比了 {total} 对;集合与结构全同 {identical} 对 '
          f'({100.0 * identical / total:.1f}%)'
          f'({"无超差" if not rc else "有超差"})')
    print(f'  其中对称差**严格为空**(连一个点都不差){exact} 对;'
          f'容差内相等 {identical - exact} 对'
          f'(坐标差到 tol 量级,典型就是计算出来的交点差 1 ulp)')
    print('  判据三条腿(oracle 侧算,不碰本库谓词):对称差面积 + 两结果总长之差 '
          '+ Hausdorff')
    if not getattr(oracle, 'has_hausdorff', False):
        print('  ⚠️ 本 oracle 没有 Hausdorff,第三条腿退回更松的"对称差线长"——'
              '近退化构型会因此多报超差(见 _XOR_LENGTH_ARTIFACT 的最小复现)。')
    if identical == 0:
        print('⚠️ 一对都没全同 —— 判定可能在替真 bug 打掩护,'
              '先把一个简单用例(重叠方块)对到全同再谈别的。')
    return rc


if __name__ == '__main__':
    sys.exit(main())
