# ახალი bot-ის დამატების წესი

ეს საქაღალდე არის ახალი bot-ების სამუშაო ადგილი. მოქმედი bot-ების registry არის
ერთი დონით ზემოთ: `..\bot_registry.json`.

## მინიმალური სტრუქტურა

```text
bots\my-new-bot\
  README_GE.md
  my-new-bot.mq5 ან run_my_new_bot.py
  my-new-bot.set (თუ MT5 EA არის)
```

შემდეგ დაამატე registry-ში:

- უნიკალური `id`;
- `display_name`;
- `status: research_only` ან `demo_only`;
- symbol და timeframe-ები;
- execution source/preset;
- research runner/config;
- `real_account_allowed: false`;
- პირდაპირი Python order-ები ყოველთვის `false`.

ახალი bot არ უნდა გახდეს active მხოლოდ იმიტომ, რომ backtest-ში კარგი რიცხვი აქვს.
ჯერ საჭიროა cost stress, chronological holdout, Strategy Tester და demo forward log.
