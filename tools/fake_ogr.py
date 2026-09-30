#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""用**假的** `osgeo.ogr` 把 :mod:`tools.verify_topology` 的管道跑起来。

本机没有 GDAL(见 `DESIGN.md` §5),`tools/verify_topology.py` 因此永远走
"没装 osgeo —— skip" 那条分支,等于**从来没被真正执行过**。这个脚本把它
救回来:注入一个 `osgeo.ogr`,其 `CreateGeometryFromWkt` 与各谓词**转发给本库
自己**。

⚠️ **它不产生真值。** 真值还是本库。它证明的只有两件事:

1. **(a) 端到端能跑** —— 四层输入、`_remap` / `_shift` / `_jitter`、
   `to_gdal()`、`compare()`,一条路径都不炸。没有这一条,那些代码的正确性
   全靠读;`ShapeType() takes no arguments`、把 `(name, geom)` 元组当几何用
   这两个 bug 都是在这一步当场现形的。
2. **(b) 真的不一致时会报出来** —— 否则它就是个永远绿的摆设。
   `FLIP="contains touches"` 让那几个谓词**故意答反**,
   输出里必须出现对应条数的不符;一条都没有,说明对比逻辑本身废了。

顺带说一句它的战功:`to_gdal()` 会把 `g.wkt()` 交给"GDAL"去解析(本库的
`from_wkt`),于是**非法 WKT 当场暴露** —— `LINESTRING ((0 0), (10 10))` 和
被吞掉的 `Z`/`M` 后缀就是这么抓出来的,而那 134 个绿色用例一个都没抓到。

用法::

    python tools/fake_ogr.py                    # 全部四层
    python tools/fake_ogr.py --synthetic-only   # 不读语料,更快
    FLIP="contains overlaps" python tools/fake_ogr.py    # 验 (b)

退出码与 `verify_topology.py` 一致:0 = 一致,1 = 有不符。
"""
from __future__ import annotations

import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from pyopenfilegdb.geometry import Geometry            # noqa: E402

#: 这些名字会被故意答反(用环境变量 `FLIP` 指定),用来验证"不一致真的会报"。
FLIP = frozenset(filter(None, os.environ.get('FLIP', '').split()))

#: 与 `verify_topology.PREDICATES` 一致 —— 转发时按名字逐个取。
_PREDS = ('intersects', 'disjoint', 'contains', 'within', 'covers',
          'covered_by', 'touches', 'crosses', 'overlaps', 'equals')


class _FakeGeom:
    """把 OGR 几何的调用面转发给本库的 :class:`Geometry`。"""

    def __init__(self, g):
        self._g = g

    def relate(self, other):
        return self._g.relate(other._g)

    def __getattr__(self, name):
        if name not in _PREDS:
            raise AttributeError(name)

        def call(other, _name=name):
            v = getattr(self._g, _name)(other._g)
            return (not v) if _name in FLIP else v
        return call


def _make_fake_shapely():
    """假 `shapely`,同样转发给本库。

    ⚠️ **这一块是补上去的,原因是这个脚本曾经被"绕过"过。**
    `verify_topology.pick_oracle()` **优先挑 shapely**(它是 GEOS 本体,还能比
    DE-9IM 矩阵),而本机恰好装了 shapely —— 于是 `fake_ogr.py` 注入的假 `osgeo`
    压根没被用到,它一直在跟**真的 GEOS** 对比,却自称在跑"假的 osgeo 管道"。
    它当时报出来的不符合(薄片那个已知精度地板)就是**真 GEOS 的**结果,不是它
    自己那套"本库自比"的结果。

    ⚠️ 这与本仓库记过的那次**假绿**是同一个形状:**守卫悄悄失效,而它看上去还
    在守**。当年的假绿是"比了 0 对却报一致";这次是"说的是一套,跑的是另一套"。
    所以下面 :func:`install` 结尾有一条**硬断言**:装完之后 `import shapely`
    必须拿到假的,否则直接抛异常 —— 宁可它响,也不要它再默默换成别的 oracle。

    `ShapelyOracle` 的调用面是模块级的(`shapely.from_wkt` / `shapely.relate(a,b)`
    / `shapely.<谓词>(a,b)`),所以这里的几何对象直接用本库的 :class:`Geometry`,
    不需要 :class:`_FakeGeom` 那层包装。
    """
    sh = types.ModuleType('shapely')
    sh.__version__ = 'FakeShapely(本库自比,非真值)'
    sh.geos_version_string = 'n/a'
    sh._IS_FAKE = True
    sh.from_wkt = lambda w: Geometry.from_wkt(w)

    def relate(a, b):
        return a.relate(b)
    sh.relate = relate

    for name in _PREDS:
        def call(a, b, _name=name):
            v = getattr(a, _name)(b)
            return (not v) if _name in FLIP else v
        setattr(sh, name, call)
    return sh


def install():
    """把假的 `shapely` / `osgeo` 塞进 `sys.modules`。"""
    ogr = types.ModuleType('osgeo.ogr')
    ogr.CreateGeometryFromWkt = lambda w: _FakeGeom(Geometry.from_wkt(w))
    # `verify_topology.main()` 会 `from osgeo import gdal` 取版本号,给一个假的
    gdal = types.ModuleType('osgeo.gdal')
    gdal.VersionInfo = lambda: 'FakeGDAL (本库自比,非真值)'
    osgeo_mod = types.ModuleType('osgeo')
    osgeo_mod.ogr = ogr
    osgeo_mod.gdal = gdal
    sys.modules['osgeo'] = osgeo_mod
    sys.modules['osgeo.ogr'] = ogr
    sys.modules['osgeo.gdal'] = gdal

    # ⚠️ 两个都要注入。只注入 osgeo 的话,pick_oracle 会挑中**真的** shapely,
    # 本脚本就悄悄变成"跑真 GEOS"了 —— 见 _make_fake_shapely 的说明。
    sys.modules['shapely'] = _make_fake_shapely()

    # 硬断言:两个 oracle 必须都是假的。不做这一条,上面那句话就只是注释。
    import importlib
    for mod, attr in (('shapely', '_IS_FAKE'), ('osgeo', None)):
        got = importlib.import_module(mod)
        if attr is not None and not getattr(got, attr, False):
            raise RuntimeError(
                f'假的 {mod} 没装上 —— 本脚本会拿**真** oracle 去比,'
                f'那就不是它该干的事了。')
        if attr is None and getattr(got, '__file__', None):
            raise RuntimeError(
                f'{mod} 来自真实包({got.__file__}),注入没生效。')


def main():
    args = sys.argv[1:] or ['--pairs', '600']
    if FLIP:
        print(f'!! 故意答反这些谓词:{" ".join(sorted(FLIP))}')
    install()
    tool = os.path.join(_HERE, 'verify_topology.py')
    src = open(tool, encoding='utf-8').read()
    # 直接把工具**当脚本**执行 —— 它自己的模块级 import 拿到的就是上面注入的假 osgeo。
    sys.argv = ['verify_topology.py'] + args
    exec(compile(src, tool, 'exec'),
         {'__name__': '__main__', '__file__': tool})


if __name__ == '__main__':
    # exec 里的 `sys.exit(main())` 会抛 SystemExit,这里原样放行。
    main()
