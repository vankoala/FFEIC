import io
import zipfile
from pathlib import Path

import duckdb
import httpx
import pytest
from typer.testing import CliRunner

from armreset.cli import app
from armreset.db import connect
from armreset.fetch.hmda import csv_to_parquet
from armreset.manifest import Manifest
from armreset.pipeline import build
from armreset.qa import write_report
from armreset.settings import Settings, load_settings
from tests.cdr_fixtures import make_cdr_zip
from tests.hmda_fixtures import (
    BANK_LEI,
    CASES,
    CU_LEI,
    IMC_LEI,
    PANEL_ROWS,
    UNPANELLED_LEI,
    panel_csv,
    write_lar_csv,
)

runner = CliRunner()


def _panel_zip(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("2021_public_panel.csv", panel_csv(PANEL_ROWS))
    path.write_bytes(buffer.getvalue())
    return path


@pytest.fixture
def built(project: Path) -> Settings:
    s = load_settings()
    manifest = Manifest.for_settings(s)
    # Call Reports for 2021Q4: RSSD 100 (BIG BANK) files; 555 (the credit union) doesn't.
    cdr = make_cdr_zip(s.raw_dir / "cdr", stamp="12312021")
    manifest.record("cdr:2021Q4", "cdr", cdr, period="2021-12-31")
    csv = write_lar_csv(s.raw_dir / "hmda" / "lar_2021.csv")
    csv_to_parquet(csv, csv.with_suffix(".parquet"))
    manifest.record("hmda:2021:nationwide", "hmda", csv, period="2021")
    manifest.add_derived("hmda:2021:nationwide", csv.with_suffix(".parquet"))
    panel = _panel_zip(s.raw_dir / "hmda_panel" / "2021_public_panel_csv.zip")
    manifest.record("hmda_panel:2021", "hmda_panel", panel, period="2021")
    summary = build(s)
    assert {"stg_hmda", "v_hmda_orig_summary", "v_bank_hmda_link"} <= set(summary.views)
    return s


def _rows(con: duckdb.DuckDBPyConnection, sql: str) -> list[dict]:
    cursor = con.execute(sql)
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]


def test_orig_summary_reconciles_to_staging(built: Settings) -> None:
    con = connect(built, read_only=True)
    try:
        staged = con.execute("SELECT count(*), sum(loan_amount) FROM stg_hmda").fetchone()
        summary = con.execute("SELECT sum(loans), sum(amount) FROM v_hmda_orig_summary").fetchone()
        assert tuple(map(float, summary)) == tuple(map(float, staged))
        segments = {
            r["holder_segment"]
            for r in _rows(con, "SELECT holder_segment FROM v_hmda_orig_summary")
        }
        assert {"retained", "gse", "ginnie", "other", "unmapped"} <= segments  # 99 is unmapped
    finally:
        con.close()


def test_bank_hmda_link_statuses(built: Settings) -> None:
    con = connect(built, read_only=True)
    try:
        link = {r["lei"]: r for r in _rows(con, "SELECT * FROM v_bank_hmda_link")}
    finally:
        con.close()
    assert link[BANK_LEI]["match_status"] == "matched"
    assert link[BANK_LEI]["call_report_name"] == "BIG BANK, N.A."
    assert link[BANK_LEI]["lender_type"] == "bank"
    assert link[CU_LEI]["match_status"] == "rssd_not_a_call_report_filer"
    assert link[CU_LEI]["lender_type"] == "credit_union"
    assert link[IMC_LEI]["match_status"] == "no_rssd"
    assert link[IMC_LEI]["lender_type"] == "independent_mortgage_company"
    assert link[UNPANELLED_LEI]["match_status"] == "not_in_panel"
    kept = [r for _, r, rate in CASES if rate is not None]
    assert sum(r["loans"] for r in link.values()) == len(kept)
    arms = [r for _, r, rate in CASES if rate == "arm"]
    assert sum(r["arm_loans"] for r in link.values()) == len(arms)


def test_qa_report_has_hmda_sections(built: Settings) -> None:
    text = write_report(built).read_text()
    for heading in (
        "## HMDA",
        "### Months to first reset",
        "### ARMs by holder segment",
        "### Lenders linked to a Call Report filer",
    ):
        assert heading in text
    assert "| 2021 | 8 |" in text  # year row: 8 staged loans


def test_cli_fetch_hmda_and_panel(project: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import armreset.fetch.hmda as hmda
    import armreset.fetch.panel as panel

    lar = write_lar_csv(project / "lar_src.csv").read_bytes()
    panel_zip = _panel_zip(project / "panel_src.zip").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if "static-data" in request.url.path:
            return httpx.Response(200, content=panel_zip)
        return httpx.Response(200, content=lar)

    def mock_client(*args, **kwargs) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)

    monkeypatch.setattr(hmda, "polite_client", mock_client)
    monkeypatch.setattr(panel, "polite_client", mock_client)
    result = runner.invoke(app, ["fetch", "hmda", "--years", "2021"])
    assert result.exit_code == 0, result.output
    assert "downloaded" in result.output and f"{len(CASES)}" in result.output
    result = runner.invoke(app, ["fetch", "panel", "--years", "2021"])
    assert result.exit_code == 0, result.output
    assert "downloaded" in result.output
    result = runner.invoke(app, ["build"])
    assert result.exit_code == 0, result.output
    assert "HMDA 2021: 8 loans staged" in result.output
