"""An in-app guide to the existing browser and command-line workflows."""

import streamlit as st


@st.dialog("How it works", width="large")
def show_workflow_guide() -> None:
    """Explain the complete review path without leaving the current workspace."""
    st.write("Turn a public tender excerpt into a brief you can verify, review, and trace back to its source.")
    quick, pasted, method, cli = st.tabs(
        ["Quick start", "Pasted excerpts", "Behind the brief", "Command line"],
    )
    with quick:
        st.subheader("Explore the project in two minutes", anchor=False)
        st.markdown(
            "1. **Choose a bundled sample** in the sidebar. Start with a public "
            "Luxembourg excerpt, or choose the synthetic sample to explore information gaps.\n"
            "2. **Scan Overview.** See the technical scopes, flagged gaps, and suggested questions.\n"
            "3. **Open Review findings.** Compare the selected finding with its source "
            "on the left. Choose Accepted, Rejected, or Needs follow-up on the right, "
            "add an optional note, and select Save decision.\n"
            "4. **Download the JSON brief.** It includes the generated brief, source evidence, "
            "processing record, and all saved decision events."
        )
        st.info("Each save adds to the history. It never rewrites the generated finding or your earlier decisions.", icon=":material/history:")
        st.caption("Evidence & source shows the excerpt beside the evidence index. Run details holds versions, the source fingerprint, and qualitative validation.")
    with pasted:
        st.subheader("Try your own public excerpt", anchor=False)
        st.markdown(
            "1. Choose **Analyse a public excerpt** in the sidebar.\n"
            "2. Paste public, non-confidential text, up to **50,000 characters**. "
            "A title and source URL are optional; the URL is a reference and is not fetched.\n"
            "3. Select **Generate review brief**. The preview appears beside the input, "
            "with separate views for findings, evidence, questions, and source text.\n"
            "4. Edit the input and generate again to update it. "
            "The last generated preview remains available while you use the app."
        )
        st.info("Pasted previews stay in the current session. They have no saved reviewer decisions or JSON download.", icon=":material/visibility:")
    with method:
        st.subheader("Transparent rules, traceable results", anchor=False)
        st.markdown(
            "**01 · Prepare the text**\n\nNormalise whitespace and line endings.\n\n"
            "**02 · Match declared rules**\n\nLook for configured domain keywords and "
            "explicit missing-information phrases. Capture the matching source text.\n\n"
            "**03 · Build the brief**\n\nGroup technical scopes, review focus areas, "
            "gaps, questions, and evidence. No external model or API is called.\n\n"
            "**04 · Let a human decide**\n\nSave decisions against a specific brief, "
            "keeping the generated output and decision history separately."
        )
        st.caption("Bundled results are stored in SQLite. Completed samples are reused on later visits; explicit reprocessing creates a new brief with a separate review history.")
        st.warning("Keyword matches do not interpret context. Confidence is fixed at Low, locations are approximate, and the three-sample validation set makes no statistical accuracy claim. The output supports human technical review, not legal, regulatory, compliance, safety, or engineering decisions.")
    with cli:
        st.subheader("Run the same pipeline locally", anchor=False)
        st.caption("From the repository root, after installing the dependencies:")
        st.code("python -m src.pipeline --db /tmp/tender-review-demo.db", language="bash")
        st.write("This processes the bundled samples into a separate local database.")
        st.code(
            "python -m src.pipeline\n"
            "python -m streamlit run src/app/streamlit_app.py",
            language="bash",
        )
        st.caption("The first command explicitly reprocesses samples in the app's default database. It appends new briefs; earlier decisions remain attached to their earlier briefs. The second command starts the interface.")
        st.link_button("Read the development guide", "https://github.com/NikolayAir/tender-inspection-pipeline/blob/main/docs/development.md", icon=":material/open_in_new:")
