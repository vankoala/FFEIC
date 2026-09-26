"""Page 1, the home page: HMDA ARMs by the year of their first rate reset (PLAN.md §8)."""

import plotly.graph_objects as go
import polars as pl
import streamlit as st

from armreset.app import data, ui
from armreset.db import connect
from armreset.qa import reset_coverage_matrix

st.title("Reset calendar")
st.caption(
    "First rate resets of adjustable-rate mortgages originated in 2018-2025 (HMDA), by "
    "calendar year and holder. Never add these to the Call Report figures: the sources "
    "overlap."
)
if not data.ready():
    st.stop()

s = data.settings()
t = ui.tokens()
model = s.model
first, last = model.calendar_years
as_of_year = model.as_of.year
scenarios = data.scenarios()
names = scenarios["scenario"].to_list()
cprs = dict(zip(names, scenarios["cpr"].to_list(), strict=True))
options = data.calendar_options()

# One row of filters above everything they scope.
c1, c2, c3, c4 = st.columns([1.3, 1.4, 2.0, 1.6])
scenario = c1.selectbox(
    f"CPR scenario, after {model.as_of}",
    names,
    index=names.index("base") if "base" in names else 0,
    format_func=lambda n: f"{n}: {ui.pct(cprs[n])} a year",
)
measure = c2.radio("Amount", ["Balance at reset", "Original amount"], horizontal=True)
window = c3.slider(
    "Selected window (reset years)",
    first,
    last,
    (max(first, as_of_year), min(last, as_of_year + 2)),
)
conforming = c4.multiselect("Loan size", options["conforming"], placeholder="All")
with st.expander("More filters"):
    f1, f2, f3 = st.columns(3)
    occupancy = f1.multiselect("Occupancy", options["occupancy"], placeholder="All")
    lender_type = f2.multiselect("Lender type", options["lender_type"], placeholder="All")
    state = f3.multiselect("State", options["state"], placeholder="All")

df = data.calendar(
    scenario,
    first,
    last,
    {"conforming": conforming, "occupancy": occupancy, "lender_type": lender_type, "state": state},
)
value = "bal_at_reset" if measure == "Balance at reset" else "orig_amount"
io_value = "bal_io" if value == "bal_at_reset" else "orig_io"
inside = df.filter(pl.col("reset_year").is_between(*window))
total = inside[value].sum()

m1, m2, m3 = st.columns(3)
label = f"{window[0]}-{window[1]}" if window[0] != window[1] else str(window[0])
m1.metric(f"{measure}, first resets in {label}", ui.bn(total))
m2.metric("ARM loans resetting in the window", f"{inside['w_loans'].sum():,.0f}")
m3.metric(
    "Interest-only share of that amount",
    f"{inside[io_value].sum() / total:.0%}" if total else "-",
    help="Interest-only ARMs are assumed to stay interest-only through the first reset, so "
    "they reach it without amortizing. HMDA doesn't report the interest-only period.",
)

years = list(range(first, last + 1))
fig = go.Figure()
order = [seg for seg in data.holder_order() if seg in set(df["holder_segment"])]
for i, segment in enumerate(order):
    rows = df.filter(pl.col("holder_segment") == segment)
    by_year = dict(zip(rows["reset_year"].to_list(), rows[value].to_list(), strict=True))
    name = rows["holder_label"][0]
    color = t.ramp[min(i, len(t.ramp) - 1)]
    for in_window in (True, False):
        # Resets inside the window carry the data hue; the rest is de-emphasis gray. Each
        # segment is two traces so the legend always shows its hue.
        ys = [
            by_year.get(y, 0) / 1e9 if (window[0] <= y <= window[1]) == in_window else None
            for y in years
        ]
        fig.add_bar(
            x=years,
            y=ys,
            name=name,
            legendgroup=segment,
            showlegend=in_window,
            marker={
                "color": color if in_window else t.muted,
                "line": {"color": t.surface, "width": 2},
            },
            hovertemplate="<b>$%{y:.1f}bn</b>  " + name + "<extra></extra>",
        )
fig.add_bar(
    x=[None], y=[None], name="Outside the selected window", marker_color=t.muted, hoverinfo="skip"
)
totals = df.group_by("reset_year").agg(pl.col(value).sum()).sort("reset_year")
labels = totals.filter(pl.col("reset_year").is_between(*window))
fig.add_scatter(
    x=labels["reset_year"].to_list(),
    y=(labels[value] / 1e9).to_list(),
    mode="text",
    text=[f"${v / 1e9:,.1f}bn" for v in labels[value]],
    textposition="top center",
    textfont={"color": t.text},
    hoverinfo="skip",
    showlegend=False,
)
fig.update_layout(barmode="stack", bargap=0.75)
ui.style(fig, t, height=420, y_title="$bn")
fig.update_xaxes(
    tickmode="array",
    tickvals=years,
    ticktext=[f"{y} *" if y == as_of_year else str(y) for y in years],
)
ui.show(fig)

history = data.history_cprs()
history_text = ", ".join(
    f"{y} {ui.pct(c)}" for y, c in zip(history["orig_year"], history["cpr"], strict=True)
)
ui.caption(
    "HMDA LAR 2018-2025 (CFPB Data Browser), originated first-lien ARMs; holder at origination "
    "from purchaser type",
    str(model.as_of),
    f"balances are modeled. Up to {model.as_of} each origination year prepays at an assumed "
    f"history CPR ({history_text or 'none set'}), after it at the {scenario} scenario's "
    f"{ui.pct(cprs[scenario])}; all are placeholders. * {as_of_year} includes resets that already "
    "happened this year. Some cells lack origination years HMDA doesn't have: see the "
    "coverage below.",
)

with st.expander("Table view and download"):
    table = (
        df.with_columns((pl.col(value) / 1e9).round(1).alias("$bn"))
        .pivot(on="holder_label", index="reset_year", values="$bn")
        .sort("reset_year")
        .fill_null(0.0)
    )
    table = table.with_columns(
        pl.sum_horizontal(pl.exclude("reset_year")).round(1).alias("Total")
    ).rename({"reset_year": "Reset year"})
    st.dataframe(table, hide_index=True, width="stretch")
    st.download_button(
        "Download CSV (USD)",
        df.with_columns(pl.lit(scenario).alias("scenario")).write_csv(),
        file_name=f"reset_calendar_{scenario}.csv",
        mime="text/csv",
    )

st.subheader("Coverage")
st.caption(
    "A cell is complete (✓) when every origination year behind it is loaded; ✗ names the "
    "missing years, and a percentage gives the share that is loaded (PLAN.md §7.2)."
)
con = connect(s, read_only=True)
try:
    matrix, other = reset_coverage_matrix(con, first, last, as_of_year, min_share=0.01)
finally:
    con.close()
st.dataframe(
    matrix.with_columns(pl.col("share of ARM $") * 100),
    hide_index=True,
    width="stretch",
    column_config={"share of ARM $": st.column_config.NumberColumn(format=ui.PERCENT)},
)
exempt = data.exempt_shares()
left, right = st.columns([2, 3])
left.dataframe(
    exempt.with_columns(pl.exclude("year") * 100),
    hide_index=True,
    width="stretch",
    column_config={
        "year": st.column_config.NumberColumn("Origination year", format="%d"),
        "exempt share (loans)": st.column_config.NumberColumn(format=ui.PERCENT),
        "exempt share ($)": st.column_config.NumberColumn(format=ui.PERCENT),
    },
)
right.markdown(
    f"- **Rows** are the intro periods with at least 1% of ARM dollars; the other "
    f"{other:.1%} are in `v_reset_coverage`.\n"
    "- **Exempt filers** don't report the intro period, so their ARMs can't be placed in the "
    "calendar. The table gives their share of each origination year.\n"
    "- **Lenders below HMDA's reporting thresholds** don't file, so their loans are absent "
    "from every year.\n"
    f"- **{as_of_year} is the current year** (`model.as_of` {model.as_of}): its bar includes "
    "resets that already happened."
)
