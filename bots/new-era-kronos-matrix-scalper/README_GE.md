# New Era Kronos Matrix Scalper v1

ეს არის **research-only** ახალი კანდიდატი. ის არ არის დადასტურებული profitable bot და ჯერ არ უნდა
ჩაირთოს რეალურ ანგარიშზე.

## იდეა

ბოტი არ ცდილობს ივარაუდოს, რომ ბაზარი ზუსტად იმეორებს წარსულს. ის ამოწმებს სამ დამოუკიდებელ ფენას:

1. **Matrix breakout** — H4/H1 trend, M15 momentum და დახურული M5 range breakout.
2. **Historical analogues** — მიმდინარე normalized-return pattern-ის მსგავსი ძველი ფანჯრები და მათი
   შემდგომი 12 M5 candle-ის შედეგები.
3. **Kronos forecast** — ბოლო OHLCV კონტექსტიდან მომდევნო 12 M5 candle-ის პროგნოზი.

BUY/SELL gate იწერება მხოლოდ მაშინ, როცა სამივე მიმართულება ემთხვევა და spread/cost gate გადის.
სხვა შემთხვევაში იწერება `ABSTAIN`.

## გაშვების რიგი

1. მოამზადე მინიმუმ 600 **დახურული** XAUUSD M5 candle CSV:

   ```text
   timestamp,open,high,low,close,volume,amount
   ```

2. შეინახე მაგალითად:

   ```text
   TradingAgentsData\market\xauusd_m5.csv
   ```

3. Docker image ააშენე:

   ```powershell
   .\NewEra.ps1 -Action BuildKronosImage
   ```

4. ჯერ გაუშვი უსაფრთხო dry-run:

   ```powershell
   .\NewEra.ps1 -Action KronosDryRun
   ```

5. რეალური model inference-ისთვის მიუთითე broker spread:

   ```powershell
   .\NewEra.ps1 -Action KronosResearch -SpreadPoints 25
   ```

პირველ გაშვებაზე Hugging Face-დან ჩამოიტვირთება `Kronos-small` და tokenizer. მოდელის cache დარჩება
`KronosData\hf-cache`-ში.

## Point-in-time backtest

ჯერ გაუშვი იაფი deterministic ablation:

```powershell
.\NewEra.ps1 -Action KronosBacktest -BacktestMode matrix-analog -SpreadPoints 25
```

შემდეგ სრული Kronos walk-forward:

```powershell
.\NewEra.ps1 -Action KronosBacktest -BacktestMode combined -SpreadPoints 25 -StartIndex 3000 -Step 12
```

სრული backtest შეიძლება ნელი იყოს CPU-ზე. მისი გადაწყვეტილება anchor-მდე არსებულ მონაცემებს იყენებს;
მომდევნო candle-ები მხოლოდ შედეგის შესაფასებლად იკითხება.

## MT5-ში demo გამოყენება

Kronos Matrix იყენებს საერთო execution engine-ს `NewEraCouncilDemo.mq5`, მაგრამ თავისი ცალკე preset-ით:

```text
NewEraKronosMatrixScalper.set
```

წინასწარ გადაიტანე gate:

```powershell
.\NewEra.ps1 -Action SyncKronosGate -WhatIf
.\NewEra.ps1 -Action SyncKronosGate
```

EA-ს compile-ის შემდეგ ჩართე მხოლოდ demo-ზე. Preset-ში:

- `RiskPercent=0.25`;
- `MaximumRiskPercent=0.50`;
- `MaximumSpreadPoints=35`;
- `EnableExternalAIGate=true`;
- `AllowRealAccount=false`.

თუ gate ფაილი არ არსებობს, ვადაგასულია, სხვა strategy ID-ს ეკუთვნის, ან მიმართულება არ ემთხვევა
MT5-ის deterministic votes-ს, order არ გაიგზავნება.

## რა უნდა შევადაროთ

სავალდებულო ablation-ებია:

- matrix-only;
- historical-analogue-only;
- Kronos-only;
- matrix + analogue;
- matrix + Kronos;
- სრული სამმაგი კომბინაცია.

თუ მხოლოდ სრული კომბო გამოიყურება კარგი, მაგრამ ablation-ებში იშლება, ეს შეიძლება იყოს overfitting.
