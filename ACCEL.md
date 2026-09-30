# 把 `_gdbaccel.c` 编成 `.pyd`

这份文档只讲一件事:**怎么把这个 C 扩展编译出来、装到哪、怎么确认生效、出错怎么修**。
关于它为什么存在、边界划在哪、性能数字是多少,见 [`DESIGN.md`](DESIGN.md)
§2.19.4(C 扩展)与 §2.19.5(flat 容器 —— 那两个 `*_flat` 入口就是给它用的)。

> **它是可选的。** 编不出来、没编、换了解释器,库都会**自动回退纯 Python**,
> 功能完全一样、结果逐位相同,只是几何解码慢**约 44 倍**(`村行政区划`
> 4,494 万顶点:65.4 s vs 1.50 s)。所以下面任何一步失败
> 都**不是故障**,可以先不编,回头再弄。

---

## 0. 四个入口,以及为什么有两个"没有调用方"的

`_gdbaccel.c` 导出 4 个函数,分两组:

| C 入口 | 结果容器 | 谁在用 |
|---|---|---|
| `decode_xy_flat` | `array('d')`(XY 交错) | **库的热路径**(`_read_xy_array`) |
| `decode_scalar_flat` | `array('d')` | **库的热路径**(`_read_scalar_array`) |
| `decode_xy` | `list[(float, float)]` | **库里没有调用方** —— 只给 `tools/` 做对照 |
| `decode_scalar` | `list[float]` | 同上 |

后两个是**刻意留着的**:它们是"同一段循环,只差容器形状"的对照物,
`tools/bench_cext_varint.py` 靠 `t_tuple − t_flat` 量出**"把 double 包成
Python 对象"的过路费**(实测 **37.4 ns/值**)。留一个没有调用方的入口是有代价的
(要跟着维护、过同一套差分闸门),换来的是这个数字永远可复核。
细节见 [`DESIGN.md`](DESIGN.md) §2.19.5。

---

## 0b. 30 秒版

```bash
# 1. 进 MSVC 环境(见 §2,这是唯一的前置条件)
# 2. 用你真正跑代码的那个解释器
D:/zsh/app/py_3.13.1/python setup.py build_ext --inplace

# 3. 确认
D:/zsh/app/py_3.13.1/python -c "import pyopenfilegdb; print(pyopenfilegdb.HAS_ACCEL)"
# -> True
```

产物落在 `pyopenfilegdb/_gdbaccel.cp313-win_amd64.pyd`,紧接着 `_accel.py` 就能
`from . import _gdbaccel` 成功。

---

## 1. 前置条件

| 需要 | 本机是什么 | 说明 |
|---|---|---|
| C 编译器 | MSVC 2022 Community | 只要 C99,不用 C++;**不要 C++ 运行时** |
| Python 头文件 | 跟解释器一起装的 | `Python.h`,在解释器的 `include/` 下 |
| `setuptools>=64` | 项目解释器自带;`.venv` 里是 80.3.1 | `build_ext` 由它提供 |

**不需要**的:numpy、pybind11、CMake、Cython、任何 GDAL 相关的东西。
整个扩展是单文件、只用 CPython 的 C API(`Python.h` + `structmember` 都不需要)。

---

## 2. 进 MSVC 环境(最容易卡的一步)

`cl.exe` 默认不在 PATH 上,必须先跑 `vcvars64.bat` 把它和 Windows SDK 一起注进来。

```
C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat
```

**(a) `cmd` 里:一次会话跑一次**

```cmd
call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat"
cd /d E:\code\pyopenfilegdb
D:\zsh\app\py_3.13.1\python.exe setup.py build_ext --inplace
```

**(b) Git Bash 里:`cmd //c` 的引号会被搅坏**

下面这种写法**会失败**("系统找不到指定的路径"),因为 Git Bash 先把引号吃掉一层,
`cmd` 收到的命令被拆散了:

```bash
# ✗ 不要这么写
cmd //c 'call "C:\...\vcvars64.bat" && python setup.py build_ext --inplace'
```

正确做法是**写成 `.bat` 再调**,而且 `cmd //c` 后面要给**绝对路径**
(`cmd //c .build.bat` 也会找不到文件):

```bash
cat > 'E:\code\pyopenfilegdb\.build.bat' <<'EOF'
call "C:\Program Files\Microsoft Visual Studio\2022\Community\VC\Auxiliary\Build\vcvars64.bat" >nul
cd /d E:\code\pyopenfilegdb
D:\zsh\app\py_3.13.1\python.exe setup.py build_ext --inplace
EOF
cmd //c 'E:\code\pyopenfilegdb\.build.bat'
rm -f 'E:\code\pyopenfilegdb\.build.bat'
```

> ⚠️ **`.bat` 文件里不要写中文注释。** `cmd.exe` 按 OEM 代码页(这台机器是 **936 /
> GBK**)读 `.bat`,而文件是 UTF-8 —— 一个中文注释就能让 `call` 那行报
> `'MSVC' 不是内部或外部命令`。`.bat` 一律 ASCII。这跟下面 §5 的 `/utf-8` 是
> **同一类问题**(UTF-8 内容被按 GBK 读),只是一个在 `.bat` 上、一个在 `.c` 上。

**(c) 已有 "x64 Native Tools Command Prompt for VS 2022" 快捷方式**
从开始菜单打开它,`cl.exe` 直接可用,不用 `call`。

---

## 3. 三种打包方式

### A. 源码目录直跑 —— `build_ext --inplace`(**推荐**)

```bash
python setup.py build_ext --inplace
```

- 产物**就地**放进 `pyopenfilegdb/`,不用安装;
- `main.py`、`tools/`、`tests/` 都是从源码目录跑的,这条最省事;
- **改完 `.c` 重跑一次**即可(它按时间戳判要不要重编)。

想从零编一遍:

```bash
rm -rf build pyopenfilegdb/_gdbaccel*.pyd
python setup.py build_ext --inplace
```

### B. 装进某个环境

```bash
python -m pip install .            # 普通安装(会拷进 site-packages)
python -m pip install -e .         # 可编辑安装(指向源码目录)
```

⚠️ 这两条会**触发构建隔离**:`pip` 会临时建一个只装了 `setuptools>=64` 的环境,
**在没网的机器上可能卡在下载**。要么确保能联网,要么加 `--no-build-isolation`
(那就要求当前环境里已经有 `setuptools>=64`)。

### C. 出 wheel / sdist

```bash
# wheel:平台相关,装的时候不用再编译
python -m pip wheel . --no-deps -w dist/

# sdist:只有源码,装的时候在目标机上编译
python -m pip install build && python -m build --sdist
```

实测这个项目编出来的 wheel 是:

```
pyopenfilegdb-0.1.0-cp311-cp311-win_amd64.whl      # 用 3.11 编的
```

里面同时有 **编译产物和源码**:

```
pyopenfilegdb/_gdbaccel.cp311-win_amd64.pyd     ← 编译好的扩展
pyopenfilegdb/_gdbaccel.c                       ← 源码(靠 package_data 带进去)
pyopenfilegdb/*.py
```

带 `.c` 是 `pyproject.toml` 里这一行干的,删了 wheel 就只剩 `.pyd`:

```toml
[tool.setuptools.package-data]
pyopenfilegdb = ["_gdbaccel.c"]
```

> ⚠️ **注意 wheel 的 tag 是 `cp311`,不是 `py3-none-any`。** 因为它含一个绑 ABI
> 的 `.pyd`,这个 wheel **只能装到 3.11**(见 §4)。想给 3.13 用,就得用 3.13 再编一个。

---

## 4. ⚠️ `.pyd` 是绑解释器 + 绑 ABI 的(本机最常踩)

`.pyd` 的文件名带 tag,这个 tag 就是它的"血型":

```
_gdbaccel.cp313-win_amd64.pyd     只能被 CPython 3.13 载入
_gdbaccel.cp311-win_amd64.pyd     只能被 CPython 3.11 载入
```

**不同 tag 的文件可以放在同一个目录里共存**,`import` 时解释器只挑自己那一个。
所以给多个解释器各编一份是最省事的做法(本机现在就有 CPython 3.11 和 3.13 两份)。

**踩过的实例:** 用项目解释器(3.13)编完,再用仓库里那个 `.venv`(3.11.9)跑
`main.py`,结果 `HAS_ACCEL = False`,退回纯 Python,**65.4 s**。用 3.11 再编一份
之后变成 **1.50 s**。

判断"该用哪个解释器编"的原则很简单:**哪个解释器跑你的代码,就用哪个编。**

```bash
# 给 .venv 的 3.11 编一份(和 3.13 那份并存,互不影响)
.venv/Scripts/python.exe setup.py build_ext --inplace
```

> 换解释器小版本(3.11.9 → 3.11.13)通常**不用**重编:文件名 tag 只到 `cp311`,
> ABI 在小版本间是稳定的。跨 3.11 → 3.12/3.13 才必须重编。

---

## 5. `/utf-8` 不是消警告,是正确性

`setup.py` 里有一段专门干这个:

```python
def build_extensions(self):
    if self.compiler.compiler_type == 'msvc':
        for ext in self.extensions:
            ext.extra_compile_args = ['/utf-8'] + list(ext.extra_compile_args or [])
    super().build_extensions()
```

为什么必须有:`_gdbaccel.c` 的注释是**中文**,MSVC 默认按系统 ANSI 代码页(936/GBK)
读源文件 —— 它会把 UTF-8 的中文字节当 GBK 成对解析,**某个尾字节可能正好是 `0x5C`**,
在 GBK 里 `0x5C` 是行继续符,于是**一行注释的结尾被吃掉、接到下一行代码上**,静默
改掉程序。所以这不是"消个 C4819 警告",是"不加就会编出错的东西"。

只在 MSVC 下加:gcc/clang 默认按 UTF-8 读,而且**不认** `/utf-8` 这个参数。

---

## 6. 怎么确认生效

```bash
python -c "import pyopenfilegdb; print(pyopenfilegdb.HAS_ACCEL)"
# True  = 编好了,走 C
# False = 回退纯 Python(没编 / 版本不匹配 / 被环境变量关了)
```

`HAS_ACCEL` 是**导入时**的事实,对外公开,可以对上层代码断言或打日志。

想确认两条路**结果一样**(不只"是否生效"),跑差分闸门:

```bash
python tools/verify_accel.py
# 8 个样例库逐位对拍 + 定向损坏用例 + 4000 轮 fuzz
```

---

## 7. 回退:三种情况,都是正常状态

| 情况 | 结果 |
|---|---|
| 没编(只想读文档) | 自动回退,纯 Python |
| 换了解释器 / ABI 不匹配 | 自动回退,纯 Python |
| 设了 `PYOPENFILEGDB_NO_ACCEL=1` | **强制**回退(差分测试、验回退路径时用) |

`PYOPENFILEGDB_NO_ACCEL` 的值按"读作真"处理 —— `0` / `false` / `no` / 空串都算
**不**关闭,其余(含 `1`)算关闭。分派处**每次调用都重读**这个开关,所以进程内用
`pyopenfilegdb._accel.use(False)` 也能即时切换。

判断逻辑在 `pyopenfilegdb/_accel.py`,只有 60 多行,`import` 失败就置 `None`:

```python
try:
    from . import _gdbaccel as _impl
except ImportError:          # 没编译 / ABI 不匹配 / 换了解释器
    _impl = None
```

---

## 8. 排错表

| 现象 | 原因 | 修法 |
|---|---|---|
| `'cl.exe' 不是内部或外部命令` | 没进 MSVC 环境 | 跑 `vcvars64.bat`,见 §2 |
| `vswhere.exe 不是内部或外部命令` | 同上,但报在 `vcvars64.bat` 内部 | **无害**,后续照常编译;是 VS 安装器路径的小毛病 |
| `'MSVC' 不是内部或外部命令`(跑 `.bat` 时) | `.bat` 里有中文注释,被按 GBK 读串了行 | `.bat` 改 ASCII,见 §2(b) |
| `系统找不到指定的路径`(Git Bash 里) | `cmd //c '...'` 的引号被搅坏 | 写成 `.bat` + 绝对路径,见 §2(b) |
| `C4819` 警告或注释"吃掉"下一行代码 | 缺 `/utf-8` | 确认 `setup.py` 的 `_BuildExt` 还在(§5) |
| `HAS_ACCEL = False`,但明明编过 | 编译用的解释器 ≠ 运行的解释器 | 用运行的那个解释器重编,见 §4 |
| `ModuleNotFoundError: No module named 'encodings'` | 用了 PATH 上那个坏掉的 `python` | 用**绝对路径**指向项目解释器 |
| `ImportError: DLL load failed` | 架构不匹配(32 位解释器编出 x64 的 pyd)或换了大版本 | 确认是 x64 解释器,重编 |
| 改了 `.c` 但行为没变 | `build_ext` 以为是最新的 | `rm -rf build pyopenfilegdb/_gdbaccel*.pyd` 再编 |

---

## 9. 已知限制

1. **从 sdist 装在没编译器的机器上会硬失败。** 运行期扩展是"可选"的,但
   **构建期不是** —— `pip install` 从 sdist 装时,编译失败会让整个安装报错退出,
   不会"跳过扩展、装个纯 Python 版"。要真做到"没编译器也能装",得再加一个构建期
   开关(比如 `PYOPENFILEGDB_NO_ACCEL=1` 时干脆不注册 `ext_modules`)。
   **目前没有这个开关** —— 这是本扩展封装上唯一不圆的地方。
2. **wheel 是平台 + 版本相关的**(§3)。要覆盖多版本 Python,得每个版本各出一份
   wheel(或用 `abi3` 限定 API,那是另一项改造)。
3. **只对 Windows/MSVC 做过实测。** `setup.py` 里 gcc/clang 那条分支(不加 `/utf-8`)
   是按代码写的,没在 Linux/macOS 上验证过。
