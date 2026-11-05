# New Era Trading Bot — როგორ გავუშვა

## რა ფაილებია

- `NewEraCouncilDemo.mq5` — პირდაპირი MT5 Expert Advisor.
- `NewEraCouncilDemo.set` — უსაფრთხო demo პარამეტრები.
- `NewEraCouncilDemo.pine` — TradingView-ის backtest ვერსია.

## MT5-ში გაშვება

1. MT5-ში გახსენი **File → Open Data Folder**.
2. შედი `MQL5\Experts` საქაღალდეში.
3. ჩააკოპირე `NewEraCouncilDemo.mq5`.
4. გახსენი MetaEditor, დააჭირე **Compile** ან `F7` — განსაკუთრებით AI-gate ცვლილების შემდეგ.
5. MT5-ში Navigator → Expert Advisors → Refresh.
6. ცალკე demo ანგარიშზე გახსენი `XAUUSD`-ის **M5 chart**.
7. მიამაგრე `NewEraCouncilDemo`.
8. `Inputs`-ში Load-ით აირჩიე `NewEraCouncilDemo.set`.
9. AutoTrading ჩართე მხოლოდ demo ანგარიშზე.

### მნიშვნელოვანი

- `AllowRealAccount=false` არის.
- real account-ზე EA ინიციალიზაციას შეწყვეტს.
- default risk არის 0.5%, maximum 1%.
- minimum reward:risk არის 1.5; `RewardRisk`-ის დაბალი მნიშვნელობა ინიციალიზაციას ბლოკავს.
- დღიური loss limit არის 10 ანგარიშის ვალუტაში.
- ერთდროულად მხოლოდ ერთი პოზიციაა.
- მოგებიან trade-ს დროის გამო ავტომატურად არ ხურავს; time guard მხოლოდ წაგებიან პოზიციას ხურავს.
- ეს არის deterministic `Council-lite` EA: H4, H1, M15 და M5 votes + ATR/spread guard. Python AI model-ს პირდაპირ არ ტვირთავს.
- chart-ზე ჩანს ბოლო დახურული M5 candle-ის execution diagnostic panel; structured CSV იწერება MT5 Common Files-ში.

## TradingView-ში ტესტირება

1. გახსენი TradingView → Pine Editor.
2. ჩასვი `NewEraCouncilDemo.pine`.
3. დააჭირე **Add to chart**.
4. გამოიყენე XAUUSD-ის 5-minute chart.
5. Strategy Tester → Properties-ში მიუთითე შენი broker-ის commission და slippage.
6. შეამოწმე სხვადასხვა პერიოდი, spread და commission.

Pine Script მხოლოდ TradingView-ის backtest/alert-ს აკეთებს. ის MT5-ში ავტომატურად trade-ს არ გახსნის.

## TradingAgents AI research layer

TauricResearch-ის `TradingAgents` framework უკვე დამატებულია ამ საქაღალდეში:

- `TradingAgents\` — upstream repository, version `0.7.0`, Apache-2.0 license.
- `run_tradingagents_research.py` — უსაფრთხო bridge, რომელიც მხოლოდ research report-ს წერს.
- `run_tradingagents_research.ps1` — Docker launcher.
- `TradingAgentsData\research\` — შედეგები და HTML/markdown reports.

Docker image უკვე აწყობილია სახელით `new-era-tradingagents:local` და Python 3.13-ს იყენებს.
ლოკალური Windows Python 3.15-ის ნაცვლად ეს არის რეკომენდებული გზა.

### პირველი გაშვება

1. გააკეთე მხოლოდ ერთხელ:

   ```powershell
   Copy-Item .\TradingAgents\.env.example .\TradingAgents\.env
   ```

2. `.env`-ში შეავსე მხოლოდ შენს მიერ არჩეული provider-ის key. ეს ფაილი არ უნდა აიტვირთოს Git-ში.
3. ჯერ გაუშვი უსაფრთხო smoke test:

   ```powershell
   .\run_tradingagents_research.ps1 -DryRun
   ```

4. რეალური research run-ისთვის, მაგალითად OpenAI provider-ით:

   ```powershell
   .\run_tradingagents_research.ps1 -Provider openai -Ticker XAUUSD
   ```

   `XAUUSD` TradingAgents-ის Yahoo data layer-ში ავტომატურად გადადის `GC=F`-ზე.

AI შედეგში `ai_action` შეიძლება იყოს `BUY`, `SELL` ან `ABSTAIN`, მაგრამ bridge ყოველთვის წერს:
`execution_allowed=false` და `real_account_allowed=false`. AI გადაწყვეტილება MT5-ში პირდაპირ order-ს
არ აგზავნის.

თუ ცალკე read-only MT5 council-ის JSON-იც არსებობს, ორივე მხარის თანხმობის სანახავად:

```powershell
.\run_tradingagents_research.ps1 `
  -Provider openai `
  -Gate "D:\Documents\new era\market_council\outputs\live_council_signal.json"
```

AI და deterministic council თუ არ დაემთხვა, `consensus_action=ABSTAIN` იქნება. დამთხვევის შემთხვევაშიც
execution დაბლოკილი რჩება — ეს ფენა ჯერ მხოლოდ research/demo validation-ისთვისაა.

## ერთიანი workspace და გაშვების წესრიგი

ამ ფოლდერში მთავარი მართვის ფაილია `NewEra.ps1`, ხოლო bot-ების კატალოგია `bot_registry.json`.
ამიტომ მომავალში ახალი bot-ის დამატება არ მოითხოვს ყველაფრის თავიდან აწყობას.

### სისტემის შემოწმება

```powershell
cd C:\Users\LeVI\Desktop\NewEraTradingBot
.\NewEra.ps1 -Action Status
.\NewEra.ps1 -Action Validate
```

### უსაფრთხო dry-run

```powershell
.\NewEra.ps1 -Action DryRun
```

ეს არ აგზავნის order-ს და ქმნის:

- `TradingAgentsData\research\tradingagents_latest.json`
- `TradingAgentsData\research\new_era_ai_gate.csv`

### AI research run

ლოკალურად უნდა არსებობდეს `TradingAgents\.env`, რომელშიც არჩეული provider-ის key წერია.
შემდეგ:

```powershell
.\NewEra.ps1 -Action Research -Provider openai -Ticker XAUUSD
```

ან სხვა provider-ით:

```powershell
.\NewEra.ps1 -Action Research -Provider anthropic -Ticker XAUUSD
```

### AI gate-ის MT5-ში გადატანა

პირველ ეტაპზე ჩვეულებრივი preset გამოიყენე — AI gate გამორთულია:

```text
NewEraCouncilDemo.set
```

როდესაც mirror-mode-ში დარწმუნდებით, რომ AI signal-ს ვადა, symbol და მიმართულება სწორია:

```powershell
.\NewEra.ps1 -Action SyncGate -WhatIf
.\NewEra.ps1 -Action SyncGate
```

შემდეგ MT5-ში მხოლოდ demo ანგარიშზე ჩატვირთე:

```text
NewEraCouncilDemo_AI_Gated.set
```

AI-gated preset-ში `EnableExternalAIGate=true` არის. EA შეამოწმებს:

- gate-ის version-ს;
- strategy ID-ს;
- symbol-ს;
- BUY/SELL მიმართულების დამთხვევას;
- gate-ის ვადას;
- maximum age-ს;
- account და spread/risk guard-ებს.

ფაილის არქონის, ვადის გასვლის ან mismatch-ის დროს trade არ გაიხსნება.

### MT5-ში საბოლოო გაშვება

1. MT5 → **File → Open Data Folder**.
2. `MQL5\Experts`-ში ჩააკოპირე `NewEraCouncilDemo.mq5`.
3. MetaEditor-ში დააჭირე `F7`-ს.
4. მხოლოდ demo account-ზე გახსენი broker-ის XAUUSD chart, timeframe M5.
5. ჯერ გამოიყენე `NewEraCouncilDemo.set`.
6. Strategy Tester-ში შეამოწმე commission, spread და slippage.
7. AI gate-ის გამოყენება ჩართე მხოლოდ mirror-mode-ის შემოწმების შემდეგ.

## მომავალში ახალი bot-ის დამატება

ყველა ახალი bot თავიდან უნდა იყოს `research_only` ან `demo_only`. ახალი bot-ისთვის:

1. შექმენი ცალკე source/preset folder ან დაამატე ახალი `.mq5`/Python runner.
2. დაამატე entry `bot_registry.json`-ში უნიკალური `id`-ით.
3. დაამატე შესაბამისი config `configs\<bot-id>.json`-ში.
4. მიუთითე execution engine, symbol, preset, safety rules და research runner.
5. `real_account_allowed` ყოველთვის დატოვე `false`.
6. გაუშვი:

   ```powershell
   .\NewEra.ps1 -Action List
   .\NewEra.ps1 -Action Validate
   ```

ახალი bot-ის promotion არ ნიშნავს ძველი bot-ების შედეგების ერთმანეთზე დამატებას. თითოეულს სჭირდება
ცალკე backtest, cost stress, chronological holdout და demo forward log.

## New Era Kronos Matrix Scalper v1

დაემატა მეორე, ჯერ **research-only** კანდიდატი:

```text
new-era-kronos-matrix-v1
```

მისი წყაროა MIT-licensed `Kronos\` repository. Kronos არის OHLCV candle-ების foundation model და არა
მზა trading strategy. ჩვენ მას მხოლოდ ერთ ფენად ვიყენებთ.

### იდეა

ბოტი ეძებს არა უბრალოდ „Kronos-მა BUY თქვა“-ს, არამედ სამმაგ თანხმობას:

1. **Matrix breakout** — H4/H1 trend + M15 momentum + დახურული M5 range breakout;
2. **Historical analogue** — მიმდინარე normalized-return pattern-ის მსგავსი ძველი ფანჯრების შედეგები;
3. **Kronos forecast** — მომდევნო 12 M5 candle-ის forecast.

თუ რომელიმე ფენა არ ეთანხმება, spread უცნობია, მონაცემი არასრულია ან model ვერ გაეშვა, gate არის
`ABSTAIN`. ეს არის market-repetition hypothesis-ის ტესტი და არა გარანტია, რომ ბაზარი იმეორებს თავს.

### Market data export

Kronos-ს სჭირდება მინიმუმ 600 დახურული XAUUSD M5 candle. MT5-დან read-only export:

```powershell
.\NewEra.ps1 -Action ExportMarketData -Ticker XAUUSD -Bars 5000 -PythonPath "C:\Python313\python.exe"
```

Python 3.13 path მხოლოდ მაგალითია; MT5 package-ისთვის გამოიყენე მხარდაჭერილი 64-bit Python 3.11–3.14.
Export არ აგზავნის order-ს და ბოლო, ჯერ კიდევ ფორმირებად candle-ს გამოტოვებს.

### Kronos image და dry-run

Docker image ააშენე ერთხელ:

```powershell
.\NewEra.ps1 -Action BuildKronosImage
```

შემდეგ უსაფრთხო შემოწმება:

```powershell
.\NewEra.ps1 -Action KronosDryRun
```

რეალური research inference-ისთვის მიუთითე მიმდინარე broker spread:

```powershell
.\NewEra.ps1 -Action KronosResearch -SpreadPoints 25
```

პირველ გაშვებაზე Hugging Face-დან ჩამოიტვირთება `Kronos-small` და tokenizer. cache ინახება:

```text
KronosData\hf-cache
```

შედეგებია:

```text
TradingAgentsData\research\kronos_matrix_latest.json
TradingAgentsData\research\new_era_kronos_matrix_gate.csv
```

### Point-in-time backtest

ჯერ იაფი ablation გაუშვი — matrix + historical analogue, Kronos-ის გარეშე:

```powershell
.\NewEra.ps1 -Action KronosBacktest -BacktestMode matrix-analog -SpreadPoints 25
```

შემდეგ სრული walk-forward კომბო, სადაც Kronos თითოეულ ისტორიულ anchor-ზე ცალკე forecast-ს აკეთებს:

```powershell
.\NewEra.ps1 -Action KronosBacktest -BacktestMode combined -SpreadPoints 25 -StartIndex 3000 -Step 12
```

ეს შეიძლება ნელი იყოს CPU-ზე, რადგან თითოეულ ისტორიულ გადაწყვეტილების წერტილში model inference
გადის. შედეგები ჩაიწერება:

```text
TradingAgentsData\research\kronos_matrix_backtest.json
TradingAgentsData\research\kronos_matrix_backtest_trades.csv
```

Backtest-ში decision მხოლოდ anchor-მდე არსებულ candle-ებს ხედავს; შემდეგი 12 candle მხოლოდ შედეგის
შესაფასებლად გამოიყენება. ეს შემოწმება აუცილებელია მანამდე, სანამ gate-ს demo EA-ს მივაბამთ.

### Demo MT5 execution

ეს კანდიდატი იყენებს იმავე შემოწმებულ execution engine-ს `NewEraCouncilDemo.mq5`, მაგრამ ცალკე preset-ს:

```text
NewEraKronosMatrixScalper.set
```

Gate-ის გადატანა:

```powershell
.\NewEra.ps1 -Action SyncKronosGate -WhatIf
.\NewEra.ps1 -Action SyncKronosGate
```

MT5-ში compile-ის შემდეგ მხოლოდ demo-ზე მიამაგრე EA და Load-ით აირჩიე `NewEraKronosMatrixScalper.set`.
Preset-ის საწყისი რისკია 0.25%, maximum 0.50%, maximum spread 35 points და real account გამორთულია.

### სრული Combo: TradingAgents + Kronos + Matrix

თუ ორივე research layer-ის თანხმობა გინდა, გამოიყენე:

```powershell
.\NewEra.ps1 -Action ComboDryRun
```

რეალური research run-ისთვის:

```powershell
.\NewEra.ps1 -Action ComboResearch -Provider openai -SpreadPoints 25
```

სრული combo საბოლოო BUY/SELL gate-ს მხოლოდ მაშინ წერს, როცა:

- TradingAgents `BUY` ან `SELL` აბრუნებს;
- Kronos Matrix იგივე მიმართულებას აბრუნებს;
- ორივე gate ჯერ არ არის ვადაგასული;
- Kronos Matrix-ის matrix, analogue და cost checks უკვე გავლილია.

ნებისმიერი განსხვავებისას საბოლოო gate არის `ABSTAIN`. TradingAgents-ის confidence ამ ეტაპზე
calibrated probability არ არის და execution engine-ს პირდაპირი წვდომა მაინც არ აქვს.

## ერთიანი local backend / desktop-app საფუძველი

დაემატა FastAPI orchestrator, რომელიც ერთ local service-ში აერთიანებს:

- TradingAgents research runner-ს;
- Kronos Matrix runner-ს;
- gate combiner-ს;
- ChromaDB memory-ს, JSONL fallback-ით;
- WebSocket event stream-ს UI-სთვის;
- bot/gate/scan status endpoint-ებს.

მთავარი ფაილებია:

```text
main_api.py
newera_api\config.py
newera_api\events.py
newera_api\memory.py
newera_api\orchestrator.py
newera_api\schemas.py
requirements_api.txt
start_newera_api.ps1
api.env.example
ui\README_GE.md
```

API research/demo-only არის. მას order-placement endpoint არ აქვს, `execution_allowed=false` რჩება
და process failure-ის დროს fresh `ABSTAIN` gate იწერება.

### API-ის გაშვება

რეკომენდებულია ცალკე Python 3.13 environment:

```powershell
cd C:\Users\LeVI\Desktop\NewEraTradingBot
py -3.13 -m venv .venv-api
.\.venv-api\Scripts\python.exe -m pip install -r requirements_api.txt
.\start_newera_api.ps1 -PythonPath ".\.venv-api\Scripts\python.exe"
```

შემდეგ გახსენი:

```text
http://127.0.0.1:8787/docs
```

Health/status endpoints:

```powershell
Invoke-RestMethod http://127.0.0.1:8787/health
Invoke-RestMethod http://127.0.0.1:8787/bots/status
Invoke-RestMethod http://127.0.0.1:8787/gates/latest
```

Manual scan-ის მაგალითი:

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8787/scan/manual `
  -ContentType 'application/json' `
  -Body '{"symbol":"XAUUSD","spread_points":25,"include_tradingagents":true,"include_kronos":true}'
```

Realtime UI stream:

```text
ws://127.0.0.1:8787/ws/logs
```

PyQt6 ან React UI შემდეგ ეტაპზე ამ API-სა და WebSocket-ზე დაჯდება; UI-მ არ უნდა გაუშვას shell commands
და არ უნდა მიიღოს broker/API credentials.

## ჩემი შეფასება Kronos + TradingAgents + matrix კომბინაციაზე

ეს კომბინაცია აზრიანია, რადგან თითოეულ კომპონენტს სხვადასხვა როლი აქვს:

- **Kronos** — candle-path forecast;
- **matrix/analog engine** — წესებზე დაფუძნებული pattern და breakout validation;
- **TradingAgents** — news/macro/risk context;
- **MT5** — რეალური spread, stop distance, lot sizing და execution.

მაგრამ სამი AI/სტატისტიკური ფენის ერთმანეთზე დადება ავტომატურად უპირატესობას არ ქმნის. აუცილებლად უნდა
შევადაროთ matrix-only, Kronos-only, analogue-only და სრული კომბო. თუ მხოლოდ სრული კომბო მუშაობს,
ეს შეიძლება overfitting იყოს.

## რას უნდა ველოდოთ

ეს არ არის გარანტირებული profit machine. სწორი გამოსავალია ჯერ TradingView-ში და MT5 demo-ზე შედეგების შედარება:

- იგივე პერიოდი;
- იგივე symbol;
- იგივე spread/commission;
- იგივე risk;
- მინიმუმ 4–8 კვირა demo forward test.

თუ TradingView და MT5-ის demo შედეგები ძლიერ განსხვავდება, მიზეზი ჩვეულებრივ არის broker feed, spread, slippage ან symbol specification.
