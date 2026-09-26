"""Page 6: every assumption the tool makes (docs/methodology.md, PLAN.md §8)."""

from pathlib import Path

import streamlit as st

from armreset.app import data

# Next to config.yaml, or else in the checkout this package runs from.
candidates = [
    data.settings().root / "docs" / "methodology.md",
    Path(__file__).resolve().parents[4] / "docs" / "methodology.md",
]
path = next((p for p in candidates if p.is_file()), None)
if path is None:
    st.warning("docs/methodology.md isn't next to config.yaml or in this checkout.")
    st.stop()
st.markdown(path.read_text(encoding="utf-8"))
st.caption(
    "What was checked against real files, and when, is in docs/verification.md; every table "
    "and view is in docs/data_dictionary.md."
)
