#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""全量语料 WKT 出口体检:每条几何都要能 `wkt()` → `from_wkt()` 走一个来回。

为什么这条非要有,**而 `tests/test_geometry.py::TestWkt` 还不够**
--------------------------------------------------------------
`TestWkt` 断言的是**精确字符串**(合法与否是给别人看的,不是给自己解析的),
但它的输入是**手写的**几十个 WKT —— 覆盖的是"我想到的构型"。真实语料里
23,967 条面里有什么,只有扫一遍才知道。而这个工具断言的是**另外四件事**:

1. `wkt()` 产出的文本 `from_wkt` **肯不肯收**(结构上合法);
2. 读回来的 `kind` / `has_z` / `has_m` **一个字都不能变**;
3. `point_count` 不变;
4. `wkt()` **再导一次文本逐位相同**(幂等 —— 不稳的文本没法拿去比对/哈希)。

⚠️ **第 2 条是有来历的**:`M` 维曾经被静默读回成 `Z`(`LINESTRING M (...)` 导出时
后缀被吞,而 WKT1 里没有后缀的三坐标就是 Z),`point_count` 同理能抓住"环的闭合点
被多补一次/少补一次"。这些在**只比坐标**的往返里全都看不出来。

⚠️ **不要把这个加进 `tests/`。** 全语料跑一遍是 **~700 s**、而且必须有机读语料
(有些机器上根本没有 `D:/work/*.gdb`)。它是**上线前的体检**,不是单元测试。

用法::

    python tools/verify_wkt_roundtrip.py                    # 扫 D:/work/*.gdb
    python tools/verify_wkt_roundtrip.py some.gdb other.gdb
    python tools/verify_wkt_roundtrip.py --limit 2000        # 冒烟

退出码:0 = 全过;1 = 有失败,**或者一条几何都没读到**。
⚠️ 后者是**从 `tools/verify_topology.py` 的"假绿"事故里学来的**:那个工具曾经
在比了 0 对的情况下打印"逐对一致 ✓"、`exit 0`。一个什么都没检查却在报平安的
体检脚本,比没有体检脚本更坏。
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ⚠️ Windows 默认控制台是 GBK,编码不了 `✓` / `⚠️`。见 verify_topology.py 里的
# 同一段说明 —— 崩在"打印报告"那一步是最难查的,因为一路设了
# PYTHONIOENCODING=utf-8 的开发机碰不到。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):
        pass

#: 每类失败最多打几行样本 —— 真实语料上错起来是成片的,全打完只会刷屏。
_MAX_SAMPLES = 5


def check_one(lname, oid, g):
    """返回 ``None`` 表示通过,否则返回 ``(类别, 说明)``。"""
    from pyopenfilegdb.geometry import Geometry
    try:
        text = g.wkt()
    except Exception as exc:                                # noqa: BLE001
        return ('导出抛异常', f'{type(exc).__name__}: {exc}')
    try:
        back = Geometry.from_wkt(text)
    except Exception as exc:                                # noqa: BLE001
        return ('解析失败', f'{type(exc).__name__}: {exc} | {text[:100]}')
    if back.kind != g.kind:
        return ('kind 变了', f'{g.kind} -> {back.kind}')
    if (back.has_z, back.has_m) != (g.has_z, g.has_m):
        return ('维度变了',
                f'Z {g.has_z}->{back.has_z}  M {g.has_m}->{back.has_m}')
    if back.point_count != g.point_count:
        return ('顶点数变了', f'{g.point_count} -> {back.point_count}')
    back_text = back.wkt()
    if back_text != text:
        return ('文本不稳定', f'{text[:80]} | 回程 {back_text[:80]}')
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('gdb', nargs='*', help='语料库(默认扫 D:/work/*.gdb)')
    ap.add_argument('--limit', type=int, default=0,
                    help='总共最多检查多少条(0 = 不限)')
    args = ap.parse_args()

    env = os.environ.get('PYOPENFILEGDB_TEST_GDB', '')
    paths = args.gdb or ([d for d in env.split(os.pathsep) if d and os.path.isdir(d)]
                         if env else sorted(glob.glob('D:/work/*.gdb')))
    if not paths:
        print('没找到语料库 —— 传路径,或设 PYOPENFILEGDB_TEST_GDB,'
              '或把 gdb 放到 D:/work/。')
        return 1

    from pyopenfilegdb import OpenFileGDB

    n = 0
    kinds = {}
    fails = []
    per_layer = {}
    t0 = time.perf_counter()
    for path in paths:
        print(f'-- {path} --')
        try:
            db = OpenFileGDB.open(path)
        except Exception as exc:                            # noqa: BLE001
            print(f'   打不开:{exc}')
            continue
        try:
            for lname in db.list_layers():
                try:
                    lyr = db.get_layer(lname)
                except Exception:                           # noqa: BLE001
                    continue
                if lyr.geometry_field is None:
                    continue
                cnt = bad = 0
                for feat in lyr.read_features():
                    g = feat.geometry
                    if g is None or g.is_empty:
                        continue
                    if g.kind == 'multipatch':
                        continue            # WKT 出口对它主动抛错,不是缺陷
                    cnt += 1
                    n += 1
                    kinds[g.kind] = kinds.get(g.kind, 0) + 1
                    why = check_one(lname, feat.oid, g)
                    if why is not None:
                        bad += 1
                        fails.append((f'{lname}#{feat.oid}', why[0], why[1]))
                    if args.limit and n >= args.limit:
                        break
                if cnt:
                    per_layer[lname] = (cnt, bad)
                if args.limit and n >= args.limit:
                    break
        finally:
            db.close()
        if args.limit and n >= args.limit:
            print(f'   (达到 --limit {args.limit},停止)')
            break

    dt = time.perf_counter() - t0
    for lname, (cnt, bad) in per_layer.items():
        print(f'   {lname:16s} {cnt:7d} 条,失败 {bad}')
    print(f'\n共 {n} 条几何  类型 {kinds}  用时 {dt:.1f}s')

    if fails:
        print(f'\n!! 失败 {len(fails)} 条,按类别:')
        by_kind = {}
        for _, k, _ in fails:
            by_kind[k] = by_kind.get(k, 0) + 1
        for k, v in sorted(by_kind.items(), key=lambda kv: -kv[1]):
            print(f'   {k:14s} {v}')
        print('\n样本:')
        for tag, k, detail in fails[:_MAX_SAMPLES]:
            print(f'   [{k}] {tag}\n       {detail}')
        return 1

    # ⚠️ 硬闸门:一条都没读到 = 脚本废了,不是通过。见模块 docstring。
    if n == 0:
        print('!! **一条几何都没读到** —— 这不是通过,是脚本废了。')
        print('   语料路径对不对?图层有几何字段吗?')
        return 1

    print('全部通过 ✓')
    return 0


if __name__ == '__main__':
    sys.exit(main())
