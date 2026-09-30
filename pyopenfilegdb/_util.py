"""二进制读写小工具:varuint、UTF-16 字符串、带游标的字节读取器。

FileGDB 记录体里变长字段的长度前缀是 **unsigned LEB128**(GDAL 里叫
``ReadVarUInt`` / ``WriteVarUInt``),见:

- ``ogr/ogrsf_frmts/openfilegdb/filegdbtable.cpp``    ``ReadVarUInt``
- ``ogr/ogrsf_frmts/openfilegdb/filegdbtable_priv.h`` ``WriteVarUInt``

UTF-16 字符串(字段名、别名、WKT)在字段描述区里是
``[uint8 字符数][UTF-16LE 数据]``,见 ``ReadUTF16String``。
"""
from __future__ import annotations

import struct
from typing import Tuple

from ._datatypes import GdbFormatError

# ----------------------------------------------------------------------------
# 结构化标量读写(GDAL 里是一堆 GetUInt32/GetFloat64 宏,这里集中成函数)
# ----------------------------------------------------------------------------
_u16 = struct.Struct('<H')
_u32 = struct.Struct('<I')
_i16 = struct.Struct('<h')
_i32 = struct.Struct('<i')
_i64 = struct.Struct('<q')
_u64 = struct.Struct('<Q')
_f32 = struct.Struct('<f')
_f64 = struct.Struct('<d')


def get_uint16(buf: bytes, off: int = 0) -> int:
    return _u16.unpack_from(buf, off)[0]


def get_int16(buf: bytes, off: int = 0) -> int:
    return _i16.unpack_from(buf, off)[0]


def get_uint32(buf: bytes, off: int = 0) -> int:
    return _u32.unpack_from(buf, off)[0]


def get_int32(buf: bytes, off: int = 0) -> int:
    return _i32.unpack_from(buf, off)[0]


def get_int64(buf: bytes, off: int = 0) -> int:
    return _i64.unpack_from(buf, off)[0]


def get_uint64(buf: bytes, off: int = 0) -> int:
    return _u64.unpack_from(buf, off)[0]


def get_float32(buf: bytes, off: int = 0) -> float:
    return _f32.unpack_from(buf, off)[0]


def get_float64(buf: bytes, off: int = 0) -> float:
    return _f64.unpack_from(buf, off)[0]


# ----------------------------------------------------------------------------
# unsigned LEB128
# ----------------------------------------------------------------------------
def read_varuint(buf: bytes, pos: int, end: int = -1) -> Tuple[int, int]:
    """读一个 unsigned LEB128。

    :returns: ``(值, 新位置)``
    :raises GdbFormatError: 越界或编码超过 64 位
    """
    if end < 0:
        end = len(buf)
    if pos >= end:
        raise GdbFormatError('read_varuint: 越界')

    b = buf[pos]
    if (b & 0x80) == 0:
        return b, pos + 1

    pos += 1
    shift = 7
    val = b & 0x7F
    while True:
        if pos >= end:
            raise GdbFormatError('read_varuint: 截断的变长整数')
        b = buf[pos]
        pos += 1
        val |= (b & 0x7F) << shift
        if (b & 0x80) == 0:
            return val, pos
        shift += 7
        if shift >= 64:
            raise GdbFormatError('read_varuint: 变长整数过长')


def write_varuint(out: bytearray, val: int) -> None:
    """写一个 unsigned LEB128(与 GDAL ``WriteVarUInt`` 等价)。"""
    if val < 0:
        raise ValueError('write_varuint 不接受负数')
    while True:
        if val >= 0x80:
            out.append(0x80 | (val & 0x7F))
            val >>= 7
        else:
            out.append(val)
            return


def varuint_size(val: int) -> int:
    """写 :func:`write_varuint` 会占用的字节数(写前算长度用)。"""
    n = 1
    val >>= 7
    while val:
        n += 1
        val >>= 7
    return n


# ----------------------------------------------------------------------------
# UTF-16 字符串
# ----------------------------------------------------------------------------
def read_utf16_string(buf: bytes, pos: int, char_count: int) -> Tuple[str, int]:
    """读 ``char_count`` 个 UTF-16LE 码元构成的字符串。

    与 GDAL ``ReadUTF16String(pabyIter, nCarCount)`` 一致:它处理了 BOM 与
    代理对,这里直接交给 Python 的 ``utf-16-le`` 解码(遇到孤立代理对时
    用 surrogatepass 兜底,避免抛出)。
    """
    nbytes = char_count * 2
    raw = buf[pos:pos + nbytes]
    if len(raw) < nbytes:
        raise GdbFormatError('read_utf16_string: 数据不足')
    return decode_utf16(raw), pos + nbytes


def decode_utf16(raw: bytes) -> str:
    """解码 UTF-16 字节串。自动跳过 BOM。"""
    if not raw:
        return ''
    if raw[:2] in (b'\xff\xfe', b'\xfe\xff'):
        raw = raw[2:]
    try:
        return raw.decode('utf-16-le')
    except UnicodeDecodeError:
        # FileGDB 里偶尔出现半截代理对,容错处理
        return raw.decode('utf-16-le', errors='replace')


def encode_utf16(text: str) -> bytes:
    """编码成 UTF-16LE(不含 BOM)。"""
    return text.encode('utf-16-le')


def utf16_units(text: str) -> int:
    """字符串占多少个 UTF-16 码元(即字节数 / 2)。

    字段描述区的长度前缀是 **码元数** 而不是字符数,emoji 之类会占 2 个。
    """
    return len(text.encode('utf-16-le')) // 2


# ----------------------------------------------------------------------------
# 字节游标
# ----------------------------------------------------------------------------
class ByteReader:
    """带边界的顺序读取器,用于解析字段描述区与记录体。

    :param data: 缓冲区(通常比实际数据长,尾部零填充)
    :param end:  真实数据结束位置
    :param pos:  起始位置
    """

    __slots__ = ('data', 'pos', 'end')

    def __init__(self, data: bytes, pos: int = 0, end: int = -1) -> None:
        self.data = data
        self.pos = pos
        self.end = len(data) if end < 0 else end

    # -- 基础 ---------------------------------------------------------------
    @property
    def remaining(self) -> int:
        return self.end - self.pos

    def need(self, n: int) -> None:
        if self.remaining < n:
            raise GdbFormatError(
                f'缓冲区不足: 需要 {n} 字节, 只剩 {self.remaining}'
            )

    def u8(self) -> int:
        self.need(1)
        v = self.data[self.pos]
        self.pos += 1
        return v

    def u16(self) -> int:
        self.need(2)
        v = get_uint16(self.data, self.pos)
        self.pos += 2
        return v

    def i16(self) -> int:
        self.need(2)
        v = get_int16(self.data, self.pos)
        self.pos += 2
        return v

    def u32(self) -> int:
        self.need(4)
        v = get_uint32(self.data, self.pos)
        self.pos += 4
        return v

    def i32(self) -> int:
        self.need(4)
        v = get_int32(self.data, self.pos)
        self.pos += 4
        return v

    def u64(self) -> int:
        self.need(8)
        v = get_uint64(self.data, self.pos)
        self.pos += 8
        return v

    def i64(self) -> int:
        self.need(8)
        v = get_int64(self.data, self.pos)
        self.pos += 8
        return v

    def f32(self) -> float:
        self.need(4)
        v = get_float32(self.data, self.pos)
        self.pos += 4
        return v

    def f64(self) -> float:
        self.need(8)
        v = get_float64(self.data, self.pos)
        self.pos += 8
        return v

    def skip(self, n: int) -> None:
        self.need(n)
        self.pos += n

    def varuint(self) -> int:
        v, self.pos = read_varuint(self.data, self.pos, self.end)
        return v

    def utf16_prefixed(self) -> str:
        """``[uint8 码元数][UTF-16LE]`` 形式的字符串。"""
        n = self.u8()
        s, self.pos = read_utf16_string(self.data, self.pos, n)
        return s

    def utf16_bytes(self, nbytes: int) -> str:
        """``[nbytes 字节的 UTF-16LE]`` 形式的字符串(几何字段的 WKT 用)。"""
        self.need(nbytes)
        s = decode_utf16(self.data[self.pos:self.pos + nbytes])
        self.pos += nbytes
        return s


class ByteWriter:
    """与 :class:`ByteReader` 对应的顺序写入器。"""

    __slots__ = ('buf',)

    def __init__(self) -> None:
        self.buf = bytearray()

    def __len__(self) -> int:
        return len(self.buf)

    def u8(self, v: int) -> None:
        self.buf.append(v & 0xFF)

    def u16(self, v: int) -> None:
        self.buf += _u16.pack(v)

    def i16(self, v: int) -> None:
        self.buf += _i16.pack(v)

    def u32(self, v: int) -> None:
        self.buf += _u32.pack(v)

    def i32(self, v: int) -> None:
        self.buf += _i32.pack(v)

    def u64(self, v: int) -> None:
        self.buf += _u64.pack(v)

    def i64(self, v: int) -> None:
        self.buf += _i64.pack(v)

    def f32(self, v: float) -> None:
        self.buf += _f32.pack(v)

    def f64(self, v: float) -> None:
        self.buf += _f64.pack(v)

    def raw(self, b: bytes) -> None:
        self.buf += b

    def zeros(self, n: int) -> None:
        self.buf += b'\x00' * n

    def varuint(self, v: int) -> None:
        write_varuint(self.buf, v)

    def utf16_prefixed(self, s: str) -> None:
        """``[uint8 码元数][UTF-16LE]``。"""
        units = utf16_units(s)
        if units > 255:
            raise GdbFormatError(f'UTF-16 字符串过长 ({units} 码元): {s[:40]!r}')
        self.u8(units)
        self.buf += encode_utf16(s)

    def utf16_bytes(self, s: str) -> None:
        """裸 UTF-16LE(调用方自己写长度)。"""
        self.buf += encode_utf16(s)

    def getvalue(self) -> bytes:
        return bytes(self.buf)


# ----------------------------------------------------------------------------
# 位操作(空值位图沿用 GDAL 的 LSB-first 约定)
# ----------------------------------------------------------------------------
def make_bit_array(bit_count: int) -> bytearray:
    """分配 :func:`_constants.bit_array_size_in_bytes` 大小、全 0 的位图。"""
    return bytearray((bit_count + 7) // 8)


def test_bit(data: bytes, idx: int) -> bool:
    """读第 idx 位(LSB-first),对应 GDAL ``TEST_BIT``。"""
    return (data[idx >> 3] & (1 << (idx & 7))) != 0


def set_bit(data: bytearray, idx: int) -> None:
    """置位第 idx 位(LSB-first)。"""
    data[idx >> 3] |= 1 << (idx & 7)


def clear_bit(data: bytearray, idx: int) -> None:
    """清位第 idx 位(LSB-first)。"""
    data[idx >> 3] &= ~(1 << (idx & 7)) & 0xFF
