"""``config/segments.yaml``: HMDA purchaser_type -> holder segment, and panel -> lender type."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl
import yaml


@dataclass(frozen=True)
class Segments:
    holder: dict[str, dict[str, Any]]  # segment -> {label, purchaser_types, overlaps}
    unmapped: str
    lender_rules: list[dict[str, Any]]  # first match wins

    @classmethod
    def load(cls, path: Path) -> Segments:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        holder = raw["holder_segments"]
        seen: dict[int, str] = {}
        for segment, spec in holder.items():
            for code in spec["purchaser_types"]:
                if code in seen:
                    raise ValueError(f"purchaser_type {code} is in both {seen[code]} and {segment}")
                seen[code] = segment
        return cls(holder, raw.get("unmapped_segment", "unmapped"), raw["lender_type_rules"])

    def purchaser_table(self) -> pl.DataFrame:
        """``dim_purchaser_segment``: one row per purchaser_type code."""
        rows = [
            {
                "purchaser_type": code,
                "holder_segment": segment,
                "holder_label": spec["label"],
                "overlaps_with": spec.get("overlaps"),
            }
            for segment, spec in self.holder.items()
            for code in spec["purchaser_types"]
        ]
        return pl.DataFrame(rows).sort("purchaser_type")

    def lender_type_expr(self) -> pl.Expr:
        """Polars expression mapping panel columns to ``lender_type`` (first rule wins).

        Expects columns ``agency_code``, ``other_lender_code`` (integers), ``name`` and
        ``in_call_reports`` (boolean).
        """
        expr: pl.Expr = pl.lit(None, dtype=pl.Utf8)
        for rule in reversed(self.lender_rules):
            conditions = []
            for key, value in rule.items():
                if key == "lender_type":
                    continue
                if key == "name_pattern":
                    conditions.append(pl.col("name").str.contains(f"(?i){value}").fill_null(False))
                elif key == "in_call_reports":
                    # Unknown (no Call Reports loaded for the year) matches neither value.
                    conditions.append((pl.col("in_call_reports") == bool(value)).fill_null(False))
                elif key in ("agency_code", "other_lender_code"):
                    conditions.append(pl.col(key).is_in(value).fill_null(False))
                else:
                    raise ValueError(f"Unknown lender_type_rules condition: {key}")
            condition = pl.all_horizontal(conditions) if conditions else pl.lit(True)
            expr = pl.when(condition).then(pl.lit(rule["lender_type"])).otherwise(expr)
        return expr
