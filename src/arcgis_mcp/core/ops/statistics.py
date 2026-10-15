"""统计操作：汇总统计 / 频数。

（分区统计在 ops/raster.py，因为它要栅格。）
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .common import WorkerError
from .tabular import table_output

OPERATIONS: Dict[str, Dict[str, Any]] = {}


def register(name: str, summary: str, run, min_inputs: int = 1, allowed=()) -> None:
    OPERATIONS[name] = {"summary": summary, "run": run,
                        "min_inputs": min_inputs, "allowed": set(allowed)}


#: 汇总统计允许的统计方式
STATISTICS_TYPES = ("SUM", "MEAN", "MIN", "MAX", "RANGE", "STD", "COUNT", "FIRST",
                    "LAST", "MEDIAN", "VARIANCE", "UNIQUE", "CONCATENATE")


def _parse_statistics_fields(value: Any) -> List[List[str]]:
    """把 ["POP SUM", "AREA MEAN"] 解析成 arcpy 要的 [["POP", "SUM"], ["AREA", "MEAN"]]。

    实测：直接把 "POP SUM" 当字符串传给 arcpy，它会把整串当成统计方式，报
    「该值不是 SUM | MEAN | ... 的成员」。
    """
    if not value:
        raise WorkerError(
            '汇总统计需要 statistics_fields，格式是「字段 统计方式」，例如 '
            '["POP SUM", "AREA_KM2 MEAN"]\n统计方式可选：' + " / ".join(STATISTICS_TYPES)
        )
    if isinstance(value, str):
        items: List[Any] = [s.strip() for s in value.replace(";", ",").split(",") if s.strip()]
    else:
        items = list(value)

    parsed: List[List[str]] = []
    for item in items:
        if isinstance(item, (list, tuple)):
            if len(item) != 2:
                raise WorkerError(f"统计项 {item!r} 应该是 [字段, 统计方式] 两个元素")
            field, stat = str(item[0]).strip(), str(item[1]).strip().upper()
        else:
            parts = str(item).replace(",", " ").split()
            if len(parts) != 2:
                raise WorkerError(f'统计项 {item!r} 要写成「字段 统计方式」两段，例如 "POP SUM"')
            field, stat = parts[0], parts[1].upper()
        if stat not in STATISTICS_TYPES:
            raise WorkerError(f"统计方式 {stat!r} 不认识，可用：" + " / ".join(STATISTICS_TYPES))
        parsed.append([field, stat])
    return parsed


def _summary_statistics(inputs: List[str], out: str,
                        params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """按字段汇总统计（可以分组），输出一张统计表。"""
    import arcpy

    stats = _parse_statistics_fields(params.get("statistics_fields"))
    case_field = params.get("case_field")
    if isinstance(case_field, str):
        case_field = [c.strip() for c in case_field.replace(";", ",").split(",") if c.strip()]
    with table_output(out, params.get('clean_csv', True)) as table:
        arcpy.analysis.Statistics(inputs[0], table, stats, case_field or None,
                                  params.get("concatenation_separator"))
    return {'group_fields': case_field or [], 'clean_csv': params.get('clean_csv', True)}


def _frequency(inputs: List[str], out: str, params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """频数统计：每个取值组合出现多少次。"""
    import arcpy

    fields = params.get("frequency_field")
    if not fields:
        raise WorkerError('频数统计需要 frequency_field，例如 ["TYPE"] 或 ["TYPE", "ZONE"]')
    if isinstance(fields, str):
        fields = [f.strip() for f in fields.replace(";", ",").split(",") if f.strip()]
    summary = params.get("summary_fields")
    if isinstance(summary, str):
        summary = [s.strip() for s in summary.replace(";", ",").split(",") if s.strip()]
    with table_output(out, params.get('clean_csv', True)) as table:
        arcpy.analysis.Frequency(inputs[0], table, fields, summary or None)
    return {'group_fields': fields, 'clean_csv': params.get('clean_csv', True),
            'frequency_semantics': '每行代表 frequency_field 的一个完整取值组合；FREQUENCY 是输入记录中该组合的出现次数。'
                                   '若输入已按这些字段融合，每个组合只有一个要素，FREQUENCY 全为 1 是正常结果。'}


register("summary_statistics", "汇总统计（可分组）", _summary_statistics, min_inputs=1,
         allowed={"statistics_fields", "case_field", "concatenation_separator", 'clean_csv'})
register("frequency", "频数统计", _frequency, min_inputs=1,
         allowed={"frequency_field", "summary_fields", 'clean_csv'})
