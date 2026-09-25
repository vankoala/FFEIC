"""``config/segments.yaml``: HMDA purchaser_type -> holder segment, and the Lender File's
institution type -> lender type."""

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
    institution_types: dict[int, tuple[str, str]]  # TYPE code -> (lender_type, label)
    unknown_lender_type: str
    call_report_filer_type: str  # overrides the code when the RSSD files a Call Report

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
        types: dict[int, tuple[str, str]] = {}
        for lender_type, codes in raw["lender_types"].items():
            for code, label in codes.items():
                if code in types:
                    raise ValueError(
                        f"institution type {code} is in both {types[code][0]} and {lender_type}"
                    )
                types[int(code)] = (lender_type, label)
        return cls(
            holder,
            raw.get("unmapped_segment", "unmapped"),
            types,
            raw.get("unknown_lender_type", "unknown"),
            raw.get("call_report_filer_type", "bank"),
        )

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

    def institution_type_table(self) -> pl.DataFrame:
        """``dim_institution_type``: one row per Lender File TYPE code."""
        return pl.DataFrame(
            [
                {"institution_type": code, "institution_label": label, "lender_type": kind}
                for code, (kind, label) in self.institution_types.items()
            ],
            schema={
                "institution_type": pl.Int64,
                "institution_label": pl.Utf8,
                "lender_type": pl.Utf8,
            },
        ).sort("institution_type")

    def type_from_code(self, column: str = "institution_type") -> pl.Expr:
        """Map an integer TYPE column to a lender type; unlisted or null codes are unknown."""
        mapping = {code: kind for code, (kind, _) in self.institution_types.items()}
        return pl.col(column).replace_strict(
            mapping, default=self.unknown_lender_type, return_dtype=pl.Utf8
        )

    def lender_type_expr(self) -> pl.Expr:
        """``lender_type``: ``call_report_filer_type`` when ``in_call_reports`` is true, else
        the type from ``institution_type``. Unknown ``in_call_reports`` never overrides."""
        return (
            pl.when(pl.col("in_call_reports").fill_null(False))
            .then(pl.lit(self.call_report_filer_type))
            .otherwise(self.type_from_code())
        )
