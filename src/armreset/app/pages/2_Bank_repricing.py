"""Page 2: bank-held first liens repricing or maturing, from the Call Reports (PLAN.md §8)."""

import plotly.graph_objects as go
import polars as pl
import streamlit as st

from armreset.app import data, ui

st.title("Bank repricing")
st.caption(
    "Closed-end first liens on 1-4 family homes held by banks, by time to repricing or "
    "maturity (Call Report Schedule RC-C Part I, Memorandum item 2.a). These are not ARM "
    "resets: fixed-rate loans sit here by remaining maturity."
)
if not data.ready():
    st.stop()

t = ui.tokens()
states = data.query("SELECT DISTINCT state FROM dim_bank WHERE state IS NOT NULL ORDER BY 1")
c1, c2, c3 = st.columns([1.2, 2.2, 1.2])
band = c1.selectbox("Total assets", [label for label, _, _ in data.ASSET_BANDS])
chosen = c2.multiselect("State", states["state"].to_list(), placeholder="All")
min_book = c3.number_input("Minimum first-lien book ($mm)", min_value=0, value=0, step=100)

series = data.industry(band, chosen, min_book)
if series.is_empty():
    st.warning("No bank matches these filters.")
    st.stop()
latest = series["report_date"].max()
quarters = [d.isoformat() for d in series["report_date"]]

fig = go.Figure()
for i, (label, _) in enumerate(data.BUCKET_BANDS):
    fig.add_scatter(
        x=quarters,
        y=(series[label] / 1e9).to_list(),
        name=label,
        mode="lines",
        stackgroup="buckets",
        line={"color": t.surface, "width": 2},
        fillcolor=t.ramp[i],
        hovertemplate="<b>$%{y:.1f}bn</b>  " + label + "<extra></extra>",
    )
ui.style(fig, t, height=380, y_title="$bn")
st.subheader("Repricing or maturing, by quarter")
ui.show(fig)

share = go.Figure()
share.add_scatter(
    x=quarters,
    y=(series["within_12m_share"] * 100).to_list(),
    mode="lines",
    line={"color": t.accent, "width": 2},
    name="Within 12 months",
    hovertemplate="<b>%{y:.1f}%</b> of the first-lien book<extra></extra>",
)
end = series["within_12m_share"][-1] * 100
share.add_annotation(
    x=quarters[-1],
    y=end,
    text=f"{end:.1f}%",
    showarrow=False,
    xanchor="left",
    xshift=6,
    font={"color": t.text},
)
ui.style(share, t, height=200, y_title="% of first-lien book")
share.update_layout(showlegend=False)
st.markdown("**Share of the first-lien book repricing or maturing within 12 months**")
ui.show(share)
ui.caption(
    "FFIEC Call Reports (CDR bulk data), RCON items A564-A569 and 5367, every bank that filed "
    f"that quarter ({series['banks'][-1]:,} in the latest)",
    latest.isoformat(),
    "floating-rate loans sit in the buckets by next repricing date and fixed-rate loans by "
    "remaining maturity, measured from each report date; the chart combines 5-15 years and "
    "over 15 years. A change between quarters also reflects banks merging, failing and "
    "opening.",
)
with st.expander("Table view"):
    st.dataframe(
        series.with_columns(
            [(pl.col(label) / 1e9).round(1) for label, _ in data.BUCKET_BANDS]
            + [(pl.col("within_12m_share") * 100).round(1).alias("within 12m, % of book")]
        ).drop("first_lien_total", "within_12m", "within_12m_share"),
        hide_index=True,
        width="stretch",
    )

st.subheader(f"Banks, {latest.isoformat()}")
banks = data.banks_latest(band, chosen, min_book)
table = banks.select(
    pl.col("name").alias("Bank"),
    pl.col("state").alias("State"),
    pl.col("rssd_id").alias("RSSD"),
    (pl.col("total_assets") / 1e6).round(1).alias("Total assets $mm"),
    (pl.col("first_lien_total") / 1e6).round(1).alias("First liens $mm"),
    (pl.col("within_12m") / 1e6).round(1).alias("Within 12m $mm"),
    (pl.col("within_12m_pct_first_lien")).round(1).alias("Within 12m, % of first liens"),
    (pl.col("within_12m_pct_assets")).round(2).alias("Within 12m, % of assets"),
    (pl.col("within_3y") / 1e6).round(1).alias("Within 3y $mm"),
    (pl.col("within_3y_pct_first_lien")).round(1).alias("Within 3y, % of first liens"),
    pl.col("qa_flags").alias("QA flags"),
)
st.dataframe(
    table,
    hide_index=True,
    width="stretch",
    height=460,
    column_config={
        "RSSD": st.column_config.NumberColumn(format="%d"),
        **{
            c: st.column_config.NumberColumn(format=ui.AMOUNT)
            for c in table.columns
            if c.endswith("$mm")
        },
        **{
            c: st.column_config.NumberColumn(format=ui.PERCENT)
            for c in table.columns
            if "% of" in c
        },
    },
)
st.caption(
    f"{len(table):,} banks, sorted by the amount within 12 months; click a column to sort. "
    "Amounts in $mm. Only banks that filed in the latest quarter: a bank that merged or "
    "failed is left out, because another filer now holds its loans."
)
