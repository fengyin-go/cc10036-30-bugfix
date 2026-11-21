"""切片查询领域服务：多维立方消费，按维度切片、过滤和排序。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class SliceQueryService(BaseModuleService):
    MODULE = "slice_query"
    ENTITY = "查询模板"
    OPERATION_TERM = "聚合"
    LIST_FIELDS = ["模板名称", "数据立方", "查询维值", "过滤条件", "排序规则", "返回行数", "缓存TTL"]
    REQUIRED_FIELDS = ["模板名称", "数据立方", "查询维值"]
    STATUS_ORDER = ["草稿", "已保存", "已发布", "已过期"]
    ACTION_RULES = {"保存模板": "已保存", "发布模板": "已发布", "标记过期": "已过期"}
    NEGATIVE_ACTIONS = ["标记过期"]

    # ---- 业务方法 ----

    def execute_slice_query(
        self, template_id: int, data: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """执行切片查询：根据模板配置对数据筛选、排序和截断。"""
        template = store.find(self.MODULE, template_id)
        if template is None:
            return {"ok": False, "message": f"查询模板 {template_id} 不存在"}

        dim_values_raw = str(template.get("查询维值", ""))
        filter_expr = str(template.get("过滤条件", ""))
        sort_expr = str(template.get("排序规则", ""))
        row_limit = _parse_int(template.get("返回行数"), 100)
        cache_ttl = _parse_int(template.get("缓存TTL"), 300)

        # 按维值筛选
        if dim_values_raw:
            conditions = [
                cond.strip().split("=", 1)
                for cond in dim_values_raw.split(";") if "=" in cond
            ]
            filtered: list[dict[str, Any]] = []
            for row in data:
                if all(
                    str(row.get(field.strip(), "")) == val.strip()
                    for field, val in conditions
                ):
                    filtered.append(row)
        else:
            filtered = list(data)

        # 应用过滤条件
        if filter_expr and filtered:
            filtered = _apply_slice_filter(filtered, filter_expr)

        # 排序
        if sort_expr and filtered:
            sort_parts = sort_expr.rsplit(":", 1)
            field = sort_parts[0].strip()
            reverse = sort_parts[1].strip().upper() == "DESC" if len(sort_parts) > 1 else False
            try:
                filtered.sort(
                    key=lambda r: float(r.get(field, 0)),
                    reverse=reverse,
                )
            except (ValueError, TypeError):
                filtered.sort(key=lambda r: str(r.get(field, "")), reverse=reverse)

        total = len(filtered)
        result = filtered[:row_limit]

        # 跨模块消费：向多维立方取行数数据，分组计数与切片明细用同一覆盖范围
        cube_row_stats = None
        cube_name = str(template.get("数据立方", "")).strip()
        if cube_name:
            from app.services import service_for
            cube_svc = service_for("cube_builder")
            if cube_svc is not None and hasattr(cube_svc, "cube_row_stats"):
                cube_row_stats = cube_svc.cube_row_stats(cube_name, filtered)

        response: dict[str, Any] = {
            "ok": True,
            "template_name": str(template.get("模板名称", "")),
            "result_rows": len(result),
            "total_rows": total,
            "row_limit": row_limit,
            "cache_ttl": cache_ttl,
            "data": result,
        }
        if cube_row_stats is not None:
            response["cube_row_stats"] = cube_row_stats
        return response

    def get_templates_for_cube(self, cube_name: str) -> list[dict[str, Any]]:
        """获取某立方关联的所有查询模板（供即席导出消费）。"""
        return [
            row for row in store.rows(self.MODULE)
            if row.get("数据立方") == cube_name and row.get("status") == "已发布"
        ]

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        base["artifact"] = "查询模板数据集"
        base["published_templates"] = sum(
            1 for row in store.rows(self.MODULE) if row.get("status") == "已发布"
        )
        return base


def _parse_int(val: Any, default: int) -> int:
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _apply_slice_filter(
    rows: list[dict[str, Any]], filter_expr: str,
) -> list[dict[str, Any]]:
    """解析过滤表达式并应用。支持 field>value; field<value 等。"""
    results = list(rows)
    for cond_part in filter_expr.split(";"):
        cond_part = cond_part.strip()
        if not cond_part:
            continue
        for op in (">=", "<=", "!=", ">", "<", "="):
            if op in cond_part:
                field, val = cond_part.split(op, 1)
                field = field.strip()
                val = val.strip()
                results = _filter_by_op(results, field, op, val)
                break
    return results


def _filter_by_op(
    rows: list[dict[str, Any]], field: str, op: str, val: str,
) -> list[dict[str, Any]]:
    try:
        val_num = float(val)
        is_numeric = True
    except ValueError:
        is_numeric = False

    if is_numeric:
        return [
            r for r in rows
            if _compare_num(
                float(r.get(field, 0)) if r.get(field) is not None else 0,
                val_num, op,
            )
        ]
    val_lower = val.lower()
    return [
        r for r in rows
        if _compare_str(str(r.get(field, "")).lower(), val_lower, op)
    ]


def _compare_num(a: float, b: float, op: str) -> bool:
    if op == ">":
        return a > b
    if op == "<":
        return a < b
    if op == ">=":
        return a >= b
    if op == "<=":
        return a <= b
    if op == "!=":
        return a != b
    return a == b


def _compare_str(a: str, b: str, op: str) -> bool:
    if op == "!=":
        return a != b
    if op == ">":
        return a > b
    if op == "<":
        return a < b
    return b in a