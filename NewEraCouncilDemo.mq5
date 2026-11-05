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
input double           RewardRisk               = 1.50;
input double           BreakEvenAtR             = 0.80;
input double           BreakEvenLockPrice       = 0.00;
input int              MaximumLosingHoldMinutes = 90;
input bool             EnableTrailing           = false;
input double           TrailingATRMultiplier    = 1.00;

input group "Diagnostics"
input bool             PrintDiagnostics         = true;
input bool             WriteCommonCsvLog        = true;
input string           CommonCsvFile            = "new_era_council_demo_log.csv";
input string           DiagnosticCsvFile        = "new_era_council_diagnostics.csv";

CTrade trade;
int h4_fast_handle = INVALID_HANDLE;
int h4_slow_handle = INVALID_HANDLE;
int h1_fast_handle = INVALID_HANDLE;
int m15_ema_handle = INVALID_HANDLE;
int m15_rsi_handle = INVALID_HANDLE;
int m5_atr_handle = INVALID_HANDLE;
datetime last_signal_bar = 0;
datetime blocked_until = 0;
datetime diagnostic_bar_time = 0;
string last_diagnostic_status = "STARTING";
string last_diagnostic_reason = "Waiting for the first closed M5 candle";
string last_diagnostic_h4 = "UNKNOWN";
string last_diagnostic_h1 = "UNKNOWN";
string last_diagnostic_m15 = "UNKNOWN";
string last_diagnostic_m5 = "UNKNOWN";
string last_diagnostic_decision = "ABSTAIN";
string last_diagnostic_ai_gate = "DISABLED";
string last_diagnostic_freshness = "N/A";
string last_diagnostic_risk = "NOT_CHECKED";
double last_diagnostic_spread = 0.0;
string last_ai_gate_state = "DISABLED";
string last_ai_gate_freshness = "N/A";
string last_risk_check_state = "NOT_CHECKED";

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
void LogDiagnostic(const datetime bar_time,
                   const double spread,
                   const string h4,
                   const string h1,
                   const string m15,
                   const string m5,
                   const string decision,
                   const string status,
                   const string reason);

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

void SetDiagnostic(const datetime bar_time,
                   const string status,
                   const string reason,
                   const string h4,
                   const string h1,
                   const string m15,
                   const string m5,
                   const string decision,
                   const double spread)
{
   diagnostic_bar_time = bar_time;
   last_diagnostic_status = status;
   last_diagnostic_reason = reason;
   last_diagnostic_h4 = h4;
   last_diagnostic_h1 = h1;
   last_diagnostic_m15 = m15;
   last_diagnostic_m5 = m5;
   last_diagnostic_decision = decision;
   last_diagnostic_ai_gate = last_ai_gate_state;
   last_diagnostic_freshness = last_ai_gate_freshness;
   last_diagnostic_risk = last_risk_check_state;
   last_diagnostic_spread = spread;

   LogDiagnostic(bar_time,
                 spread,
                 h4,
                 h1,
                 m15,
                 m5,
                 decision,
                 status,
                 reason);

   string bar_text = bar_time > 0 ? TimeToString(bar_time, TIME_DATE | TIME_MINUTES) : "n/a";
   string spread_text = spread == DBL_MAX ? "UNAVAILABLE" : DoubleToString(spread, 1);
   Comment("NEW ERA MT5 EXECUTION AUDIT\n",
           "Status: ", status, " | Decision: ", decision, "\n",
           "Symbol: ", _Symbol, " | Timeframe: M5 | Closed bar: ", bar_text, "\n",
           "Spread: ", spread_text, " pts | H4=", h4, " H1=", h1,
           " M15=", m15, " M5=", m5, "\n",
           "AI gate: ", last_ai_gate_state, " | Freshness: ", last_ai_gate_freshness,
           " | Risk checks: ", last_risk_check_state, "\n",
           "Reason: ", reason);
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
   {
      last_ai_gate_state = "DISABLED";
      last_ai_gate_freshness = "N/A";
      details = "AI_GATE=DISABLED";
      return true;
   }

   AIGateData gate;
   string reason = "";
   if(!ReadAIGate(gate, reason))
   {
      string lowered = reason;
      StringToLower(lowered);
      last_ai_gate_state = "ABSTAIN";
      last_ai_gate_freshness = (StringFind(lowered, "expired") >= 0 ||
                                StringFind(lowered, "older") >= 0 ||
                                StringFind(lowered, "clock") >= 0)
                               ? "STALE"
                               : "INVALID";
      details = reason;
      LogEvent("AI_GATE_BLOCK", reason);
      return false;
   }

   const string expected = direction > 0 ? "BUY" : "SELL";
   if(gate.action != expected)
   {
      last_ai_gate_state = "ABSTAIN";
      last_ai_gate_freshness = "VALID_BUT_DIRECTION_MISMATCH";
      details = StringFormat("AI gate=%s deterministic=%s", gate.action, expected);
      LogEvent("AI_GATE_BLOCK", details);
      return false;
   }

   last_ai_gate_state = gate.action;
   last_ai_gate_freshness = "FRESH";
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

void LogDiagnostic(const datetime bar_time,
                   const double spread,
                   const string h4,
                   const string h1,
                   const string m15,
                   const string m5,
                   const string decision,
                   const string status,
                   const string reason)
{
   if(!WriteCommonCsvLog)
      return;

   int handle = FileOpen(DiagnosticCsvFile,
                         FILE_READ | FILE_WRITE | FILE_CSV | FILE_COMMON | FILE_ANSI,
                         ',');
   if(handle == INVALID_HANDLE)
      return;
   if(FileSize(handle) == 0)
   {
      FileWrite(handle,
                "time",
                "symbol",
                "timeframe",
                "closed_bar_time",
                "spread_points",
                "h4",
                "h1",
                "m15",
                "m5",
                "ai_gate",
                "freshness",
                "risk_checks",
                "decision",
                "status",
                "reason");
   }
   FileSeek(handle, 0, SEEK_END);
   FileWrite(handle,
             TimeToString(TimeCurrent(), TIME_DATE | TIME_SECONDS),
             _Symbol,
             "M5",
             bar_time > 0 ? TimeToString(bar_time, TIME_DATE | TIME_SECONDS) : "",
             spread == DBL_MAX ? "" : DoubleToString(spread, 1),
             h4,
             h1,
             m15,
             m5,
             last_ai_gate_state,
             last_ai_gate_freshness,
             last_risk_check_state,
             decision,
             status,
             reason);
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

bool GuardAllowsTrading(string &reason)
{
   reason = "";
   last_risk_check_state = "NOT_CHECKED";
   if(TimeCurrent() < blocked_until)
   {
      reason = StringFormat("symbol cooldown active until %s",
                            TimeToString(blocked_until, TIME_DATE | TIME_SECONDS));
      last_risk_check_state = "COOLDOWN_BLOCK";
      return false;
   }
   const double spread = CurrentSpreadPoints();
   if(spread == DBL_MAX)
   {
      reason = "current bid/ask unavailable";
      last_risk_check_state = "MARKET_DATA_BLOCK";
      return false;
   }
   if(MaximumSpreadPoints > 0.0 && spread > MaximumSpreadPoints)
   {
      reason = StringFormat("spread=%.1f exceeds maximum %.1f points", spread, MaximumSpreadPoints);
      last_risk_check_state = "SPREAD_BLOCK";
      LogEvent("GUARD_BLOCK", reason);
      return false;
   }

   int trades = 0;
   int losses = 0;
   int consecutive_losses = 0;
   double net = 0.0;
   TodayStats(trades, losses, net, consecutive_losses);
   if(MaximumTradesPerDay > 0 && trades >= MaximumTradesPerDay)
   {
      reason = StringFormat("daily completed trades=%d reaches maximum %d", trades, MaximumTradesPerDay);
      last_risk_check_state = "DAILY_TRADE_LIMIT";
      LogEvent("GUARD_BLOCK", reason);
      return false;
   }
   if(MaximumDailyLossMoney > 0.0 && net <= -MaximumDailyLossMoney)
   {
      reason = StringFormat("daily net P/L=%.2f reaches loss limit %.2f", net, -MaximumDailyLossMoney);
      last_risk_check_state = "DAILY_LOSS_LIMIT";
      LogEvent("GUARD_BLOCK", reason);
      return false;
   }
   if(MaximumConsecutiveLosses > 0 && consecutive_losses >= MaximumConsecutiveLosses)
   {
      reason = StringFormat("consecutive losses=%d reaches maximum %d",
                            consecutive_losses, MaximumConsecutiveLosses);
      last_risk_check_state = "CONSECUTIVE_LOSS_LIMIT";
      LogEvent("GUARD_BLOCK", reason);
      return false;
   }
   last_risk_check_state = "PASS";
   return true;
}

bool CouncilSignal(int &direction,
                   int &votes,
                   string &details,
                   double &atr,
                   string &h4_state,
                   string &h1_state,
                   string &m15_state,
                   string &m5_state,
                   string &signal_status)
{
   direction = 0;
   votes = 0;
   details = "";
   atr = 0.0;
   h4_state = "UNKNOWN";
   h1_state = "UNKNOWN";
   m15_state = "UNKNOWN";
   m5_state = "UNKNOWN";
   signal_status = "NO SIGNAL";

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
      signal_status = "SIGNAL BLOCKED BY FILTER";
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
      h4_state = "BUY";
      vote_text += "H4=BUY ";
   }
   else if(h4_close < h4_fast && h4_fast < h4_slow)
   {
      sell_votes++;
      h4_state = "SELL";
      vote_text += "H4=SELL ";
   }
   else
   {
      h4_state = "FLAT";
      vote_text += "H4=FLAT ";
   }

   if(h1_close > h1_fast)
   {
      buy_votes++;
      h1_state = "BUY";
      vote_text += "H1=BUY ";
   }
   else if(h1_close < h1_fast)
   {
      sell_votes++;
      h1_state = "SELL";
      vote_text += "H1=SELL ";
   }
   else
   {
      h1_state = "FLAT";
      vote_text += "H1=FLAT ";
   }

   if(m15_close > m15_ema && m15_rsi >= BuyRSIMinimum)
   {
      buy_votes++;
      m15_state = "BUY";
      vote_text += "M15=BUY ";
   }
   else if(m15_close < m15_ema && m15_rsi <= SellRSIMaximum)
   {
      sell_votes++;
      m15_state = "SELL";
      vote_text += "M15=SELL ";
   }
   else
   {
      m15_state = "FLAT";
      vote_text += "M15=FLAT ";
   }

   double range_high = -DBL_MAX;
   double range_low = DBL_MAX;
   bool breakout_data_valid = true;
   for(int i = 2; i <= BreakoutLookbackBars + 1; ++i)
   {
      const double high = iHigh(_Symbol, PERIOD_M5, i);
      const double low = iLow(_Symbol, PERIOD_M5, i);
      if(high <= 0.0 || low <= 0.0)
      {
         breakout_data_valid = false;
         break;
      }
      range_high = MathMax(range_high, high);
      range_low = MathMin(range_low, low);
   }
   if(!breakout_data_valid)
   {
      details = "closed M5 breakout history unavailable";
      return false;
   }
   if(m5_close > range_high)
   {
      buy_votes++;
      m5_state = "BUY";
      vote_text += "M5=BUY ";
   }
   else if(m5_close < range_low)
   {
      sell_votes++;
      m5_state = "SELL";
      vote_text += "M5=SELL ";
   }
   else
   {
      m5_state = "FLAT";
      vote_text += "M5=FLAT ";
   }

   const int required_votes = MinimumVotes < 4 ? 4 : MinimumVotes;
   if(buy_votes >= required_votes && buy_votes > sell_votes)
   {
      direction = 1;
      votes = buy_votes;
   }
   else if(sell_votes >= required_votes && sell_votes > buy_votes)
   {
      direction = -1;
      votes = sell_votes;
   }

   details = StringFormat("%s|buy=%d sell=%d atr_ratio=%.2f spread=%.1f",
                           vote_text, buy_votes, sell_votes, atr_ratio, CurrentSpreadPoints());
   if(direction != 0)
      signal_status = "SIGNAL READY";
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

double CalculateVolume(const double entry, const double sl, string &reason)
{
   reason = "";
   const double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   const double risk_money = equity * RiskPercent / 100.0;
   const double money_per_price = MoneyPerPriceUnitPerLot();
   const double risk_price = MathAbs(entry - sl);
   if(!MathIsValidNumber(equity) || !MathIsValidNumber(risk_money) ||
      !MathIsValidNumber(money_per_price) || !MathIsValidNumber(risk_price) ||
      equity <= 0.0 || risk_money <= 0.0 || money_per_price <= 0.0 || risk_price <= 0.0)
   {
      reason = "equity, risk budget, tick value, or stop distance is invalid";
      last_risk_check_state = "VOLUME_BLOCK";
      return 0.0;
   }

   const double requested = risk_money / (risk_price * money_per_price);
   const double volume = NormalizeVolume(requested);
   if(!MathIsValidNumber(volume) || volume <= 0.0)
   {
      reason = "volume normalization returned an invalid or zero volume";
      last_risk_check_state = "VOLUME_BLOCK";
      return 0.0;
   }
   const double actual_risk = volume * risk_price * money_per_price;
   const double max_risk_money = equity * MaximumRiskPercent / 100.0;
   if(actual_risk > max_risk_money * 1.02)
   {
      reason = StringFormat("normalized volume risk=%.2f exceeds maximum %.2f",
                            actual_risk, max_risk_money);
      last_risk_check_state = "RISK_BLOCK";
      LogEvent("GUARD_BLOCK", reason);
      return 0.0;
   }
   last_risk_check_state = "PASS";
   return volume;
}

bool TradingPermissionsAllow(const int direction, string &reason)
{
   reason = "";
   if(!TerminalInfoInteger(TERMINAL_CONNECTED))
   {
      reason = "MT5 terminal is not connected";
      return false;
   }
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED))
   {
      reason = "terminal automated trading is disabled";
      return false;
   }
   if(!MQLInfoInteger(MQL_TRADE_ALLOWED))
   {
      reason = "EA trading permission is disabled (MQL_TRADE_ALLOWED=false)";
      return false;
   }
   if(!AccountInfoInteger(ACCOUNT_TRADE_ALLOWED))
   {
      reason = "account trading permission is disabled";
      return false;
   }
   if(!AccountInfoInteger(ACCOUNT_TRADE_EXPERT))
   {
      reason = "expert trading permission is disabled for this account";
      return false;
   }

   const long trade_mode = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_MODE);
   if(trade_mode == SYMBOL_TRADE_MODE_DISABLED || trade_mode == SYMBOL_TRADE_MODE_CLOSEONLY)
   {
      reason = StringFormat("symbol trade mode=%I64d does not permit opening positions", trade_mode);
      return false;
   }
   if(direction > 0 && trade_mode == SYMBOL_TRADE_MODE_SHORTONLY)
   {
      reason = "symbol is SHORTONLY; BUY is not permitted";
      return false;
   }
   if(direction < 0 && trade_mode == SYMBOL_TRADE_MODE_LONGONLY)
   {
      reason = "symbol is LONGONLY; SELL is not permitted";
      return false;
   }

   const long order_mode = SymbolInfoInteger(_Symbol, SYMBOL_ORDER_MODE);
   if((order_mode & SYMBOL_ORDER_MARKET) == 0)
   {
      reason = "symbol does not permit market orders";
      return false;
   }
   const long filling_mode = SymbolInfoInteger(_Symbol, SYMBOL_FILLING_MODE);
   const long execution_mode = SymbolInfoInteger(_Symbol, SYMBOL_TRADE_EXEMODE);
   if(execution_mode == SYMBOL_TRADE_EXECUTION_MARKET && filling_mode <= 0)
   {
      reason = "symbol has no valid filling mode for market execution";
      return false;
   }
   return true;
}

bool StopsAreValid(const int direction,
                   const double entry,
                   const double sl,
                   const double tp,
                   string &reason)
{
   reason = "";
   if(!MathIsValidNumber(entry) || !MathIsValidNumber(sl) || !MathIsValidNumber(tp) ||
      entry <= 0.0 || sl <= 0.0 || tp <= 0.0)
   {
      reason = "entry, stop-loss, and take-profit must be positive finite prices";
      return false;
   }
   if(direction > 0 && !(sl < entry && tp > entry))
   {
      reason = StringFormat("BUY geometry invalid: require SL < entry < TP (%.2f, %.2f, %.2f)",
                            sl, entry, tp);
      return false;
   }
   if(direction < 0 && !(tp < entry && sl > entry))
   {
      reason = StringFormat("SELL geometry invalid: require TP < entry < SL (%.2f, %.2f, %.2f)",
                            tp, entry, sl);
      return false;
   }

   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick) || tick.bid <= 0.0 || tick.ask <= 0.0)
   {
      reason = "live bid/ask unavailable while validating stops";
      return false;
   }
   const int minimum_points = (int)MathMax((double)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL),
                                           (double)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_FREEZE_LEVEL));
   const double minimum_distance = MathMax(0, minimum_points) * _Point;
   const double reference = direction > 0 ? tick.bid : tick.ask;
   if(direction > 0 && (reference - sl < minimum_distance || tp - reference < minimum_distance))
   {
      reason = StringFormat("BUY stops are inside stop/freeze distance: minimum=%.5f", minimum_distance);
      return false;
   }
   if(direction < 0 && (sl - reference < minimum_distance || reference - tp < minimum_distance))
   {
      reason = StringFormat("SELL stops are inside stop/freeze distance: minimum=%.5f", minimum_distance);
      return false;
   }
   return true;
}

bool SuccessfulTradeRetcode(const uint retcode)
{
   return retcode == TRADE_RETCODE_DONE ||
          retcode == TRADE_RETCODE_DONE_PARTIAL ||
          retcode == TRADE_RETCODE_PLACED;
}

bool PlaceCouncilTrade(const int direction,
                       const double atr,
                       const string details,
                       string &order_status,
                       string &order_reason)
{
   order_status = "ORDER VALIDATION FAILED";
   order_reason = "";
   last_risk_check_state = "NOT_CHECKED";

   string permission_reason = "";
   if(!TradingPermissionsAllow(direction, permission_reason))
   {
      order_reason = permission_reason;
      LogEvent("ORDER_BLOCK", order_reason);
      return false;
   }

   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick) || tick.bid <= 0.0 || tick.ask <= 0.0)
   {
      order_reason = "live bid/ask unavailable";
      LogEvent("ORDER_BLOCK", order_reason);
      return false;
   }
   if(!MathIsValidNumber(atr) || atr <= 0.0 || StopLossATRMultiplier <= 0.0)
   {
      order_reason = "ATR or stop-loss multiplier is invalid";
      last_risk_check_state = "ATR_BLOCK";
      LogEvent("ORDER_BLOCK", order_reason);
      return false;
   }

   const double entry = direction > 0 ? tick.ask : tick.bid;
   const double risk_price = atr * StopLossATRMultiplier;
   const double sl = NormalizePrice(direction > 0 ? entry - risk_price : entry + risk_price);
   const double actual_risk = MathAbs(entry - sl);
   const double tp = NormalizePrice(direction > 0 ? entry + actual_risk * RewardRisk
                                                    : entry - actual_risk * RewardRisk);
   if(actual_risk <= 0.0 || MathAbs(tp - entry) / actual_risk + 1e-9 < 1.50)
   {
      order_reason = "normalized prices would produce reward:risk below the hard 1.50 minimum";
      last_risk_check_state = "RR_BLOCK";
      LogEvent("ORDER_BLOCK", order_reason);
      return false;
   }
   if(actual_risk / atr - 1e-9 > 2.50)
   {
      order_reason = "normalized stop distance exceeds the hard 2.50 ATR maximum";
      last_risk_check_state = "ATR_BLOCK";
      LogEvent("ORDER_BLOCK", order_reason);
      return false;
   }
   string stops_reason = "";
   if(!StopsAreValid(direction, entry, sl, tp, stops_reason))
   {
      order_reason = stops_reason;
      LogEvent("ORDER_BLOCK", order_reason);
      return false;
   }

   string volume_reason = "";
   const double volume = CalculateVolume(entry, sl, volume_reason);
   if(volume <= 0.0)
   {
      order_reason = volume_reason == "" ? "volume/risk calculation rejected the plan" : volume_reason;
      LogEvent("ORDER_BLOCK", order_reason);
      return false;
   }

   const ENUM_ORDER_TYPE order_type = direction > 0 ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   double margin_required = 0.0;
   if(!OrderCalcMargin(order_type, _Symbol, volume, entry, margin_required) ||
      !MathIsValidNumber(margin_required) || margin_required <= 0.0)
   {
      order_reason = "OrderCalcMargin failed for the normalized order volume";
      last_risk_check_state = "MARGIN_BLOCK";
      LogEvent("ORDER_BLOCK", order_reason);
      return false;
   }
   const double free_margin = AccountInfoDouble(ACCOUNT_MARGIN_FREE);
   if(!MathIsValidNumber(free_margin) || free_margin < margin_required)
   {
      order_reason = StringFormat("free margin %.2f is below required margin %.2f",
                                  free_margin, margin_required);
      last_risk_check_state = "MARGIN_BLOCK";
      LogEvent("ORDER_BLOCK", order_reason);
      return false;
   }
   last_risk_check_state = "PASS";

   trade.SetExpertMagicNumber(MagicNumber);
   trade.SetDeviationInPoints(MaximumSlippagePoints);
   trade.SetTypeFillingBySymbol(_Symbol);
   bool ok = false;
   if(direction > 0)
      ok = trade.Buy(volume, _Symbol, entry, sl, tp, TradeComment);
   else
      ok = trade.Sell(volume, _Symbol, entry, sl, tp, TradeComment);

   const uint retcode = trade.ResultRetcode();
   const string retcode_description = trade.ResultRetcodeDescription();
   const string result_details = StringFormat(
      "dir=%s votes=%s local_return=%s retcode=%u description=%s order=%I64u deal=%I64u lot=%.2f entry=%.*f sl=%.*f tp=%.*f",
      direction > 0 ? "BUY" : "SELL",
      details,
      ok ? "true" : "false",
      retcode,
      retcode_description,
      trade.ResultOrder(),
      trade.ResultDeal(),
      volume,
      (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS), entry,
      (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS), sl,
      (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS), tp);
   if(ok && SuccessfulTradeRetcode(retcode))
   {
      order_status = "ORDER SUCCESSFULLY EXECUTED";
      order_reason = result_details;
      LogEvent("ORDER_SUCCESSFULLY_EXECUTED", result_details);
      blocked_until = TimeCurrent() + 60;
      return true;
   }

   order_status = "BROKER REJECTED ORDER";
   order_reason = result_details;
   LogEvent("BROKER_REJECTED_ORDER", result_details);
   return false;
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
          if(improves)
          {
             const bool modify_ok = trade.PositionModify(_Symbol, be_sl, tp);
             const uint modify_retcode = trade.ResultRetcode();
             if(modify_ok && SuccessfulTradeRetcode(modify_retcode))
                LogEvent("BREAKEVEN", StringFormat("ticket=%I64u profit_r=%.2f retcode=%u description=%s",
                                                     ticket, profit_r, modify_retcode,
                                                     trade.ResultRetcodeDescription()));
             else
                LogEvent("MANAGEMENT_REJECTED", StringFormat("breakeven ticket=%I64u local_return=%s retcode=%u description=%s",
                                                               ticket, modify_ok ? "true" : "false",
                                                               modify_retcode,
                                                               trade.ResultRetcodeDescription()));
          }
      }

      if(EnableTrailing && atr > 0.0 && profit_r >= 1.0)
      {
         const double trail_sl = NormalizePrice(type == POSITION_TYPE_BUY ? current - atr * TrailingATRMultiplier
                                                                          : current + atr * TrailingATRMultiplier);
         const bool improves = type == POSITION_TYPE_BUY ? trail_sl > old_sl : trail_sl < old_sl;
          string trailing_reason = "";
          if(improves && StopsAreValid(type == POSITION_TYPE_BUY ? 1 : -1,
                                       current,
                                       trail_sl,
                                       tp,
                                       trailing_reason))
          {
             const bool modify_ok = trade.PositionModify(_Symbol, trail_sl, tp);
             const uint modify_retcode = trade.ResultRetcode();
             if(modify_ok && SuccessfulTradeRetcode(modify_retcode))
                LogEvent("TRAILING", StringFormat("ticket=%I64u profit_r=%.2f retcode=%u description=%s",
                                                    ticket, profit_r, modify_retcode,
                                                    trade.ResultRetcodeDescription()));
             else
                LogEvent("MANAGEMENT_REJECTED", StringFormat("trailing ticket=%I64u local_return=%s retcode=%u description=%s",
                                                               ticket, modify_ok ? "true" : "false",
                                                               modify_retcode,
                                                               trade.ResultRetcodeDescription()));
          }
          else if(improves && trailing_reason != "")
             LogEvent("MANAGEMENT_BLOCK", StringFormat("trailing ticket=%I64u reason=%s", ticket, trailing_reason));
      }

      // Do not forcibly close profitable winners. Only cut a losing position
      // after the safety time limit; TP/SL/BE manage winners normally.
      if(MaximumLosingHoldMinutes > 0 && held_minutes >= MaximumLosingHoldMinutes && profit_r < 0.0)
      {
          const bool close_ok = trade.PositionClose(ticket);
          const uint close_retcode = trade.ResultRetcode();
          if(close_ok && SuccessfulTradeRetcode(close_retcode))
             LogEvent("TIME_EXIT_LOSS", StringFormat("ticket=%I64u held=%.1f profit_r=%.2f retcode=%u description=%s",
                                                      ticket, held_minutes, profit_r,
                                                      close_retcode,
                                                      trade.ResultRetcodeDescription()));
          else
             LogEvent("MANAGEMENT_REJECTED", StringFormat("time-exit ticket=%I64u local_return=%s retcode=%u description=%s",
                                                           ticket, close_ok ? "true" : "false",
                                                           close_retcode,
                                                           trade.ResultRetcodeDescription()));
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
   if(AllowRealAccount)
   {
      Print("AllowRealAccount=true is not supported: this EA is permanently demo-only.");
      return INIT_PARAMETERS_INCORRECT;
   }
   if((ENUM_ACCOUNT_TRADE_MODE)AccountInfoInteger(ACCOUNT_TRADE_MODE) != ACCOUNT_TRADE_MODE_DEMO)
   {
      Print("Non-demo account blocked. Attach this EA to a DEMO account.");
      return INIT_FAILED;
   }
   if(RestrictToGoldSymbols && !IsGoldSymbol())
   {
      Print("This EA is restricted to XAU/GOLD symbols.");
      return INIT_FAILED;
   }
   if(_Period != PERIOD_M5)
   {
      Print("Attach this EA to an M5 chart; its signal clock is the closed M5 candle.");
      return INIT_FAILED;
   }
   if(RiskPercent <= 0.0 || MaximumRiskPercent <= 0.0 || RiskPercent > MaximumRiskPercent ||
      MaximumRiskPercent > 1.50 || RewardRisk < 1.50 || StopLossATRMultiplier <= 0.0 ||
      StopLossATRMultiplier > 2.50 || MinimumVotes < 4 || BreakoutLookbackBars < 1 ||
      ATRPeriodM5 < 1 || ATRAverageBarsM5 < 50 || MaximumOpenPositions < 1 ||
      MaximumSpreadPoints < 0.0 || MaximumSlippagePoints < 0 ||
      AIGateMinimumConfidence < 0.0 || AIGateMinimumConfidence > 1.0)
   {
      Print("Invalid safety inputs. Require risk <= 1.5%, R:R >= 1.5, SL <= 2.5 ATR, and MinimumVotes >= 4.");
      return INIT_PARAMETERS_INCORRECT;
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
   SetDiagnostic(0,
                 "NO SIGNAL",
                 "EA initialized; waiting for a completed M5 candle",
                 "UNKNOWN",
                 "UNKNOWN",
                 "UNKNOWN",
                 "UNKNOWN",
                 "ABSTAIN",
                 CurrentSpreadPoints());
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
   Comment("");
}

void OnTick()
{
   ManagePositions();
   if(!IsNewClosedM5Bar())
      return;

   const datetime closed_bar = iTime(_Symbol, PERIOD_M5, 1);
   const double spread = CurrentSpreadPoints();
   last_ai_gate_state = EnableExternalAIGate ? "PENDING" : "DISABLED";
   last_ai_gate_freshness = EnableExternalAIGate ? "PENDING" : "N/A";
   last_risk_check_state = "NOT_CHECKED";

   int direction = 0;
   int votes = 0;
   double atr = 0.0;
   string details = "";
   string h4_state = "UNKNOWN";
   string h1_state = "UNKNOWN";
   string m15_state = "UNKNOWN";
   string m5_state = "UNKNOWN";
   string signal_status = "NO SIGNAL";
   const bool signal_available = CouncilSignal(direction,
                                               votes,
                                               details,
                                               atr,
                                               h4_state,
                                               h1_state,
                                               m15_state,
                                               m5_state,
                                               signal_status);

   string guard_reason = "";
   if(!GuardAllowsTrading(guard_reason))
   {
      SetDiagnostic(closed_bar,
                    "SIGNAL BLOCKED BY FILTER",
                    guard_reason,
                    h4_state,
                    h1_state,
                    m15_state,
                    m5_state,
                    "ABSTAIN",
                    spread);
      return;
   }

   if(OurPositionCount() >= MaximumOpenPositions)
   {
      const string reason = StringFormat("open position limit reached: %d/%d",
                                         OurPositionCount(), MaximumOpenPositions);
      last_risk_check_state = "POSITION_LIMIT";
      SetDiagnostic(closed_bar,
                    "SIGNAL BLOCKED BY FILTER",
                    reason,
                    h4_state,
                    h1_state,
                    m15_state,
                    m5_state,
                    "ABSTAIN",
                    spread);
      LogEvent("FILTER_BLOCK", reason);
      return;
   }

   if(!signal_available)
   {
      SetDiagnostic(closed_bar,
                    signal_status,
                    details,
                    h4_state,
                    h1_state,
                    m15_state,
                    m5_state,
                    "ABSTAIN",
                    spread);
      LogEvent("NO_TRADE", details);
      return;
   }
   string ai_gate_details = "";
   if(!AIGateAllowsDirection(direction, ai_gate_details))
   {
      string lowered_gate_reason = ai_gate_details;
      StringToLower(lowered_gate_reason);
      const string gate_status = (StringFind(lowered_gate_reason, "expired") >= 0 ||
                                  StringFind(lowered_gate_reason, "older") >= 0 ||
                                  StringFind(lowered_gate_reason, "clock") >= 0)
                                 ? "SIGNAL STALE"
                                 : "AI GATE ABSTAIN";
      SetDiagnostic(closed_bar,
                    gate_status,
                    ai_gate_details,
                    h4_state,
                    h1_state,
                    m15_state,
                    m5_state,
                    "ABSTAIN",
                    spread);
      LogEvent("NO_TRADE", ai_gate_details);
      return;
   }
   if(ai_gate_details != "")
      details += "|" + ai_gate_details;
   LogEvent("COUNCIL_SIGNAL", StringFormat("direction=%s votes=%d %s", direction > 0 ? "BUY" : "SELL", votes, details));
   string order_status = "ORDER VALIDATION FAILED";
   string order_reason = "";
   PlaceCouncilTrade(direction, atr, details, order_status, order_reason);
   SetDiagnostic(closed_bar,
                 order_status,
                 order_reason,
                 h4_state,
                 h1_state,
                 m15_state,
                 m5_state,
                 direction > 0 ? "BUY" : "SELL",
                 spread);
}
