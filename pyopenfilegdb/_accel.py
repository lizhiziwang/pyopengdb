"""可选 C 加速模块的探测与开关。

单独放一个模块,是为了让 :mod:`._esri_geometry` 保持"只做几何"的 import
清单 —— 它本来不需要 ``os``,把环境变量读取塞进去会往几何模块里掺无关依赖。

C 模块是**可选**的:没有编译产物(换了解释器、ABI 不匹配、还没
``build_ext``)就自动回退到纯 Python 实现,功能完全一样,只是慢。

开关
----
``PYOPENFILEGDB_NO_ACCEL=1`` 强制走纯 Python(差分测试、以及想验证回退
路径时用)。值按"读作真"处理,``0`` / ``false`` / ``no`` / 空串都算不启用。

回退是**正常状态**,不是错误 —— 比如仓库里那个 3.11.9 的 ``.venv`` 就拿不到
为 3.13 编的 ``.pyd``。
"""
from __future__ import annotations

import contextlib
import os

try:
    from . import _gdbaccel as _impl
except ImportError:                 # 没编译 / ABI 不匹配 / 换了解释器
    _impl = None

#: 真值集合之外的一律当作"不关闭"。
_FALSEY = ('', '0', 'false', 'no')

if os.environ.get('PYOPENFILEGDB_NO_ACCEL', '').strip().lower() not in _FALSEY:
    _impl = None

#: C 加速模块在**导入时**是否可用。注意这只是"构建/导入"的事实,
#: 不等价于"这一次调用走了 C" —— 测试里可以用 :func:`use` 在进程内切换,
#: 分派处读的就是下面这几个名字。
HAS_ACCEL = _impl is not None

#: 热路径用的两个入口:**结果是 array('d')**,不建逐点的 Python 对象。
#: 这是 :func:`_esri_geometry._read_xy_array` / ``_read_scalar_array`` 的
#: 落点(见 DESIGN.md §2.19.5)。
decode_xy_flat = _impl.decode_xy_flat if HAS_ACCEL else None
decode_scalar_flat = _impl.decode_scalar_flat if HAS_ACCEL else None

#: 元组版。**库的热路径不用它们** —— 只留给基准与差分工具做 A/B 对照
#: (``tools/bench_cext_varint.py`` / ``tools/verify_accel.py``),它们
#: 量的就是"建元组 vs 建 array"这条差。
decode_xy = _impl.decode_xy if HAS_ACCEL else None
decode_scalar = _impl.decode_scalar if HAS_ACCEL else None

#: WKT 写出(点序列 -> ``"(...)"``)。落点是 :func:`_esri_geometry.to_wkt`
#: 里的 ``seq_of`` —— 灌 PostgreSQL 这类"把几何导出去"的活儿,七成时间
#: 在这里(见 DESIGN.md §2.19.6)。
#:
#: ⚠️ 它与上面四个入口的**失败口径不同**:缓冲不是 double 数组时**返回
#: ``None``** 而不是抛异常(调用方收到 None 就退回纯 Python),其余情况
#: 与纯 Python 逐字符相同、抛的错也同型。
wkt_seq = _impl.wkt_seq if HAS_ACCEL else None

#: 分派处要读的全部名字。加新入口时**必须**加进来,否则 :func:`use`
#: 切不干净 —— 关掉加速后仍有一个入口是 C 的,"C 比纯 Python"就假了。
_ENTRIES = ('decode_xy_flat', 'decode_scalar_flat', 'decode_xy',
            'decode_scalar', 'wkt_seq')


@contextlib.contextmanager
def use(enabled: bool = True):
    """在进程内临时开/关加速,退出时还原。

    给差分测试用 —— "两条实现必须逐位相同"这种断言只能靠**同一份输入
    跑两遍**来证明(tests/test_read.py 与 tools/verify_accel.py 都靠它)。

    ``_esri_geometry`` 的派发处每次调用都重新读这些模块属性,所以在这里
    改是立刻生效的,不需要重新 import 任何模块::

        with _accel.use(False):
            ...        # 这一段走纯 Python
    """
    g = globals()
    saved = {name: g[name] for name in _ENTRIES}
    live = enabled and _impl is not None
    for name in _ENTRIES:
        g[name] = getattr(_impl, name, None) if live else None
    try:
        yield
    finally:
        g.update(saved)


__all__ = ['HAS_ACCEL', 'decode_xy_flat', 'decode_scalar_flat',
           'decode_xy', 'decode_scalar', 'wkt_seq', 'use']

