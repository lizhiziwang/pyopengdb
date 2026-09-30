#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""**可选**的权威对拍:本库的 DE-9IM 与 GEOS / GDAL 逐对比较。

⚠️ 这个文件是本仓库里**唯一**允许 ``import shapely`` / ``import osgeo`` 的地方。
理由:谓词这一档 GDAL 自己没有纯 C++ 实现 —— ``OGRGeometry::Intersects`` /
``Contains`` / ``Touches`` … 在 ``ogrgeometry.cpp`` 里全部转手给 GEOS,只加了
一句"包围盒先快速排除"。也就是说**没有 C++ 可以抄**,我们能做的只有
"按 OGC 规范实现 + 找一个权威实现当参照"。

两个 oracle,按可得性自动挑(**都不是依赖**)
--------------------------------------------
======================  ===================================  ==============
oracle                  提供什么                              本机
======================  ===================================  ==============
**shapely(GEOS)**       `relate` 矩阵 + 全部 10 个谓词          ✅ 2.1.2 / GEOS 3.13.1
**GDAL/`osgeo`**        7 个谓词 + `Equals`(**结构**比较)      ✅ 3.13.3(QGIS 自带)
(都没有)                 —                                       skip,exit 0
======================  ===================================  ==============

**优先 shapely**:它就是 GEOS 本体,而且 `relate` 直接给 DE-9IM 矩阵 —— 那是
这九个谓词的地基,能比到矩阵就等于比到了根。GDAL 的 Python 绑定**不暴露**
`relate` / `Covers` / `CoveredBy`,而且它的 `Equals` 是**结构比较**
(对应本库的 `exactly_equals()`),所以走 GDAL 时那几项只能标成"未暴露"。

约束(不要破坏)
---------------
* shapely / GDAL **只在此文件中出现**。``pyopenfilegdb`` 包的 import 图不含它们,
  ``tests/`` 不依赖它们,``pyproject.toml`` 里没有它们。
* **一个都没装是正常状态**,不是失败 —— 打印一行说明并 ``exit 0``(skip)。
* ⚠️ **比了 0 对是失败,不是成功。** 见下面"假绿"那段。

⚠️ 假绿:这个脚本**曾经**绿过,但一对都没比
------------------------------------------------
GDAL 的 Python 绑定**没有** `relate` 方法。原版 `compare()` 里
`a.relate(b)` 抛 `AttributeError`,被宽泛的 `except Exception` 吞成"跳过",
于是它打出"与 GDAL 逐对一致 ✓"、`exit 0` —— **而实际比较的对数是 0**。
用**假的** osgeo(`tools/fake_ogr.py`,转发给本库自己)永远测不出这个,
因为那条路上 `relate` 恰好存在。

所以现在有三条硬闸门:

1. ``main()`` 结尾检查 **总对数 == 0 就直接 exit 1**,并说明为什么(附各
   跳过原因的分类计数);
2. 每个 oracle 的**不支持项要显式列出来**(不是静默跳过),打在报告里;
3. 端到端"能不能跑"由 `tools/fake_ogr.py` 守,而"比到的结论对不对"由本脚本
   在**真** oracle 上守。两者缺一不可。

✅ 战功:它第一次在**真** GEOS 上跑起来就抓出一个真 bug
--------------------------------------------------------
`relate()` 在**交点坐标不可精确表示**时会把 `II`/`IB`/`BI` 整格塌成 `F`
—— 不是次 ULP 的退化构型,是"两条线交点除不尽"这种极常见的情形::

    A = LINESTRING(-0.0000003 0.0000004, 10.0000002 9.9999998)
    B = LINESTRING(0 10, 10 0)
    老实现 FF1FF0102   GEOS 0F1FF0102

它躲过了当时全部 147 个用例(真值表用的是整数坐标,交点是逐位精确的
(5, 5)),也躲过了 `fake_ogr.py`。根因与修法见 `_geometry_ops.relate_of`
和 `_split_parameters` 的 docstring,以及 `DESIGN.md` §2.21。

**残留 2 处不符**,同一个构型:`pg_degenerate_sliver` 平移后只有 2.1 ULP 高,
直线穿过它那一段**比一个 ULP 还短**,双精度网格上没有内部点可采样。那是精度
地板,不是 bug(见 `DESIGN.md` §2.21「残留一处」)。看到这 2 处不必当成回归。


为什么对拍要在 **UTM 量级坐标**上做
-----------------------------------
合成用例如果都摆在原点附近,浮点方向判定永远不会退化,对拍也就永远绿 ——
那种绿是假绿。本脚本会把合成图形平移到真实语料的坐标系量级(横坐标 3.9e7),
同一组相对构型再跑一遍。真实语料(第 4 层)已经在那里了。

四层输入
--------
1. **合成用例**:点/线/面两两共 3×3 种维数组合,覆盖相离、相接(共点/共边/
   共端)、部分重叠、包含、相等这几种相对位置。**谓词的覆盖面主要靠这一层。**
2. **真实语料**:从样例库每层取若干要素,以本库导出的 WKT 为准构造两侧几何 ——
   两边看到**逐位相同**的输入,这样差异只可能来自谓词语义,不会混入读取路径
   的差别。行政界线天然产生大量"相邻"对(should touch)。
3. **扰动**:对 #1 的每对做 1e-6 相对量的坐标抖动,专打近退化构型。
4. **UTM 量级平移**:同 #1,整体搬到真实坐标系。

用法::

    python tools/verify_topology.py                     # 扫 D:/work/*.gdb
    python tools/verify_topology.py --synthetic-only    # 不读语料
    python tools/verify_topology.py --pairs 400
    python tools/verify_topology.py --oracle gdal       # 强制走 GDAL
"""
from __future__ import annotations

import argparse
import glob
import itertools
import os
import random
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ⚠️ **Windows 默认控制台是 GBK,不会编码 `⚠️` / `✓`。** 不兜住的话,报错恰好
# 发生在**打印报告**这一步 —— 也就是你唯一想看到输出的那一刻,而且只在"没装
# oracle,准备打印说明"或"有未支持项,准备打印提示"这些**分支**上触发,所以
# 开发时一路 `PYTHONIOENCODING=utf-8` 根本碰不到。实测:
#   python312(默认代码页)-> UnicodeEncodeError: 'gbk' codec can't encode '⚠'
# 别的工具不用管这条,是因为它们的输出全是 ASCII。**这个工具的每一段提示都带符号。**
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except (AttributeError, ValueError):        # 不是 TextIOWrapper(比如被重定向)
        pass


#: 与 ``Geometry`` 的方法名一一对应。
PREDICATES = ('intersects', 'disjoint', 'contains', 'within', 'covers',
              'covered_by', 'touches', 'crosses', 'overlaps', 'equals')

#: 真实语料的坐标系量级 —— 平移到这里才算"在真实数值环境下测过"。
UTM_OFFSET = (39393621.0, 3179757.0)


# --------------------------------------------------------------------------
# oracle:把"权威实现"抽象成一个能造几何、能算矩阵、能算谓词、能算 buffer 的对象
#
# ⚠️ 实现搬到了 tools/_oracle.py —— verify_buffer.py 要用同一套。
#    那条"shapely / osgeo 只允许出现在 tools/ 里"的规矩没变,只是从"这一个
#    文件"收紧成了"tools/_oracle.py 这一个文件"。
# --------------------------------------------------------------------------

#: 让 `from _oracle import ...` 在"当脚本跑"和"当模块 import"两种方式下都成立
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _oracle import GdalOracle, ShapelyOracle, pick_oracle  # noqa: E402,F401


# --------------------------------------------------------------------------
# 合成用例与变换(与 oracle 无关)
# --------------------------------------------------------------------------

def _pairs_2d():
    """(名字, wkt) —— 全部 2D,相对构型固定,只关心维数与相对位置。"""
    return [
        # --- 点 ---
        ('pt_origin', 'POINT(0 0)'),
        ('pt_inside', 'POINT(3 3)'),
        ('pt_on_edge', 'POINT(5 0)'),
        ('pt_on_vertex', 'POINT(0 0)'),
        ('pt_outside', 'POINT(40 40)'),
        ('pt_multi', 'MULTIPOINT(0 0, 40 40)'),
        # --- 线 ---
        ('ln_diag', 'LINESTRING(0 0, 10 10)'),
        ('ln_bottom', 'LINESTRING(0 0, 10 0)'),
        ('ln_shared_end', 'LINESTRING(0 0, -5 -5)'),
        ('ln_inside', 'LINESTRING(2 2, 4 4)'),
        ('ln_cross', 'LINESTRING(0 10, 10 0)'),
        ('ln_t_corner', 'LINESTRING(5 0, 5 10)'),
        ('ln_far', 'LINESTRING(100 100, 110 110)'),
        ('ln_overlap_part', 'LINESTRING(4 4, 14 14)'),
        ('ln_vertical', 'LINESTRING(0 0, 0 10)'),
        # --- 面 ---
        ('pg_sq', 'POLYGON((0 0,10 0,10 10,0 10,0 0))'),
        ('pg_same', 'POLYGON((0 0,10 0,10 10,0 10,0 0))'),
        # ⚠️ 这条是"同一块地、多一个顶点"的正解:新顶点 (5,0) 落在**边上**,
        # 面积仍是 100 —— 所以 GEOS 的 `equals`(拓扑)判 True 而 GDAL 的
        # `Equals`(结构)判 False,正好把本库 `equals()` / `exactly_equals()`
        # 的分工同时钉住。
        # (早先这里写的是 `POLYGON((0 0,10 0,5 10,0 10,0 0))` 并声称"同面积",
        #  **那是错的** —— 它的面积是 75,`equals` 本来就该 False。DESIGN 里
        #  曾引过这个错例子,已更正。)
        ('pg_extra_collinear', 'POLYGON((0 0,5 0,10 0,10 10,0 10,0 0))'),
        ('pg_edge_share', 'POLYGON((10 0,20 0,20 10,10 10,10 0))'),
        ('pg_corner_touch', 'POLYGON((10 10,20 10,20 20,10 20,10 10))'),
        ('pg_overlap_half', 'POLYGON((5 0,15 0,15 10,5 10,5 0))'),
        ('pg_inside', 'POLYGON((2 2,8 2,8 8,2 8,2 2))'),
        ('pg_contains_it', 'POLYGON((-1 -1,11 -1,11 11,-1 11,-1 -1))'),
        ('pg_far', 'POLYGON((30 30,40 30,40 40,30 40,30 30))'),
        ('pg_with_hole',
         'POLYGON((0 0,10 0,10 10,0 10,0 0),(4 4,6 4,6 6,4 6,4 4))'),
        ('pg_in_hole', 'POLYGON((4.5 4.5,5.5 4.5,5.5 5.5,4.5 5.5,4.5 4.5))'),
        ('pg_multi',
         'MULTIPOLYGON(((0 0,2 0,2 2,0 2,0 0)),((5 5,7 5,7 7,5 7,5 5)))'),
        ('pg_degenerate_sliver',
         'POLYGON((0 0,10 0,10 1e-9,0 1e-9,0 0))'),
    ]


#: WKT 里的数字。WKT 中除坐标外**没有别的数字**,而 2D 几何的坐标正好
#: x, y 交替 —— 所以按出现顺序替换就是"按顶点改坐标"。这个假设不靠自觉,
#: :func:`_remap` 会核对替换个数,错了直接抛。
_NUM = re.compile(r'-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?')


def _remap(g, f, label):
    """把几何的每个 XY 过一遍 ``f(value, is_y)``,返回新几何。

    实现是**在 WKT 文本上替换数字再解析回来**,而不是去拼 ``Geometry`` 的
    构造参数 —— 后者要同时在 point / multipoint / (lens, pts) 三种公开形态
    之间游走,还得自己保住 ``_shells``(环角色),很容易写出一个"拓扑上不等价
    但看起来对"的副本。走 WKT 则天然保结构,代价只是要求几何是 2D。

    自检:替换的数字个数必须是**偶数**(x/y 成对),且解析回来的 kind 与
    point_count 没变。**没有这两条,`f` 写错时会静默地把用例变成另一条几何,
    对拍照样"全绿"。**

    ⚠️ 这里**不**去核对"替换个数 == 2 × point_count":WKT 文本里的面环比内存
    里多一个闭合点(导出时补的),拿 point_count 当预期必然差一。要验的是
    "变换真的生效了",那个由 :func:`_shift` 自己看包围盒。
    """
    from pyopenfilegdb.geometry import Geometry
    if g.has_z or g.has_m:
        raise ValueError(f'{label}: 只支持 2D 变换')
    n = [0]

    def sub(m):
        v = float(m.group())
        out = f(v, n[0] % 2 == 1)
        n[0] += 1
        return repr(out)

    new = _NUM.sub(sub, g.wkt())
    if n[0] % 2:
        raise ValueError(f'{label}: 替换了奇数个数字({n[0]})—— x/y 不成对')
    ng = Geometry.from_wkt(new)
    if ng.kind != g.kind or ng.point_count != g.point_count:
        raise ValueError(f'{label}: 几何变了 {g.kind}/{g.point_count} -> '
                         f'{ng.kind}/{ng.point_count}')
    return ng


def _shift(g, dx, dy):
    """整体平移 —— 用来把合成构型搬到真实坐标系的量级上。

    变换完**核对包围盒真的移动了 ``(dx, dy)``**。这一步不是多余的:平移是
    这一层的全部意义,若 `_NUM` 没匹配上(比如 wkt() 换了数字格式),几何会
    原样返回,于是"UTM 量级"这一层悄悄退化成第 1 层的副本,两边都还是绿的。
    """
    ng = _remap(g, lambda v, is_y: v + (dy if is_y else dx), 'shift')
    e0, e1 = g.envelope(), ng.envelope()
    if e0 and e1:
        for got, want, name in ((e1[0] - e0[0], dx, 'dx'),
                                (e1[1] - e0[1], dy, 'dy')):
            if abs(got - want) > 1e-9 * max(1.0, abs(want)):
                raise ValueError(f'shift: {name} 实际移动了 {got},期望 {want}')
    return ng


def _jitter(g, rnd, scale=1e-6):
    """按坐标量级做相对抖动,制造近退化构型。

    两处讲究:

    * 用 ``v + ±scale·max(1, |v|)`` 而不是 ``v·(1±scale)`` —— 后者在原点
      附近是**恒等变换**(``0 * 任何东西 == 0``),而合成用例里一堆点就摆在
      原点,那样这一层等于什么都没测。
    * **同一个坐标值只抖一次**(``memo``)。WKT 文本里的面环带一个闭合点,
      它和首点是同一个数值;各自独立抖一次环就不闭合了,``from_wkt`` 会把它
      当成 5 个顶点的环 —— 实测就是这么炸的。
    """
    memo = {}

    def f(v, is_y):
        key = (v, is_y)
        r = memo.get(key)
        if r is None:
            r = memo[key] = v + rnd.uniform(-scale, scale) * max(1.0, abs(v))
        return r
    return _remap(g, f, 'jitter')


def collect(cases, gdb_paths, n_feat, max_verts):
    """把合成用例 + 真实语料凑成一个 ``(标签, 本库几何)`` 列表。

    ⚠️ ``max_verts`` 是**比 ``--pairs`` 更要紧的那个旋钮**。``relate`` 是
    O(顶点数²) 级的,所以"多取几个要素"和"取几个大要素"完全不是一回事:
    ``--features 60`` 配上真实语料里 400 顶点的多边形,实际工作量是默认配置的
    几百倍 —— 一对就要几十万次线段运算,看起来就是卡住。
    **想要更宽的语料覆盖,应该调大 ``--features`` 同时调小 ``max_verts``**,
    让对数的增长不带上单对成本的增长。
    """
    from pyopenfilegdb import OpenFileGDB
    from pyopenfilegdb.geometry import Geometry

    out = []
    for name, wkt in cases:
        try:
            g = Geometry.from_wkt(wkt)
        except Exception as exc:                        # noqa: BLE001
            print(f'  !! 本库解析不了 {name}: {exc}')
            continue
        out.append((name, g))

    # 真实语料:用**本库导出的 WKT** 构造,保证两边输入逐位相同
    for path in gdb_paths:
        try:
            db = OpenFileGDB.open(path)
        except Exception as exc:                        # noqa: BLE001
            print(f'  跳过语料 {path}: {exc}')
            continue
        for lname in db.list_layers():
            try:
                lyr = db.get_layer(lname)
            except Exception:                           # noqa: BLE001
                continue
            if lyr.geometry_field is None:
                continue
            got = skipped = 0
            for feat in lyr.read_features():
                g = feat.geometry
                if g is None or g.is_empty or g.kind == 'multipatch':
                    continue
                if g.has_z or g.has_m:
                    continue
                if g.point_count > max_verts:
                    skipped += 1
                    continue
                out.append((f'{lname}#{feat.oid}', g))
                got += 1
                if got >= n_feat:
                    break
            if got:
                note = f'(跳过 {skipped} 条超过 {max_verts} 顶点的)' if skipped else ''
                print(f'  语料 {lname}: 取 {got} 条 {note}')
        db.close()
    return out


# --------------------------------------------------------------------------
# 逐对比较
# --------------------------------------------------------------------------

def compare(geoms_a, geoms_b, oracle, pair_budget, rnd, tag, stats):
    """逐对比较 ``relate`` 与谓词。返回不符列表。

    ``geoms_a is geoms_b`` 时是自内积(无序对,每对只比一次);给了两份不同的表
    则比**交叉积** —— 抖动那一层靠它把"抖过的"和"没抖的"配对,得到刚差一点点
    的构型。

    ⚠️ ``pair_budget`` 卡的是**对数**,不是**工作量**。单对成本随顶点数平方级
    上涨,所以这个预算在真实语料上并不兜底 —— 想控制总耗时,请调
    :func:`collect` 的 ``max_verts``。这里每隔一段打一次进度,免得看起来像死了。
    """
    bad = []
    if geoms_a is geoms_b:
        combos = list(itertools.combinations(range(len(geoms_a)), 2))
    else:
        combos = list(itertools.product(range(len(geoms_a)), range(len(geoms_b))))
    if pair_budget and len(combos) > pair_budget:
        combos = rnd.sample(combos, pair_budget)

    cache = {}
    t0 = time.time()
    tick = max(1, len(combos) // 20)

    def side(geoms, i):
        """``(名字, 本库几何, oracle 几何)``,oracle 那份惰性构造并缓存。"""
        key = (id(geoms), i)
        if key not in cache:
            name, g = geoms[i]
            cache[key] = (name, g, oracle.make(g.wkt()))
        return cache[key]

    for step, (i, j) in enumerate(combos):
        if step and step % tick == 0:
            print(f'    ... {step}/{len(combos)} 对,{time.time() - t0:.0f}s')
        ni, gi, a = side(geoms_a, i)
        nj, gj, b = side(geoms_b, j)
        pair = f'{tag} {ni} × {nj}'
        try:
            m_mine = gi.relate(gj)
        except NotImplementedError:
            stats['skip_relate_unsupported'] += 1
            continue
        n_pair = 0

        if oracle.supports_relate:
            m_theirs = oracle.relate(a, b)
            n_pair += 1
            if m_mine != m_theirs:
                bad.append((pair, 'relate', m_mine, m_theirs))
                continue                                # 矩阵不对,谓词必然连带错
        else:
            # ⚠️ **不支持的项要显式计数,不能静默跳过。** GDAL 的 Python 绑定没有
            # `relate`,这条路径以前是"什么都不做",于是报告里看不出矩阵压根没比过
            # —— 那正是当年"假绿"的形状。现在它和 `covers` 等一起打在报告里。
            stats['unsupported']['relate'] = \
                stats['unsupported'].get('relate', 0) + 1

        for p in PREDICATES:
            theirs = oracle.pred(p, a, b)
            if theirs is None:
                stats['unsupported'][p] = stats['unsupported'].get(p, 0) + 1
                continue
            n_pair += 1
            mine = bool(getattr(gi, p)(gj))
            if mine != theirs:
                bad.append((pair, p, mine, theirs))

        # 结构比较单列:GDAL 的 `Equals` 对应的是 `exactly_equals()`
        if oracle.structural_equals:
            n_pair += 1
            if bool(getattr(gi, 'exactly_equals')(gj)) != oracle.pred(
                    oracle.structural_equals, a, b):
                bad.append((pair, 'exactly_equals',
                            gi.exactly_equals(gj),
                            oracle.pred(oracle.structural_equals, a, b)))

        if n_pair == 0:
            stats['skip_no_predicate'] += 1
        else:
            stats['pairs'] += n_pair
            stats['cases'] += 1
    return bad


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('gdb', nargs='*', help='语料库(默认扫 D:/work/*.gdb)')
    ap.add_argument('--synthetic-only', action='store_true')
    ap.add_argument('--features', type=int, default=12,
                    help='每个图层取多少条真实要素')
    ap.add_argument('--max-verts', type=int, default=400,
                    help='语料里超过这么多顶点的要素不取 —— relate 是平方级的,'
                         '想要更宽的覆盖请调大 --features 同时调小这个')
    ap.add_argument('--pairs', type=int, default=2500, help='每层最多比多少对')
    ap.add_argument('--seed', type=int, default=20240929)
    ap.add_argument('--oracle', choices=('auto', 'shapely', 'gdal'),
                    default='auto', help='强制指定参照实现')
    args = ap.parse_args()

    try:
        oracle = pick_oracle(args.oracle)
    except ImportError as exc:
        print(f'!! 指定了 --oracle {args.oracle} 但装不上:{exc}')
        return 1
    if oracle is None:
        print('shapely 和 osgeo 都没有 —— 跳过对拍。')
        print('(这是**正常状态**:它们只是本脚本的可选参照,库和测试都不依赖。')
        print(' 要跑这个对拍,装任意一个即可:')
        print('   pip install shapely        # 推荐:直接是 GEOS,还能比 DE-9IM 矩阵')
        print('   conda install gdal         # 后备:没有 relate / covers / covered_by)')
        return 0

    rnd = random.Random(args.seed)
    cases = _pairs_2d()

    gdb_paths = args.gdb
    if not gdb_paths and not args.synthetic_only:
        env = os.environ.get('PYOPENFILEGDB_TEST_GDB', '')
        gdb_paths = ([d for d in env.split(os.pathsep) if d and os.path.isdir(d)]
                     if env else sorted(glob.glob('D:/work/*.gdb')))
    if args.synthetic_only:
        gdb_paths = []

    print(f'参照实现:{oracle.name}  {oracle.describe()}')
    print(f'  合成用例 {len(cases)} 个  语料库 {len(gdb_paths)} 个')
    if oracle.unsupported:
        print(f'  ⚠️ 该参照**未暴露**这些项,比对时会计入"未支持":'
              f'{" ".join(oracle.unsupported)}')
    print('\n-- 收集几何 --')
    geoms = collect(cases, gdb_paths, args.features, args.max_verts)
    print(f'  共 {len(geoms)} 条几何')

    stats = {'pairs': 0, 'cases': 0, 'unsupported': {},
             'skip_relate_unsupported': 0, 'skip_no_predicate': 0}
    all_bad = []

    base = [g for g in geoms if '#' not in g[0]]

    print('\n-- 第 1 层:合成构型 --')
    bad = compare(base, base, oracle, args.pairs, rnd, '合成', stats)
    print(f'  不符 {len(bad)}')
    all_bad += bad

    print('\n-- 第 2 层:合成构型 + UTM 量级平移 --')
    shifted = []
    for name, g in base:
        try:
            shifted.append((name + '@utm', _shift(g, *UTM_OFFSET)))
        except Exception as exc:                        # noqa: BLE001
            print(f'  !! UTM 平移失败 {name}: {exc}')
    bad = compare(shifted, shifted, oracle, args.pairs, rnd, 'UTM', stats)
    print(f'  不符 {len(bad)}')
    all_bad += bad

    print('\n-- 第 3 层:合成构型 + 1e-6 抖动 --')
    jit = []
    for name, g in base:
        try:
            jit.append((name + '@jit', _jitter(g, rnd)))
        except Exception as exc:                        # noqa: BLE001
            print(f'  !! 抖动失败 {name}: {exc}')
    bad = compare(jit, base, oracle, args.pairs, rnd, '抖动', stats)
    print(f'  不符 {len(bad)}')
    all_bad += bad

    print('\n-- 第 4 层:真实语料 --')
    real = [g for g in geoms if '#' in g[0]]
    if real:
        bad = compare(real, real, oracle, args.pairs, rnd, '语料', stats)
        print(f'  不符 {len(bad)}')
        all_bad += bad
    else:
        print('  (无可比语料)')

    print(f'\n{"=" * 60}')
    print(f'比了 {stats["cases"]} 个几何对 / {stats["pairs"]} 项判定')
    if stats['unsupported']:
        print('该参照未暴露、未参与比对的项:')
        for k, v in sorted(stats['unsupported'].items()):
            print(f'   {k:14s} {v} 次')
    if all_bad:
        by_pred = {}
        for row in all_bad:
            by_pred[row[1]] = by_pred.get(row[1], 0) + 1
        print('按项统计:')
        for k, v in sorted(by_pred.items(), key=lambda kv: -kv[1]):
            print(f'   {k:14s} {v}')
        print('\n前 20 处(本库 vs 参照):')
        for tag, what, mine, theirs in all_bad[:20]:
            print(f'   [{what}] {tag}\n      本库 {mine}\n      参照 {theirs}')
        return 1

    # ⚠️ 这条闸门就是为了不让"假绿"再发生:曾经比了 0 对却打印"逐对一致 ✓"。
    if stats['pairs'] == 0:
        print('!! **一项都没比到** —— 这不是通过,是脚本废了。')
        print(f'   跳过原因:{stats["skip_relate_unsupported"]} 个用例 '
              f'relate 不支持(本库抛 NotImplementedError),'
              f'{stats["skip_no_predicate"]} 个用例一条谓词都没比到。')
        return 1

    print('与参照实现逐对一致 ✓')
    return 0


if __name__ == '__main__':
    sys.exit(main())
