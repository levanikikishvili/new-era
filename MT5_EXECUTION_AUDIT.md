# New Era MT5 Execution Audit

Audit date: **2026-11-03** (read-only terminal inspection)

## Finding summary

The missing live trades are not currently attributable to a broker rejection.
The evidence points to a combination of signal-path separation, no current
four-vote setup, and the absence of a current live New Era EA log.

### Confirmed current-state findings

1. The connected terminal is an ICMarkets demo environment. Read-only checks
   showed the terminal connected, automated trading enabled, expert trading
   enabled, `XAUUSD` selected, full symbol trading mode, no open XAUUSD
   positions, and a current spread observed between 5 and 11 points during the
   read-only checks.
2. The current closed-bar snapshot is:

   ```text
   H4=SELL, H1=BUY, M15=FLAT, M5=FLAT
   ```

   `NewEraCouncilDemo.mq5` requires four votes. Therefore the current result is
   correctly `NO SIGNAL` (one BUY vote and one SELL vote); the audit does not
   loosen this filter.
3. `Common/Files/new_era_council_demo_log.csv` contains a historical tester run
   from 2026-10-01 through 2026-10-15. It contains 39 `COUNCIL_SIGNAL` and 39
   `ORDER_SENT` records with retcode `10009`, and no `ORDER_FAILED` records. It
   is not evidence that the EA is running on the current live chart.
4. The current terminal log shows another XAUUSD EA, while no current New Era
   EA runtime entries were found. The New Era compiled artifact in the terminal
   is older than the current source. The EA must be compiled and attached to an
   XAUUSD M5 chart after this update.
5. The Python research layer is not an order sender. `run_tradingagents_research.py`
   always writes `execution_allowed=false`; `combine_research_gates.py` only
   writes a research artifact. The EA independently evaluates its deterministic
   votes and only optionally reads a CSV gate when an AI-gated preset is loaded.
6. The checked-in AI gate is expired as of the audit date. The combined report
   is `ABSTAIN` because Kronos has only `1-of-3` confluence. An AI-gated EA
   loaded with that artifact must remain blocked. The MT5 Common `Files`
   directory also contained no current `new_era_ai_gate.csv` or
   `new_era_kronos_matrix_gate.csv`; the AI-gated preset therefore has no valid
   gate to read until the explicit sync step is completed.

## Confirmed defects repaired

### `NewEraCouncilDemo.mq5`

- `CTrade::Buy`/`Sell` now require both the local return value and a successful
  `ResultRetcode()`. The log includes retcode, retcode description, order, and
  deal identifiers.
- Broker rejection and successful execution are separate states:
  `BROKER REJECTED ORDER` and `ORDER SUCCESSFULLY EXECUTED`.
- The EA is permanently demo-only. `AllowRealAccount=true` is rejected, and
  every non-demo account is rejected during initialization.
- The EA now rejects attachment to a non-M5 chart instead of silently using a
  different chart's tick cadence for the M5 signal clock.
- Added permission checks for terminal, MQL, account, expert, symbol trade
  mode, market-order support, and market-execution filling configuration.
- Added normalized volume, free-margin, stop/freeze distance, price geometry,
  R:R, ATR, and risk validation before the order request.
- The four-vote minimum is enforced even if an unsafe preset tries to lower it.
- Added a chart diagnostic panel and one structured CSV record per completed
  M5 candle. The panel/log distinguishes:

  ```text
  NO SIGNAL
  SIGNAL BLOCKED BY FILTER
  SIGNAL STALE
  AI GATE ABSTAIN
  ORDER VALIDATION FAILED
  BROKER REJECTED ORDER
  ORDER SUCCESSFULLY EXECUTED
  ```

- Position-management `PositionModify`/`PositionClose` calls now also record
  their result retcodes and descriptions.

### Presets and strategy metadata

The following files now use the hard minimum `RewardRisk=1.50` and include a
diagnostic CSV filename:

- `NewEraCouncilDemo.set`
- `NewEraCouncilDemo_AI_Gated.set`
- `NewEraKronosMatrixScalper.set`
- `configs/new-era-gold-council-v1.json`
- `configs/new-era-kronos-matrix-v1.json`
- `NewEraCouncilDemo.pine`

## Validation performed

- MetaEditor compile of the updated EA: **0 errors, 0 warnings**.
- Read-only MT5 telemetry and symbol/rate inspection: **passed**.
- No order-placement call was made during the audit.
- Python unit tests and static EA contract tests: **92 passed**.
- Python bytecode compilation and `git diff --check`: **passed**.
- A live DEMO order was not manufactured when no valid signal exists.

## MT5 update instructions

1. In MT5, open **File → Open Data Folder**.
2. Copy the updated `NewEraCouncilDemo.mq5` into `MQL5\Experts`.
3. Open it in MetaEditor and press **F7**. Require `0 errors, 0 warnings`.
4. Refresh Navigator and attach the EA to the broker's exact `XAUUSD` **M5**
   chart.
5. Load `NewEraCouncilDemo.set` first. Verify:

   ```text
   AllowRealAccount=false
   EnableExternalAIGate=false
   MinimumVotes=4
   RewardRisk=1.50
   RiskPercent=0.50
   MaximumRiskPercent=1.00
   ```

6. Enable MT5 **Algo Trading** on the demo account only. The chart panel must
   show `NO SIGNAL`, `SIGNAL BLOCKED BY FILTER`, or a later execution state;
   the absence of a panel/update means the EA is not attached or not receiving
   ticks.
7. Inspect the common files directory:

   ```text
   %APPDATA%\MetaQuotes\Terminal\Common\Files\
   ```

   The readable files are `new_era_council_demo_log.csv` and
   `new_era_council_demo_diagnostics.csv`.

## Optional AI-gated mode

Use this only after deterministic EA forward visibility is confirmed:

1. Generate a fresh research gate.
2. Validate and preview the copy:

   ```powershell
   .\NewEra.ps1 -Action SyncGate -WhatIf
   ```

3. Copy the validated gate:

   ```powershell
   .\NewEra.ps1 -Action SyncGate
   ```

   This places `new_era_ai_gate.csv` under MT5's common `Files` directory.
4. Load `NewEraCouncilDemo_AI_Gated.set`. A missing, expired, mismatched, or
   direction-conflicting gate remains blocked and is reported as `SIGNAL STALE`
   or `AI GATE ABSTAIN`.

The Python research output is still research-only. It does not bypass the EA's
four-vote, risk, permission, or broker checks.

## Safe DEMO verification plan

Run these cases in Strategy Tester or on the isolated DEMO account, without
creating a synthetic signal:

1. Initialization on non-gold, real, and demo accounts.
2. Missing/warming-up data and no-signal candles.
3. Four-vote signal with external AI gate disabled.
4. Fresh matching AI gate, stale gate, missing gate, and direction mismatch.
5. Invalid volume, insufficient margin, invalid geometry, and stop/freeze
   distance.
6. Unsupported permissions or market/filling mode.
7. Broker rejection and verification of retcode/description logging.
8. Duplicate-position and one-minute symbol-lock protection.
9. Restart/re-attach behavior and recovery of the diagnostic panel/log.

If no valid four-vote setup occurs, the correct result is a diagnostic record,
not a forced order. A successful broker execution is only reported after the
EA sees a successful server retcode.

## Remaining limitations

- This audit cannot prove a live DEMO order until a genuine four-vote signal
  occurs while the freshly compiled EA is attached and enabled.
- The current research gate artifacts should be regenerated before any
  AI-gated demo test.
- Broker-specific execution behavior still requires the DEMO forward/tester
  cases above; no live-account test is permitted.
