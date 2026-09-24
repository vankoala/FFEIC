"""Call Report repricing wall (PLAN.md §7.1): staging -> dim_bank, fact_cdr_repricing, qa_cdr_flags.

Amounts are in US dollars. The buckets are "repricing or maturing" amounts, never "ARM resets":
floating-rate loans sit in them by next repricing date and fixed-rate loans by remaining
maturity, both measured from the report date.
"""

from __future__ import annotations

import polars as pl

from armreset.ingest.cdr import MdrmSpec, non_numeric

# Each figure is filed in whole thousands of dollars, so the buckets plus nonaccrual can miss
# the first-lien total by a few thousand. The largest gap in the verified files is $4k.
ROUNDING_TOLERANCE_USD = 5_000
HIGH_NONACCRUAL_SHARE = 0.25  # PLAN.md §5.1 QA threshold


def fact_columns(spec: MdrmSpec) -> list[str]:
    return [
        "rssd_id",
        "report_date",
        "total_assets",
        "first_lien_total",
        *spec.buckets,
        "sum_buckets",
        "implied_nonaccrual",
        "reported_nonaccrual",
        "n_buckets_reported",
        "prefix_source",
    ]


def _number(col: str) -> pl.Expr:
    return pl.col(col).str.strip_chars().replace("", None).cast(pl.Float64, strict=False)


def resolve(
    df: pl.DataFrame, code: str, prefixes: tuple[str, ...] = ("RCFD", "RCON")
) -> tuple[pl.Expr, str | None]:
    """Coalesce RCFD→RCON for one MDRM item, convert thousands → dollars.
    Returns the expression and which prefixes were present (for the prefix_source audit column)."""
    cols = [f"{p}{code}" for p in prefixes if f"{p}{code}" in df.columns]
    if not cols:
        return pl.lit(None, dtype=pl.Float64), None
    expr = pl.coalesce([_number(c) for c in cols]) * 1_000
    return expr, ",".join(cols)


def resolved_column(df: pl.DataFrame, code: str, prefixes: tuple[str, ...]) -> pl.Expr:
    """Per row, the column that :func:`resolve` took its value from (null when none)."""
    expr = pl.lit(None, dtype=pl.Utf8)
    for col in reversed([f"{p}{code}" for p in prefixes if f"{p}{code}" in df.columns]):
        expr = pl.when(_number(col).is_not_null()).then(pl.lit(col)).otherwise(expr)
    return expr


def build_fact(stg: pl.DataFrame, spec: MdrmSpec) -> pl.DataFrame:
    """One row per bank-quarter, in dollars, with the bucket reconciliation and an audit trail."""
    values, sources = [], []
    for column, code in spec.items.items():
        expr, _ = resolve(stg, code, spec.prefixes_for(code))
        values.append(expr.alias(column))
        sources.append(resolved_column(stg, code, spec.prefixes_for(code)).alias(f"src_{column}"))
    fact = stg.select("rssd_id", "report_date", *values, *sources)

    buckets = list(spec.buckets)
    n_reported = pl.sum_horizontal([pl.col(b).is_not_null().cast(pl.Int8) for b in buckets])
    fact = fact.with_columns(
        n_buckets_reported=n_reported,
        sum_buckets=pl.when(n_reported > 0).then(pl.sum_horizontal(buckets)),
    ).with_columns(implied_nonaccrual=pl.col("first_lien_total") - pl.col("sum_buckets"))

    # prefix_source, e.g. "RCFD2170,RCON5367,RCONA564-A569,RCONC229": the columns each value
    # came from, with the six bucket columns collapsed when they share a prefix.
    bucket_src = [pl.col(f"src_{b}") for b in buckets]
    first, last = spec.buckets[buckets[0]], spec.buckets[buckets[-1]]
    same_prefix = pl.all_horizontal(
        [s.is_not_null() for s in bucket_src]
        + [s.str.slice(0, 4) == bucket_src[0].str.slice(0, 4) for s in bucket_src[1:]]
    )
    listed = pl.concat_str(bucket_src, separator=",", ignore_nulls=True)
    bucket_summary = (
        pl.when(same_prefix)
        .then(pl.concat_str([bucket_src[0].str.slice(0, 4), pl.lit(f"{first}-{last}")]))
        .when(listed != "")
        .then(listed)
    )
    fact = fact.with_columns(
        prefix_source=pl.concat_str(
            [
                pl.col("src_total_assets"),
                pl.col("src_first_lien_total"),
                bucket_summary,
                pl.col("src_reported_nonaccrual"),
            ],
            separator=",",
            ignore_nulls=True,
        )
    )
    return fact.select(fact_columns(spec)).sort("report_date", "rssd_id")


def _detail(*cols: str) -> pl.Expr:
    parts: list[pl.Expr] = []
    for i, col in enumerate(cols):
        parts.append(pl.lit(("; " if i else "") + f"{col}="))
        parts.append(pl.col(col).cast(pl.Int64).cast(pl.Utf8).fill_null("null"))
    return pl.concat_str(parts)


def qa_flags(fact: pl.DataFrame, stg: pl.DataFrame, spec: MdrmSpec) -> pl.DataFrame:
    """Rows for ``qa_cdr_flags``. Flagged bank-quarters stay in the fact table."""
    tol = ROUNDING_TOLERANCE_USD
    implied, reported = pl.col("implied_nonaccrual"), pl.col("reported_nonaccrual")
    n_buckets = len(spec.buckets)
    reconciliation = _detail(
        "first_lien_total", "sum_buckets", "implied_nonaccrual", "reported_nonaccrual"
    )
    rules: list[tuple[str, pl.Expr, pl.Expr]] = [
        ("implied_nonaccrual_negative", implied < -tol, reconciliation),
        ("implied_nonaccrual_negative_rounding", (implied < 0) & (implied >= -tol), reconciliation),
        (
            "implied_nonaccrual_gt_25pct",
            (pl.col("first_lien_total") > 0)
            & (implied > HIGH_NONACCRUAL_SHARE * pl.col("first_lien_total")),
            reconciliation,
        ),
        ("nonaccrual_mismatch", (implied - reported).abs() > tol, reconciliation),
        (
            "buckets_not_reported",
            (pl.col("n_buckets_reported") == 0) & pl.col("first_lien_total").is_not_null(),
            _detail("first_lien_total"),
        ),
        (
            "buckets_partial",
            (pl.col("n_buckets_reported") > 0) & (pl.col("n_buckets_reported") < n_buckets),
            _detail(*spec.buckets),
        ),
    ]
    frames = [
        fact.filter(condition).select(
            "rssd_id", "report_date", pl.lit(flag).alias("flag"), detail.alias("detail")
        )
        for flag, condition, detail in rules
    ]
    for col in (c for c in spec.wanted_columns if c in stg.columns):
        frames.append(
            stg.filter(non_numeric(col)).select(
                "rssd_id",
                "report_date",
                pl.lit("unparseable_value").alias("flag"),
                pl.concat_str([pl.lit(f"{col}="), pl.col(col)]).alias("detail"),
            )
        )
    return pl.concat(frames).sort("report_date", "rssd_id", "flag")


def build_dim_bank(stg: pl.DataFrame) -> pl.DataFrame:
    """Bank identity per quarter, from the POR file."""
    return stg.select(
        "rssd_id",
        "report_date",
        "name",
        "city",
        "state",
        "form",
        "fdic_cert",
        "last_submission",
    ).sort("report_date", "rssd_id")
