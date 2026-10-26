"""One-shot navigation effects requested by explicit user actions."""

from pathlib import Path
from typing import Literal
from uuid import uuid4

import streamlit as st

ScrollTarget = Literal["review", "preview"]
TARGET_SELECTORS: dict[ScrollTarget, str] = {
    "review": ".st-key-brief_tabs",
    "preview": ".st-key-preview_result",
}


def request_scroll(target: ScrollTarget) -> None:
    """Schedule a single reveal after the destination has been rendered."""
    # A fresh ID lets another explicit click reveal the same destination again.
    st.session_state["_pending_scroll"] = (target, uuid4().hex)


def render_pending_scroll() -> None:
    """Execute only trusted local code, with selectors from a fixed allowlist."""
    # Consume the request so ordinary editing reruns do not replay the reveal.
    pending = st.session_state.pop("_pending_scroll", None)
    if pending is None:
        return
    target, request_id = pending
    script = Path(__file__).with_name("scroll.js").read_text(encoding="utf-8")
    script = script.replace("__TARGET_SELECTOR__", TARGET_SELECTORS[target]).replace("__REQUEST_ID__", request_id)
    with st.container(key="scroll_effect"):
        st.html(f"<script>{script}</script>", unsafe_allow_javascript=True)
