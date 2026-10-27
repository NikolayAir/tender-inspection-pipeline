"""An in-app guide to the existing browser and command-line workflows."""

import streamlit as st


@st.dialog("How it works", width="large")
def show_workflow_guide() -> None:
    """Explain the complete review path without leaving the current workspace."""
    with st.container(key="workflow_guide_intro"):
        st.write("Check suggested review topics against the source text, then record your decisions on a bundled sample.")
    quick, pasted, method, cli = st.tabs(
        ["Quick start", "Pasted excerpts", "How findings are generated", "Command line"],
    )
    with quick:
        st.subheader("Review a bundled sample", anchor=False)
        st.markdown(
            "1. **Choose a document** under Bundled samples in the sidebar. These examples "
            "are provided with the app: two public Luxembourg excerpts and one "
            "synthetic sample, which is a made-up example for testing.\n"
            "2. **Scan Overview.** See the construction topics found by the keyword rules, "
            "phrases suggesting missing information, and questions to check.\n"
            "3. **Open Review findings.** Compare one suggested finding with the quoted source text. "
            "Choose Accepted, Rejected, or Needs follow-up in the decision panel, "
            "add an optional note, and select Save decision.\n"
            "4. **Download review brief (JSON).** This structured data file contains the "
            "generated brief, supporting text, processing details and saved decision history. "
            "Unsaved decisions and notes are not included."
        )
        st.info("Each saved decision adds a history entry and keeps the generated finding unchanged. Earlier decisions and notes remain available under Decision history.", icon=":material/history:")
        st.caption("Source snippets are passages quoted from the input. Evidence & source shows them beside the original excerpt. Run details explains how the saved brief was produced and shows the checks on the bundled samples.")
    with pasted:
        st.subheader("Try your own public excerpt", anchor=False)
        st.markdown(
            "1. Choose **Analyse a public excerpt** in the sidebar.\n"
            "2. Paste public, non-confidential text, up to **50,000 characters**. "
            "A title and source URL are optional. The app shows the URL as a reference; "
            "it does not open or download the page.\n"
            "3. Select **Generate review brief**. The preview contains separate views "
            "for findings, evidence, questions, and source text.\n"
            "4. **Generate again after editing** to update the preview. Until then, "
            "the previous preview stays visible. Opening help or switching between "
            "Review workspace and Analyse a public excerpt keeps your draft and preview."
        )
        st.info("The draft and preview are temporary. Reloading this browser tab clears them. Pasted previews have no saved reviewer decisions or JSON download.", icon=":material/visibility:")
        st.caption("In the hosted app, pasted text is sent to the application server for processing. This preview is not written to the app's database. Use public, non-confidential text only.")
    with method:
        st.subheader("How the keyword rules work", anchor=False)
        st.markdown(
            "**01 · Clean up the text**\n\nRemove repeated spaces and standardise line breaks.\n\n"
            "**02 · Look for keywords and phrases**\n\nCheck predefined keywords, such as "
            "those for fire safety or ventilation, and phrases about missing information. "
            "Matches ignore capital letters. A keyword can also match part of a longer word.\n\n"
            "**03 · Suggest checks and show the matching text**\n\nGroup possible review "
            "topics, information gaps and follow-up questions. Quote the passages behind "
            "the matches. Approximate line numbers refer to the cleaned excerpt shown in the app.\n\n"
            "**04 · Record the reviewer's decisions**\n\nFor bundled samples, a reviewer can "
            "save a decision on each finding. The app keeps the generated brief unchanged "
            "and adds each decision to its history."
        )
        st.write("The app uses fixed keyword rules. It does not use a large language model (LLM) or call external AI services to generate findings.")
        st.caption("Bundled results are saved in the app's database and reused on later visits. Reprocessing creates a new brief; earlier decisions stay with the earlier brief.")
        st.warning("A keyword match suggests what to check; it does not confirm a defect. A phrase about missing information applies to the excerpt and may not describe the full tender. The app does not make engineering, safety, legal, regulatory or compliance decisions.")
        st.caption("Confidence is always labelled Low. It is a fixed label, not an accuracy score. Checks on the three bundled samples do not show how accurately the rules will work on other documents.")
    with cli:
        st.subheader("Run the bundled samples from a terminal", anchor=False)
        st.caption("Run these commands from the project folder after installing its Python dependencies:")
        st.code("python -m src.pipeline --db data/processed/tender-review-demo.db", language="bash")
        st.write("This saves results in a separate database file, leaving the app's usual database unchanged.")
        st.code(
            "python -m src.pipeline\n"
            "python -m streamlit run src/app/streamlit_app.py",
            language="bash",
        )
        st.caption("Processing again adds new briefs to the app's database. Earlier decisions stay with their original briefs. Streamlit starts the web interface.")
        st.link_button("Read the development guide", "https://github.com/NikolayAir/tender-inspection-pipeline/blob/main/docs/development.md", icon=":material/open_in_new:")
