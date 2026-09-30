#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""差分验证:C 加速路径 vs 纯 Python 路径,几何结果必须**逐位相同**。

这是加速模块能不能留的唯一判据 —— 两条实现并存,只有能证明等价才有意义
(见 STATUS.md / DESIGN.md §2.19.4)。

三层证据
--------
1. **真实数据对拍**。遍历样例库的全部图层,把每条要素的几何 blob 分别用
   两条路解一遍,比 ``shape_type`` / ``has_z`` / ``has_m`` / ``coordinates``
   / WKT **全部逐位相同**,并且 ``decode_geometry_ex`` 返回的**消费位置**
   也要相同 —— 位置一致才说明 ``dr.pos`` 的回写时机没写错。
   跑全量纯 Python 太慢(``村行政区划`` 4,494 万顶点要 59 s),所以按
   **顶点预算**抽样,默认 200 万顶点(约 2.6 s);``--all`` 关掉预算。

2. **定向损坏用例**。截断的 blob、被改写成天文数字的点数、超过 8 个续字节
   的超长 varint —— 三条都要求两条路**抛同一种异常**,而且必须是
   ``GdbFormatError``(不是 IndexError 漏出去、不是 MemoryError 撑爆内存)。
   这是 ``_gdbaccel.c`` 里三道闸的验收条件。

3. **随机 fuzz**。随机字节流喂两个 C 入口,与工具内写的一份**参考实现**
   对拍。参考实现把 C 的语义(接受域上限、64 位累加器绕回)用 Python 写死,
   所以能覆盖真实数据碰不到的垃圾输入。

⚠️ 为什么 ``peek_envelope`` 不在对拍范围里
----------------------------------------
它只读 blob 头部的 4 个 varuint,走的是另一条路径,**不经过**加速边界,
所以天然不可能因为加速而改变。这里反过来用它当**校准**:抽样比一下"存储
包围盒"和"逐点解出来的实际包围盒"是否吻合 —— 如果 ``dr.pos`` 回写错了,
整条数组会错位,这个比对立刻就能发现。

用法::

    python tools/verify_accel.py                     # 扫 D:/work/*.gdb
    python tools/verify_accel.py D:/work/xxx.gdb
    python tools/verify_accel.py --all               # 关掉顶点预算
"""
from __future__ import annotations

import glob
import os
import random
import sys
from array import array
from itertools import chain

_HERE = os.path.dirname(os.path.abspath(__file__))
# tools/ 的上一级 = 仓库根(pyopenfilegdb 包在这儿);_HERE 留给同目录的 import。
sys.path[:0] = [os.path.dirname(_HERE), _HERE]

from pyopenfilegdb import OpenFileGDB                       # noqa: E402
from pyopenfilegdb import _accel                            # noqa: E402
from pyopenfilegdb import _esri_geometry as G               # noqa: E402
from pyopenfilegdb._datatypes import GdbFormatError, _LazyGeometry  # noqa: E402

#: 纯 Python 那条路最多解多少顶点(它才是瓶颈)。
PY_VERTEX_BUDGET = 2_000_000
#: 每条记录最多截成几个不同长度来试。
TRUNC_SAMPLES = 8

_U64 = 1 << 64


#: 进程内切换加速开关(分派处每次调用都读模块属性,所以立刻生效)。
accel_enabled = _accel.use


# ----------------------------------------------------------------------
# 比较几何
# ----------------------------------------------------------------------
def _same(a, b) -> bool:
    """逐位比较,NaN 视为相等(NaN != NaN,直接 == 会误报)。"""
    if type(a) is not type(b):
        return False
    if isinstance(a, float):
        return a == b or (a != a and b != b)
    if isinstance(a, (list, tuple, array)):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return a == b


def geom_tuple(g):
    """对拍用的规范形态。

    ⚠️ flat 坐标(``xy_parts`` / ``z_parts`` / ``m_parts``)才是几何的**主
    存储**,``coordinates`` 是从它惰性物化出来的派生视图(§2.19.5)。只比
    ``coordinates`` 的话,"主存储错一处、物化时又恰好错回来"这种双错会被
    放过。两样都进 key;``*_part_types`` 顺带钉住"两条路产出的容器类型
    一致"(都是 array('d'),不是一边 list 一边 array)。
    """
    if g is None:
        return None
    xy = g.xy_parts
    zs, ms = g.z_parts, g.m_parts
    return (g.shape_type, g.has_z, g.has_m, g.coordinates,
            [type(p).__name__ for p in xy], xy, zs, ms,
            None if zs is None else [type(p).__name__ for p in zs],
            None if ms is None else [type(p).__name__ for p in ms])


def n_vertices(g) -> int:
    if g is None:
        return 0
    # 走 flat 主存储数点数 —— 不物化 coordinates,免得为了记账把预算要省的
    # 那笔开销又付一遍(44,967,709 顶点物化成元组列表约 1.7 s)。
    return sum(len(p) for p in g.xy_parts) // 2


def bbox_from(g):
    """从解出来的坐标算实际包围盒(走 flat 主存储);算不出返回 None。"""
    if g is None:
        return None
    xs, ys = [], []
    for p in g.xy_parts:
        xs.extend(x for x in p[0::2] if x == x)
        ys.extend(y for y in p[1::2] if y == y)
    if not xs or not ys:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


# ----------------------------------------------------------------------
# 1. 真实数据对拍
# ----------------------------------------------------------------------
def raw_of(feat):
    g = feat._geometry
    if not isinstance(g, _LazyGeometry):
        return None
    try:
        return g._raw if g._raw is not None else g._read_bytes(g._len)
    except OSError:
        return None


def check_layer(ly, budget: list, do_all: bool) -> tuple:
    """返回 ``(比对条数, 比对顶点数, 不一致数, 错误信息列表)``。"""
    tab = ly.table
    if tab is None or tab.geom_field is None:
        return 0, 0, 0, []

    q = G._Quantizer(tab.geom_field)
    n_cmp = n_vert = n_bad = 0
    errs = []

    for feat in ly.read_features():
        raw = raw_of(feat)
        if not raw:
            continue

        with accel_enabled(True):
            try:
                ga, pos_a = G.decode_geometry_ex(raw, 0, tab.geom_field,
                                                  tab.has_z, tab.has_m)
                exc_a = None
            except Exception as e:                     # noqa: BLE001
                ga, pos_a, exc_a = None, None, type(e).__name__

        # 预算算过了就不再跑慢的那条
        if not do_all and budget[0] <= 0 and exc_a is None:
            continue

        with accel_enabled(False):
            try:
                gb, pos_b = G.decode_geometry_ex(raw, 0, tab.geom_field,
                                                  tab.has_z, tab.has_m)
                exc_b = None
            except Exception as e:                     # noqa: BLE001
                gb, pos_b, exc_b = None, None, type(e).__name__

        n_cmp += 1
        if exc_a != exc_b:
            n_bad += 1
            errs.append(f'行 {feat.oid}: 异常不同 C={exc_a} PY={exc_b}')
        elif exc_a is None:
            if pos_a != pos_b:
                n_bad += 1
                errs.append(f'行 {feat.oid}: 消费位置不同 C={pos_a} PY={pos_b}')
            elif not _same(geom_tuple(ga), geom_tuple(gb)):
                n_bad += 1
                errs.append(f'行 {feat.oid}: 几何不同\n'
                            f'      C : {str(geom_tuple(ga))[:120]}\n'
                            f'      PY: {str(geom_tuple(gb))[:120]}')
            else:
                n_vert += n_vertices(ga)
                budget[0] -= n_vertices(ga)
                # 校准:存储包围盒 vs 逐点算出来的包围盒。
                # 只在前若干条做(它验的是"整条数组没被 dr.pos 写错而错位",
                # 抽到就够了)。
                #
                # ⚠️ 两者**不是**相等关系。实测(乡行政区划,xy_scale=20000
                # → 一个量化单位 5e-5)存储包围盒在每个方向都比实解**宽
                # 4 个量化单位** —— 这是格式/实现的既有性质,与加速无关
                # (peek_envelope 只读头部 4 个 varuint,根本不进加速边界)。
                # 所以这里只要求"实解落在存储包围盒附近",容差给到 16 个
                # 量化单位:既容得下那 4 个,又远小于"错位一个 varint"会
                # 造成的偏移(那是整条数组变垃圾,坐标要差出几十万)。
                if n_cmp <= 20:
                    env = G.peek_envelope(raw, tab.geom_field, tab.has_z,
                                          tab.has_m)
                    bb = bbox_from(ga)
                    if env and bb:
                        tol = 16.0 / abs(q.xy_scale) if q.xy_scale else 1.0
                        if (bb[0] < env[0] - tol or bb[1] < env[1] - tol
                                or bb[2] > env[2] + tol or bb[3] > env[3] + tol):
                            n_bad += 1
                            errs.append(
                                f'行 {feat.oid}: 实解坐标跑到存储包围盒外\n'
                                f'      存储 {env}\n      实解 {bb}\n'
                                f'      (容差 {tol:.3g})')
        if n_bad >= 5:
            break

    return n_cmp, n_vert, n_bad, errs


# ----------------------------------------------------------------------
# 2. 定向损坏用例
# ----------------------------------------------------------------------
def crafted_cases(real_blobs: list) -> list:
    """构造 (说明, blob, 期望) —— 期望是 'GdbFormatError' 或 None(应正常)。

    ⚠️ 构造 blob 时要注意解码器的**早退分支**,否则用例根本走不到数组:
      * ``n_parts == 0`` 会在读数组之前直接返回空几何(不报错);
      * 形状头那个 varuint 读坏了会被 ``_decode_at`` 吞掉、返回 ``None``
        (这是既有行为,与加速无关)。
    所以下面每个用例都铺到"真的要读 XY 数组"的位置。
    """
    from pyopenfilegdb._constants import ShapeType as ST
    from pyopenfilegdb._util import write_varuint

    def vu(v):
        b = bytearray()
        write_varuint(b, v)
        return bytes(b)

    head = vu(ST.POLYGON) + vu(1) + vu(1) + vu(0) * 4   # 形状|点数|1个部件|包围盒
    cases = []

    # 点数被改成天文数字:varuint 无上界,损坏的 blob 可以这么写。
    # ⚠️ 这条正是"先验点数再分配"那道闸的验收条件 —— 没有它,C 会去
    #    PyList_New(2**40) 然后抛 MemoryError,直接废掉整层迭代。
    for huge in (2 ** 40, 2 ** 32, 10 ** 6):
        blob = vu(ST.POLYGON) + vu(huge) + vu(1) + vu(0) * 4 + b'\x00' * 16
        cases.append((f'点数={huge} 的损坏面', blob, 'GdbFormatError'))

    # varint 超过 8 个续字节:两条路必须同时判非法(_MAX_SHIFT 的验收条件)。
    cases.append(('XY 里 9 个续字节', head + b'\x80' * 9 + b'\x00',
                  'GdbFormatError'))
    cases.append(('XY 里 20 个续字节', head + b'\x80' * 20, 'GdbFormatError'))

    # 形状头 + 点数,数组整个缺失。
    cases.append(('点数数组整个缺失', head, 'GdbFormatError'))

    # 真实 blob 逐段截断 —— 这是最贴近"盘上真坏了"的用例。
    for k, raw in enumerate(real_blobs):
        if len(raw) < 8:
            continue
        for frac in (0.25, 0.5, 0.75, 0.95):
            cut = max(1, int(len(raw) * frac))
            cases.append((f'真实 blob #{k} 截到 {cut}/{len(raw)}',
                          raw[:cut], 'AGREE'))
    return cases


def run_crafted(real_blobs: list) -> int:
    print('\n=== 定向损坏用例(两条路必须抛同一种异常) ===')
    bad = 0
    for desc, blob, want in crafted_cases(real_blobs):
        res = []
        for on in (True, False):
            with accel_enabled(on):
                try:
                    G.decode_geometry(blob, None, False, False)
                    res.append(None)
                except Exception as e:                 # noqa: BLE001
                    res.append(type(e).__name__)
        # 'AGREE' 只要求两条路一致(截断点不一定恰好落在数组里);
        # 其余用例还要求确实是 GdbFormatError。
        ok = res[0] == res[1] and (want == 'AGREE' or res[0] == want)
        bad += 0 if ok else 1
        if ok:
            print(f'  ✓ {desc:30s} C={res[0]} PY={res[1]}')
        else:
            print(f'  ✗ {desc:30s} C={res[0]} PY={res[1]}  期望={want}')
    return bad


# ----------------------------------------------------------------------
# 3. 随机 fuzz:C 入口 vs 工具内的参考实现
# ----------------------------------------------------------------------
_REF_MAX_SHIFT = 57


def _ref_add(acc, val, neg):
    """与 _gdbaccel.c 的 RD_ADD 同语义:64 位无符号绕回再转回有符号。"""
    u = (acc - val if neg else acc + val) % _U64
    return u - _U64 if u >= (_U64 >> 1) else u


def _ref_one(blob, pos, end):
    if pos >= end:
        raise IndexError('truncated')
    b0 = blob[pos]
    pos += 1
    val = b0 & 0x3F
    if b0 & 0x80:
        shift = 6
        while True:
            if pos >= end:
                raise IndexError('truncated')
            b = blob[pos]
            pos += 1
            val |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
            if shift >= _REF_MAX_SHIFT:
                raise IndexError('too long')
    return val, bool(b0 & 0x40), pos


def cargs_xy(blob, pos, npt, scale, ox, oy):
    """XY 的 C 入口参数:比参考实现多两个**跨 part 连续的累加器** dx/dy。"""
    return blob, pos, npt, scale, ox, oy, 0, 0


def cargs_scalar(blob, pos, npt, scale, origin):
    """标量入口多一个累加器 acc。"""
    return blob, pos, npt, scale, origin, 0


def ref_decode_xy(blob, pos, n_points, scale, ox, oy):
    """参考实现:与 decode_xy 对外等价。返回 ``(点表, 消费位置)``。"""
    end = len(blob)
    if pos < 0 or n_points < 0:
        raise IndexError('bad args')
    if n_points == 0:
        return [], pos
    if pos > end:
        raise IndexError('pos past end')
    if n_points > (end - pos) // 2:
        raise IndexError('truncated')
    out = []
    dx = dy = 0
    for _ in range(n_points):
        val, neg, pos = _ref_one(blob, pos, end)
        dx = _ref_add(dx, val, neg)
        val, neg, pos = _ref_one(blob, pos, end)
        dy = _ref_add(dy, val, neg)
        out.append((dx / scale + ox, dy / scale + oy))
    return out, pos


def ref_decode_xy_flat(blob, pos, n_points, scale, ox, oy):
    """与 decode_xy_flat 对外等价:同一段解码,结果拍成交错的 array('d')。

    直接复用 ref_decode_xy 的输出 —— 算术部分已经在元组那条路逐位核过了,
    这里只加"拍平进 array"这一步,正好把容器形状隔离成唯一变量。
    """
    pts, pos = ref_decode_xy(blob, pos, n_points, scale, ox, oy)
    return array('d', chain.from_iterable(pts)), pos


def ref_decode_scalar(blob, pos, n_points, scale, origin):
    """参考实现:与 decode_scalar / decode_scalar_flat 对外等价(Z/M 共用)。"""
    end = len(blob)
    if pos < 0 or n_points < 0:
        raise IndexError('bad args')
    if n_points == 0:
        return [], pos
    if pos > end:
        raise IndexError('pos past end')
    if n_points > end - pos:
        raise IndexError('truncated')
    out = []
    acc = 0
    for _ in range(n_points):
        val, neg, pos = _ref_one(blob, pos, end)
        acc = _ref_add(acc, val, neg)
        out.append(acc / scale + origin)
    return out, pos


def ref_decode_scalar_flat(blob, pos, n_points, scale, origin):
    vals, pos = ref_decode_scalar(blob, pos, n_points, scale, origin)
    return array('d', vals), pos


def fuzz(rounds: int = 4000, seed: int = 20260929) -> int:
    print(f'\n=== 随机 fuzz({rounds} 轮,4 个 C 入口 vs 参考实现) ===')
    if not _accel.HAS_ACCEL:
        print('  没有加速模块,跳过')
        return 0
    import pyopenfilegdb._gdbaccel as m

    rnd = random.Random(seed)
    bad = 0
    for i in range(rounds):
        n = rnd.randrange(0, 40)
        # 一半用纯随机字节,一半用"大量续字节"的病态输入
        if i % 2:
            blob = bytes(rnd.choice((0x80, 0xFF, 0x81, 0xC0, rnd.randrange(256)))
                         for _ in range(n))
        else:
            blob = bytes(rnd.randrange(256) for _ in range(n))
        pos = rnd.randrange(0, max(1, n + 1))
        npt = rnd.choice((0, 1, 2, 3, 5, 17))
        # x/y 两个 origin 取**不同**的随机值 —— 传同一个(或把其中一个写死
        # 成 0)会让"第二项 origin 有没有接对"这件事永远验不到。
        ox = rnd.choice((0.0, -400.0, 116.39))
        oy = rnd.choice((0.0, -400.0, 116.39, 1e7))
        s_scale = rnd.choice((1.0, 1e9, -3.0, 0.5))
        s_org = rnd.choice((0.0, -400.0, 116.39))

        # 每个 (C 入口, 参考实现, 期望的返回容器类型, C 参数, 参考参数)。
        # flat 入口比 tuple 入口多了"落进 array('d')"这一步 —— 那正是热路径
        # 真正走的那条(tuple 版只留给基准做 A/B),所以两个都得在闸门里。
        cases = [
            (m.decode_xy, ref_decode_xy, 'list',
             cargs_xy(blob, pos, npt, 1.0, ox, oy),
             (blob, pos, npt, 1.0, ox, oy)),
            (m.decode_xy_flat, ref_decode_xy_flat, 'array',
             cargs_xy(blob, pos, npt, 1.0, ox, oy),
             (blob, pos, npt, 1.0, ox, oy)),
            (m.decode_scalar, ref_decode_scalar, 'list',
             cargs_scalar(blob, pos, npt, s_scale, s_org),
             (blob, pos, npt, s_scale, s_org)),
            (m.decode_scalar_flat, ref_decode_scalar_flat, 'array',
             cargs_scalar(blob, pos, npt, s_scale, s_org),
             (blob, pos, npt, s_scale, s_org)),
        ]
        for fn, ref, wanttype, cargs, rargs in cases:
            got = err = None
            try:
                got = fn(*cargs)
            except Exception as e:                     # noqa: BLE001
                err = type(e).__name__
            try:
                want, wend = ref(*rargs)
                werr = None
            except IndexError:
                want, wend, werr = None, None, 'IndexError'

            if err != werr:
                bad += 1
                print(f'  ✗ #{i} {fn.__name__} 异常不同 C={err} 参考={werr} '
                      f'blob={blob.hex()} pos={pos} n={npt}')
            elif err is None:
                if type(got[0]).__name__ != wanttype:
                    bad += 1
                    print(f'  ✗ #{i} {fn.__name__} 容器类型 '
                          f'{type(got[0]).__name__} ≠ {wanttype}')
                elif not _same(list(got[0]), list(want)):
                    bad += 1
                    print(f'  ✗ #{i} {fn.__name__} 结果不同\n'
                          f'      C  : {str(got[0])[:100]}\n'
                          f'      参考: {str(want)[:100]}')
                elif got[1] != wend:
                    # 消费位置即"下一段从哪读起",错了整条几何都要错位。
                    bad += 1
                    print(f'  ✗ #{i} {fn.__name__} end_pos C={got[1]} '
                          f'参考={wend}')
            if bad >= 5:
                break
        if bad >= 5:
            break

    print(f'  {"✓ 全部一致" if not bad else f"✗ {bad} 处不一致"}')
    return bad


# ----------------------------------------------------------------------
def collect_real_blobs(cands: list, limit: int = 3) -> list:
    """抓几个真实几何 blob,给截断用例用。"""
    out = []
    for gdb_path in cands:
        try:
            gdb = OpenFileGDB.open(gdb_path)
        except Exception:                              # noqa: BLE001
            continue
        try:
            for name in gdb.list_feature_classes():
                ly = gdb.get_layer(name)
                if ly.table is None or ly.table.geom_field is None:
                    continue
                for feat in ly.read_features(limit=20):
                    raw = raw_of(feat)
                    if raw and len(raw) >= 64:
                        out.append(raw)
                        break
                if len(out) >= limit:
                    return out
        except Exception:                              # noqa: BLE001
            continue
        finally:
            gdb.close()
    return out


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    do_all = '--all' in sys.argv

    if not _accel.HAS_ACCEL:
        print('⚠️ 没有可用的 C 加速模块(没编译或 PYOPENFILEGDB_NO_ACCEL)。')
        print('   先跑 `python setup.py build_ext --inplace`。')
        return 2

    cands = args or sorted(glob.glob('D:/work/*.gdb'))
    real_blobs = collect_real_blobs(cands)
    if not real_blobs:
        print('⚠️ 没拿到真实 blob,截断用例会少一块。')

    bad = run_crafted(real_blobs)
    bad += fuzz()

    print(f'\n=== 真实数据对拍(纯 Python 顶点预算 '
          f'{"∞" if do_all else f"{PY_VERTEX_BUDGET:,}"}) ===')
    tot_cmp = tot_vert = 0
    for gdb_path in cands:
        try:
            gdb = OpenFileGDB.open(gdb_path)
        except Exception as e:                         # noqa: BLE001
            print(f'跳过 {os.path.basename(gdb_path)}: {e}')
            continue
        print(f'\n--- {os.path.basename(gdb_path)} ---')
        for name in gdb.list_feature_classes():
            ly = gdb.get_layer(name)
            if ly.table is None or ly.table.tablx is None:
                continue
            budget = [PY_VERTEX_BUDGET]
            n_cmp, n_vert, n_bad, errs = check_layer(ly, budget, do_all)
            tot_cmp += n_cmp
            tot_vert += n_vert
            bad += n_bad
            mark = '✓' if not n_bad else '✗'
            print(f'  {mark} {name:20s} 比对 {n_cmp:6d} 条 / {n_vert:9,d} 顶点')
            for e in errs:
                print(f'      {e}')
        gdb.close()

    print(f'\n合计:比对 {tot_cmp:,} 条 / {tot_vert:,} 顶点')
    print('全部一致 ✓' if not bad else f'⚠️ 有 {bad} 处不一致')
    return 1 if bad else 0


if __name__ == '__main__':
    raise SystemExit(main())
