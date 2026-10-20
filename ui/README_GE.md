# New Era UI contract

ეს ფოლდერი შეგვიძლია შემდეგ ეტაპზე გამოვიყენოთ PyQt6 shell-ისთვის ან React/Streamlit dashboard-ისთვის.
UI-მ არ უნდა გაუშვას shell command-ები და არ უნდა წაიკითხოს MT5 credentials. ის უკავშირდება მხოლოდ
ლოკალურ API-ს.

## HTTP

- `GET /health` — API, safe mode და memory backend.
- `GET /bots/status` — registry და bot statuses.
- `GET /gates/latest` — TradingAgents/Kronos/combined gate.
- `POST /scan/manual` — research cycle-ის დაწყება.
- `POST /scan/automatic` — სრული fixed pipeline-ის დაწყება.
- `GET /scans` — ბოლო scan-ები.
- `GET /scans/{scan_id}` — ერთი scan-ის მდგომარეობა და bounded result.
- `GET /memory/search?q=...` — წარსული research cycles.

## WebSocket

```text
ws://127.0.0.1:8787/ws/logs
```

Event types:

- `process.log`
- `tradingagents.debate`
- `kronos.forecast`
- `gate.changed`
- `memory.stored`
- `scan.completed`

WebSocket მხოლოდ live stream-ია. თუ UI თავიდან შეუერთდა, ბოლო მდგომარეობა HTTP endpoint-ებიდან უნდა
წაიკითხოს.
