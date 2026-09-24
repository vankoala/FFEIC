from datetime import date
from pathlib import Path

import polars as pl
import pytest

from armreset.ingest.cdr import MdrmSpec, stage_quarter
from armreset.model.repricing import (
    ROUNDING_TOLERANCE_USD,
    build_dim_bank,
    build_fact,
    qa_flags,
    resolve,
    resolved_column,
)
from tests.cdr_fixtures import BANKS, make_cdr_zip
from tests.conftest import REPO_ROOT


@pytest.fixture
def spec() -> MdrmSpec:
    return MdrmSpec.load(REPO_ROOT / "config" / "mdrm.yaml")


@pytest.fixture
def stg(spec: MdrmSpec, tmp_path: Path) -> pl.DataFrame:
    zip_path = make_cdr_zip(tmp_path / "raw")
    out = tmp_path / "stg.parquet"
    stage_quarter(zip_path, date(2026, 6, 30), spec, out, "sha")
    return pl.read_parquet(out)


@pytest.fixture
def fact(stg: pl.DataFrame, spec: MdrmSpec) -> pl.DataFrame:
    return build_fact(stg, spec)


def _row(df: pl.DataFrame, rssd: int) -> dict:
    return df.filter(pl.col("rssd_id") == rssd).row(0, named=True)


# PLAN.md §11: resolve()
def test_resolve_prefers_rcfd_then_rcon_and_scales_to_dollars() -> None:
    df = pl.DataFrame({"RCFD2170": ["1000", "", None, " 7 "], "RCON2170": ["5", "20", None, "9"]})
    expr, present = resolve(df, "2170")
    assert present == "RCFD2170,RCON2170"
    assert df.select(expr.alias("v"))["v"].to_list() == [1_000_000, 20_000, None, 7_000]


def test_resolve_blank_is_null_and_missing_columns_resolve_to_null() -> None:
    df = pl.DataFrame({"RCON5367": ["", "  ", "0"]})
    expr, present = resolve(df, "5367")
    assert df.select(expr.alias("v"))["v"].to_list() == [None, None, 0.0]
    expr, present = resolve(df, "9999")
    assert present is None
    assert df.with_columns(expr.alias("v"))["v"].to_list() == [None, None, None]


def test_resolved_column_names_the_source_per_row() -> None:
    df = pl.DataFrame({"RCFD2170": ["1", ""], "RCON2170": ["2", "3"]})
    src = df.select(resolved_column(df, "2170", ("RCFD", "RCON")).alias("s"))["s"]
    assert src.to_list() == ["RCFD2170", "RCON2170"]


def test_first_lien_total_stays_on_the_domestic_basis(fact: pl.DataFrame) -> None:
    big = _row(fact, 100)  # an 031 filer that also reports RCFD5367 = 600
    assert big["first_lien_total"] == 500_000  # RCON5367, the basis of the RCON buckets
    assert big["total_assets"] == 1_000_000  # RCFD2170 for the 031 filer
    assert big["prefix_source"] == "RCFD2170,RCON5367,RCONA564-A569,RCONC229"
    assert _row(fact, 200)["prefix_source"] == "RCON2170,RCON5367,RCONA564-A569,RCONC229"


def test_bucket_reconciliation_columns(fact: pl.DataFrame) -> None:
    big = _row(fact, 100)
    assert big["sum_buckets"] == 490_000
    assert big["implied_nonaccrual"] == big["first_lien_total"] - big["sum_buckets"]
    assert big["implied_nonaccrual"] == big["reported_nonaccrual"] == 10_000
    assert big["n_buckets_reported"] == 6

    none = _row(fact, 300)  # no buckets reported: sums stay null, not zero
    assert none["n_buckets_reported"] == 0
    assert none["sum_buckets"] is None and none["implied_nonaccrual"] is None

    partial = _row(fact, 600)
    assert partial["n_buckets_reported"] == 5
    assert partial["b_1_3y"] is None
    assert partial["total_assets"] is None  # "CONF" is not a number
    assert partial["prefix_source"] == (
        "RCON5367,RCONA564,RCONA565,RCONA567,RCONA568,RCONA569,RCONC229"
    )


def test_fact_has_one_row_per_bank_quarter(fact: pl.DataFrame, spec: MdrmSpec) -> None:
    assert fact.height == len(BANKS)
    assert not fact.select(pl.struct("rssd_id", "report_date").is_duplicated().any()).item()
    assert fact.columns[:4] == ["rssd_id", "report_date", "total_assets", "first_lien_total"]
    assert list(spec.buckets) == fact.columns[4:10]


def test_qa_flags_per_bank(fact: pl.DataFrame, stg: pl.DataFrame, spec: MdrmSpec) -> None:
    flags = qa_flags(fact, stg, spec)
    by_bank: dict[int, set[str]] = {}
    for rssd, flag in flags.select("rssd_id", "flag").iter_rows():
        by_bank.setdefault(rssd, set()).add(flag)
    assert by_bank == {
        200: {"implied_nonaccrual_negative_rounding"},
        300: {"buckets_not_reported"},
        400: {"implied_nonaccrual_gt_25pct"},
        500: {"nonaccrual_mismatch"},
        600: {"buckets_partial", "unparseable_value"},
        700: {"implied_nonaccrual_negative", "nonaccrual_mismatch"},
    }
    detail = flags.filter(pl.col("flag") == "unparseable_value")["detail"].to_list()
    assert detail == ["RCON2170=CONF"]
    rounding = flags.filter(pl.col("flag") == "implied_nonaccrual_negative_rounding")
    assert "implied_nonaccrual=-1000" in rounding["detail"][0]


def test_rounding_boundary(spec: MdrmSpec, stg: pl.DataFrame) -> None:
    # Shift bank 200's total so the gap is exactly -tolerance (rounding) or just past it.
    # Reported nonaccrual is 0, so past the tolerance the reconciliation breaks as well.
    assert ROUNDING_TOLERANCE_USD == 5_000
    for gap_thousands, expected in [
        (-5, {"implied_nonaccrual_negative_rounding"}),
        (-6, {"implied_nonaccrual_negative", "nonaccrual_mismatch"}),
    ]:
        tweaked = stg.with_columns(
            pl.when(pl.col("rssd_id") == 200)
            .then(pl.lit(str(101 + gap_thousands)))
            .otherwise(pl.col("RCON5367"))
            .alias("RCON5367")
        )
        fact = build_fact(tweaked, spec)
        flags = qa_flags(fact, tweaked, spec).filter(pl.col("rssd_id") == 200)
        assert set(flags["flag"]) == expected


def test_dim_bank(stg: pl.DataFrame) -> None:
    dim = build_dim_bank(stg)
    assert dim.columns[:6] == ["rssd_id", "report_date", "name", "city", "state", "form"]
    assert _row(dim, 100)["form"] == "031"
