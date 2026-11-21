"""多维立方领域服务：预计算、维度组合生成、构建调度。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store
from app.analytics_engine import (
    build_cube_cell,
    generate_dimension_combinations,
)


class CubeBuilderService(BaseModuleService):
    MODULE = "cube_builder"
    ENTITY = "OLAP立方"
    OPERATION_TERM = "聚合"
    LIST_FIELDS = ["立方名称", "事实表", "维度列表", "预聚合度量", "分区策略", "刷新频率", "存储引擎"]
    REQUIRED_FIELDS = ["立方名称", "事实表", "维度列表"]
    STATUS_ORDER = ["草稿", "构建中", "已上线", "已下线"]
    ACTION_RULES = {"开始构建": "构建中", "上线立方": "已上线", "下线立方": "已下线"}
    NEGATIVE_ACTIONS = ["下线立方"]

    # ---- 业务方法 ----

    @staticmethod
    def _parse_dimensions(cube: dict[str, Any]) -> list[str]:
        return [d.strip() for d in str(cube.get("维度列表", "")).split(",") if d.strip()]

    @staticmethod
    def _parse_measures(cube: dict[str, Any]) -> list[dict[str, str]]:
        measures: list[dict[str, str]] = []
        for m in str(cube.get("预聚合度量", "")).split(","):
            parts = m.strip().split(":")
            if len(parts) >= 2:
                measures.append({"name": parts[0].strip(), "aggregator": parts[1].strip()})
            elif m.strip():
                measures.append({"name": m.strip(), "aggregator": "SUM"})
        return measures

    @staticmethod
    def _row_count_data(cells: list[Any]) -> dict[str, Any]:
        """汇总一组立方单元格的行数数据：事实行总数、各度量明细行数、全空事实行数。"""
        measure_row_counts: dict[str, int] = {}
        for cell in cells:
            for name, count in cell.measure_counts.items():
                measure_row_counts[name] = measure_row_counts.get(name, 0) + count
        return {
            "fact_rows": sum(c.row_count for c in cells),
            "measure_row_counts": measure_row_counts,
            "empty_measure_rows": sum(c.empty_measure_rows for c in cells),
        }

    def precompute_cube(self, cube_id: int, sample_data: list[dict[str, Any]]) -> dict[str, Any]:
        """对指定立方执行预计算，按维度组合分组聚合。"""
        cube = store.find(self.MODULE, cube_id)
        if cube is None:
            return {"ok": False, "message": f"立方 {cube_id} 不存在"}
        dimensions = self._parse_dimensions(cube)
        measures = self._parse_measures(cube)

        cells = build_cube_cell(sample_data, dimensions, measures)
        dimension_combos = generate_dimension_combinations(dimensions)
        row_count_data = self._row_count_data(cells)

        return {
            "ok": True,
            "cube_id": cube_id,
            "cube_name": str(cube.get("立方名称", "")),
            "dimensions": dimensions,
            "dimension_combinations": len(dimension_combos),
            "cells": len(cells),
            "fact_rows": row_count_data["fact_rows"],
            "measure_row_counts": row_count_data["measure_row_counts"],
            "empty_measure_rows": row_count_data["empty_measure_rows"],
            "cells_preview": [
                {
                    "dimensions": c.dimensions,
                    "measures": c.measure_values,
                    "row_count": c.row_count,
                    "measure_counts": c.measure_counts,
                    "empty_measure_rows": c.empty_measure_rows,
                }
                for c in cells[:10]
            ],
            "storage_engine": cube.get("存储引擎"),
        }

    def cube_row_stats(
        self, cube_name: str, rows: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """向切片查询交付立方行数数据。

        按立方口径对给定的覆盖范围（同一批明细行）分组统计：
        每个维组的事实行数、每个度量各自纳入的明细行数，以及
        所有度量都缺失的全空事实行数，便于下游核对空度量行的归属。
        """
        cube = next(
            (c for c in store.rows(self.MODULE) if c.get("立方名称") == cube_name),
            None,
        )
        if cube is None:
            return None
        dimensions = self._parse_dimensions(cube)
        measures = self._parse_measures(cube)
        cells = build_cube_cell(rows, dimensions, measures)
        row_count_data = self._row_count_data(cells)
        return {
            "cube_id": cube.get("id"),
            "cube_name": str(cube.get("立方名称", "")),
            "dimensions": dimensions,
            "scope_rows": row_count_data["fact_rows"],
            "fact_rows": row_count_data["fact_rows"],
            "measure_row_counts": row_count_data["measure_row_counts"],
            "empty_measure_rows": row_count_data["empty_measure_rows"],
            "groups": [
                {
                    "dimensions": c.dimensions,
                    "row_count": c.row_count,
                    "measure_counts": c.measure_counts,
                    "empty_measure_rows": c.empty_measure_rows,
                }
                for c in cells
            ],
        }

    def get_cube_dimensions(self, cube_id: int) -> list[str]:
        """获取立方的维度列表（供切片查询消费）。"""
        cube = store.find(self.MODULE, cube_id)
        if cube is None:
            return []
        return self._parse_dimensions(cube)

    def get_online_cubes(self) -> list[dict[str, Any]]:
        """获取所有已上线立方。"""
        return [
            row for row in store.rows(self.MODULE)
            if row.get("status") == "已上线"
        ]

    def estimate_storage(self, cube_id: int, sample_row_count: int) -> dict[str, Any]:
        """估算立方存储需求。"""
        cube = store.find(self.MODULE, cube_id)
        if cube is None:
            return {"ok": False, "message": f"立方 {cube_id} 不存在"}
        dimensions_count = len(
            [d for d in str(cube.get("维度列表", "")).split(",") if d.strip()]
        )
        measures_count = len(
            [m for m in str(cube.get("预聚合度量", "")).split(",") if m.strip()]
        )
        combos = 2 ** dimensions_count - 1
        estimated_cells = combos * min(sample_row_count, 1000)
        estimated_bytes = estimated_cells * measures_count * 32
        return {
            "ok": True,
            "dimension_count": dimensions_count,
            "measure_count": measures_count,
            "dimension_combinations": combos,
            "estimated_cells": estimated_cells,
            "estimated_mb": round(estimated_bytes / (1024 * 1024), 2),
        }

    def domain_snapshot(self) -> dict[str, Any]:
        base = super().domain_snapshot()
        online_cubes = sum(
            1 for row in store.rows(self.MODULE) if row.get("status") == "已上线"
        )
        base["online_cubes"] = online_cubes
        base["artifact"] = "OLAP立方数据集"
        return base