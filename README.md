# FoundF framework

FoundF is a Python/DuckDB framework for a long-running personal investment research system. It includes data-provider contracts, a local warehouse, factor research, walk-forward governance, portfolio/risk analysis, a read-only dashboard, and fail-closed simulated-phone execution tooling.

## Public snapshot boundary

This repository is a sanitized framework snapshot. It intentionally excludes:

- databases, parquet files, reports, screenshots, UI dumps, logs, backups, and runtime state;
- portfolio holdings, account records, transaction records, device identifiers, and local network addresses;
- `.env`, `.secrets`, credentials, passwords, tokens, and production configuration;
- real-broker login, account capture, and live-order automation;
- private project memory, operational handoff notes, and Git history.

Only example configuration is included. Copy examples locally and keep real values outside Git.

For AI-assisted changes, start with [`AGENTS.md`](AGENTS.md). Development routing is documented in [`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md), and credential application/configuration is documented in [`docs/DATA_SOURCE_SETUP.md`](docs/DATA_SOURCE_SETUP.md).

## Architecture

```text
foundf_db
  -> data_provider
  -> factor_engine / quant_strategy / risk_engine
  -> portfolio_manager
  -> portfolio_ai / strategy_manager
```

The project prioritizes data reliability, portfolio analysis, risk control, and reproducible strategy evidence. Missing or stale inputs should fail closed instead of being filled with guessed values.

## Quick start

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
docker compose up -d --build api
```

The default dashboard is available at `http://localhost:8000`. Do not expose it outside a trusted network without authentication and HTTPS.

## Phone simulation tooling

`deploy/phone/` contains the simulated-account capture and execution framework plus the `phone_client.py` adapter. Set the local ctlphone checkout explicitly:

```bash
export PHONE_CTL_HOME=/path/to/ctlphone
export FOUNDF_ADB_SERIAL=your-adb-device-serial
python3 deploy/phone/phone_client.py devices
```

The included flow is for clearly identified simulated trading pages. Real-broker login and live trading are outside this snapshot.

## Data and secrets

Runtime directories are ignored by Git. Keep all credentials in local environment variables or permission-restricted secret files. Never commit real holdings, broker exports, screenshots, UI trees, or generated reports.

No download or crawling Token is bundled. Each operator must obtain credentials from the provider's official channel, verify the applicable license and quota, and configure them only in the local `.env` file.

See [PROJECT_VISION.md](docs/PROJECT_VISION.md) for the architectural goals and [docs/README.md](docs/README.md) for the public file map.
