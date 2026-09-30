"""打包配置里只放 C 扩展的编译信息 —— 其余元数据都在 ``pyproject.toml``。

之所以还需要一个 ``setup.py``:``python setup.py build_ext --inplace`` 是
"从源码目录直接跑"(``main.py`` 和 ``tools/`` 都是这么用的)时最省事的编译
入口,而 ``ext_modules`` 只能从 ``setup()`` 里给。
"""
from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext


class _BuildExt(build_ext):
    """MSVC 下必须加 ``/utf-8``。

    ``_gdbaccel.c`` 的注释是中文,MSVC 默认按系统 ANSI 代码页(这台机器是
    936)去读,会报 C4819;更麻烦的是按 GBK 成对解析 UTF-8 字节时,某个
    尾字节可能正好是 ``0x5C``(反斜杠),把一行注释的结尾"吃掉"、接到下一行
    代码上,静默改掉程序。所以这不是"消个警告",是**正确性**要求。

    只在 MSVC 下加:gcc/clang 默认就按 UTF-8 读,而且不认 ``/utf-8``。
    """

    def build_extensions(self):
        if self.compiler.compiler_type == 'msvc':
            for ext in self.extensions:
                ext.extra_compile_args = ['/utf-8'] + list(
                    ext.extra_compile_args or [])
        super().build_extensions()


setup(
    cmdclass={'build_ext': _BuildExt},
    ext_modules=[
        Extension('pyopenfilegdb._gdbaccel',
                  sources=['pyopenfilegdb/_gdbaccel.c']),
    ],
)
