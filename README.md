# New Era Trading Bot

This repository contains the New Era research/demo trading workspace. The
root service is a FastAPI backend; the existing PyQt6 desktop terminal and
MetaTrader 5 bridge remain Windows-oriented integrations.

The upstream research engines are pinned as Git submodules. Clone the full
workspace with:

```bash
git clone --recurse-submodules <repository-url> NewEraTradingBot
cd NewEraTradingBot
```

For an existing checkout, initialize them with:

```bash
git submodule update --init --recursive
```

The API is intentionally safe-mode/research-first. Keep provider credentials
out of source control and pass them through the environment only when a
research provider is required.

## JoeyCIO execution profile

Autonomous Demo execution is allowlisted to **XAUUSD**, **USTEC**, and
**US500**. Other scanned symbols remain in **SHADOW** mode: the waterfall is
evaluated and a virtual signal is logged, but no order is sent. The profile
also enforces:

- Step 2 qualified **2-of-3** Matrix/Analogue/Forecast confluence with a
  non-contradictory TA-Lib trend;
- minimum reward:risk **1.5**, maximum stop distance **2.5 ATR**, and maximum
  estimated risk **1.5% of equity**;
- one open position per symbol, two open positions account-wide, and a
  60-second per-symbol execution lock;
- dynamic break-even/trailing management and a **120-minute** time-stop;
- order comments such as `JoeyCIO_USTEC_2of3` for post-trade profiling.

All of these are enforced again at the MT5 bridge boundary. Paper fallback is
still opt-in and disabled by default.

## Phase 2 telemetry and runtime controls

Every scan/screener decision is persisted to
`logs/decision_history.json` and published as a `trade.decision` event on
`/ws/logs`. Records include the signal, each waterfall layer's `PASS`,
`BLOCKED`, or `BYPASSED` status, the human-readable veto reason, and whether
the outcome was `LIVE`, `SHADOW`, or `BLOCKED`.

The desktop terminal exposes four Phase 2 tabs:

1. **Live Execution & Interactive Chart**
2. **Quant Gate & Confluence Control**
3. **Bot & Strategy Fleet Manager**
4. **Live Decision Audit & Analytics**

The control API is available locally at:

- `GET /controls` and `POST /controls`
- `GET /decision-history?limit=100`
- `GET /decision-history/summary`
- `GET /bots/status`
- `POST /bots`
- `POST /bots/{bot_id}/enabled`
- `DELETE /bots/{bot_id}`

The UI accepts the requested operator ranges (`risk_pct` 0.5–5.0,
`min_rr` 1.0–4.0, `max_sl_atr` 0.5–5.0, and `time_stop_minutes` 30–240).
The API returns both the requested controls and `effective_controls`; the
protected demo execution path still clamps risk to 1.5%, reward:risk to at
least 1.5, stop distance to 2.5 ATR, and time-stop to at most 120 minutes.
Disabling Kronos or L2 is recorded as an explicit `BYPASSED` layer rather than
being misreported as a green signal.

## Phase 3 native desktop workspace and Bot Strategy Lab

The PyQt6 terminal now has a native MT5/VeighNa-style workspace:

- `File`, `View`, `Insert`, `Strategy Tools`, `Bot Lab`, and `Help` menus;
- floatable, movable, closable `QDockWidget` panels for Live Chart/Execution,
  L2 Orderbook, Decision Audit, Quant Controls, Bot Fleet, and Bot Strategy Lab;
- QSettings workspace geometry/state persistence through **Save Workspace
  Layout** and **Restore Workspace Layout**;
- Bloomberg Dark/Cyber Matrix status-bar telemetry for API health, WebSocket
  latency estimate, active bot count, and MT5 connection health;
- JSON/CSV audit telemetry export without including provider credentials.

The Strategy Lab is research-only. It supports visual JSON rule flows, local
Python strategy protocol validation (`on_bar(bar, context)`), deterministic
backtests, walk-forward train/validation splits, and workspace-local dynamic
loading. Strategy profiles and the loader always expose:

```text
research_only = true
real_account_allowed = false
order_execution_allowed = false
```

Strategy Lab endpoints:

- `GET /strategies`
- `GET /strategies/{strategy_id}`
- `POST /strategies` and `PUT /strategies/{strategy_id}`
- `POST /strategies/backtest`
- `POST /strategies/walk-forward`
- `POST /strategies/load`

Python strategy modules and OHLCV data must resolve inside the workspace; paths
outside the workspace are rejected before loading or backtesting.

## Runtime and dependencies

- Python **3.13.5**, pinned in `.python-version`.
- Full local API/desktop requirements: `requirements_api.txt`.
- Current constraints snapshot: `requirements_api.lock.txt`.
- Linux API-only requirements used by Docker: `requirements_api_linux.txt`.

The lock file is a pip constraints snapshot captured from the existing local
`.venv-api` environment. It pins the packages present there; conditional
ChromaDB and the Windows-only MetaTrader5 package are handled by their
platform/Python markers. Refresh the snapshot intentionally when changing
requirements or the pinned runtime.

## Windows setup and run

From the repository root in PowerShell:

```powershell
py -3.13 -m venv .venv-api
& .\.venv-api\Scripts\python.exe -m pip install -r requirements_api.txt -c requirements_api.lock.txt
if (-not (Test-Path .env)) { Copy-Item api.env.example .env }
```

Start the API:

```powershell
& .\.venv-api\Scripts\python.exe -m uvicorn main_api:app --host 127.0.0.1 --port 8787
```

Open <http://127.0.0.1:8787/docs>. Useful checks are:

```powershell
Invoke-RestMethod http://127.0.0.1:8787/health
Invoke-RestMethod http://127.0.0.1:8787/bots/status
```

The existing launcher is also available:

```powershell
.\start_newera_api.ps1 -PythonPath ".\.venv-api\Scripts\python.exe"
```

To launch the existing PyQt6 desktop terminal after the API is running:

```powershell
.\run_desktop_ui.ps1 -PythonPath ".\.venv-api\Scripts\python.exe" -NoAutoStartApi
```

MetaTrader5 support requires a Windows terminal and a supported Python bridge
environment. The API can still start without a connected MT5 terminal; MT5
endpoints report the unavailable bridge rather than making Linux assumptions.

## Tests

The single offline test command is:

```text
make test
```

It runs the repository's standard-library `unittest` discovery without
network calls. On Windows, the Makefile automatically uses
`.venv-api\Scripts\python.exe` when that environment exists. Otherwise,
activate the project environment first or pass an interpreter explicitly:

```text
make test PYTHON=.venv/bin/python
```

The equivalent direct command is:

```text
python -m unittest discover -s . -p "test_*.py"
```

## Linux Docker API

The root `Dockerfile` builds only the API-side service:

```bash
docker build -t newera-api:local -f Dockerfile .
docker run --rm --name newera-api \
  -p 8787:8787 \
  --env-file api.env.example \
  -e NEWERA_CHROMA_ENABLED=false \
  newera-api:local
```

Then open <http://127.0.0.1:8787/docs> or query `/health`.

The image does not contain PyQt6, MetaTrader5, ChromaDB, provider keys, or
local research outputs. Linux Docker therefore provides the FastAPI surface
and the JSONL memory fallback; it does not provide the full desktop GUI, a
Windows MT5 terminal connection, or ChromaDB persistence. Mount a host volume
at `/app/TradingAgentsData` if API runtime data should survive container
recreation.

The downloaded model/cache directory `KronosData/` is intentionally ignored;
recreate it with the Kronos research workflow when needed rather than putting
large model artifacts in Git.

For a deployment run using a private environment file, keep that file outside
the image and outside Git:

```bash
docker run -d --restart unless-stopped --name newera-api \
  -p 8787:8787 \
  --env-file .env \
  -e NEWERA_API_HOST=0.0.0.0 \
  -e NEWERA_SAFE_MODE=true \
  -v newera-data:/app/TradingAgentsData \
  newera-api:local
```

No Docker build or deployment step should receive credentials through a
`Dockerfile` `ARG`, `ENV`, or `COPY` instruction.

## Existing trading documentation

The detailed Georgian MT5, TradingView, TradingAgents, and Kronos operating
notes remain in [`README_GE.md`](README_GE.md). This reproducibility layer does
not change trading logic or delete existing research/data files.
