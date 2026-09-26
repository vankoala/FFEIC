"""Page 3: one bank's Call Report history, its HMDA lenders, and the HMDA vs Call Report
diagnostic of PLAN.md §7.3. Bank figures are in $mm."""

import plotly.graph_objects as go
import polars as pl
import streamlit as st

from armreset.app import data, ui

FLOOR = 10_000  # USD: the smallest amount the cross-check scatter plots

st.title("Bank drill-down")
if not data.ready():
    st.stop()

t = ui.tokens()
choices = data.bank_choices()
latest_report = choices["last_report"].max()
labels = {
    rssd: f"{name} (RSSD {rssd}, {state or '-'})"
    + ("" if last == latest_report else f", last filed {last.isoformat()}")
    for rssd, name, state, last in choices.select(
        "rssd_id", "name", "state", "last_report"
    ).iter_rows()
}
rssd = st.selectbox(
    "Bank: type a name or RSSD",
    list(labels),
    format_func=labels.get,
    help="Every bank that filed a Call Report since 2018Q1, the largest first-lien book first.",
)
history = data.bank_history(rssd)
now = history.row(-1, named=True)
st.markdown(
    f"**{now['name']}**, {now['city']}, {now['state']}. RSSD {rssd}, FDIC certificate "
    f"{now['fdic_cert']}, FFIEC {now['form']}. Latest report {now['report_date']}."
)

m1, m2, m3, m4 = st.columns(4)
book = now["first_lien_total"]
within_12m = (now["b_le_3m"] or 0) + (now["b_3_12m"] or 0)
within_3y = within_12m + (now["b_1_3y"] or 0)


def share(amount: float) -> str:
    return f" ({amount / book:.1%} of the book)" if book else ""


m1.metric("First-lien book", ui.money(book))
m2.metric("Within 12 months" + share(within_12m), ui.money(within_12m))
m3.metric("Within 3 years" + share(within_3y), ui.money(within_3y))
m4.metric("Implied nonaccrual", ui.money(now["implied_nonaccrual"]))

quarters = [d.isoformat() for d in history["report_date"]]
bands = history.with_columns(
    [pl.sum_horizontal(expr.split(" + ")).alias(label) for label, expr in data.BUCKET_BANDS]
)
fig = go.Figure()
for i, (label, _) in enumerate(data.BUCKET_BANDS):
    fig.add_scatter(
        x=quarters,
        y=(bands[label] / 1e6).to_list(),
        name=label,
        mode="lines",
        stackgroup="buckets",
        line={"color": t.surface, "width": 2},
        fillcolor=t.ramp[i],
        hovertemplate="<b>$%{y:,.1f}mm</b>  " + label + "<extra></extra>",
    )
ui.style(fig, t, height=340, y_title="$mm")
st.subheader("Repricing or maturing, by quarter")
ui.show(fig)

nonaccrual = go.Figure()
nonaccrual.add_scatter(
    x=quarters,
    y=(history["implied_nonaccrual"] / 1e6).to_list(),
    mode="lines",
    line={"color": t.accent, "width": 2},
    hovertemplate="<b>$%{y:,.1f}mm</b> implied nonaccrual<extra></extra>",
)
ui.style(nonaccrual, t, height=200, y_title="$mm")
nonaccrual.update_layout(showlegend=False)
st.markdown("**Implied nonaccrual: the first-lien book minus the six buckets**")
ui.show(nonaccrual)
ui.caption(
    "FFIEC Call Reports (CDR bulk data), this bank's filings",
    now["report_date"].isoformat(),
    "the buckets mix fixed-rate maturities with floating-rate repricing, measured from each "
    "report date; the chart combines 5-15 years and over 15 years. Implied nonaccrual "
    "should equal reported nonaccrual (RC-N), and the table shows both.",
)
with st.expander("Table view"):
    st.dataframe(
        history.select(
            "report_date",
            *[
                (pl.col(c) / 1e6).round(1)
                for c in (
                    "first_lien_total",
                    "b_le_3m",
                    "b_3_12m",
                    "b_1_3y",
                    "b_3_5y",
                    "b_5_15y",
                    "b_gt_15y",
                    "implied_nonaccrual",
                    "reported_nonaccrual",
                )
            ],
        ).sort("report_date", descending=True),
        hide_index=True,
        width="stretch",
    )

flags = data.bank_flags(rssd)
st.subheader("QA flags")
if flags.is_empty():
    st.caption("None: every quarter reconciles.")
else:
    st.dataframe(flags, hide_index=True, width="stretch")

st.subheader("HMDA lenders linked to this bank")
lenders = data.bank_lenders(rssd)
if lenders.is_empty():
    st.caption(
        "No HMDA lender in the Lender File has this RSSD. Loans the bank bought or holds "
        "through affiliates aren't linked."
    )
else:
    lender_types = data.labels("lender_type")
    st.dataframe(
        lenders.select(
            pl.col("activity_year").alias("Year"),
            pl.col("lei").alias("LEI"),
            pl.col("name").alias("Name"),
            pl.col("lender_type").replace_strict(lender_types, default=None).alias("Lender type"),
            pl.col("match_status")
            .replace_strict(data.MATCH_STATUS, default=None)
            .alias("Call Report link"),
            pl.col("loans").alias("Loans"),
            pl.col("arm_loans").alias("ARM loans"),
            (pl.col("arm_amount") / 1e6).round(1).alias("ARM originations $mm"),
        ),
        hide_index=True,
        width="stretch",
        column_config={
            "Year": st.column_config.NumberColumn(format="%d"),
            "Loans": st.column_config.NumberColumn(format=ui.AMOUNT),
            "ARM loans": st.column_config.NumberColumn(format=ui.AMOUNT),
            "ARM originations $mm": st.column_config.NumberColumn(format=ui.AMOUNT),
        },
    )
    mix = data.arm_mix(lenders["lei"].unique().to_list())
    if not mix.is_empty():
        # Columns in the order of the months to first reset, not of first appearance.
        order = mix.group_by("intro_bucket").agg(pl.col("sort_key").min()).sort("sort_key")
        pivot = (
            mix.with_columns((pl.col("arm_amount") / 1e6).round(1))
            .pivot(on="intro_bucket", index="orig_year", values="arm_amount")
            .sort("orig_year")
            .fill_null(0.0)
            .select("orig_year", *order["intro_bucket"])
        )
        st.markdown("**ARM originations by year and months to first reset ($mm)**")
        st.dataframe(
            pivot.rename({"orig_year": "Year"}),
            hide_index=True,
            width="stretch",
            column_config={"Year": st.column_config.NumberColumn(format="%d")},
        )

st.subheader("HMDA vs Call Report")
st.caption(
    "PLAN.md §7.3, a diagnostic and not a reconciliation. For each bank in the latest quarter: "
    "its HMDA lenders' retained ARMs whose first reset falls within the Call Report window, "
    "against its own buckets. HMDA should come in lower."
)
c1, c2 = st.columns(2)
scenarios = data.scenarios()["scenario"].to_list()
scenario = c1.selectbox(
    "CPR scenario", scenarios, index=scenarios.index("base") if "base" in scenarios else 0
)
window = c2.radio("Window", ["12 months", "3 years"], horizontal=True)
cr_col, hmda_col = (
    ("within_12m", "hmda_within_12m") if window == "12 months" else ("within_3y", "hmda_within_3y")
)
check = data.crosscheck(scenario).filter((pl.col(cr_col) > 0) & (pl.col(hmda_col) > 0))
if check.filter((pl.col(cr_col) >= FLOOR) & (pl.col(hmda_col) >= FLOOR)).is_empty():
    st.caption("No bank has both figures at $10k or more.")
else:
    # Amounts under $10k are rounding for a bank; they'd only stretch the log scale.
    shown = check.filter((pl.col(cr_col) >= FLOOR) & (pl.col(hmda_col) >= FLOOR))
    others = shown.filter(pl.col("rssd_id") != rssd)
    mine = shown.filter(pl.col("rssd_id") == rssd)
    scatter = go.Figure()
    scatter.add_scatter(
        x=(others[cr_col] / 1e6).to_list(),
        y=(others[hmda_col] / 1e6).to_list(),
        mode="markers",
        name="Other banks",
        text=others["name"].to_list(),
        marker={"color": t.muted, "size": 8, "line": {"color": t.surface, "width": 2}},
        hovertemplate="<b>%{text}</b><br>Call Report $%{x:,.1f}mm<br>HMDA $%{y:,.1f}mm"
        "<extra></extra>",
    )
    top = max(shown[cr_col].max(), shown[hmda_col].max()) / 1e6
    low = min(shown[cr_col].min(), shown[hmda_col].min()) / 1e6
    scatter.add_scatter(
        x=[low, top],
        y=[low, top],
        mode="lines",
        name="HMDA equals the Call Report",
        line={"color": t.secondary, "width": 1},
        hoverinfo="skip",
    )
    if not mine.is_empty():
        scatter.add_scatter(
            x=(mine[cr_col] / 1e6).to_list(),
            y=(mine[hmda_col] / 1e6).to_list(),
            # The legend names it; a label on the point would clip at the chart's edge.
            mode="markers",
            name=now["name"],
            text=[now["name"]],
            marker={"color": t.accent, "size": 12, "line": {"color": t.surface, "width": 2}},
            hovertemplate="<b>%{text}</b><br>Call Report $%{x:,.1f}mm<br>HMDA $%{y:,.1f}mm"
            "<extra></extra>",
        )
    ui.style(scatter, t, height=440)
    scatter.update_layout(hovermode="closest")
    scatter.update_xaxes(type="log", title={"text": f"Call Report, within {window} ($mm)"})
    scatter.update_yaxes(type="log", title={"text": f"HMDA first resets, within {window} ($mm)"})
    ui.show(scatter)
    ui.caption(
        "HMDA first-reset calendar (retained ARMs, lenders linked by the RSSD of their "
        "origination year) and FFIEC Call Reports",
        check["report_date"].max().isoformat(),
        "the Call Report buckets also hold fixed-rate loans near maturity, purchased loans, "
        "pre-2018 ARMs and ARMs past their first reset, so points above the line are the ones "
        f"to look at. Log scales; {len(check):,} banks have both figures, and the "
        f"{len(check) - len(shown):,} with either under $10k aren't plotted.",
    )
    ratio = "ratio_12m" if window == "12 months" else "ratio_3y"
    outliers = check.filter(pl.col(ratio) > 1).sort(ratio, descending=True)
    st.markdown(f"**Banks where HMDA exceeds the Call Report ({len(outliers)})**")
    st.dataframe(
        outliers.select(
            pl.col("name").alias("Bank"),
            pl.col("state").alias("State"),
            pl.col("rssd_id").alias("RSSD"),
            (pl.col(cr_col) / 1e6).round(1).alias("Call Report $mm"),
            (pl.col(hmda_col) / 1e6).round(1).alias("HMDA $mm"),
            pl.col(ratio).round(2).alias("HMDA ÷ Call Report"),
        ),
        hide_index=True,
        width="stretch",
    )
