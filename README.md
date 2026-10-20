# New Era Trading Bot

This repository contains the New Era research/demo trading workspace. The
root service is a FastAPI backend; the existing PyQt6 desktop terminal and
MetaTrader 5 bridge remain Windows-oriented integrations.

The API is intentionally safe-mode/research-first. Keep provider credentials
out of source control and pass them through the environment only when a
research provider is required.

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
