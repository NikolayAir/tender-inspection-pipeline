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

The test suite covers:

| Area | Tests |
| --- | --- |
| Extraction, evidence, and qualitative regression | [Smoke tests](../tests/test_smoke.py), [validation](../tests/test_validation.py) |
| Processing provenance and legacy restoration | [Provenance](../tests/test_provenance.py) |
| Atomic writes, recovery, and database access | [Transactions](../tests/test_pipeline_transactions.py), [sample recovery](../tests/test_sample_recovery.py), [connections](../tests/test_database_connections.py) |
| Reviewer decisions and interface behaviour | [Decision history](../tests/test_reviewer_decisions.py), [reviewer UI](../tests/test_reviewer_ui.py), [recovery UI](../tests/test_sample_recovery_ui.py), [review snapshots](../tests/test_review_snapshot.py) |
| Export contracts and stable output across runs | [Exports](../tests/test_exports.py), [reproducibility](../tests/test_reproducibility.py) |

These checks verify software behaviour. They do not establish statistical extraction accuracy or engineering correctness.

## Continuous integration

The [GitHub Actions workflow](../.github/workflows/ci.yml) runs on pull requests, pushes to `main`, and manual dispatch. Each Ubuntu job in the Python 3.11–3.13 matrix installs development dependencies, runs Ruff and compilation checks, processes the bundled samples, and runs pytest.

The CI pipeline command uses the default database in a fresh checkout. Generated databases are excluded from version control.
