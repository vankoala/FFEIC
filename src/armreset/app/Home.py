"""`armtool app` runs this: the dashboard's pages, with the reset calendar as the home page
(PLAN.md §8). The SQL console, page 5, arrives in phase 6."""

import streamlit as st

st.set_page_config(page_title="ARM reset exposure", layout="wide")

st.navigation(
    [
        st.Page("pages/1_Reset_calendar.py", title="Reset calendar", default=True),
        st.Page("pages/2_Bank_repricing.py", title="Bank repricing", url_path="bank-repricing"),
        st.Page("pages/3_Bank_drilldown.py", title="Bank drill-down", url_path="bank-drilldown"),
        st.Page("pages/4_HMDA_explorer.py", title="HMDA explorer", url_path="hmda-explorer"),
        st.Page("pages/6_Methodology.py", title="Methodology", url_path="methodology"),
    ]
).run()
