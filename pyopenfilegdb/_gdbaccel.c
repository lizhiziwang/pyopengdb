/* pyopenfilegdb 的可选 C 加速模块:几何坐标数组的 delta-varint 解码 + WKT 写出。
 *
 * 前半部分(四个 decode_*)是 _esri_geometry.py 里两个最热函数的 C 版本:
 *
 *     _read_xy_array(blob, dr, n_points, q, dx, dy)
 *         -> decode_xy(blob, pos, n_points, scale, x_origin, y_origin, dx, dy)
 *     _read_scalar_array(blob, dr, n_points, scale, origin, acc)
 *         -> decode_scalar(blob, pos, n_points, scale, origin, acc)
 *
 * 算法逐字照抄 GDAL 的 C++ 实现,不做任何"等价改写":
 *
 *     ogr/ogrsf_frmts/openfilegdb/filegdbtable.cpp
 *       - ReadVarIntAndAddNoCheck()   约 1474-1517 行
 *       - ReadXYArray()               约 3451-3479 行
 *       - ReadZArray() / ReadMArray()
 *
 * 编码规则(与 LEB128 形似但**不同**,别搞混):
 *
 *     首字节   bit0-5 是幅度低 6 位;bit6 是**符号位**;bit7 是续字节标志
 *     续字节   每字节 7 位,贡献 (b & 0x7F) << shift
 *              shift 从 **6** 起,每读一个续字节 +7
 *              (varuint 才是从 7 起 —— 这两个千万别写串)
 *     累加器   64 位有符号(GDAL 里是 GIntBig),增量直接加/减上去
 *     还原     (double)dx / scale + origin
 *              **必须保持这个写法**:改成 dx * (1/scale) 会差最后一位,
 *              与 Python 那边 int / float 的结果不再逐位相同。
 *
 * 为什么这样算出来是逐位相同的
 * ---------------------------
 * Python 侧写的是 `x / scale + origin`(int / float → float,再 + float),
 * C 侧写 `(double)dx / scale + origin`。两边都是同一串 IEEE-754 双精度
 * 运算、同样的运算顺序,所以结果逐位相同 —— 这是等式,不是近似。
 *
 * 尺度参数由调用方从 _Quantizer 取(q.xy_scale / q.z_scale / q.m_scale),
 * 已经过 _sanitize_scale(0 或 NaN 会变成 1.0),所以 C 侧不会再遇到
 * scale == 0 的除零。负数 scale 是允许的,两边同样逐位相同。
 *
 * 边界与异常口径(重要)
 * --------------------
 * 纯 Python 的那两段热循环是**去边界检查**的,截断的 blob 直接抛
 * IndexError,由 _esri_geometry._decode_at 统一转成 GdbFormatError
 * (_esri_geometry.py 约 330 行)。_gdbtable.iter_rows 只 catch
 * GdbFormatError(约 931 行),口径是"单条记录损坏不中断整表"。
 *
 * 所以上面四个 decode_* 入口的**所有**失败路径都必须抛 IndexError。不能抛
 * ValueError / MemoryError / OverflowError —— 那些没有任何调用方接得住,
 * 漏出去会让一条坏记录废掉整个图层迭代。
 *
 * ⚠️ **本文件末尾的 wkt_seq 不在这条规则里**,它走的是另一条口径,写在
 *    那一节的开头。改这个文件时先看清自己改的是哪一半。
 *
 * 参见 _gdbaccel 的三道闸:
 *   1. 先按剩余字节数卡 n_points,再分配(封住 PyList_New 的分配量);
 *   2. 参数校验也抛 IndexError;
 *   3. varint 长度上限与 Python 侧取同一个值,见下面 _MAX_SHIFT。
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

/* varint 续字节的 shift 上限。
 *
 * shift 的取值是 6, 13, 20, ... —— 第 k 个续字节用 6 + 7*(k-1)。
 * 取 57 意味着**最多接受 8 个续字节**:第 8 个用 shift 55,贡献的最高位是
 * 55 + 6 = 61,连符号位一起稳稳落在 64 位累加器里。
 *
 * 为什么不取 64(即"接着往下收")?因为第 9 个续字节的 shift 是 62,贡献
 * 会摸到 62+6 = 68 位,超出 unsigned long long —— C 里要么是未定义行为
 * (shift >= 64),要么悄悄截掉高位,而 Python 的大整数不会截。两边就会
 * 分叉。取 57 则 8 个续字节以内两边的算术都精确,第 9 个字节起两边同时
 * 判非法,接受域严格相同。
 *
 * 真实数据碰不到这条线:量化增量的量级取决于图层跨度,全县边界也才
 * 2^39 上下(scale=1e9、经度跨度 360 度 ≈ 3.6e11),8 个续字节能表到
 * 2^61,余量巨大。所以这条上限只在损坏/恶意数据上生效。
 */
#define _MAX_SHIFT 57

/* 读一个增量并累加到 acc(acc 是 lvalue)。
 *
 * 不做边界检查 —— 靠外层 `p + 20 <= end` 保证:一个点最多两个 varint,
 * 每个 varint 最多 1 + 8 = 9 字节,20 字节足以覆盖两个。
 */
#define RD_FAST(acc)                                                    \
    do {                                                                \
        unsigned int b0 = *p++;                                         \
        unsigned long long val = b0 & 0x3Fu;                            \
        if (b0 & 0x80u) {                                               \
            int shift = 6;                                              \
            unsigned int b;                                             \
            for (;;) {                                                  \
                b = *p++;                                               \
                val |= (unsigned long long)(b & 0x7Fu) << shift;        \
                if (!(b & 0x80u)) break;                                \
                shift += 7;                                             \
                if (shift >= _MAX_SHIFT) goto truncated;                \
            }                                                           \
        }                                                               \
        RD_ADD(acc, val, b0);                                           \
    } while (0)

/* 同样的事,带边界检查,给末尾几个点用。 */
#define RD_CHECKED(acc)                                                 \
    do {                                                                \
        if (p >= end) goto truncated;                                   \
        unsigned int b0 = *p++;                                         \
        unsigned long long val = b0 & 0x3Fu;                            \
        if (b0 & 0x80u) {                                               \
            int shift = 6;                                              \
            unsigned int b;                                             \
            for (;;) {                                                  \
                if (p >= end) goto truncated;                           \
                b = *p++;                                               \
                val |= (unsigned long long)(b & 0x7Fu) << shift;        \
                if (!(b & 0x80u)) break;                                \
                shift += 7;                                             \
                if (shift >= _MAX_SHIFT) goto truncated;                \
            }                                                           \
        }                                                               \
        RD_ADD(acc, val, b0);                                           \
    } while (0)

/* 把增量加到累加器上。
 *
 * 走 unsigned 再转回来,是为了避免有符号溢出的未定义行为 —— GDAL 那边
 * `GIntBig` 直接加减,真实数据永远不会溢出,这里保持同样的结果又不踩 UB。
 * (损坏数据上两边都会得到"绕回"的值,这一点与 Python 的大整数不同;
 *  见文件末尾"与纯 Python 的差异"一节。)
 */
#define RD_ADD(acc, val, b0)                                            \
    do {                                                                \
        unsigned long long u = (unsigned long long)(acc);               \
        u = (b0 & 0x40u) ? u - (val) : u + (val);                       \
        (acc) = (long long)u;                                           \
    } while (0)

#define RD(acc)                                                         \
    do {                                                                \
        if (p + 20 <= end) { RD_FAST(acc); } else { RD_CHECKED(acc); }  \
    } while (0)


/* 三个入口共用的参数检查。
 *
 * 返回 0 表示通过;返回 -1 表示已经设好异常(一律 IndexError),调用方
 * 直接返回 NULL。通过时 *p_n_points 是校验过的点数,*p_remaining 是
 * pos0 之后还剩多少字节。
 */
static int
check_args(Py_ssize_t pos0, PyObject *n_points_obj, Py_ssize_t buf_len,
           Py_ssize_t *p_n_points, Py_ssize_t *p_remaining)
{
    int overflow = 0;
    long long n_ll = PyLong_AsLongLongAndOverflow(n_points_obj, &overflow);

    if (overflow != 0) {
        PyErr_SetString(PyExc_IndexError,
                        "n_points 超出 64 位(损坏的 blob)");
        return -1;
    }
    if (n_ll == -1 && PyErr_Occurred())
        return -1;                  /* 不是整数:TypeError,调用方测不到 */
    if (n_ll < 0 || pos0 < 0) {
        PyErr_SetString(PyExc_IndexError, "pos / n_points 不能为负");
        return -1;
    }

    *p_n_points = (Py_ssize_t)n_ll;

    /* n_points == 0:与 Python 完全一致 —— 循环体一次都不跑,所以连
       pos 越界都不检查,直接返回空表、位置原样带回。这是能被真实数据
       走到的:坏掉的部件表可以给出 0 个点。 */
    if (*p_n_points == 0) {
        *p_remaining = 0;
        return 0;
    }

    if (pos0 > buf_len) {
        PyErr_SetString(PyExc_IndexError, "起点越过 blob 末尾");
        return -1;
    }
    *p_remaining = buf_len - pos0;

    /* 每个点至少要吃掉 min_bytes 个字节(min_bytes 由调用方给),所以
       n_points 一旦超过 remaining / min_bytes 就**必然**读取越界。
       这是一条只可能误放、不可能误杀的判据(它是必要条件不是充分条件),
       用来在 PyList_New 之前封住分配量:损坏的 blob 可以把 n_points 报成
       2^60,直接 new 会去要几十 GB,而纯 Python 早在第一次 blob[pos]
       就抛 IndexError 了。 */
    return 0;
}


/* decode_xy(blob, pos, n_points, scale, x_origin, y_origin, dx, dy)
 *   -> (list[(float, float)], end_pos, dx, dy)
 *
 * 与 _esri_geometry._read_xy_array 对外等价。dx/dy 是**跨 part 连续**的
 * 累加器,由调用方传进来、再带回去,不能在这里重置。
 *
 * ⚠️ POINT 那个 (x - 1) 偏移**不属于这个边界** —— 走的是 _decode_point
 * (用 read_varuint),不经过本函数。C 侧不需要知道它。
 */
static PyObject *
decode_xy(PyObject *self, PyObject *args)
{
    Py_buffer buf;
    PyObject *n_points_obj;
    Py_ssize_t pos0, n_points, remaining;
    double scale, x_origin, y_origin;
    long long dx, dy;
    PyObject *list = NULL;

    if (!PyArg_ParseTuple(args, "y*nOdddLL", &buf, &pos0, &n_points_obj,
                          &scale, &x_origin, &y_origin, &dx, &dy))
        return NULL;

    if (check_args(pos0, n_points_obj, buf.len, &n_points, &remaining) < 0) {
        PyBuffer_Release(&buf);
        return NULL;
    }

    if (n_points == 0) {
        PyBuffer_Release(&buf);
        list = PyList_New(0);
        if (list == NULL) return NULL;
        return Py_BuildValue("NnLL", list, pos0, dx, dy);
    }

    /* 每个点两个 varint,X、Y 各一,每个至少 1 字节。 */
    if (n_points > remaining / 2) {
        PyBuffer_Release(&buf);
        PyErr_SetString(PyExc_IndexError, "几何 blob 被截断(点数超出剩余字节)");
        return NULL;
    }

    list = PyList_New(n_points);
    if (list == NULL) {
        PyBuffer_Release(&buf);
        return NULL;
    }

    const unsigned char *p = (const unsigned char *)buf.buf + pos0;
    const unsigned char *end = (const unsigned char *)buf.buf + buf.len;

    for (Py_ssize_t i = 0; i < n_points; i++) {
        RD(dx);
        RD(dy);

        PyObject *fx = PyFloat_FromDouble((double)dx / scale + x_origin);
        PyObject *fy = PyFloat_FromDouble((double)dy / scale + y_origin);
        if (fx == NULL || fy == NULL) {
            Py_XDECREF(fx);
            Py_XDECREF(fy);
            Py_DECREF(list);
            PyBuffer_Release(&buf);
            return NULL;
        }
        PyObject *t = PyTuple_New(2);
        if (t == NULL) {
            Py_DECREF(fx);
            Py_DECREF(fy);
            Py_DECREF(list);
            PyBuffer_Release(&buf);
            return NULL;
        }
        PyTuple_SET_ITEM(t, 0, fx);         /* 偷引用 */
        PyTuple_SET_ITEM(t, 1, fy);
        PyList_SET_ITEM(list, i, t);        /* 偷引用 */
    }

    Py_ssize_t endpos = (Py_ssize_t)(p - (const unsigned char *)buf.buf);
    PyBuffer_Release(&buf);
    return Py_BuildValue("NnLL", list, endpos, dx, dy);

truncated:
    Py_XDECREF(list);
    PyBuffer_Release(&buf);
    PyErr_SetString(PyExc_IndexError, "几何 blob 被截断");
    return NULL;
}


/* decode_scalar(blob, pos, n_points, scale, origin, acc)
 *   -> (list[float], end_pos, acc)
 *
 * 与 _esri_geometry._read_scalar_array 对外等价,Z 和 M 共用这一个入口
 * (两者只差 scale/origin,累加器各自独立)。
 *
 * ⚠️ "这一段到底有没有 M" 的判断**不在**这里:_decode_parts 里那个
 * _NO_M_MARKER(值 66)的特判留在 Python,在调用之前就判完。本函数只管
 * "读 n_points 个连续性值"。
 */
static PyObject *
decode_scalar(PyObject *self, PyObject *args)
{
    Py_buffer buf;
    PyObject *n_points_obj;
    Py_ssize_t pos0, n_points, remaining;
    double scale, origin;
    long long acc;
    PyObject *list = NULL;

    if (!PyArg_ParseTuple(args, "y*nOddL", &buf, &pos0, &n_points_obj,
                          &scale, &origin, &acc))
        return NULL;

    if (check_args(pos0, n_points_obj, buf.len, &n_points, &remaining) < 0) {
        PyBuffer_Release(&buf);
        return NULL;
    }

    if (n_points == 0) {
        PyBuffer_Release(&buf);
        list = PyList_New(0);
        if (list == NULL) return NULL;
        return Py_BuildValue("NnL", list, pos0, acc);
    }

    /* 每个值一个 varint,至少 1 字节。 */
    if (n_points > remaining) {
        PyBuffer_Release(&buf);
        PyErr_SetString(PyExc_IndexError, "几何 blob 被截断(标量数超出剩余字节)");
        return NULL;
    }

    list = PyList_New(n_points);
    if (list == NULL) {
        PyBuffer_Release(&buf);
        return NULL;
    }

    const unsigned char *p = (const unsigned char *)buf.buf + pos0;
    const unsigned char *end = (const unsigned char *)buf.buf + buf.len;

    for (Py_ssize_t i = 0; i < n_points; i++) {
        RD(acc);

        PyObject *v = PyFloat_FromDouble((double)acc / scale + origin);
        if (v == NULL) {
            Py_DECREF(list);
            PyBuffer_Release(&buf);
            return NULL;
        }
        PyList_SET_ITEM(list, i, v);        /* 偷引用 */
    }

    Py_ssize_t endpos = (Py_ssize_t)(p - (const unsigned char *)buf.buf);
    PyBuffer_Release(&buf);
    return Py_BuildValue("NnL", list, endpos, acc);

truncated:
    Py_XDECREF(list);
    PyBuffer_Release(&buf);
    PyErr_SetString(PyExc_IndexError, "几何 blob 被截断");
    return NULL;
}


/* ---------------------------------------------------------------------
 * flat 版:同一段循环,结果直接落进 array('d'),不建任何 Python 对象
 * ---------------------------------------------------------------------
 *
 * 为什么要多这两个入口
 * --------------------
 * 实测 _read_xy_array 那 2.0 s 里,varint 循环本身只占 0.33 s,剩下的
 * ~1.7 s 全是 PyFloat_FromDouble + PyTuple_New —— 4,496 万个 float 加
 * 2,121 万个元组,纯粹是"把 double 包装成 Python 对象"的过路费。GDAL 的
 * OGRLineString 是一条 double*,一分钱不付,所以这段差距靠改算法、改 IO 都
 * 抹不平,**只能换坐标容器**(见 DESIGN.md §2.19.5)。
 *
 * 返回 array('d') 而不是 memoryview:这样调用方拿到的是能切片、能
 * .tolist()、能 numpy.frombuffer 零拷贝的常规序列,而不是一个需要再解释
 * 的视图。代价是 array 构造时多一次 memcpy —— 实测 1.3 ns/值(整层
 * ~0.06 s),值这个钱。
 *
 * 与 decode_xy / decode_scalar **逐位相同**:同样的 varint 循环、同样的
 * (double)dx / scale + origin。差别只有结果的容器形状。
 */
static PyObject *g_array_type = NULL;

/* 惰性取 array.array 这个类型对象。
 *
 * 拿到的引用故意**不释放**:它就活到这个进程结束,下次直接用。并发首次
 * 调用最多各 import 一次,Python 的 import 锁保证不出错(结果相同)。
 */
static PyObject *
get_array_type(void)
{
    PyObject *mod;
    if (g_array_type != NULL)
        return g_array_type;
    mod = PyImport_ImportModule("array");
    if (mod == NULL)
        return NULL;
    g_array_type = PyObject_GetAttrString(mod, "array");
    Py_DECREF(mod);
    return g_array_type;                /* 可能为 NULL(异常已设) */
}

/* 新建长度 n 的 array('d')(全 0),并把可写 buffer 锁进 *ob。
 *
 * 用 ``array('d', [0.0]) * n``:array 的重复走 C 层 memcpy,不是 n 次
 * Python 级 append。成功时返回的 arr 由调用方负责 DECREF,且必须先
 * PyBuffer_Release(ob) 再 DECREF。
 */
static PyObject *
new_double_array(Py_ssize_t n, Py_buffer *ob)
{
    PyObject *t, *one, *cnt, *arr;

    t = get_array_type();
    if (t == NULL)
        return NULL;
    one = PyObject_CallFunction(t, "s[d]", "d", 0.0);   /* array('d', [0.0]) */
    if (one == NULL)
        return NULL;
    cnt = PyLong_FromSsize_t(n);
    if (cnt == NULL) {
        Py_DECREF(one);
        return NULL;
    }
    arr = PyNumber_Multiply(one, cnt);
    Py_DECREF(one);
    Py_DECREF(cnt);
    if (arr == NULL)
        return NULL;
    if (PyObject_GetBuffer(arr, ob, PyBUF_WRITABLE) < 0) {
        Py_DECREF(arr);
        return NULL;
    }
    return arr;
}


/* decode_xy_flat(blob, pos, n_points, scale, x_origin, y_origin, dx, dy)
 *   -> (array('d'), end_pos, dx, dy)
 *
 * 交错存放:下标 2*i 是 x,2*i+1 是 y。边界与异常口径与 decode_xy 完全
 * 一致(所有失败路径抛 IndexError,见文件头)。
 */
static PyObject *
decode_xy_flat(PyObject *self, PyObject *args)
{
    Py_buffer buf;
    PyObject *n_points_obj;
    Py_ssize_t pos0, n_points, remaining;
    double scale, x_origin, y_origin;
    long long dx, dy;
    PyObject *arr = NULL;
    Py_buffer ob;

    if (!PyArg_ParseTuple(args, "y*nOdddLL", &buf, &pos0, &n_points_obj,
                          &scale, &x_origin, &y_origin, &dx, &dy))
        return NULL;

    if (check_args(pos0, n_points_obj, buf.len, &n_points, &remaining) < 0) {
        PyBuffer_Release(&buf);
        return NULL;
    }

    if (n_points == 0) {                /* 与 decode_xy 同:一个字节都不查 */
        PyBuffer_Release(&buf);
        arr = new_double_array(0, &ob);
        if (arr == NULL) return NULL;
        PyBuffer_Release(&ob);
        return Py_BuildValue("NnLL", arr, pos0, dx, dy);
    }

    /* 先卡点数再分配(见 check_args 的注释):每个点至少 2 字节。 */
    if (n_points > remaining / 2) {
        PyBuffer_Release(&buf);
        PyErr_SetString(PyExc_IndexError, "几何 blob 被截断(点数超出剩余字节)");
        return NULL;
    }

    arr = new_double_array(n_points * 2, &ob);
    if (arr == NULL) {
        PyBuffer_Release(&buf);
        return NULL;
    }

    const unsigned char *p = (const unsigned char *)buf.buf + pos0;
    const unsigned char *end = (const unsigned char *)buf.buf + buf.len;
    double *out = (double *)ob.buf;

    for (Py_ssize_t i = 0; i < n_points; i++) {
        RD(dx);
        RD(dy);
        out[2 * i]     = (double)dx / scale + x_origin;
        out[2 * i + 1] = (double)dy / scale + y_origin;
    }

    Py_ssize_t endpos = (Py_ssize_t)(p - (const unsigned char *)buf.buf);
    PyBuffer_Release(&ob);
    PyBuffer_Release(&buf);
    return Py_BuildValue("NnLL", arr, endpos, dx, dy);

truncated:
    PyBuffer_Release(&ob);
    Py_DECREF(arr);
    PyBuffer_Release(&buf);
    PyErr_SetString(PyExc_IndexError, "几何 blob 被截断");
    return NULL;
}


/* decode_scalar_flat(blob, pos, n_points, scale, origin, acc)
 *   -> (array('d'), end_pos, acc)
 *
 * Z / M 共用;每点一个值,顺序存放(不交错)。
 */
static PyObject *
decode_scalar_flat(PyObject *self, PyObject *args)
{
    Py_buffer buf;
    PyObject *n_points_obj;
    Py_ssize_t pos0, n_points, remaining;
    double scale, origin;
    long long acc;
    PyObject *arr = NULL;
    Py_buffer ob;

    if (!PyArg_ParseTuple(args, "y*nOddL", &buf, &pos0, &n_points_obj,
                          &scale, &origin, &acc))
        return NULL;

    if (check_args(pos0, n_points_obj, buf.len, &n_points, &remaining) < 0) {
        PyBuffer_Release(&buf);
        return NULL;
    }

    if (n_points == 0) {
        PyBuffer_Release(&buf);
        arr = new_double_array(0, &ob);
        if (arr == NULL) return NULL;
        PyBuffer_Release(&ob);
        return Py_BuildValue("NnL", arr, pos0, acc);
    }

    /* 每个值一个 varint,至少 1 字节。 */
    if (n_points > remaining) {
        PyBuffer_Release(&buf);
        PyErr_SetString(PyExc_IndexError, "几何 blob 被截断(标量数超出剩余字节)");
        return NULL;
    }

    arr = new_double_array(n_points, &ob);
    if (arr == NULL) {
        PyBuffer_Release(&buf);
        return NULL;
    }

    const unsigned char *p = (const unsigned char *)buf.buf + pos0;
    const unsigned char *end = (const unsigned char *)buf.buf + buf.len;
    double *out = (double *)ob.buf;

    for (Py_ssize_t i = 0; i < n_points; i++) {
        RD(acc);
        out[i] = (double)acc / scale + origin;
    }

    Py_ssize_t endpos = (Py_ssize_t)(p - (const unsigned char *)buf.buf);
    PyBuffer_Release(&ob);
    PyBuffer_Release(&buf);
    return Py_BuildValue("NnL", arr, endpos, acc);

truncated:
    PyBuffer_Release(&ob);
    Py_DECREF(arr);
    PyBuffer_Release(&buf);
    PyErr_SetString(PyExc_IndexError, "几何 blob 被截断");
    return NULL;
}


/* ==========================================================================
 * WKT 写出:_esri_geometry.to_wkt 里那个逐点格式化的 seq_of
 * ==========================================================================
 *
 * 为什么在这里做:WKT 出口是 pyopenfilegdb 里**唯一**剩下的、按顶点计的
 * 纯 Python 热循环。实测(村行政区划 2000 条 = 4494 万顶点 / 75 MB 文本)
 * 灌 PostgreSQL 时 2000 条几何转 WKT 要 3.7 ~ 4.0 s,占整条插入循环
 * 5.2 s 的七成以上,而同一份数据"读+解码"只要 0.12 s(C 解码,见
 * DESIGN.md §2.19.5)。瓶颈就是这里的逐坐标 repr() —— 约 2.5 µs/坐标。
 *
 * 异常口径(**与文件头那四个 decode_* 不同**)
 * ------------------------------------------
 * 本函数**不在** iter_rows 的调用链上,不会被 _decode_at 翻译成
 * GdbFormatError,所以"一律抛 IndexError"那条不适用。这里要的是**与
 * 纯 Python 的 seq_of + _fmt 等价**,包括抛什么错:
 *
 *   * 参数/长度不对 -> IndexError,与 Python 那边 z[i] 越界同型;
 *   * 坐标是 ±inf -> OverflowError("cannot convert float infinity to
 *     integer"),因为 Python 的 _fmt(inf) 会走 int(v) 抛这个;
 *   * 分配失败 -> MemoryError(Python 那边同样)。
 *
 * 唯一的"另一条路"是**返回 None**:表示"给进来的缓冲我处理不了",调用方
 * 收到 None 就退回纯 Python 实现。它**不设异常**,所以不是错误信号,是
 * "不归我管" —— 这样纯 Python 侧的报错(比如传了个 list 进来)仍然由纯
 * Python 抛出,两条路的失败行为也一致。
 *
 * 逐字符复刻 _fmt
 * --------------
 * 浮点必须用 PyOS_double_to_string(v, 'r', 0, Py_DTSF_ADD_DOT_0, NULL) ——
 * 这就是 CPython 里 repr(float) 的实现(floatobject.c 的 float_repr),
 * 也就是"最短且能往返"的那串数字。⚠️ 不许换成 %.17g 或 strtod 能读的
 * 近似写法:WKT 是本库的**无损**出口(PostGIS 那头用 strtod 读回来),
 * 换一个格式化就等于把坐标改掉最后一位。
 *
 * 整数那一支同理:Python 的 `v == int(v) and abs(v) < 1e15` 打成
 * `str(int(v))`,C 侧对应"向零取整回来还等于自己,且 |v| < 1e15" -> `%lld`。
 * 这两个判据必须逐条对上 —— 差一点点就会出现"某个值某些时候变成
 * 1e+15 形式"这种只在个别坐标上冒头的分叉。写法与等价性论证见 wkt_fmt。
 * ⚠️ -0.0 要单独接住:它的整数值是 0,不加特判会打出 "-0",而 Python 的
 * str(int(-0.0)) 是 "0"。所以先判 `v == 0.0`。
 */
typedef struct {
    char *p;
    Py_ssize_t len;
    Py_ssize_t cap;
} _WktBuf;

static int
wkt_reserve(_WktBuf *b, Py_ssize_t extra)
{
    if (b->len + extra <= b->cap)
        return 0;
    Py_ssize_t cap = b->cap ? b->cap : 256;
    while (cap < b->len + extra) {
        if (cap > PY_SSIZE_T_MAX / 2) {     /* 到此为止,不再翻倍 */
            cap = b->len + extra;
            break;
        }
        cap *= 2;
    }
    char *q = (char *)PyMem_Realloc(b->p, (size_t)cap);
    if (q == NULL) {
        PyErr_NoMemory();
        return -1;
    }
    b->p = q;
    b->cap = cap;
    return 0;
}

static int
wkt_put(_WktBuf *b, const char *s, Py_ssize_t n)
{
    if (wkt_reserve(b, n) < 0)
        return -1;
    memcpy(b->p + b->len, s, (size_t)n);
    b->len += n;
    return 0;
}

/* 一个坐标分量,与 _esri_geometry._fmt 逐字符相同(含抛错口径)。 */
static int
wkt_fmt(_WktBuf *b, double v)
{
    char tmp[32];

    if (v != v)                                     /* NaN */
        return wkt_put(b, "NaN", 3);

    if (v == 0.0)                                   /* 顺手接住 -0.0,见文件头 */
        return wkt_put(b, "0", 1);

    if (fabs(v) < 1e15) {
        /* "v 是整数"这一支。
         *
         * ⚠️ 写成 `v == trunc(v)`(与 Python 的字面写法一一对应)会让每个
         *    坐标都白付一次 MSVC 的 `trunc()` —— 它是 CRT 函数,不是一条
         *    指令。实测:换成下面这句之后整个 wkt_seq 从 0.61 µs/顶点掉到
         *    0.11 µs/顶点,**五倍多**。这才是这条路上最大的一笔,比浮点
         *    格式化本身大得多。
         *
         * `(long long)v` 是一条 `cvttsd2si`,语义就是"向零取整"。等价性:
         *   * |v| < 1e15 < 2^63,转换不会溢出(C 里溢出是 UB,所以外层的
         *     `fabs(v) < 1e15` 不只是照抄 Python 的判据,也是这里的前提);
         *   * |v| < 1e15 < 2^53,`(double)iv` 精确,所以 `(double)iv == v`
         *     与 Python 的 `v == int(v)`(int 精确、比较也精确)等价;
         *   * iv == 0 一条把 ±0.0 都接住了(str(int(-0.0)) 就是 "0"),
         *     所以上面那句 `v == 0.0` 其实已经是多余的 —— 留着是为了让
         *     "-0.0 要特判"这件事显式可见,也省掉一次转换。
         *   * `%.0f` 换成 `%lld`:对 |iv| < 1e15 打出来的十进制串与
         *     str(int(v)) 完全相同,还顺带不用操心"%.0f 会不会进位"。
         */
        long long iv = (long long)v;
        if ((double)iv == v) {
            int n = snprintf(tmp, sizeof(tmp), "%lld", iv);
            if (n < 0 || (size_t)n >= sizeof(tmp)) {
                PyErr_SetString(PyExc_RuntimeError, "WKT 整数格式化异常");
                return -1;
            }
            return wkt_put(b, tmp, (Py_ssize_t)n);
        }
    }
    else if (isinf(v)) {
        /* Python 的 _fmt(inf):先算 int(inf),抛的正是这一句。
           (注意 Python 里 `int(v)` 在 `abs(v) < 1e15` **之前**求值,所以
            inf 是在整数那一支抛的,不是走到 repr —— 异常类型两边一致。) */
        PyErr_SetString(PyExc_OverflowError,
                        "cannot convert float infinity to integer");
        return -1;
    }

    char *s = PyOS_double_to_string(v, 'r', 0, Py_DTSF_ADD_DOT_0, NULL);
    if (s == NULL)                                  /* 内存不足,异常已设 */
        return -1;
    int rc = wkt_put(b, s, (Py_ssize_t)strlen(s));
    PyMem_Free(s);
    return rc;
}

/* 一个**不带括号**的点 "x y[ z][ m]"(即 _pt_wkt_bare)。 */
static int
wkt_vertex(_WktBuf *b, const double *xy, const double *z, const double *m,
           Py_ssize_t k)
{
    if (wkt_fmt(b, xy[2 * k]) < 0 || wkt_put(b, " ", 1) < 0 ||
        wkt_fmt(b, xy[2 * k + 1]) < 0)
        return -1;
    /* z / m 为 NULL 就是"这一维不存在",与 Python 的 `if z is not None` 同。 */
    if (z != NULL && (wkt_put(b, " ", 1) < 0 || wkt_fmt(b, z[k]) < 0))
        return -1;
    if (m != NULL && (wkt_put(b, " ", 1) < 0 || wkt_fmt(b, m[k]) < 0))
        return -1;
    return 0;
}

/* 取一个一维 double 缓冲。
 *   返回  1 -> 拿到了,*out 持有缓冲,用完要 PyBuffer_Release;
 *   返回  0 -> 传的是 None("没有这一维"),*out 不持有任何东西;
 *   返回 -1 -> 处理不了(**不留异常**),调用方退回纯 Python。
 *
 * 认的是 array('d') / numpy float64 / 元素为 double 的 memoryview
 * (format == "d")。bytes、list、tuple、array('b') 一律走 -1 —— 这么严是
 * 因为一旦按错的类型去解指针,读出来的是垃圾,而垃圾坐标**照样能拼成
 * 合法 WKT**,那就成了静默改数据。
 */
static int
get_double_buffer(PyObject *obj, Py_buffer *out)
{
    if (obj == Py_None)
        return 0;
    if (PyObject_GetBuffer(obj, out, PyBUF_FORMAT | PyBUF_C_CONTIGUOUS) < 0) {
        PyErr_Clear();
        return -1;
    }
    if (out->format == NULL || strcmp(out->format, "d") != 0 ||
        out->itemsize != (Py_ssize_t)sizeof(double) ||
        out->len % (Py_ssize_t)sizeof(double) != 0) {
        PyBuffer_Release(out);
        return -1;
    }
    return 1;
}

/* wkt_seq(xy, z=None, m=None, close=False, point_parens=False) -> str | None
 *
 * 与 _esri_geometry.to_wkt 里的 seq_of 对外等价:把一个点序列渲染成 WKT
 * 里那个**带外层括号**的 "(...)"。
 *
 *   point_parens=False  (线、环)  "(0 0, 1 1)"
 *   point_parens=True   (多点)    "(0 0), (1 1)" 的**内层** —— 外层括号由
 *                                 调用方那句 "MULTIPOINT (" 拼好
 *   close=True          (环)      末尾再补一个首点(内存里的环不存闭合点)
 *
 * 三个调用形态刚好对应 MULTIPOINT / LINESTRING(含 MULTILINESTRING 的段)/
 * 环,于是整个 WKT 出口只剩一个小函数要维护。
 *
 * ⚠️ "点本身带不带括号"是 **WKT 类型决定的**,不是风格:POINT /
 *    LINESTRING 要裸点,MULTIPOINT 要带括号的点。写串了两边都是**非法
 *    WKT**,本库自己的 from_wkt 也读不回来(实测)。
 *
 * 点数按 `len(xy) // 2` 取 —— 与 Python 的 `len(a) // 2` 一致:最后一个
 * 落单的分量被**忽略**,不报错。z / m 比点数**短**才抛 IndexError(对应
 * Python 那边 z[i] 越界);比点数**长**同样忽略尾巴。
 */
static PyObject *
wkt_seq(PyObject *self, PyObject *args)
{
    PyObject *xy_obj, *z_obj = Py_None, *m_obj = Py_None;
    int close = 0, point_parens = 0;
    Py_buffer bx, bz, bm;
    int gx, gz, gm;
    _WktBuf b = {NULL, 0, 0};
    PyObject *result = NULL;

    if (!PyArg_ParseTuple(args, "O|OOpp", &xy_obj, &z_obj, &m_obj,
                          &close, &point_parens))
        return NULL;

    gx = get_double_buffer(xy_obj, &bx);
    if (gx != 1)                        /* xy 都不是 double 数组:不归我管 */
        Py_RETURN_NONE;
    gz = get_double_buffer(z_obj, &bz);
    gm = (gz < 0) ? -1 : get_double_buffer(m_obj, &bm);
    if (gz < 0 || gm < 0) {
        PyBuffer_Release(&bx);
        if (gz == 1)
            PyBuffer_Release(&bz);
        Py_RETURN_NONE;
    }

    const double *xy = (const double *)bx.buf;
    const double *z = (gz == 1) ? (const double *)bz.buf : NULL;
    const double *m = (gm == 1) ? (const double *)bm.buf : NULL;
    Py_ssize_t n = (bx.len / (Py_ssize_t)sizeof(double)) / 2;

    if ((z != NULL && bz.len / (Py_ssize_t)sizeof(double) < n) ||
        (m != NULL && bm.len / (Py_ssize_t)sizeof(double) < n)) {
        PyErr_SetString(PyExc_IndexError,
                        "Z/M 分量数少于点数(与纯 Python 的 z[i] 越界同义)");
        goto done;
    }

    if (wkt_put(&b, "(", 1) < 0)
        goto done;
    for (Py_ssize_t k = 0; k < n; k++) {
        if (k > 0 && wkt_put(&b, ", ", 2) < 0)
            goto done;
        if (point_parens && wkt_put(&b, "(", 1) < 0)
            goto done;
        if (wkt_vertex(&b, xy, z, m, k) < 0)
            goto done;
        if (point_parens && wkt_put(&b, ")", 1) < 0)
            goto done;
    }
    /* Python 的条件是 `close and len(a) >= 2`,即"至少有一个点";
       n >= 1 时上面已经写过点,所以这里一定需要一个逗号。 */
    if (close && n >= 1) {
        if (wkt_put(&b, ", ", 2) < 0 || wkt_vertex(&b, xy, z, m, 0) < 0)
            goto done;
    }
    if (wkt_put(&b, ")", 1) < 0)
        goto done;

    result = PyUnicode_FromStringAndSize(b.p, b.len);

done:
    PyMem_Free(b.p);
    PyBuffer_Release(&bx);
    if (gz == 1)
        PyBuffer_Release(&bz);
    if (gm == 1)
        PyBuffer_Release(&bm);
    return result;                      /* NULL 时异常已设 */
}


static PyMethodDef Methods[] = {
    {"decode_xy", decode_xy, METH_VARARGS,
     "解 n 个 XY 点(delta-varint)-> (list[(float, float)], end_pos, dx, dy)"},
    {"decode_scalar", decode_scalar, METH_VARARGS,
     "解 n 个 Z/M 值(delta-varint)-> (list[float], end_pos, acc)"},
    {"decode_xy_flat", decode_xy_flat, METH_VARARGS,
     "解 n 个 XY 点(delta-varint)-> (array('d') 交错, end_pos, dx, dy)"},
    {"decode_scalar_flat", decode_scalar_flat, METH_VARARGS,
     "解 n 个 Z/M 值(delta-varint)-> (array('d'), end_pos, acc)"},
    {"wkt_seq", wkt_seq, METH_VARARGS,
     "点序列 -> WKT 的 \"(...)\";缓冲不是 double 数组时返回 None(调用方回退)"},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef gdbaccel = {
    PyModuleDef_HEAD_INIT, "_gdbaccel",
    "FileGDB 几何 delta-varint 解码 + WKT 写出的可选加速实现"
    "(纯 Python 实现仍是回退)",
    -1, Methods, NULL, NULL, NULL, NULL
};

PyMODINIT_FUNC
PyInit__gdbaccel(void)
{
    return PyModule_Create(&gdbaccel);
}
