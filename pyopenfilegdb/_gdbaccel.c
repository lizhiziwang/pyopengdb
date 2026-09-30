/* pyopenfilegdb 的可选 C 加速模块:几何坐标数组的 delta-varint 解码。
 *
 * 这是 _esri_geometry.py 里两个最热函数的 C 版本:
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
 * 所以本模块**所有**失败路径都必须抛 IndexError。不能抛 ValueError /
 * MemoryError / OverflowError —— 那些没有任何调用方接得住,漏出去会让
 * 一条坏记录废掉整个图层迭代。
 *
 * 参见 _gdbaccel 的三道闸:
 *   1. 先按剩余字节数卡 n_points,再分配(封住 PyList_New 的分配量);
 *   2. 参数校验也抛 IndexError;
 *   3. varint 长度上限与 Python 侧取同一个值,见下面 _MAX_SHIFT。
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdint.h>

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


static PyMethodDef Methods[] = {
    {"decode_xy", decode_xy, METH_VARARGS,
     "解 n 个 XY 点(delta-varint)-> (list[(float, float)], end_pos, dx, dy)"},
    {"decode_scalar", decode_scalar, METH_VARARGS,
     "解 n 个 Z/M 值(delta-varint)-> (list[float], end_pos, acc)"},
    {"decode_xy_flat", decode_xy_flat, METH_VARARGS,
     "解 n 个 XY 点(delta-varint)-> (array('d') 交错, end_pos, dx, dy)"},
    {"decode_scalar_flat", decode_scalar_flat, METH_VARARGS,
     "解 n 个 Z/M 值(delta-varint)-> (array('d'), end_pos, acc)"},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef gdbaccel = {
    PyModuleDef_HEAD_INIT, "_gdbaccel",
    "FileGDB 几何 delta-varint 解码的可选加速实现(纯 Python 实现仍是回退)",
    -1, Methods, NULL, NULL, NULL, NULL
};

PyMODINIT_FUNC
PyInit__gdbaccel(void)
{
    return PyModule_Create(&gdbaccel);
}
