# Development

[Project overview and quick start](../README.md#run-locally) · [Architecture and data contracts](architecture.md) · [Data and validation](data-and-validation.md)

Run commands from the repository root. CI covers Python 3.11, 3.12, and 3.13. Runtime dependencies are pinned in [`requirements.txt`](../requirements.txt); [`requirements-dev.txt`](../requirements-dev.txt) includes them along with pytest and Ruff.

## Environment setup

Follow the [quick start](../README.md#run-locally) to create the virtual environment. For development, activate it and install:

```bash
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
```

On Windows PowerShell, create and activate the environment with:

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
```

## Pipeline commands

The application automatically recovers missing or incomplete bundled results. To explicitly process all bundled samples:

```bash
python -m src.pipeline
```

To process one sample:

```bash
python -m src.pipeline \
  --sample data/samples/synthetic_sample_tender_001.txt
```

Both commands use `data/processed/tender_inspection.db`. An explicit run refreshes the current source record and appends new processing runs and briefs, even for completed samples. Earlier reviewer decisions stay attached to the earlier briefs; new briefs start without decisions.

Use `--db` to select a separate database. For an isolated pipeline smoke check on macOS or Linux:

```bash
smoke_dir="$(mktemp -d)"
python -m src.pipeline --db "$smoke_dir/tender_inspection.db"
```

On Windows, pass a separate path with `--db`, for example `--db "$env:TEMP\tender-inspection-smoke.db"`.

Start the interface with:

```bash
python -m streamlit run src/app/streamlit_app.py
```

The Streamlit interface uses the default database path. `--db` applies to the pipeline command. See [architecture](architecture.md#persisted-records) for document identity, transactions, migrations, and retained history.

## Local verification

With development dependencies installed:

```bash
python -m ruff check .
python -m compileall -q src tests
python -m pytest -q
git diff --check
```

Run the isolated pipeline smoke check above to exercise the bundled command-line workflow. Tests use temporary databases.

For browser checks that save decisions or simulate damaged data, use a disposable checkout and run the app from that checkout's root. The app uses that copy's default database, keeping these checks separate from your normal review history.

The test suite covers:

| Area | Tests |
| --- | --- |
| Extraction, evidence, and qualitative regression | [Smoke tests](../tests/test_smoke.py), [validation](../tests/test_validation.py) |
| Processing provenance and legacy restoration | [Provenance](../tests/test_provenance.py) |
| Atomic writes, recovery, and database access | [Transactions](../tests/test_pipeline_transactions.py), [sample recovery](../tests/test_sample_recovery.py), [connections](../tests/test_database_connections.py) |
| Reviewer decisions and interface behaviour | [Decision history](../tests/test_reviewer_decisions.py), [reviewer UI](../tests/test_reviewer_ui.py), [recovery UI](../tests/test_sample_recovery_ui.py), [review snapshots](../tests/test_review_snapshot.py) |
| Focused workspace and session previews | [Review queues and source selection](../tests/test_review_workspace.py), [preview persistence and storage isolation](../tests/test_excerpt_workspace.py) |
| Export contracts and stable output across runs | [Exports](../tests/test_exports.py), [reproducibility](../tests/test_reproducibility.py) |

These checks verify software behaviour. They do not establish statistical extraction accuracy or engineering correctness.

For UI changes, also inspect the app in a browser at desktop and phone widths, including a smaller desktop window with the sidebar open and closed. Check tab navigation, full source snippets, saving a decision under a status filter, retrying a failed save, downloading with an unfinished note, and opening and closing **How it works**. In the excerpt workspace, check that unsubmitted drafts survive workflow navigation, that a changed draft remains distinct from its previous preview, and that blank titles and unmatched text receive clear feedback. AppTest verifies one-shot reveal requests and their absence during saves or invalid submissions; actual scrolling, focus, responsive layout, and reduced-motion behaviour require browser verification.

UI presentation lives in `src/app/streamlit_app.py`, with pure finding and evidence helpers in `src/app/review.py`, the guide in `src/app/guide.py`, and one-shot reveal effects in `src/app/scroll.py` and `scroll.js`. Theme colours use `.streamlit/config.toml`; `styles.css` scopes layout refinements to named containers, and `navigation.css` gives the native sidebar opener its visible **Menu & samples** label. The reveal script is trusted local code and receives only fixed target selectors, never source text or metadata.

## Continuous integration

The [GitHub Actions workflow](../.github/workflows/ci.yml) runs on pull requests, pushes to `main`, and manual dispatch. Each Ubuntu job in the Python 3.11–3.13 matrix installs development dependencies, runs Ruff and compilation checks, processes the bundled samples, and runs pytest.

The CI pipeline command uses the default database in a fresh checkout. Generated databases are excluded from version control.
