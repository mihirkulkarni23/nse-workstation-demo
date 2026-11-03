# NSE workstation — read-only demo

A self-contained Streamlit demo with the nine navigation sections from the reviewed dashboard. This is a new demo implementation, not the original trading application with simulated broker access. Original source files are not included.

**Every signal, quote, position, candle, review and performance result is synthetic.** The fixture is a fixed example from 2 November 2026, not a live or backtested result. Reloading does not advance it. This app cannot place orders or modify positions, reviews or equity files.

## Run locally

Use Python 3.12 and the pinned requirements for deployment. All six tests passed on Ubuntu/Python 3.12 in GitHub Actions and locally on Python 3.14.0. The local server also returned HTTP 200 from its health endpoint. Streamlit Community Cloud deployment and browser verification remain separate steps.

Public repository (code, documentation and synthetic fixtures only): [mihirkulkarni23/nse-workstation-demo](https://github.com/mihirkulkarni23/nse-workstation-demo). [Initial Linux test results](https://github.com/mihirkulkarni23/nse-workstation-demo/actions/runs/41436038874).

```text
python -m pip install -r requirements.txt
python -m streamlit run Ldashboard_V9.py
```

No secrets, account, Google Drive connection, broker login, or database is needed to run the demo.

## Deploy to Streamlit Community Cloud

1. Put this directory's contents at the root of a dedicated private GitHub repository.
2. In Streamlit Community Cloud, choose **Create app → Deploy from GitHub** (wording may vary).
3. Select that repository, branch `main`, and entry point `Ldashboard_V9.py`.
4. In Advanced settings select **Python 3.12**. Leave Secrets empty for this demo.
5. Deploy and verify all nine sections. Keep viewing restricted to specific people; if another private app already occupies the account's private-app slot, resolve that before continuing. Do not make a real-data deployment public by default.

## What is included

- Three signal workspaces: filters, earlier/latest signal windows, detail charts and illustrative historical metrics.
- Historical signal/trade explorer with synthetic CSV export.
- Portfolio with consistent long/short MTM calculations and chart prices.
- Journal, equity/performance analytics and read-only review archive.
- System Health showing demo validation separately from live-source status.
- Shared global metrics across all sections and explicit demo labels throughout.

## Tests

```text
python -m unittest discover -s tests -v
```

Tests cover all nine views, signal/history filters, reload, snapshot immutability, accounting, malformed data, reference integrity and prohibited writer paths. Streamlit's AppTest comes with Streamlit; there is no separate test package.

## Real data later

`dashboard_data.load_snapshot()` is the single data-loading boundary. The UI consumes a schema-versioned snapshot; the included `data/demo_snapshot.json` demonstrates it. Keep producer credentials and trading code on the laptop.

The demo validator still rejects any mode other than `demo`, preventing an accidental real-data upload from being shown as a demo. Local real-data viewing now uses the separate `uploaded_snapshot` mode described below. Authenticated Google Drive reading of manually uploaded, manifest-verified generations is available in `drive_snapshot` mode. It has been tested with synthetic data only, and no real data should be uploaded until the hosted app's viewer access is restricted. A later update must add explicit per-dataset source freshness, a tested production mode, appropriate access controls, and the additional real-data fields required by the original dashboard. Publish equity, MTM and research results on the laptop; do not reintroduce cloud write callbacks. Preserve the original audited nine-section application separately while deciding which detailed original panels to reintegrate.

Never commit actual account data, broker credentials, OAuth tokens, `.env` or `.streamlit/secrets.toml`. The demo needs none of them.

## Data modes

The mode is chosen explicitly with environment variables. Unset means `demo`, so existing deployments are unchanged.

| `NSE_DATA_MODE` | Source | Notes |
|---|---|---|
| unset or `demo` | `data/demo_snapshot.json` | Synthetic fixture; every view is labelled DEMO DATA |
| `uploaded_snapshot` | `NSE_SNAPSHOT_PATH`: a snapshot `.sqlite` file, or a folder whose newest completed `snapshots/<generation>` is used | Opened read-only. Seven sections show supplied records; Historical Cache and Trade Reviews state that their original data was not supplied |
| `drive_snapshot` | A private Google Drive folder, read with a Viewer-only service account configured in Streamlit Secrets | Same views. Only a generation whose size, SHA-256 and contents match `current_manifest.json` is activated; on a later failure it stays visible marked STALE, otherwise the page shows unavailable. See [docs/drive_setup.md](docs/drive_setup.md) |

Any other mode value, a missing path, or an unreadable snapshot shows an error page. The app never falls back to demo data. A section whose source table fails reconciliation is shown as invalid while the other sections still work. Run real-data mode on `--server.address localhost` so it is not reachable from other machines.

```text
set NSE_DATA_MODE=uploaded_snapshot
set NSE_SNAPSHOT_PATH=<private folder outside Git>
python -m streamlit run Ldashboard_V9.py --server.address localhost --server.port 8503
```

## Private snapshot importer (laptop only)

`producer/import_csv.py` reads the six application CSVs (`scanner_30m_signals`, `hourly_signal_scan`, `daily_signal_scan`, `open_positions`, `closed_positions`, `daily_equity`) and builds a new, versioned SQLite generation in a private folder. It uses only the Python standard library, never modifies or imports the original trading code, and refuses to write inside a Git working tree. The hosted app does not import it.

```text
python -m producer.import_csv --source-dir <folder with the six CSVs> --output-dir <private folder outside Git>
```

To publish a generation, export it, then upload the two resulting files by hand to the private Drive folder, following [docs/drive_setup.md](docs/drive_setup.md). `python -m producer.synthetic --output-dir <folder>` builds and exports a generation labelled `synthetic_test` from made-up records, for testing the Drive path.

```text
python -m producer.export_snapshot --private-dir <private folder outside Git>
```

The export copies the generation with SQLite's backup API into `publish/<generation_id>/nse_snapshot_<generation_id>.sqlite`, validates the copy, and writes `current_manifest.json` with the schema, generation, file size and SHA-256, row counts, source times and quality totals. Exports are never overwritten. The Drive reader (`drive_reader.py`) requests only the `drive.readonly` scope and makes GET requests only.

Each import run writes `snapshots/<generation_id>/dashboard_snapshot.sqlite` plus `validation_report.json` and `validation_report.txt`. A failed run (missing file or column, source change during import, failed reconciliation) leaves no generation behind. Rows keep their source row number and raw strings; incomplete time fragments stay NULL, timezones are marked unverified, invalid stops are flagged rather than repaired, and `daily_equity.total_equity` is stored as recorded cumulative P&L. The tests in `tests/test_importer.py` use synthetic fixtures only.
