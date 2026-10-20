#property strict
#property version   "1.00"
#property description "New Era Council demo-only XAUUSD M5 EA. Deterministic multi-timeframe votes, cost guard and risk guard."
#property description "This EA does not load Python/AI models; it may read a versioned external AI gate when enabled."
#property description "It never bypasses the real-account guard by default."

#include <Trade/Trade.mqh>

input group "Safety"
input ulong            MagicNumber              = 26010101;
input string           TradeComment             = "NE_COUNCIL_DEMO";
input bool             AllowRealAccount         = false;
input bool             RestrictToGoldSymbols    = true;

input group "External AI gate"
input bool             EnableExternalAIGate     = false;
input string           AIGateFileName           = "new_era_ai_gate.csv";
input string           AIGateStrategyId         = "new-era-gold-council-v1";
input int              AIGateMaxAgeMinutes      = 90;
input double           AIGateMinimumConfidence  = 0.00;

input group "Risk and costs"
input double           RiskPercent              = 0.50;
input double           MaximumRiskPercent       = 1.00;
input double           MaximumSpreadPoints      = 50.0;
input int              MaximumSlippagePoints    = 20;
input int              MaximumOpenPositions     = 1;
input int              MaximumTradesPerDay     = 2;
input double           MaximumDailyLossMoney    = 10.0;
input int              MaximumConsecutiveLosses = 2;

input group "Council votes"
input int              MinimumVotes             = 4;
input int              BreakoutLookbackBars     = 20;
input int              FastEMAPeriodH4          = 50;
input int              SlowEMAPeriodH4          = 200;
input int              FastEMAPeriodH1          = 20;
input int              MomentumEMAPeriodM15     = 20;
input int              RSIPeriodM15             = 14;
input double           BuyRSIMinimum            = 52.0;
input double           SellRSIMaximum           = 48.0;
input int              ATRPeriodM5              = 14;
input int              ATRAverageBarsM5         = 200;
input double           MinimumATRRatio          = 0.50;
input double           MaximumATRRatio          = 2.00;

input group "Trade management"
input double           StopLossATRMultiplier    = 1.50;
input double           RewardRisk               = 1.00;
input double           BreakEvenAtR             = 0.80;
input double           BreakEvenLockPrice       = 0.00;
input int              MaximumLosingHoldMinutes = 90;
input bool             EnableTrailing           = false;
input double           TrailingATRMultiplier    = 1.00;

input group "Diagnostics"
input bool             PrintDiagnostics         = true;
input bool             WriteCommonCsvLog        = true;
input string           CommonCsvFile            = "new_era_council_demo_log.csv";

CTrade trade;
int h4_fast_handle = INVALID_HANDLE;
int h4_slow_handle = INVALID_HANDLE;
int h1_fast_handle = INVALID_HANDLE;
int m15_ema_handle = INVALID_HANDLE;
int m15_rsi_handle = INVALID_HANDLE;
int m5_atr_handle = INVALID_HANDLE;
datetime last_signal_bar = 0;
datetime blocked_until = 0;

struct AIGateData
{
   bool valid;
   string strategy;
   string symbol;
   string action;
   double confidence;
   long generated_epoch;
   long expires_epoch;
};

void LogEvent(const string event_name, const string details);

int VolumeDigits()
{
   double step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   int digits = 0;
   while(digits < 8 && MathAbs(step - MathRound(step)) > 1e-9)
   {
      step *= 10.0;
      digits++;
   }
   return digits;
}

double NormalizeVolume(const double requested)
{
   const double min_volume = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MIN);
   const double max_volume = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_MAX);
   const double step = SymbolInfoDouble(_Symbol, SYMBOL_VOLUME_STEP);
   if(step <= 0.0)
      return 0.0;
   double volume = MathFloor(requested / step + 1e-9) * step;
   volume = MathMax(min_volume, MathMin(max_volume, volume));
   return NormalizeDouble(volume, VolumeDigits());
}

double NormalizePrice(const double price)
{
   const int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   const double tick_size = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tick_size <= 0.0)
      return NormalizeDouble(price, digits);
   return NormalizeDouble(MathRound(price / tick_size) * tick_size, digits);
}

bool ReadIndicatorValue(const int handle, const int shift, double &value)
{
   if(handle == INVALID_HANDLE)
      return false;
   double buffer[];
   ArraySetAsSeries(buffer, true);
   if(CopyBuffer(handle, 0, shift, 1, buffer) != 1)
      return false;
   value = buffer[0];
   return MathIsValidNumber(value) && value != EMPTY_VALUE;
}

double CurrentSpreadPoints()
{
   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick) || tick.ask <= 0.0 || tick.bid <= 0.0)
      return DBL_MAX;
   return (tick.ask - tick.bid) / _Point;
}

bool IsGoldSymbol()
{
   string name = _Symbol;
   StringToUpper(name);
   return StringFind(name, "XAU") >= 0 || StringFind(name, "GOLD") >= 0;
}

bool AIGateSymbolMatches(const string gate_symbol)
{
   string requested = gate_symbol;
   string current = _Symbol;
   StringToUpper(requested);
   StringToUpper(current);
   if(requested == current)
      return true;
   // The Python research layer uses the portable label XAUUSD while brokers
   // may expose XAUUSD.e, GOLD, XAUUSD+, or another gold suffix.
   return IsGoldSymbol() &&
          (StringFind(requested, "XAU") >= 0 || StringFind(requested, "GOLD") >= 0);
}

bool ReadAIGate(AIGateData &gate, string &reason)
{
   gate.valid = false;
   gate.strategy = "";
   gate.symbol = "";
   gate.action = "";
   gate.confidence = 0.0;
   gate.generated_epoch = 0;
   gate.expires_epoch = 0;
   reason = "";

   int handle = FileOpen(AIGateFileName,
                         FILE_READ | FILE_CSV | FILE_COMMON | FILE_ANSI,
                         ',');
   if(handle == INVALID_HANDLE)
   {
      reason = StringFormat("AI gate file unavailable: %s", AIGateFileName);
      return false;
   }

   // Header: version,strategy,symbol,action,confidence,generated_epoch,expires_epoch
   const string header_version = FileReadString(handle);
   const string header_strategy = FileReadString(handle);
   const string header_symbol = FileReadString(handle);
   const string header_action = FileReadString(handle);
   const string header_confidence = FileReadString(handle);
   const string header_generated = FileReadString(handle);
   const string header_expires = FileReadString(handle);
   if(header_version == "" || header_strategy == "" || header_symbol == "" ||
      header_action == "" || header_confidence == "" || header_generated == "" ||
      header_expires == "")
   {
      FileClose(handle);
      reason = "AI gate header is incomplete";
      return false;
   }

   const string version = FileReadString(handle);
   gate.strategy = FileReadString(handle);
   gate.symbol = FileReadString(handle);
   gate.action = FileReadString(handle);
   gate.confidence = StringToDouble(FileReadString(handle));
   gate.generated_epoch = (long)StringToInteger(FileReadString(handle));
   gate.expires_epoch = (long)StringToInteger(FileReadString(handle));
   FileClose(handle);

   string action = gate.action;
   StringToUpper(action);
   gate.action = action;

   if(version != "1")
   {
      reason = StringFormat("AI gate version unsupported: %s", version);
      return false;
   }
   if(gate.strategy != AIGateStrategyId)
   {
      reason = StringFormat("AI gate strategy mismatch: %s", gate.strategy);
      return false;
   }
   if(!AIGateSymbolMatches(gate.symbol))
   {
      reason = StringFormat("AI gate symbol mismatch: %s vs %s", gate.symbol, _Symbol);
      return false;
   }
   if(gate.action != "BUY" && gate.action != "SELL")
   {
      reason = StringFormat("AI gate action is %s", gate.action);
      return false;
   }
   if(!MathIsValidNumber(gate.confidence) || gate.confidence < AIGateMinimumConfidence)
   {
      reason = StringFormat("AI gate confidence %.3f below %.3f", gate.confidence, AIGateMinimumConfidence);
      return false;
   }

   const long now = (long)TimeCurrent();
   if(gate.generated_epoch <= 0 || gate.expires_epoch <= 0 ||
      gate.generated_epoch > now + 300 || now > gate.expires_epoch)
   {
      reason = StringFormat("AI gate expired or clock-invalid: generated=%I64d expires=%I64d now=%I64d",
                            gate.generated_epoch, gate.expires_epoch, now);
      return false;
   }
   if(AIGateMaxAgeMinutes > 0 && now - gate.generated_epoch > AIGateMaxAgeMinutes * 60)
   {
      reason = StringFormat("AI gate older than %d minutes", AIGateMaxAgeMinutes);
      return false;
   }

   gate.valid = true;
   return true;
}

bool AIGateAllowsDirection(const int direction, string &details)
{
   details = "";
   if(!EnableExternalAIGate)
      return true;

   AIGateData gate;
   string reason = "";
   if(!ReadAIGate(gate, reason))
   {
      details = reason;
      LogEvent("AI_GATE_BLOCK", reason);
      return false;
   }

   const string expected = direction > 0 ? "BUY" : "SELL";
   if(gate.action != expected)
   {
      details = StringFormat("AI gate=%s deterministic=%s", gate.action, expected);
      LogEvent("AI_GATE_BLOCK", details);
      return false;
   }

   details = StringFormat("AI_GATE=%s confidence=%.3f", gate.action, gate.confidence);
   return true;
}

bool IsRealAccount()
{
   return (ENUM_ACCOUNT_TRADE_MODE)AccountInfoInteger(ACCOUNT_TRADE_MODE) == ACCOUNT_TRADE_MODE_REAL;
}

datetime StartOfDay(const datetime when)
{
   MqlDateTime dt;
   TimeToStruct(when, dt);
   dt.hour = 0;
   dt.min = 0;
   dt.sec = 0;
   return StructToTime(dt);
}

void LogEvent(const string event_name, const string details)
{
   if(PrintDiagnostics)
      Print(event_name, " | ", details);
   if(!WriteCommonCsvLog)
      return;

   int handle = FileOpen(CommonCsvFile,
                         FILE_READ | FILE_WRITE | FILE_CSV | FILE_COMMON | FILE_ANSI,
                         ',');
   if(handle == INVALID_HANDLE)
      return;
   if(FileSize(handle) == 0)
      FileWrite(handle, "time", "symbol", "event", "details");
   FileSeek(handle, 0, SEEK_END);
   FileWrite(handle,
             TimeToString(TimeCurrent(), TIME_DATE | TIME_SECONDS),
             _Symbol,
             event_name,
             details);
   FileClose(handle);
}

bool IsOurPosition(const ulong ticket)
{
   if(ticket == 0 || !PositionSelectByTicket(ticket))
      return false;
   return PositionGetString(POSITION_SYMBOL) == _Symbol &&
          (ulong)PositionGetInteger(POSITION_MAGIC) == MagicNumber;
}

int OurPositionCount()
{
   int count = 0;
   for(int i = PositionsTotal() - 1; i >= 0; --i)
   {
      const ulong ticket = PositionGetTicket(i);
      if(IsOurPosition(ticket))
         count++;
   }
   return count;
}

bool IsExitDeal(const ulong ticket)
{
   if(ticket == 0)
      return false;
   if(HistoryDealGetString(ticket, DEAL_SYMBOL) != _Symbol)
      return false;
   if((ulong)HistoryDealGetInteger(ticket, DEAL_MAGIC) != MagicNumber)
      return false;
   const ENUM_DEAL_ENTRY entry = (ENUM_DEAL_ENTRY)HistoryDealGetInteger(ticket, DEAL_ENTRY);
   return entry == DEAL_ENTRY_OUT || entry == DEAL_ENTRY_INOUT;
}

double DealNetProfit(const ulong ticket)
{
   return HistoryDealGetDouble(ticket, DEAL_PROFIT) +
          HistoryDealGetDouble(ticket, DEAL_COMMISSION) +
          HistoryDealGetDouble(ticket, DEAL_SWAP) +
          HistoryDealGetDouble(ticket, DEAL_FEE);
}

void TodayStats(int &trades, int &losses, double &net, int &consecutive_losses)
{
   trades = 0;
   losses = 0;
   net = 0.0;
   consecutive_losses = 0;
   if(!HistorySelect(StartOfDay(TimeCurrent()), TimeCurrent()))
      return;

   const int total = HistoryDealsTotal();
   double profits[];
   ArrayResize(profits, 0);
   for(int i = 0; i < total; ++i)
   {
      const ulong ticket = HistoryDealGetTicket(i);
      if(!IsExitDeal(ticket))
         continue;
      const double profit = DealNetProfit(ticket);
      const int size = ArraySize(profits);
      ArrayResize(profits, size + 1);
      profits[size] = profit;
      trades++;
      net += profit;
      if(profit < 0.0)
         losses++;
   }
   for(int i = ArraySize(profits) - 1; i >= 0; --i)
   {
      if(profits[i] < 0.0)
         consecutive_losses++;
      else
         break;
   }
}

bool GuardAllowsTrading()
{
   if(TimeCurrent() < blocked_until)
      return false;
   const double spread = CurrentSpreadPoints();
   if(MaximumSpreadPoints > 0.0 && spread > MaximumSpreadPoints)
   {
      LogEvent("GUARD_BLOCK", StringFormat("spread=%.1f max=%.1f", spread, MaximumSpreadPoints));
      return false;
   }

   int trades = 0;
   int losses = 0;
   int consecutive_losses = 0;
   double net = 0.0;
   TodayStats(trades, losses, net, consecutive_losses);
   if(MaximumTradesPerDay > 0 && trades >= MaximumTradesPerDay)
   {
      LogEvent("GUARD_BLOCK", StringFormat("daily_trades=%d", trades));
      return false;
   }
   if(MaximumDailyLossMoney > 0.0 && net <= -MaximumDailyLossMoney)
   {
      LogEvent("GUARD_BLOCK", StringFormat("daily_net=%.2f", net));
      return false;
   }
   if(MaximumConsecutiveLosses > 0 && consecutive_losses >= MaximumConsecutiveLosses)
   {
      LogEvent("GUARD_BLOCK", StringFormat("consecutive_losses=%d", consecutive_losses));
      return false;
   }
   return true;
}

bool CouncilSignal(int &direction, int &votes, string &details, double &atr)
{
   direction = 0;
   votes = 0;
   details = "";
   atr = 0.0;

   double h4_fast = 0.0;
   double h4_slow = 0.0;
   double h1_fast = 0.0;
   double m15_ema = 0.0;
   double m15_rsi = 0.0;
   if(!ReadIndicatorValue(h4_fast_handle, 1, h4_fast) ||
      !ReadIndicatorValue(h4_slow_handle, 1, h4_slow) ||
      !ReadIndicatorValue(h1_fast_handle, 1, h1_fast) ||
      !ReadIndicatorValue(m15_ema_handle, 1, m15_ema) ||
      !ReadIndicatorValue(m15_rsi_handle, 1, m15_rsi) ||
      !ReadIndicatorValue(m5_atr_handle, 1, atr))
   {
      details = "indicator data unavailable";
      return false;
   }

   double atr_values[];
   ArraySetAsSeries(atr_values, true);
   const int copied = CopyBuffer(m5_atr_handle, 0, 1, ATRAverageBarsM5, atr_values);
   if(copied < 50)
   {
      details = "ATR regime warmup incomplete";
      return false;
   }
   double atr_average = 0.0;
   int atr_count = 0;
   for(int i = 0; i < copied; ++i)
   {
      if(MathIsValidNumber(atr_values[i]) && atr_values[i] > 0.0)
      {
         atr_average += atr_values[i];
         atr_count++;
      }
   }
   if(atr_count <= 0)
   {
      details = "ATR average unavailable";
      return false;
   }
   atr_average /= atr_count;
   const double atr_ratio = atr_average > 0.0 ? atr / atr_average : 0.0;
   if(atr_ratio < MinimumATRRatio || atr_ratio > MaximumATRRatio)
   {
      details = StringFormat("ATR ratio %.2f outside %.2f-%.2f", atr_ratio, MinimumATRRatio, MaximumATRRatio);
      return false;
   }

   const double h4_close = iClose(_Symbol, PERIOD_H4, 1);
   const double h1_close = iClose(_Symbol, PERIOD_H1, 1);
   const double m15_close = iClose(_Symbol, PERIOD_M15, 1);
   const double m5_close = iClose(_Symbol, PERIOD_M5, 1);
   if(h4_close <= 0.0 || h1_close <= 0.0 || m15_close <= 0.0 || m5_close <= 0.0)
   {
      details = "closed candle data unavailable";
      return false;
   }

   int buy_votes = 0;
   int sell_votes = 0;
   string vote_text = "";

   if(h4_close > h4_fast && h4_fast > h4_slow)
   {
      buy_votes++;
      vote_text += "H4=BUY ";
   }
   else if(h4_close < h4_fast && h4_fast < h4_slow)
   {
      sell_votes++;
      vote_text += "H4=SELL ";
   }
   else
      vote_text += "H4=FLAT ";

   if(h1_close > h1_fast)
   {
      buy_votes++;
      vote_text += "H1=BUY ";
   }
   else if(h1_close < h1_fast)
   {
      sell_votes++;
      vote_text += "H1=SELL ";
   }
   else
      vote_text += "H1=FLAT ";

   if(m15_close > m15_ema && m15_rsi >= BuyRSIMinimum)
   {
      buy_votes++;
      vote_text += "M15=BUY ";
   }
   else if(m15_close < m15_ema && m15_rsi <= SellRSIMaximum)
   {
      sell_votes++;
      vote_text += "M15=SELL ";
   }
   else
      vote_text += "M15=FLAT ";

   double range_high = -DBL_MAX;
   double range_low = DBL_MAX;
   for(int i = 2; i <= BreakoutLookbackBars + 1; ++i)
   {
      range_high = MathMax(range_high, iHigh(_Symbol, PERIOD_M5, i));
      range_low = MathMin(range_low, iLow(_Symbol, PERIOD_M5, i));
   }
   if(m5_close > range_high)
   {
      buy_votes++;
      vote_text += "M5=BUY ";
   }
   else if(m5_close < range_low)
   {
      sell_votes++;
      vote_text += "M5=SELL ";
   }
   else
      vote_text += "M5=FLAT ";

   if(buy_votes >= MinimumVotes && buy_votes > sell_votes)
   {
      direction = 1;
      votes = buy_votes;
   }
   else if(sell_votes >= MinimumVotes && sell_votes > buy_votes)
   {
      direction = -1;
      votes = sell_votes;
   }

   details = StringFormat("%s|buy=%d sell=%d atr_ratio=%.2f spread=%.1f",
                          vote_text, buy_votes, sell_votes, atr_ratio, CurrentSpreadPoints());
   return direction != 0;
}

double MoneyPerPriceUnitPerLot()
{
   const double tick_value = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
   const double tick_size = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tick_value > 0.0 && tick_size > 0.0)
      return tick_value / tick_size;
   const double contract_size = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_CONTRACT_SIZE);
   return contract_size > 0.0 ? contract_size : 0.0;
}

double CalculateVolume(const double entry, const double sl)
{
   const double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   const double risk_money = equity * RiskPercent / 100.0;
   const double money_per_price = MoneyPerPriceUnitPerLot();
   const double risk_price = MathAbs(entry - sl);
   if(equity <= 0.0 || risk_money <= 0.0 || money_per_price <= 0.0 || risk_price <= 0.0)
      return 0.0;

   const double requested = risk_money / (risk_price * money_per_price);
   const double volume = NormalizeVolume(requested);
   const double actual_risk = volume * risk_price * money_per_price;
   const double max_risk_money = equity * MaximumRiskPercent / 100.0;
   if(actual_risk > max_risk_money * 1.02)
   {
      LogEvent("GUARD_BLOCK", StringFormat("actual_risk=%.2f max=%.2f", actual_risk, max_risk_money));
      return 0.0;
   }
   return volume;
}

bool StopsAreValid(const double entry, const double sl, const double tp)
{
   const int minimum_points = MathMax((int)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL),
                                      (int)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_FREEZE_LEVEL));
   if(minimum_points <= 0)
      return true;
   const double minimum_distance = minimum_points * _Point;
   return MathAbs(entry - sl) >= minimum_distance && MathAbs(tp - entry) >= minimum_distance;
}

bool PlaceCouncilTrade(const int direction, const double atr, const string details)
{
   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick))
      return false;

   const double entry = direction > 0 ? tick.ask : tick.bid;
   const double risk_price = atr * StopLossATRMultiplier;
   const double sl = NormalizePrice(direction > 0 ? entry - risk_price : entry + risk_price);
   const double actual_risk = MathAbs(entry - sl);
   const double tp = NormalizePrice(direction > 0 ? entry + actual_risk * RewardRisk
                                                  : entry - actual_risk * RewardRisk);
   if(!StopsAreValid(entry, sl, tp))
   {
      LogEvent("ORDER_BLOCK", "broker stop/freeze distance rejected the plan");
      return false;
   }

   const double volume = CalculateVolume(entry, sl);
   if(volume <= 0.0)
   {
      LogEvent("ORDER_BLOCK", "volume/risk calculation rejected the plan");
      return false;
   }

   trade.SetExpertMagicNumber(MagicNumber);
   trade.SetDeviationInPoints(MaximumSlippagePoints);
   trade.SetTypeFillingBySymbol(_Symbol);
   bool ok = false;
   if(direction > 0)
      ok = trade.Buy(volume, _Symbol, entry, sl, tp, TradeComment);
   else
      ok = trade.Sell(volume, _Symbol, entry, sl, tp, TradeComment);

   LogEvent(ok ? "ORDER_SENT" : "ORDER_FAILED",
            StringFormat("dir=%s votes=%s lot=%.2f entry=%.*f sl=%.*f tp=%.*f retcode=%u",
                         direction > 0 ? "BUY" : "SELL",
                         details,
                         volume,
                         (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS), entry,
                         (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS), sl,
                         (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS), tp,
                         trade.ResultRetcode()));
   if(ok)
      blocked_until = TimeCurrent() + 60;
   return ok;
}

void ManagePositions()
{
   double atr = 0.0;
   ReadIndicatorValue(m5_atr_handle, 1, atr);
   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick))
      return;

   for(int i = PositionsTotal() - 1; i >= 0; --i)
   {
      const ulong ticket = PositionGetTicket(i);
      if(!IsOurPosition(ticket))
         continue;

      const ENUM_POSITION_TYPE type = (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
      const double entry = PositionGetDouble(POSITION_PRICE_OPEN);
      const double old_sl = PositionGetDouble(POSITION_SL);
      const double tp = PositionGetDouble(POSITION_TP);
      const double current = type == POSITION_TYPE_BUY ? tick.bid : tick.ask;
      const double risk = old_sl > 0.0 ? MathAbs(entry - old_sl) : atr * StopLossATRMultiplier;
      if(risk <= 0.0)
         continue;

      const double profit_r = type == POSITION_TYPE_BUY ? (current - entry) / risk : (entry - current) / risk;
      const datetime opened = (datetime)PositionGetInteger(POSITION_TIME);
      const double held_minutes = (TimeCurrent() - opened) / 60.0;

      if(profit_r >= BreakEvenAtR)
      {
         const double be_sl = NormalizePrice(type == POSITION_TYPE_BUY ? entry + BreakEvenLockPrice
                                                                       : entry - BreakEvenLockPrice);
         const bool improves = type == POSITION_TYPE_BUY ? (old_sl <= 0.0 || be_sl > old_sl)
                                                         : (old_sl <= 0.0 || be_sl < old_sl);
         if(improves && trade.PositionModify(_Symbol, be_sl, tp))
            LogEvent("BREAKEVEN", StringFormat("ticket=%I64u profit_r=%.2f", ticket, profit_r));
      }

      if(EnableTrailing && atr > 0.0 && profit_r >= 1.0)
      {
         const double trail_sl = NormalizePrice(type == POSITION_TYPE_BUY ? current - atr * TrailingATRMultiplier
                                                                          : current + atr * TrailingATRMultiplier);
         const bool improves = type == POSITION_TYPE_BUY ? trail_sl > old_sl : trail_sl < old_sl;
         if(improves && StopsAreValid(current, trail_sl, tp) && trade.PositionModify(_Symbol, trail_sl, tp))
            LogEvent("TRAILING", StringFormat("ticket=%I64u profit_r=%.2f", ticket, profit_r));
      }

      // Do not forcibly close profitable winners. Only cut a losing position
      // after the safety time limit; TP/SL/BE manage winners normally.
      if(MaximumLosingHoldMinutes > 0 && held_minutes >= MaximumLosingHoldMinutes && profit_r < 0.0)
      {
         if(trade.PositionClose(ticket))
            LogEvent("TIME_EXIT_LOSS", StringFormat("ticket=%I64u held=%.1f profit_r=%.2f", ticket, held_minutes, profit_r));
      }
   }
}

bool IsNewClosedM5Bar()
{
   const datetime bar = iTime(_Symbol, PERIOD_M5, 1);
   if(bar <= 0 || bar == last_signal_bar)
      return false;
   last_signal_bar = bar;
   return true;
}

int OnInit()
{
   if(!AllowRealAccount && IsRealAccount())
   {
      Print("Real account blocked. Attach this EA to a demo account.");
      return INIT_FAILED;
   }
   if(RestrictToGoldSymbols && !IsGoldSymbol())
   {
      Print("This EA is restricted to XAU/GOLD symbols.");
      return INIT_FAILED;
   }

   h4_fast_handle = iMA(_Symbol, PERIOD_H4, FastEMAPeriodH4, 0, MODE_EMA, PRICE_CLOSE);
   h4_slow_handle = iMA(_Symbol, PERIOD_H4, SlowEMAPeriodH4, 0, MODE_EMA, PRICE_CLOSE);
   h1_fast_handle = iMA(_Symbol, PERIOD_H1, FastEMAPeriodH1, 0, MODE_EMA, PRICE_CLOSE);
   m15_ema_handle = iMA(_Symbol, PERIOD_M15, MomentumEMAPeriodM15, 0, MODE_EMA, PRICE_CLOSE);
   m15_rsi_handle = iRSI(_Symbol, PERIOD_M15, RSIPeriodM15, PRICE_CLOSE);
   m5_atr_handle = iATR(_Symbol, PERIOD_M5, ATRPeriodM5);
   if(h4_fast_handle == INVALID_HANDLE || h4_slow_handle == INVALID_HANDLE ||
      h1_fast_handle == INVALID_HANDLE || m15_ema_handle == INVALID_HANDLE ||
      m15_rsi_handle == INVALID_HANDLE || m5_atr_handle == INVALID_HANDLE)
      return INIT_FAILED;

   trade.SetExpertMagicNumber(MagicNumber);
   trade.SetDeviationInPoints(MaximumSlippagePoints);
   LogEvent("START", "NewEraCouncilDemo started in demo-guarded mode");
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   if(h4_fast_handle != INVALID_HANDLE) IndicatorRelease(h4_fast_handle);
   if(h4_slow_handle != INVALID_HANDLE) IndicatorRelease(h4_slow_handle);
   if(h1_fast_handle != INVALID_HANDLE) IndicatorRelease(h1_fast_handle);
   if(m15_ema_handle != INVALID_HANDLE) IndicatorRelease(m15_ema_handle);
   if(m15_rsi_handle != INVALID_HANDLE) IndicatorRelease(m15_rsi_handle);
   if(m5_atr_handle != INVALID_HANDLE) IndicatorRelease(m5_atr_handle);
}

void OnTick()
{
   ManagePositions();
   if(OurPositionCount() >= MaximumOpenPositions)
      return;
   if(!IsNewClosedM5Bar())
      return;
   if(!GuardAllowsTrading())
      return;

   int direction = 0;
   int votes = 0;
   double atr = 0.0;
   string details = "";
   if(!CouncilSignal(direction, votes, details, atr))
   {
      LogEvent("NO_TRADE", details);
      return;
   }
   string ai_gate_details = "";
   if(!AIGateAllowsDirection(direction, ai_gate_details))
   {
      LogEvent("NO_TRADE", ai_gate_details);
      return;
   }
   if(ai_gate_details != "")
      details += "|" + ai_gate_details;
   LogEvent("COUNCIL_SIGNAL", StringFormat("direction=%s votes=%d %s", direction > 0 ? "BUY" : "SELL", votes, details));
   PlaceCouncilTrade(direction, atr, details);
}
