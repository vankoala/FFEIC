"""Page 4: HMDA originations by year: ARM share, months to first reset, loan size, exempt
filers and the largest ARM lenders (PLAN.md §8)."""

import plotly.graph_objects as go
import polars as pl
import streamlit as st

from armreset.app import data, ui

st.title("HMDA explorer")
st.caption(
    "Originated first-lien closed-end loans on 1-4 family homes, 2018-2025, from the CFPB "
    "Data Browser. An ARM has an intro period shorter than its term."
)
if not data.ready():
    st.stop()

t = ui.tokens()
years = data.hmda_years()
as_of = str(years["year"].max())
xs = years["year"].to_list()


def bars(y: list[float], name: str, fmt: str) -> go.Figure:
    fig = go.Figure()
    fig.add_bar(
        x=xs,
        y=y,
        name=name,
        marker={"color": t.accent, "line": {"color": t.surface, "width": 2}},
        hovertemplate="<b>%{y:" + fmt + "}</b>  " + name + "<extra></extra>",
    )
    ui.style(fig, t, height=260)
    fig.update_layout(showlegend=False, bargap=0.6)
    fig.update_xaxes(tickmode="array", tickvals=xs)
    return fig


c1, c2 = st.columns(2)
with c1:
    st.markdown("**ARM share of loans (%)**")
    ui.show(bars((years["arm_loans"] / years["loans"] * 100).to_list(), "of loans", ".1f"))
with c2:
    st.markdown("**ARM share of dollars (%)**")
    ui.show(bars((years["arm_amount"] / years["amount"] * 100).to_list(), "of dollars", ".1f"))
ui.caption(
    "HMDA LAR, originations (action taken 1), first liens",
    f"the {as_of} data",
    "exempt filers' loans are counted as neither fixed nor ARM; 2023-2024 come from the "
    "one-year datasets and 2025 from the snapshot, which later filings can still change.",
)

st.subheader("Months to first reset")
mix = data.intro_mix()
order = (
    mix.select("intro_bucket")
    .unique()
    .with_columns(pl.col("intro_bucket").str.extract(r"^(\d+)").cast(pl.Int32).alias("k"))
    .sort("k")
)["intro_bucket"].to_list()
grid = mix.pivot(on="year", index="intro_bucket", values="share").fill_null(0.0)
grid = grid.join(pl.DataFrame({"intro_bucket": order, "k": range(len(order))}), on="intro_bucket")
grid = grid.sort("k").drop("k")
year_cols = [c for c in grid.columns if c != "intro_bucket"]
z = [[row[c] * 100 for c in year_cols] for row in grid.iter_rows(named=True)]
heat = go.Figure(
    go.Heatmap(
        z=z,
        x=year_cols,
        y=[f"{b} months" for b in grid["intro_bucket"]],
        colorscale=[[i / (len(t.sequential) - 1), c] for i, c in enumerate(t.sequential)],
        text=[[f"{v:.0f}%" for v in row] for row in z],
        texttemplate="%{text}",
        hovertemplate="<b>%{z:.1f}%</b> of %{x} ARMs reset after %{y}<extra></extra>",
        showscale=False,
        xgap=2,
        ygap=2,
    )
)
ui.style(heat, t, height=420)
heat.update_layout(hovermode="closest")
heat.update_yaxes(autorange="reversed", gridcolor="rgba(0,0,0,0)")
ui.show(heat)
ui.caption(
    "HMDA LAR, ARM originations (qa_reset_inputs)",
    f"the {as_of} data",
    "shares of each year's ARM loans by count; intro periods in between the common ones are "
    "grouped, e.g. 61-83 months.",
)

c1, c2 = st.columns(2)
with c1:
    st.markdown("**Jumbo share of ARM dollars (%)**")
    ui.show(bars((years["jumbo_arm_amount"] / years["arm_amount"] * 100).to_list(), "jumbo", ".1f"))
    ui.caption(
        "HMDA LAR, ARM originations above the conforming loan limit",
        f"the {as_of} data",
        "'jumbo' is HMDA's conforming_loan_limit NC; a few loans are undetermined (U).",
    )
with c2:
    st.markdown("**Exempt share by year**")
    st.dataframe(
        years.select(
            pl.col("year").alias("Year"),
            (pl.col("exempt_loans") / pl.col("loans") * 100).alias("Share of loans"),
            (pl.col("exempt_amount") / pl.col("amount") * 100).alias("Share of dollars"),
        ),
        hide_index=True,
        width="stretch",
        column_config={
            "Year": st.column_config.NumberColumn(format="%d"),
            "Share of loans": st.column_config.NumberColumn(format=ui.PERCENT),
            "Share of dollars": st.column_config.NumberColumn(format=ui.PERCENT),
        },
    )
    st.caption(
        "Filers exempt from reporting the intro period (small filers under the 2018 partial "
        "exemptions). Their loans can't be placed in the reset calendar."
    )

st.subheader("Largest ARM lenders")
year = st.selectbox("Year", list(reversed(xs)))
top = data.top_originators(year)
st.dataframe(
    top.select(
        pl.col("lender").alias("Lender"),
        pl.col("lender_type")
        .replace_strict(data.labels("lender_type"), default=None)
        .alias("Lender type"),
        pl.col("match_status")
        .replace_strict(data.MATCH_STATUS, default=None)
        .alias("Call Report link"),
        pl.col("arm_loans").alias("ARM loans"),
        (pl.col("arm_amount") / 1e9).round(2).alias("ARM $bn"),
        (pl.col("share") * 100).alias("Share of the year's ARM $"),
        pl.col("loans").alias("All loans"),
    ),
    hide_index=True,
    width="stretch",
    column_config={
        "ARM loans": st.column_config.NumberColumn(format=ui.AMOUNT),
        "All loans": st.column_config.NumberColumn(format=ui.AMOUNT),
        "Share of the year's ARM $": st.column_config.NumberColumn(format=ui.PERCENT),
    },
)
st.caption(
    "Lenders from the Philadelphia Fed HMDA Lender File. Names are cut at 30 characters, as "
    "the file stores them."
)
