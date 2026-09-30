#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""坐标转换 —— 用 **pyproj** 把几何 / 图层从一个坐标系换到另一个。

    from pyopenfilegdb import OpenFileGDB
    from pyopenfilegdb.coordinates_system import transform_layer

    with OpenFileGDB.open('D:/work/行政区划.gdb') as gdb:
        layer = gdb.get_layer('村行政区划')          # CGCS2000 3 度带 GK zone 39
        for feat in transform_layer(layer, 4326):    # -> WGS84 经纬度
            print(feat.oid, feat.geometry.centroid())

也可以直接在几何上转::

    g2 = g.to_crs(4326, src=4527)

为什么单独放一个模块,而且 **不是** 硬依赖
------------------------------------------
本库的运行时依赖是**零**。pyproj 是**可选**的:装了就能用这个模块,没装则
``import pyopenfilegdb`` 一切照旧,只是调用这里的东西会拿到一句明确的
``ImportError``。这与 numpy / C 扩展的口径一致(见 DESIGN.md §2.21)。

⚠️ **本模块不做任何格式读写**,只是把坐标算一遍。因此它**不违反**
"格式逻辑严格按 GDAL 实现" 这条 —— 它也**不碰** ``GDAL/fiona/pygdal/libgdal``,
用的是独立的 pyproj(PROJ 的 Python 绑定)。对应 GDAL 那一侧是
``OGRCoordinateTransformation``,而 GDAL 内部调的同样是 PROJ,所以数值结果
是同一个来源。

三个必须先说清的坑(都实测过)
------------------------------

**1. 轴序 —— 不显式指定 ``always_xy=True`` 会静默给你错值。**
PROJ 默认按 EPSG 权威轴序,而 EPSG:4326 的权威轴序是 **(纬度, 经度)**;
GDAL/OGR、本库、以及几乎所有 GIS 数据都是 **(经度, 纬度)**。实测同一对
坐标::

    Transformer.from_crs(4527, 4326, always_xy=True)   -> (115.9188197, 28.7258387)  ✓
    Transformer.from_crs(4527, 4326)                   -> (inf, inf)                ✗

本模块**一律** ``always_xy=True``,没有开关 —— 因为本库的坐标就是
lon/lat 顺序,给一个能改的开关只会制造第二套轴序。

**2. ``errcheck`` 拦不住"域外",别指望它。** 实测把经度 10°(远在 zone 39
之外)喂给"经纬度 → GK zone 39"的转换器,``errcheck=True`` 和 ``False``
**都不报错**,都返回一个**有限但完全错误**的数::

    tr = Transformer.from_crs(4326, 4527, always_xy=True)
    tr.transform([10.0], [28.7], errcheck=True)   # -> ([31738730.66], [13117482.87])

31738730 看着就"像个带号坐标",但它和输入毫无关系。**所以本模块自己做
量级体检**(:func:`check_crs_range`):源是地理坐标系时坐标必须落在
±180 / ±90 之内,源是投影坐标系时量级必须大于 1000 米。这两条能挡住最常见
那种错误 —— 把投影坐标当经纬度喂进去,或者反过来。体检不通过时发
``CoordTransformWarning``,不抛异常(因为极端数据确实可能存在)。
``check=False`` 关掉。

**3. 基准面改化可能是"ballpark"。** pyproj 会如实写在转换器的
``description`` 里,实测 4527 → 4326 是::

    axis order change (2D) + Inverse of 3-degree Gauss-Kruger zone 39
    + Ballpark geographic offset from China Geodetic Coordinate System 2000 to WGS 84
    + axis order change (2D)

"Ballpark" 意思是 CGCS2000 到 WGS84 之间**没有做七参数改化**(没装对应的
格网文件),只按地理偏移处理 —— 与 GDAL 在同等条件下的行为一致。要更准
就得配 PROJ 的数据格网,那不是本模块能解决的。**看到 ``Ballpark`` 别当成
bug,但也别当成厘米级精度。**

几何语义
--------
* **返回新几何,不改原对象。** ⚠️ 这一点**与 GDAL 相反**:
  ``OGRGeometry::Transform()`` 是**就地改**的。这里不这么做,原因是本库的
  要素把解码后的几何**缓存在 ``feat.geometry`` 上**(见 ``_LazyGeometry``),
  就地改会**静默污染那个缓存** —— 同一个 feature 再取一次几何,拿到的是
  改过之后的,而盘上的没变。这种错没法从调用点看出来。
* **Z 一并转,M 原样保留。** 二维转换器收到 Z 会**原样透传**(实测),
  三维转换器则真的转 —— 交给 pyproj 判断,不自己猜。
* **multipatch 允许转**(与谓词那一档不同):转换是**逐点**的,不依赖
  "环"模型,所以没有谓词那种语义鸿沟。
* 环在内存里**不闭合**(见 DESIGN.md §2.11),转换前后都是,不用补点。
* 空几何、``None`` 原样返回。

性能
----
实测 pyproj 本身就是 ~200 ns/点(20 万点的数组),这是 PROJ 的算力下限,
不是本模块的开销。本模块在它之上只做两件事:拆出 x/y 序列、把结果重新
交错回 ``array('d')``。装了 numpy 时走 ``np.empty`` + 切片赋值 +
``frombytes``;没装就用 ``array('d')`` 的切片赋值(仍在 C 层)。
``PYOPENFILEGDB_NO_NUMPY=1`` 强制走后者,便于对拍。

遍历一个图层时几何是**惰性**的,所以 ``transform_layer()`` 只在真正需要
几何时才解码(见 ``GdbFeature.geometry``)。

命令行
------
::

    # 整个图层换个坐标系,写进一个新库
    python -m pyopenfilegdb.coordinates_system 源.gdb 村行政区划 目标.gdb 4326

    # 目标图层名默认同源;也可以显式给;--src-epsg 覆盖源坐标系
    python -m pyopenfilegdb.coordinates_system 源.gdb 村行政区划 目标.gdb 4326 \
        村_wgs84 --src-epsg 4527

源坐标系默认取 ``layer.spatial_ref``(用 WKT 交给 pyproj 解析,解析不了再
退回 ``effective_wkid``)。
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import warnings
from array import array
from functools import lru_cache
from typing import Any, Dict, Iterator, Optional, Sequence, Tuple, Union

# ⚠️ numpy 与 C 扩展同口径:装了就用,没装/被环境变量关掉就回退。
# 这里只用来把 pyproj 吐出来的两个序列重新交错成一块 array('d'),
# **不参与任何数值计算**,所以没有"符号敏感路径不许走 numpy"那类顾虑。
try:
    import numpy as _np
except ImportError:                                             # pragma: no cover
    _np = None

if os.environ.get('PYOPENFILEGDB_NO_NUMPY', '').strip().lower() \
        not in ('', '0', 'false', 'no'):
    _np = None

#: numpy 快路径在导入时是否可用(与 ``_geometry_ops.HAS_NUMPY`` 同口径)。
HAS_NUMPY = _np is not None

try:
    from pyproj import CRS as _CRS, Transformer as _Transformer
    #: pyproj 是否可用。**没装不代表本库坏了** —— 只有这个模块不能用。
    HAS_PYPROJ = True
except ImportError:                                             # pragma: no cover
    _CRS = None
    _Transformer = None
    HAS_PYPROJ = False

from ._datatypes import GdbFeature, GdbSpatialRef
from .geometry import Geometry, _FlatCoords

#: 可以喂给 :func:`resolve_crs` 的东西。
CRSLike = Union[str, int, Any]


class CoordTransformError(ValueError):
    """坐标转换的入参有问题(与"没装 pyproj"区分开)。"""


class CoordTransformWarning(UserWarning):
    """转换**做完了**,但结果看着不对(量级离谱 / 落在目标坐标系的使用域之外)。

    单独一个类是为了让调用方能精确地 ``filterwarnings`` 掉它 ——
    ``warnings.filterwarnings('ignore', category=CoordTransformWarning)``。
    """


def _require_pyproj() -> None:
    """pyproj 缺席时给一句能照做的错,而不是让 ``NameError`` 冒出去。

    措辞里点明"这只是可选依赖",免得使用者以为装漏了本库的必需依赖。
    """
    if not HAS_PYPROJ:
        raise ImportError(
            '这个功能需要 pyproj(PROJ 的 Python 绑定)。\n'
            '它是本库的**可选**依赖,没装不影响读写 —— 只有 '
            'pyopenfilegdb.coordinates_system 用不了。\n'
            '安装:pip install pyproj        (或 pip install pyopenfilegdb[crs])')


# ---------------------------------------------------------------------------
# 坐标系解析
# ---------------------------------------------------------------------------
def _prefer_epsg(crs: Any, wkid: Optional[int]) -> Any:
    """见 :func:`resolve_crs` 里那段"Esri WKT 与 EPSG 不逐字相同"。

    只在**两处独立证据一致**时才把 WKT 版换成 EPSG 版:

    * PROJ 自己按默认置信度(70)认出它是某个 EPSG 码 —— 这个匹配要求
      名字、投影方法、全部参数、单位都对得上;
    * 文件里写的 ``effective_wkid`` 就是那个码。

    ``wkid`` 为空时(比如调用方直接传了一段裸 WKT 字符串)不升级 —— 少了
    第二处证据,宁可不换。换与不换的结果实测逐位相同(见 docstring),
    所以保守的代价只是少一份"使用域"。
    """
    if not wkid:
        return crs
    try:
        code = crs.to_epsg()
    except Exception:                                           # noqa: BLE001
        return crs
    if code and code == wkid:
        try:
            return _CRS.from_epsg(code)
        except Exception:                                       # noqa: BLE001
            return crs
    return crs


def resolve_crs(obj: CRSLike) -> Any:
    """把各种"坐标系写法"统一成 ``pyproj.CRS``。

    接受:

    * ``pyproj.CRS`` —— 原样返回;
    * ``int`` / ``str`` 里的纯数字 —— 当 EPSG 代码(``4527`` / ``'4527'``
      / ``'EPSG:4527'``);
    * ``str`` 的 WKT / PROJ 串 / ``'EPSG:xxxx'`` / ``'+proj=...'``;
    * :class:`~pyopenfilegdb.GdbSpatialRef` —— **优先用 ``wkt``**(它把
      ArcGIS 的量化参数也带上了,信息比 WKID 全),解析不出来再退回
      ``effective_wkid``;
    * ``GdbLayer`` —— 走它的 ``spatial_ref``(没有坐标系就报错,因为
      "把没有坐标系的东西转到有坐标系"本来就没有依据)。

    Esri 的 WKT 与 EPSG 的"同一"坐标系并不逐字相同
    ---------------------------------------------------
    实测把 gdb 里那份 ``PROJCS["CGCS2000_3_Degree_GK_Zone_39", ...]`` 喂给
    ``CRS.from_wkt``,拿到的东西与 ``CRS.from_epsg(4527)`` **``!=``**,
    因为:

    * Esri WKT1 **不写 ``AXIS`` 节点**,PROJ 只好按 Esri 的惯例当
      ``(E, N)``;而 EPSG:4527 的权威轴序是 ``(N, E)``;
    * Esri WKT **不带 ``ID["EPSG", ...]``**,所以解析出来的 CRS 没有 EPSG
      身份,也就没有**使用域**(``area_of_use``)和 scope;
    * 单位名一处写 ``Degree`` 一处写 ``degree``(纯字面差别)。

    前两条在 ``always_xy=True`` 下**数值上完全抵消** —— 实测四个点(含一个
    远在带外的)两条路径的结果**逐位相同,差 0.0**。所以这里优先用 WKT 不会
    改变结果。

    **但 EPSG 那份多带了"使用域"**,而 PROJ 正是靠它来在多个候选转换管道
    之间挑一个(有 datum 格网文件时才会显出差别)。所以这里做个**保守升级**:
    先按 WKT 建 CRS,若 PROJ 自己认出它是某个 EPSG 码(``to_epsg()``,默认
    置信度 70),**且**与文件里的 WKID 一致,就换成 EPSG 那份。两处独立证据
    一致才升级,不猜;对不上就保留 WKT(数据自己声明的定义永远优先)。

    做这个升级还有个副作用是描述读起来正常 —— 不升级时
    ``transformer_description()`` 会写成 ``Inverse of unnnamed (Gauss
    Kruger)``,那个 ``unnnnamed`` 是 PROJ 对"没有名字的转换"的占位。

    :raises CoordTransformError: 认不出来,或者对象压根没带坐标系。
    """
    _require_pyproj()

    if isinstance(obj, _CRS):
        return obj

    # 图层:下一层就是它的 spatial_ref
    sr = getattr(obj, 'spatial_ref', None)
    if sr is not None and not isinstance(obj, GdbSpatialRef):
        return resolve_crs(sr)

    if isinstance(obj, GdbSpatialRef):
        if not obj:
            raise CoordTransformError(
                '这个图层没有坐标系(spatial_ref 为空),无法作为转换的源或目标。'
                '请显式传一个 EPSG 代码 / WKT。')
        if obj.wkt:
            try:
                return _prefer_epsg(_CRS.from_wkt(obj.wkt), obj.effective_wkid)
            except Exception:                                   # noqa: BLE001
                # Esri 的 WKT 偶尔有 pyproj 不认的方言 —— 退回 WKID。
                # ⚠️ 注意 Esri 的 WKID 与 EPSG 的 WKID 大多数时候一致,
                #    但**不是**全部;能解析 WKT 时绝不要走这一支。
                pass
        if obj.effective_wkid:
            try:
                return _CRS.from_epsg(obj.effective_wkid)
            except Exception as exc:                            # noqa: BLE001
                raise CoordTransformError(
                    f'坐标系解析失败:WKT 解析不了,WKID {obj.effective_wkid} '
                    f'也查不到({exc})。') from exc
        raise CoordTransformError(f'坐标系解析失败:{obj!r}')

    if isinstance(obj, bool):       # bool 是 int 的子类,先挡掉
        raise CoordTransformError(f'坐标系不能是 bool:{obj!r}')

    if isinstance(obj, int):
        try:
            return _CRS.from_epsg(obj)
        except Exception as exc:                                # noqa: BLE001
            raise CoordTransformError(f'没有 EPSG:{obj} 这个坐标系') from exc

    if isinstance(obj, str):
        text = obj.strip()
        if not text:
            raise CoordTransformError('坐标系字符串是空的')
        upper = text.upper()
        for prefix in ('EPSG:', 'ESRI:'):
            if upper.startswith(prefix):
                try:
                    return _CRS.from_epsg(int(text[len(prefix):]))
                except ValueError:
                    break
                except Exception as exc:                        # noqa: BLE001
                    raise CoordTransformError(f'解析 {text!r} 失败:{exc}') from exc
        if text.isdigit():
            return resolve_crs(int(text))
        try:
            return _CRS.from_user_input(text)
        except Exception as exc:                                # noqa: BLE001
            raise CoordTransformError(
                f'解析坐标系字符串失败:{text[:80]!r}...({exc})') from exc

    raise CoordTransformError(f'认不出来的坐标系写法:{type(obj).__name__}')


def make_transformer(src: CRSLike, dst: CRSLike) -> Any:
    """建一个 ``pyproj.Transformer``;同一个 ``(src, dst)`` 只建一次。

    ``always_xy=True`` 是**写死**的,没有开关 —— 理由见模块 docstring 第 1 条。
    缓存按解析后的 CRS 对象做键(pyproj 的 ``CRS`` 可哈希且按内容相等),
    所以 ``make_transformer(4527, 4326)`` 反复调不会重复建管道。

    :raises CoordTransformError: 两个坐标系之间**建不出**转换管道
        (比如一个没有基准面信息、PROJ 也找不到路径)。
    """
    _require_pyproj()
    return _cached_transformer(resolve_crs(src), resolve_crs(dst))


@lru_cache(maxsize=32)
def _cached_transformer(src: Any, dst: Any) -> Any:
    """真正的构造函数;``lru_cache`` 挂在解析之后,键一定是 ``CRS``。"""
    try:
        return _Transformer.from_crs(src, dst, always_xy=True)
    except Exception as exc:                                    # noqa: BLE001
        raise CoordTransformError(
            f'建不出 {src.name!r} -> {dst.name!r} 的转换管道:{exc}') from exc


def transformer_description(src: CRSLike, dst: CRSLike) -> str:
    """转换器的 ``description`` —— 会如实写出 "Ballpark" 之类的话术。

    建议在把整库转换一遍**之前**打一行出来看看:里面若出现
    ``Ballpark``,说明基准面之间没做改化。
    """
    return make_transformer(src, dst).description


# ---------------------------------------------------------------------------
# 坐标体检
# ---------------------------------------------------------------------------
def _range_of(crs: Any, xmin: float, ymin: float, xmax: float, ymax: float,
              role: str) -> Optional[str]:
    """看一组坐标像不像该坐标系的数,返回一句人话(不像时)或 ``None``。"""
    if not all(map(_finite, (xmin, ymin, xmax, ymax))):
        return None                     # 空几何/退化几何,不judge
    biggest = max(abs(xmin), abs(xmax), abs(ymin), abs(ymax))
    if crs.is_geographic:
        # 地理坐标系:经纬度。超过 ±180/±90 基本可以断定喂错了(最常见的是
        # 把投影坐标当经纬度)。
        if abs(xmin) > 180.0 or abs(xmax) > 180.0 \
                or abs(ymin) > 90.0 or abs(ymax) > 90.0:
            return (f'{role}坐标 ({xmin:.6g}, {ymin:.6g}) - ({xmax:.6g}, '
                    f'{ymax:.6g}) 超出了经纬度的取值范围(±180 / ±90),'
                    f'但 {role}坐标系 {crs.name!r} 是**地理坐标系**。'
                    f'多半是把投影坐标当经纬度用了。')
    elif crs.is_projected and biggest < 1000.0:
        # 投影坐标系:单位一般是米,坐标值通常在几千到几千万之间。
        # 全都很小 => 很可能把经纬度喂给了投影坐标系。
        return (f'{role}坐标 ({xmin:.6g}, {ymin:.6g}) - ({xmax:.6g}, '
                f'{ymax:.6g}) 的量级不到 1000,但{role}坐标系 '
                f'{crs.name!r} 是**投影坐标系**(单位通常是米)。'
                f'多半是把经纬度当投影坐标用了。')
    return None


def _finite(v: float) -> bool:
    return v == v and v not in (float('inf'), float('-inf'))


def check_crs_range(geom: Geometry, crs: CRSLike, role: str = '源') -> Optional[str]:
    """量级体检:``geom`` 的包围盒像不像属于 ``crs``。

    :returns: 不像时返回一句人话,像(或无从判断)时返回 ``None``。
    :rtype: Optional[str]

    ⚠️ **这条体检是必须的,因为 pyproj 不会替你拦。** 实测把域外的点喂进去,
    ``errcheck=True`` 也不报错,只是给你一个有限但错误的数(见模块 docstring
    第 2 条)。量级体检能挡住"经纬度 ↔ 投影坐标搞反了"这一类,
    而那是实际最常发生的错误。

    只做包围盒级的粗判(代价 O(1),不用扫顶点),所以**不保证**抓住所有问题
    —— 它只是个安全网,不是校验器。
    """
    env = geom.envelope()
    if env is None:
        return None
    return _range_of(resolve_crs(crs), env[0], env[1], env[2], env[3], role)


def _warn_range(msg: str) -> None:
    warnings.warn(msg, CoordTransformWarning, stacklevel=3)


# ---------------------------------------------------------------------------
# 核心:逐 part 转坐标
# ---------------------------------------------------------------------------
def _interleave(X: Sequence[float], Y: Sequence[float]) -> array:
    """把 x 序列和 y 序列交错成一块 ``array('d')``。

    两条路都**不建逐点的 Python 对象**:numpy 那条用切片赋值 + ``frombytes``
    (一次内存拷贝);纯 Python 那条用 ``array('d')`` 的**带步长切片赋值**,
    也在 C 层完成。

    ⚠️ 这里是"把 double 装箱成 Python 对象"最容易重新漏回来的地方 ——
    用列表推导逐点包元组,在 4,500 万顶点的语料上要多花几秒(见
    DESIGN.md §2.19.5 记的 37.4 ns/值 过路费)。
    """
    n = len(X)
    out = array('d', bytes(16 * n))
    if _np is not None and isinstance(X, _np.ndarray):
        buf = _np.empty(2 * n, dtype=_np.float64)
        buf[0::2] = X
        buf[1::2] = Y
        out.frombytes(buf.tobytes())
        return out
    out[0::2] = array('d', X)
    out[1::2] = array('d', Y)
    return out


def _transform_parts(tr: Any, xy_parts: Sequence[Any], z_parts: Any):
    """逐个 part 转坐标,返回 ``(xy_parts, z_parts)``(都是新造的)。

    ⚠️ 只读 :attr:`Geometry.xy_parts` / :attr:`Geometry.z_parts`,
    **绝不碰** ``.coordinates`` —— 后者是惰性物化的派生视图,碰一下就把
    "省掉逐点建对象"这件事又花回去了(见 geometry.py 的模块 docstring)。
    """
    out_xy = []
    out_z = None if z_parts is None else []
    for i, xy in enumerate(xy_parts):
        n = len(xy) >> 1
        if not n:
            out_xy.append(array('d'))
            if out_z is not None:
                out_z.append(array('d'))
            continue
        xs = xy[0::2]
        ys = xy[1::2]
        if out_z is None:
            X, Y = tr.transform(xs, ys)
        else:
            # 二维转换器会把 Z 原样透传;三维的才真的改(实测)。交给 pyproj 判。
            X, Y, Z = tr.transform(xs, ys, z_parts[i])
        out_xy.append(_interleave(X, Y))
        if out_z is not None:
            out_z.append(array('d', Z))
    return out_xy, out_z


def transform_geometry(geom: Optional[Geometry],
                        dst: CRSLike,
                        src: Optional[CRSLike] = None,
                        *,
                        transformer: Any = None,
                        check: bool = True) -> Optional[Geometry]:
    """把一个几何转到目标坐标系,返回**新几何**。

    :param geom: 要转的几何;``None`` 或空几何原样返回。
    :param dst: 目标坐标系(EPSG 号 / WKT / ``pyproj.CRS`` / ``GdbSpatialRef``)。
    :param src: 源坐标系。给了 ``transformer`` 时可以不给。
    :param transformer: 现成的 ``pyproj.Transformer``(批量转换时**一定要**
        传,否则每条几何都要重新解析 CRS 并建管道)。
    :param check: 是否做量级体检(见 :func:`check_crs_range`)。批量调用时
        建议在循环外自己查一次,这里传 ``False``,免得刷屏。
    :returns: 新 :class:`Geometry`;``shape_type`` / ``has_z`` / ``has_m``
        与原几何一致。

    ⚠️ **不改 ``geom`` 本身。** 与 ``OGRGeometry::Transform()`` 的就地语义
    不同,理由见模块 docstring —— 要素会缓存解码后的几何,就地改会静默
    污染那个缓存。

    ⚠️ **形参序是 ``(geom, dst, src)``,目标在前、源在后**,而且 ``src`` 只
    在没给 ``transformer`` 时才是必需的(几何自己不知道它属于哪个坐标系)。
    这与 :meth:`Geometry.to_crs`、:func:`transform_layer`、
    ``GdbLayer.write_transformed`` 全部一致。**别把这两个名字对调去"修"
    它** —— 对调不会报错,只会把转换方向反过来,症状是一堆 ``inf`` 坐标
    (2026-09-30 实测过一次:``to_crs(4326, src=4527)`` 返回 ``(inf, inf)``)。
    """
    if geom is None:
        return None

    from ._constants import ShapeType as _ST          # 只在需要时 import
    if geom.shape_type == _ST.NULL or geom.is_empty:
        # 空几何没有坐标可转,但要**返回一个同类型的新对象**,而不是原对象
        # —— 调用方拿到的东西不该在"空/非空"上有两套身份语义。
        # `_like` 已经处理了 point / null 那两种特殊形态,直接用。
        return geom._like([], None, None)

    if transformer is None:
        if src is None:
            raise CoordTransformError(
                'transform_geometry() 需要 src(源坐标系),或者直接给 '
                'transformer。几何自己不知道它是什么坐标系。')
        transformer = make_transformer(src, dst)

    if check:
        crs = src if src is not None else None
        if crs is None:
            # 只给了 transformer 时拿它的源 CRS 来体检
            crs = getattr(transformer, 'source_crs', None)
        if crs is not None:
            msg = check_crs_range(geom, crs, '源')
            if msg:
                _warn_range(msg)

    if geom.kind == 'geometrycollection':
        # GC **没有坐标**(见 :meth:`Geometry.geometry_collection`),下面那条
        # "取 ``xy_parts`` 再逐点转"的路对它直接抛 ``TypeError``。但转换只作用
        # 在坐标上,所以"把每个子几何各自转完、按原顺序装回一个 GC"与"整体转"
        # **完全等价** —— 子几何的层级、顺序、空子几何都原样保留(不摊平、不丢)。
        #
        # ⚠️ ``transformer`` 必须**传下去**:递归里每条子几何都重建一次管道的话,
        # 一个 100 个子几何的 GC 就要解析 100 次 CRS、建 100 条管道 —— 正是本函数
        # docstring 里"批量一定要传 transformer"那条要避免的事。
        # ``check=False`` 也是刻意的:上面已经拿整个 GC 的包围盒(＝各子几何包围盒
        # 的并)做过一次量级体检,逐子几何再报一遍只会重复刷屏。
        #
        # ⚠️ 走到这里说明 GC **非空**(空分支在上面已经返回),所以 ``transformer``
        # 与量级体检都按"真要做转换"处理。
        return Geometry.geometry_collection([
            transform_geometry(kid, dst, src, transformer=transformer,
                               check=False)
            for kid in geom.geometries()])

    xy_parts = geom.xy_parts
    z_parts = geom.z_parts
    new_xy, new_z = _transform_parts(transformer, xy_parts, z_parts)
    out = geom._like(new_xy, new_z, geom.m_parts)
    # :meth:`Geometry._like` 不带 ``_shells`` —— 而转换是**逐点**的,环的
    # 归属(哪个是外环)根本不会变,照抄过去比让 `_roles()` 重新按绕向猜更准。
    out._shells = geom._shells
    return out


# ---------------------------------------------------------------------------
# 图层:读(惰性)
# ---------------------------------------------------------------------------
def transform_layer(layer: Any, dst: CRSLike,
                    src: Optional[CRSLike] = None, *,
                    transformer: Any = None,
                    where: Any = None, bbox: Any = None,
                    fields: Any = None, limit: Optional[int] = None,
                    offset: int = 0, check: bool = True) -> Iterator[GdbFeature]:
    """遍历一个图层,产出**几何已转换**的要素(惰性,不写盘)。

    过滤参数 ``where`` / ``bbox`` / ``fields`` / ``limit`` / ``offset``
    **原样透传给** :meth:`GdbLayer.read_features` —— 所以 ``bbox`` 仍然是
    在**源坐标系**里过滤的(它走的是盘上存储包围盒的粗筛,那时还没转)。
    要在目标坐标系里过滤,请转换完再自己筛。

    ⚠️ 转出来的几何是**新对象**;原要素的 ``feat.geometry`` 不受影响,
    但遍历过程中原几何会被解码一次(本来也要解)。

    :param src: 源坐标系;默认取 ``layer.spatial_ref``。
    :param check: 量级体检。**默认会在遍历开始前用图层范围查一次**
        (一次就够,图层里各要素的量级不会差出数量级),之后逐要素不再查。
        所以传进来的是 ``True`` 时,实际每要素那次是关掉的。
    """
    if transformer is None:
        if src is None:
            src = layer.spatial_ref
        transformer = make_transformer(src, dst)

    if check:
        # 整层查一次,而不是逐要素查 —— 后者会在 21,217 条的循环里
        # 把同一句话告警两万遍。
        ext = layer.extent
        if ext:
            crs = src if src is not None else getattr(transformer, 'source_crs', None)
            if crs is not None:
                try:
                    msg = _range_of(resolve_crs(crs), ext[0], ext[1], ext[2],
                                    ext[3], '图层范围 ')
                except Exception:                               # noqa: BLE001
                    msg = None
                if msg:
                    _warn_range(msg)
        check = False               # 逐要素那次关掉

    geom_field = getattr(layer, 'geometry_field_name', 'Shape')
    for feat in layer.read_features(where=where, bbox=bbox, fields=fields,
                                    limit=limit, offset=offset):
        new_geom = transform_geometry(feat.geometry, dst, transformer=transformer,
                                      check=check)
        yield GdbFeature(feat.oid, feat.attributes, new_geom)


# ---------------------------------------------------------------------------
# 图层:写
# ---------------------------------------------------------------------------
def _bbox_in_dst(transformer: Any, bbox: Sequence[float]) -> Optional[Tuple[float, ...]]:
    """把源包围盒转到目标坐标系,返回目标坐标系里的包围盒。

    ⚠️ 取的是**四角 + 四边中点**共 8 个点,不是四个角 —— 投影是**非线性**的,
    四个角转过去再取 min/max 会**低估**真实范围(边中点的像可能鼓在外面)。
    中点是为了让边不被切得太平(更细的分割没必要:这里只用来推导量化参数,
    而量化参数的 origin 我又留了 Margin)。

    转不过去的点(inf/nan,即 PROJ 对域外点的"静默垃圾")直接丢掉;全丢完
    就返回 ``None``,让调用方退回默认量化。
    """
    x0, y0, x1, y1 = bbox
    mx, my = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    xs = [x0, x1, x1, x0, mx, mx, x0, x1]
    ys = [y0, y0, y1, y1, y0, y1, my, my]
    try:
        X, Y = transformer.transform(xs, ys)
    except Exception:                                           # noqa: BLE001
        return None
    good = [(a, b) for a, b in zip(X, Y) if _finite(a) and _finite(b)]
    if not good:
        return None
    return (min(a for a, _ in good), min(b for _, b in good),
            max(a for a, _ in good), max(b for _, b in good))


#: 地理坐标系(constant)量化:与 ``core.DEFAULT_QUANTIZATION`` 完全一致 ——
#: 那就是 GDAL ``CreateGDBItems`` / ArcGIS 空白库里 Shape 字段的值,不是我们
#: 编的。地理坐标的取值范围天然在 ±180/±90 内,固定 origin 永远安全。
_GEOG_QUANTIZATION: Dict[str, float] = {
    'x_origin': -180.0, 'y_origin': -90.0,
    'xy_scale': 1000000.0, 'xy_tolerance': 0.000002,
}


def quantization_for(crs: Any, bbox: Optional[Sequence[float]]) -> Optional[Dict[str, float]]:
    """按**目标坐标系**与**目标范围**推一套量化参数(XY 部分)。

    FileGDB 把每个坐标存成定点整数 —— ``round((v - origin) * scale)``
    (见 :mod:`._esri_geometry` 的 ``_grid``)。这套 ``origin`` / ``scale``
    存在几何字段描述区里,**建表时就定死了**,之后改不了。所以换个坐标系
    (尤其是地理 ↔ 投影之间,坐标量级差 7 个数量级)必须重推,照抄源图层的
    是错的。

    规则(两条都对着证据定的,不是拍脑袋):

    * **地理坐标系** —— 用 ``-180 / -90 / 1e6 / 2e-6``,与
      ``core.DEFAULT_QUANTIZATION`` 逐字相同。那是 GDAL ``CreateGDBItems``
      和 ArcGIS 空白库里的值。
    * **投影坐标系** —— ``xy_tolerance = 1e-4``(CRS 单位,通常是米),
      ``xy_scale = 2 / tolerance = 20000``。**这两个数正是参照语料里
      ArcGIS 自己写的值**(``村行政区划`` 的 Shape 字段实测
      ``xy_scale=20000`` / ``xy_tolerance=0.0001``)。``tolerance = 2/scale``
      这个关系在地理那一档也成立(``2/1e6 = 2e-6``),所以两档其实是同一条
      规则。
      ``origin`` 由目标包围盒推出:取 ``floor(min - 0.5 * span)``,即比最小值
      再低半个跨度。

    ⚠️ **``origin`` 必须严格小于范围内的一切坐标,而且不能只小一点点。**
    理由不是精度,是**写入会直接炸**:``_esri_geometry`` 的 ``_grid`` 在
    ``v < origin`` 时返回负数,而 ``write_varuint`` 明令
    ``raise ValueError('write_varuint 不接受负数')``。所以本函数留了半个
    跨度的余量。**这也是为什么投影档不能沿用地理档那个固定的
    ``x_origin = -180``** —— 投影坐标完全可能小于 -180(任何带负值的
    地方坐标系、州平面坐标系),那会一写就抛。

    注意这里只给 XY。Z/M 的量级与坐标系无关(仍是米),沿用默认值。

    :param crs: 目标坐标系(``pyproj.CRS``)。
    :param bbox: 目标坐标系里的范围;``None`` 时返回 ``None``(调用方退回默认)。
    :returns: 含 ``x_origin`` / ``y_origin`` / ``xy_scale`` / ``xy_tolerance``
        的字典,或 ``None``。

    ⚠️ **本函数是"本库的推导",不是从 GDAL 抄来的。** GDAL 的
    ``FileGDBGeomField::SetXYOriginScaleTolerance`` 只是收下这四个数,
    "该填多少"这一步在 GDAL 里是由创建选项(``XYSCALE`` 等)或 ArcGIS
    自己决定的,没有可抄的纯 C++ 规则。上面那两条是有实测证据的**复现**,
    但不能宣称与 ArcGIS 在所有数据上都选得一样 —— 差别只在文件体积与
    末位精度,几何本身不失真(实测往返偏差 ≤ 1e-8 米)。
    """
    if bbox is None or not all(map(_finite, bbox)):
        return None
    if crs.is_geographic:
        return dict(_GEOG_QUANTIZATION)
    if not crs.is_projected:
        return None                 # 既非地理也非投影(地心/垂直…),不猜
    span = max(bbox[2] - bbox[0], bbox[3] - bbox[1])
    if not (span > 0.0):
        return None
    tol = 1e-4
    pad = max(1.0, 0.5 * span)
    return {
        'x_origin': float(math.floor(bbox[0] - pad)),
        'y_origin': float(math.floor(bbox[1] - pad)),
        'xy_scale': 2.0 / tol,
        'xy_tolerance': tol,
    }


def create_transformed_layer(src_layer: Any, gdb: Any, dst: CRSLike,
                             name: Optional[str] = None, *,
                             quantization: Any = None,
                             transformer: Any = None) -> Any:
    """在 ``gdb`` 里建一个和 ``src_layer`` 同构、但坐标系是 ``dst`` 的图层。

    字段逐条照抄(名字 / 类型 / 长度 / 空值性 / 别名),几何类型与表级
    ``has_z`` / ``has_m`` 也照抄。

    **量化参数会按目标坐标系重新推**(见 :func:`quantization_for`)——
    这一步不能省:FileGDB 的坐标是定点整数,``origin``/``scale`` 建表时就写进
    几何字段描述区、之后改不了,而地理 ↔ 投影之间坐标量级差好几个数量级。
    推的时候要用到**目标坐标系里的图层范围**,所以这里会先拿源图层的
    ``extent`` 转一遍(只转 8 个点,不解任何几何)。

    :param quantization: 覆盖推出来的量化参数(字典,键同
        :func:`quantization_for`)。想精确复现源数据那套网格时用得上。
    :param transformer: 现成的转换器;不给就现建一个用于推范围。
    """
    _require_pyproj()
    dst_crs = resolve_crs(dst)
    if transformer is None:
        src_crs = src_layer.spatial_ref
        if src_crs:
            transformer = make_transformer(src_crs, dst_crs)
    name = name or src_layer.name
    fields = []
    for f in src_layer.fields:
        # OID 字段由 create_layer 自己生成,不要重复加。
        if f.name.lower() in ('objectid', 'oid', 'fid'):
            continue
        fields.append(f)

    # --- 空间参考:走 dict,这样 wkid / latest_wkid 才带得上 ---
    # ⚠️ 传**字符串** WKT 是不行的:`core._quantization_parts` 对 str 只设 wkt,
    #    把 wkid 与 latest_wkid 一律写成 0(见 core.py 的 str 分支)。WKT 照样
    #    能读,但 WKID 是 ArcGIS/QGIS 认坐标系的快路径,丢了一堆工具就得靠
    #    解析 WKT 去猜。EPSG 码从 to_epsg() 拿;它给出 None(自定义坐标系)
    #    时老老实实留 0,不编一个。
    wkid = dst_crs.to_epsg() or 0
    spatial_ref: Dict[str, Any] = {
        # Esri 方言的 WKT1 —— ArcGIS 认这个。pyproj 默认吐 WKT2,那是 EPSG 的
        # 方言,写进 FileGDB 也能读但不够"原生"。
        'wkt': dst_crs.to_wkt(version='WKT1_ESRI'),
        'wkid': wkid,
        'latest_wkid': wkid,
    }

    # --- 量化参数 ---
    if quantization is None:
        target_bbox = None
        if transformer is not None:
            ext = src_layer.extent
            if ext:
                target_bbox = _bbox_in_dst(transformer, ext)
        quantization = quantization_for(dst_crs, target_bbox)
    if quantization:
        spatial_ref.update(quantization)

    return gdb.create_layer(
        name,
        fields=fields,
        geometry_type=src_layer.geometry_type,
        spatial_ref=spatial_ref,
        has_z=src_layer.has_z,
        has_m=src_layer.has_m,
    )


def write_transformed(src_layer: Any, dst_layer: Any, dst: CRSLike,
                      src: Optional[CRSLike] = None, *,
                      transformer: Any = None,
                      where: Any = None, bbox: Any = None,
                      limit: Optional[int] = None, offset: int = 0,
                      check: bool = True,
                      progress: Optional[int] = None) -> int:
    """把 ``src_layer`` 转换后逐条写进 ``dst_layer``,返回写入条数。

    ``dst_layer`` 可以来自 :func:`create_transformed_layer`,也可以是自己
    建好的(字段必须对得上,否则写的时候会抛)。

    :param progress: 每写这么多条打一行进度到 stderr(``None`` = 不打)。
        转换是纯 CPU 活,几万条要素要跑一会儿,没有进度会以为它卡死了。
    """
    if transformer is None:
        if src is None:
            src = src_layer.spatial_ref
        transformer = make_transformer(src, dst)

    if check:
        _precheck_layer(src_layer, src, transformer)
        check = False

    n = 0
    for feat in transform_layer(src_layer, dst, src, transformer=transformer,
                                where=where, bbox=bbox, limit=limit,
                                offset=offset, check=check):
        dst_layer.write_feature(feat)
        n += 1
        if progress and n % progress == 0:
            print(f'  已写 {n} 条...', file=sys.stderr)
    return n


def _precheck_layer(layer: Any, src: Any, transformer: Any) -> None:
    """遍历开始前的量级体检(一次)。"""
    ext = layer.extent
    if not ext:
        return
    crs = src if src is not None else getattr(transformer, 'source_crs', None)
    if crs is None:
        return
    try:
        msg = _range_of(resolve_crs(crs), ext[0], ext[1], ext[2], ext[3], '图层范围 ')
    except Exception:                                           # noqa: BLE001
        return
    if msg:
        _warn_range(msg)


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='python -m pyopenfilegdb.coordinates_system',
        description='把一个图层换个坐标系,写进另一个 .gdb(或同一个库的新图层)。',
        epilog='源坐标系默认取 layer.spatial_ref;解析不了就用 --src-epsg。',
    )
    p.add_argument('src_gdb', help='源 .gdb')
    p.add_argument('src_layer', help='源图层名')
    p.add_argument('dst_gdb', help='目标 .gdb(不存在则新建;已存在则往里加图层)')
    p.add_argument('dst_epsg', help='目标坐标系:EPSG 号或 pyproj 能认的串')
    p.add_argument('dst_layer', nargs='?', default=None,
                   help='目标图层名(默认同源图层名)')
    p.add_argument('--src-epsg', default=None,
                   help='覆盖源坐标系(默认读图层的 spatial_ref)')
    p.add_argument('--where', default=None, help='只转满足条件的要素(SQL 子集)')
    p.add_argument('--limit', type=int, default=None,
                   help='最多转多少条(默认不限)')
    p.add_argument('--progress', type=int, default=2000,
                   help='每多少条打一次进度(0=不打)')
    p.add_argument('--quiet', action='store_true',
                   help='不打印量级体检的告警')
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    """命令行入口。返回进程退出码(0 = 成功)。"""
    args = _build_parser().parse_args(argv)

    if not HAS_PYPROJ:
        print('这个脚本需要 pyproj:pip install pyproj', file=sys.stderr)
        return 2

    from .core import OpenFileGDB

    try:
        src_crs = resolve_crs(args.src_epsg) if args.src_epsg else None
        dst_crs = resolve_crs(args.dst_epsg)
    except CoordTransformError as exc:
        print(f'坐标系不对:{exc}', file=sys.stderr)
        return 2

    print(f'打开源库:{args.src_gdb}')
    with OpenFileGDB.open(args.src_gdb) as src_db:
        try:
            src_layer = src_db.get_layer(args.src_layer)
        except Exception as exc:                                # noqa: BLE001
            print(f'取不到图层 {args.src_layer!r}:{exc}', file=sys.stderr)
            print(f'  可选:{src_db.list_feature_classes()}', file=sys.stderr)
            return 1

        if src_crs is None:
            src_crs = src_layer.spatial_ref
        try:
            tr = make_transformer(src_crs, dst_crs)
        except CoordTransformError as exc:
            print(f'建不出转换管道:{exc}', file=sys.stderr)
            return 2

        print(f'转换管道:{tr.description}')

        dst_name = args.dst_layer or src_layer.name
        # 目标库不存在就建一个;存在就打开(往里加图层)。
        if os.path.isdir(args.dst_gdb):
            dst_db = OpenFileGDB.open(args.dst_gdb, update=True)
            created = False
        else:
            dst_db = OpenFileGDB.create(args.dst_gdb)
            created = True
        try:
            dst_layer = create_transformed_layer(src_layer, dst_db, dst_crs,
                                                 name=dst_name)
            print(f'目标图层:{dst_name}(物理名 {dst_layer.physical_name})')

            with warnings.catch_warnings():
                if args.quiet:
                    warnings.simplefilter('ignore', CoordTransformWarning)
                n = write_transformed(src_layer, dst_layer, dst_crs, src_crs,
                                      transformer=tr, where=args.where,
                                      limit=args.limit,
                                      progress=args.progress or None)
            print(f'完成:写入 {n} 条要素')
        finally:
            dst_db.close()
            if created:
                pass
    return 0


if __name__ == '__main__':                                      # pragma: no cover
    sys.exit(main())
