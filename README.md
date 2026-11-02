# NSE workstation — read-only demo

A self-contained Streamlit demo with the nine navigation sections from the reviewed dashboard. This is a new demo implementation, not the original trading application with simulated broker access. Original source files are not included.

**Every signal, quote, position, candle, review and performance result is synthetic.** The fixture is a fixed example from 2 November 2026, not a live or backtested result. Reloading does not advance it. This app cannot place orders or modify positions, reviews or equity files.

## Run locally

Use Python 3.12 and the pinned requirements for deployment. It was initially tested locally with Python 3.14.0 and those exact package versions; Cloud verification is a separate step.

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

Real-data support is intentionally not enabled by this release. The validator rejects any mode other than `demo`, preventing an accidental real-data upload from being shown as a demo. A later update must add authenticated Google Drive downloads, consistent manifest generations, schema validation, explicit source freshness, a tested production mode, appropriate access controls, and the additional real-data fields required by the original dashboard. Publish equity, MTM and research results on the laptop; do not reintroduce cloud write callbacks. Preserve the original audited nine-section application separately while deciding which detailed original panels to reintegrate.

Never commit actual account data, broker credentials, OAuth tokens, `.env` or `.streamlit/secrets.toml`. The demo needs none of them.
