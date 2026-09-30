"""把 ArcGIS 新建的空 .gdb 里的系统表内容原样导出成 Python 数据模块。

``新建文件地理数据库.gdb`` 是 ArcGIS 的"新建文件地理数据库"产物 —— 只有
系统表、没有任何用户数据,正是我们的建库模板。系统表里的行都是 ArcGIS 写死
的格式样板(GDB_ItemTypes / GDB_DBTune 的取值表、GDB_SpatialRefs 的初始
坐标系),不含用户数据。

用法:  python tools/gen_template.py > pyopenfilegdb/_gdb_template.py
"""
import glob, os, sys

_HERE = os.path.dirname(os.path.abspath(__file__))
# tools/ 的上一级 = 仓库根(pyopenfilegdb 包在这儿)。
sys.path.insert(0, os.path.dirname(_HERE))

from pyopenfilegdb import _constants as C
from pyopenfilegdb._gdbtable import GdbTable

SRC = 'D:/work/新建文件地理数据库.gdb'

# 这个 gdb 里其实已经有两个用户要素类(项目红线 / 林地图斑),建库模板必须
# 把它们剔除,只留"系统表 + 库级条目"这一部分:
#   a00000001 GDB_SystemCatalog —— 只保留 ID 1..8 的系统表条目
#   a00000004 GDB_Items         —— 只保留前两条(根条目 + Workspace 条目)
ROW_FILTER = {
    'a00000001': lambda d: d['ID'] <= 8,
    'a00000004': lambda d: d['PhysicalName'] in ('', 'WORKSPACE'),
}


# 建库只需要这 7 张系统表(a00000008 GDB_ReplicaLog 在系统目录里有条目,
# 但 ArcGIS 直到第一次用复制功能才会真正建文件)。
TEMPLATE_TABLES = ['a0000000%d' % i for i in range(1, 8)]


def lit(x):
    if isinstance(x, float) and x != x:
        return "float('nan')"
    return repr(x)


def main():
    # 强制 LF。Windows 上 sys.stdout 是文本模式,写 '\n' 会被翻成 '\r\n',而
    # 仓库里所有文本文件都是 LF。不锁住的话,重新生成出来的 _gdb_template.py
    # 与已发布那个逐字节对不上,"重新生成再 cmp" 这条自检就假失败了。
    # (newline='\n' 表示写时不翻译;这句是项目里唯一需要关心行尾的地方。)
    sys.stdout.reconfigure(newline='\n')
    out = sys.stdout
    out.write('"""空 FileGDB 的系统表样板数据 —— **自动生成,不要手改**。\n\n')
    out.write('来源:一个 ArcGIS 10.x 建的 .gdb 里的 7 张系统表。\n')
    out.write('这些行是 ArcGIS 写死的目录内容(GDB_ItemTypes / GDB_DBTune 的\n')
    out.write('取值表、GDB_SpatialRefs 的初始坐标系、GDB_Items 的库级条目),\n')
    out.write('不含任何用户数据 —— 原 gdb 里的两个用户要素类已由生成脚本剔除。\n')
    out.write('建库时照抄即可让 ArcGIS/GDAL 正常打开。\n\n')
    out.write('生成脚本:``tools/gen_template.py``。\n"""\n')
    out.write('from __future__ import annotations\n\n')
    out.write('from typing import Any, Dict, List, Optional, Tuple\n\n')
    out.write('# 每张系统表是一个 dict:\n')
    out.write('#   name                物理表名,如 \'a00000001\'\n')
    out.write('#   geom_type           表级几何类型(FGTGT_*)\n')
    out.write('#   has_z / has_m       表级 Z/M 标志(字段描述区 nLayerFlags 的 bit31/30)\n')
    out.write('#                       ⚠️ 与几何字段自己的 has_z_origin_scale_tolerance\n')
    out.write('#                       是 **两回事**:a00000004 的几何字段带 Z/M 量化参数,\n')
    out.write('#                       但表级 hasZ/hasM 都是 false(实测)。\n')
    out.write('#   strings_are_utf8    字符串是否 UTF-8(nLayerFlags 的 bit8)\n')
    out.write('#   fields              字段列表,元素是\n')
    out.write('#                       (名字, FGFT 类型, nullable, required, editable,\n')
    out.write('#                        maxWidth, geom_params)\n')
    out.write('#                       geom_params 仅几何字段非 None:\n')
    out.write('#                       (wkt, xOrigin, yOrigin, xyScale, xyTolerance,\n')
    out.write('#                        hasM, mOrigin, mScale, mTolerance,\n')
    out.write('#                        hasZ, zOrigin, zScale, zTolerance,\n')
    out.write('#                        xmin, ymin, xmax, ymax, grids)\n')
    out.write('#   rows                与 fields 等长的取值元组(几何一律为空)\n')
    out.write('SYSTEM_TABLES: List[Dict[str, Any]] = [\n')

    for name in TEMPLATE_TABLES:
        path = os.path.join(SRC, name + '.gdbtable')
        tb = GdbTable.from_file(path)
        out.write('  {\n')
        out.write("    'name': %r,\n" % name)
        out.write("    'geom_type': %d,\n" % tb.table_geom_type)
        out.write("    'has_z': %r,\n" % tb.has_z)
        out.write("    'has_m': %r,\n" % tb.has_m)
        out.write("    'strings_are_utf8': %r,\n" % tb.strings_are_utf8)
        out.write("    'fields': [\n")
        for f in tb.fields:
            width = f.length if f.field_type == C.FGFT_STRING else 0
            gp = None
            if f.is_geometry:
                gf = f
                gp = (gf.wkt, gf.x_origin, gf.y_origin, gf.xy_scale,
                      gf.xy_tolerance,
                      gf.has_m_origin_scale_tolerance, gf.m_origin,
                      gf.m_scale, gf.m_tolerance,
                      gf.has_z_origin_scale_tolerance, gf.z_origin,
                      gf.z_scale, gf.z_tolerance,
                      gf.xmin, gf.ymin, gf.xmax, gf.ymax,
                      list(gf.spatial_index_grid_resolutions or []))
            out.write("      (%r, %d, %r, %r, %r, %d,\n       (%s)),\n"
                      % (f.name, f.field_type, f.nullable, f.required,
                         f.editable, width,
                         ', '.join(lit(v) for v in gp) if gp else 'None'))
        out.write('    ],\n    \'rows\': [\n')
        keep = ROW_FILTER.get(name)
        nrows = 0
        for row, vals in tb.iter_rows():
            tup = []
            for f in tb.fields:
                v = vals.get(f.name)
                if hasattr(v, 'shape_type'):
                    v = None
                tup.append(v)
            if keep is not None and not keep(dict(zip(
                    [f.name for f in tb.fields], tup))):
                continue
            nrows += 1
            out.write('      (%s),\n' % ', '.join(lit(v) for v in tup))
        out.write('    ],\n  },\n')
        sys.stderr.write('%s: %d rows kept\n' % (name, nrows))

    out.write(']\n')


main()

