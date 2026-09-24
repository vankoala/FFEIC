"""DuckDB warehouse (PLAN.md §6): connections and table writes. Views arrive in phase 2."""

from __future__ import annotations

import duckdb
import polars as pl

from armreset.settings import Settings


def connect(settings: Settings, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    path = settings.warehouse_path
    if read_only:
        if not path.exists():
            raise FileNotFoundError(f"No warehouse at {path}; run `armtool build` first.")
        return duckdb.connect(str(path), read_only=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(str(path))


def replace_table(con: duckdb.DuckDBPyConnection, name: str, df: pl.DataFrame) -> int:
    """``CREATE OR REPLACE TABLE name`` from ``df``; returns the row count."""
    if not name.isidentifier():
        raise ValueError(f"Not a table name: {name!r}")
    con.register("incoming_df", df.to_arrow())
    try:
        con.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM incoming_df")
    finally:
        con.unregister("incoming_df")
    return df.height


def relations(con: duckdb.DuckDBPyConnection) -> list[tuple[str, str]]:
    """(name, 'BASE TABLE' | 'VIEW') for everything in the main schema."""
    return con.execute(
        "SELECT table_name, table_type FROM information_schema.tables "
        "WHERE table_schema = 'main' ORDER BY table_name"
    ).fetchall()
