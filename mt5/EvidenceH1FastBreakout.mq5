#property strict
#property version   "2.20"
#property description "Clean-room XAUUSD H1 pending-breakout reconstruction with a validated long-only default."
#property description "No proprietary code was copied or decompiled. Demo/tester use is the default."

#include <Trade/Trade.mqh>

input group "Identity and safety"
input ulong            MagicNumber                 = 16155431;
input string           TradeComment                = "EFH1_LONG_V220";
input bool             AllowRealAccount            = false;
input bool             RestrictToGoldSymbols       = true;

input group "Order size"
input double           FixedLots                   = 0.03;
input bool             UseRiskBasedLots            = true;
input double           RiskPerTradePercent         = 3.00;
input double           MinimumRiskLots             = 0.01;
input double           MaximumRiskLots             = 1.00;
input int              MaxOpenPositions            = 1;
input int              MaxPendingOrders            = 14;
input int              MaxTradesPerDay             = 6;

input group "H1 peak-level setup"
input ENUM_TIMEFRAMES  SignalTimeframe             = PERIOD_H1;
input string           PlacementHours              = "03,09,15";
input bool             EnableLongs                 = true;
input bool             EnableShorts                = false;
input int              PlacementWindowMinutes      = 5;
input int              BreakoutLookbackBars        = 72;
input int              SwingStrength               = 2;
input double           EntryBufferPrice            = 10.00;
input double           MinimumEntryDistancePrice   = 1.00;
input double           DuplicateLevelTolerance     = 0.20;
input int              PendingExpiryHours          = 168;
input bool             CancelPendingOnTrendFlip    = false;
input bool             CancelPendingOnWideSpread   = false;
input bool             OnePendingPerDirection      = false;
input bool             CancelAllPendingOnEntry     = true;
input bool             CloseExcessPositionsOnEntry = true;

input group "Optional context and volatility"
input bool             UseTrendSideFilter          = false;
input bool             FilterLongsWithTrend        = false;
input bool             FilterShortsWithTrend       = true;
input bool             UseEMATrend                 = true;
input int              FastEMAPeriod               = 20;
input int              SlowEMAPeriod               = 50;
input bool             RequireFastEMASlope         = true;
input bool             UseADXConfirmation          = false;
input int              ADXPeriod                   = 14;
input double           MinimumADX                  = 15.0;
input bool             UseMomentumFallback         = false;
input int              MomentumLookbackBars        = 4;
input double           MinimumMomentumPrice        = 1.20;
input int              ATRPeriod                   = 14;
input double           MinimumATRPrice             = 0.50;
input double           MaximumATRPrice             = 50.00;
input double           MaximumEntryDistanceATR     = 8.00;

input group "Fast stop exit"
input double           HardStopPrice               = 4.00;
input double           FormalTakeProfitPrice       = 100.00;
input bool             EnableBreakEven             = true;
input double           BreakEvenTriggerPrice       = 0.20;
input double           BreakEvenLockPrice          = 0.00;
input bool             EnableSmartTrailing         = true;
input double           TrailingStartPrice          = 0.20;
input double           TrailingDistancePrice       = 0.20;
input double           TrailingStepPrice           = 0.02;
input int              MaximumHoldMinutes          = 90;

input group "Execution protection"
input bool             EnableSpreadProtection      = true;
input double           MaximumSpreadPrice          = 0.30;
input int              MaximumSlippagePoints       = 30;
input int              MinimumSecondsBetweenOrders = 0;
input int              FridayNoNewOrdersHour       = 19;
input int              FridayCancelHour            = 20;
input bool             ClosePositionsOnFriday      = true;

input group "Loss guards"
input double           MaximumDailyLossMoney       = 0.00;
input double           MaximumDailyLossPercent     = 5.00;
input double           MaximumAccountDrawdownPct   = 15.00;
input int              MaximumConsecutiveLosses    = 3;
input int              LossCooldownMinutes         = 180;
input bool             CloseOnDailyLossLimit       = true;
input bool             CancelOrdersWhenBlocked     = true;

input group "Diagnostics"
input bool             PrintDiagnostics            = true;
input bool             WriteCommonCsvLog           = true;
input string           CommonCsvFile               = "evidence_h1_fast_breakout_log.csv";

CTrade trade;

int      fast_ema_handle       = INVALID_HANDLE;
int      slow_ema_handle       = INVALID_HANDLE;
int      atr_handle            = INVALID_HANDLE;
int      adx_handle            = INVALID_HANDLE;
datetime last_slot_time        = 0;
datetime last_signal_bar       = 0;
datetime last_order_time       = 0;
datetime blocked_until         = 0;
datetime current_day_start     = 0;
bool     daily_blocked         = false;
bool     enforcing_position_cap = false;
int      last_guard_second     = -1;
double   day_start_balance     = 0.0;
double   initial_balance       = 0.0;
double   peak_equity           = 0.0;

double NormalizePrice(const double price)
{
   const int digits = (int)SymbolInfoInteger(_Symbol, SYMBOL_DIGITS);
   const double tick_size = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tick_size <= 0.0)
      return NormalizeDouble(price, digits);
   return NormalizeDouble(MathRound(price / tick_size) * tick_size, digits);
}

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
      return requested;

   double volume = MathFloor(requested / step + 1e-9) * step;
   volume = MathMax(min_volume, MathMin(max_volume, volume));
   return NormalizeDouble(volume, VolumeDigits());
}

double MoneyPerPriceUnitPerLot()
{
   const double tick_value = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_VALUE);
   const double tick_size = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_TICK_SIZE);
   if(tick_value > 0.0 && tick_size > 0.0)
      return tick_value / tick_size;

   const double contract_size = SymbolInfoDouble(_Symbol, SYMBOL_TRADE_CONTRACT_SIZE);
   if(contract_size > 0.0)
      return contract_size;

   return 0.0;
}

double ResolveOrderVolume()
{
   if(!UseRiskBasedLots)
      return NormalizeVolume(FixedLots);

   const double money_per_price = MoneyPerPriceUnitPerLot();
   const double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   const double risk_money = equity * RiskPerTradePercent / 100.0;
   if(money_per_price <= 0.0 || risk_money <= 0.0 || HardStopPrice <= 0.0)
      return NormalizeVolume(FixedLots);

   double volume = risk_money / (HardStopPrice * money_per_price);
   volume = MathMax(MinimumRiskLots, MathMin(MaximumRiskLots, volume));
   return NormalizeVolume(volume);
}

double CurrentSpreadPrice()
{
   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick) || tick.ask <= 0.0 || tick.bid <= 0.0)
      return DBL_MAX;
   return tick.ask - tick.bid;
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

bool IsGoldSymbol()
{
   string name = _Symbol;
   StringToUpper(name);
   return StringFind(name, "XAU") >= 0 || StringFind(name, "GOLD") >= 0;
}

bool IsTradingAccountAllowed()
{
   const ENUM_ACCOUNT_TRADE_MODE mode =
      (ENUM_ACCOUNT_TRADE_MODE)AccountInfoInteger(ACCOUNT_TRADE_MODE);
   if(mode == ACCOUNT_TRADE_MODE_REAL && !AllowRealAccount)
      return false;
   return true;
}

bool IsOurPendingType(const ENUM_ORDER_TYPE type)
{
   return type == ORDER_TYPE_BUY_STOP ||
          type == ORDER_TYPE_SELL_STOP ||
          type == ORDER_TYPE_BUY_STOP_LIMIT ||
          type == ORDER_TYPE_SELL_STOP_LIMIT;
}

int OurPositionCount()
{
   int count = 0;
   for(int i = PositionsTotal() - 1; i >= 0; --i)
   {
      const ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(PositionGetString(POSITION_SYMBOL) == _Symbol &&
         (ulong)PositionGetInteger(POSITION_MAGIC) == MagicNumber)
         count++;
   }
   return count;
}

double OurFloatingProfit()
{
   double total = 0.0;
   for(int i = PositionsTotal() - 1; i >= 0; --i)
   {
      const ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(PositionGetString(POSITION_SYMBOL) == _Symbol &&
         (ulong)PositionGetInteger(POSITION_MAGIC) == MagicNumber)
         total += PositionGetDouble(POSITION_PROFIT) +
                  PositionGetDouble(POSITION_SWAP);
   }
   return total;
}

int OurPendingCount(const int direction = 0)
{
   int count = 0;
   for(int i = OrdersTotal() - 1; i >= 0; --i)
   {
      const ulong ticket = OrderGetTicket(i);
      if(ticket == 0)
         continue;
      if(OrderGetString(ORDER_SYMBOL) != _Symbol ||
         (ulong)OrderGetInteger(ORDER_MAGIC) != MagicNumber)
         continue;

      const ENUM_ORDER_TYPE type = (ENUM_ORDER_TYPE)OrderGetInteger(ORDER_TYPE);
      if(!IsOurPendingType(type))
         continue;

      const bool is_buy = type == ORDER_TYPE_BUY_STOP ||
                          type == ORDER_TYPE_BUY_STOP_LIMIT;
      if(direction == 1 && !is_buy)
         continue;
      if(direction == -1 && is_buy)
         continue;
      count++;
   }
   return count;
}

bool PendingNearLevel(const int direction, const double entry)
{
   if(DuplicateLevelTolerance <= 0.0)
      return false;

   for(int i = OrdersTotal() - 1; i >= 0; --i)
   {
      const ulong ticket = OrderGetTicket(i);
      if(ticket == 0)
         continue;
      if(OrderGetString(ORDER_SYMBOL) != _Symbol ||
         (ulong)OrderGetInteger(ORDER_MAGIC) != MagicNumber)
         continue;

      const ENUM_ORDER_TYPE type =
         (ENUM_ORDER_TYPE)OrderGetInteger(ORDER_TYPE);
      if(!IsOurPendingType(type))
         continue;

      const bool is_buy = type == ORDER_TYPE_BUY_STOP ||
                          type == ORDER_TYPE_BUY_STOP_LIMIT;
      if((direction > 0 && !is_buy) || (direction < 0 && is_buy))
         continue;

      if(MathAbs(OrderGetDouble(ORDER_PRICE_OPEN) - entry) <=
         DuplicateLevelTolerance)
         return true;
   }
   return false;
}

void DeletePendingOrders(const int direction = 0, const string reason = "")
{
   for(int i = OrdersTotal() - 1; i >= 0; --i)
   {
      const ulong ticket = OrderGetTicket(i);
      if(ticket == 0)
         continue;
      if(OrderGetString(ORDER_SYMBOL) != _Symbol ||
         (ulong)OrderGetInteger(ORDER_MAGIC) != MagicNumber)
         continue;

      const ENUM_ORDER_TYPE type = (ENUM_ORDER_TYPE)OrderGetInteger(ORDER_TYPE);
      if(!IsOurPendingType(type))
         continue;

      const bool is_buy = type == ORDER_TYPE_BUY_STOP ||
                          type == ORDER_TYPE_BUY_STOP_LIMIT;
      if(direction == 1 && !is_buy)
         continue;
      if(direction == -1 && is_buy)
         continue;

      if(trade.OrderDelete(ticket))
         LogEvent("PENDING_DELETE",
                  StringFormat("ticket=%I64u reason=%s", ticket, reason));
   }
}

void CloseOurPositions(const string reason)
{
   for(int i = PositionsTotal() - 1; i >= 0; --i)
   {
      const ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol ||
         (ulong)PositionGetInteger(POSITION_MAGIC) != MagicNumber)
         continue;

      if(trade.PositionClose(ticket, MaximumSlippagePoints))
         LogEvent("POSITION_CLOSE",
                  StringFormat("ticket=%I64u reason=%s", ticket, reason));
   }
}

void EnforcePositionCap(ulong preferred_ticket = 0)
{
   if(!CloseExcessPositionsOnEntry || MaxOpenPositions < 1 ||
      enforcing_position_cap)
      return;

   enforcing_position_cap = true;
   int count = OurPositionCount();
   while(count > MaxOpenPositions)
   {
      ulong close_ticket = 0;
      if(preferred_ticket > 0 && PositionSelectByTicket(preferred_ticket) &&
         PositionGetString(POSITION_SYMBOL) == _Symbol &&
         (ulong)PositionGetInteger(POSITION_MAGIC) == MagicNumber)
      {
         close_ticket = preferred_ticket;
      }
      else
      {
         long newest_time_msc = -1;
         for(int i = PositionsTotal() - 1; i >= 0; --i)
         {
            const ulong ticket = PositionGetTicket(i);
            if(ticket == 0 || PositionGetString(POSITION_SYMBOL) != _Symbol ||
               (ulong)PositionGetInteger(POSITION_MAGIC) != MagicNumber)
               continue;

            const long opened_msc =
               (long)PositionGetInteger(POSITION_TIME_MSC);
            if(opened_msc >= newest_time_msc)
            {
               newest_time_msc = opened_msc;
               close_ticket = ticket;
            }
         }
      }

      if(close_ticket == 0)
         break;
      if(!trade.PositionClose(close_ticket, MaximumSlippagePoints))
      {
         LogEvent("POSITION_CAP_ERROR",
                  StringFormat("ticket=%I64u retcode=%u description=%s",
                               close_ticket,
                               trade.ResultRetcode(),
                               trade.ResultRetcodeDescription()));
         break;
      }

      LogEvent("POSITION_CAP_CLOSE",
               StringFormat("ticket=%I64u cap=%d", close_ticket,
                            MaxOpenPositions));
      preferred_ticket = 0;
      count = OurPositionCount();
   }
   enforcing_position_cap = false;
}

double TodayRealizedPnl()
{
   const datetime now = TimeCurrent();
   const datetime day_start = StartOfDay(now);
   if(!HistorySelect(day_start, now))
      return 0.0;

   double total = 0.0;
   const int deals = HistoryDealsTotal();
   for(int i = 0; i < deals; ++i)
   {
      const ulong ticket = HistoryDealGetTicket(i);
      if(ticket == 0)
         continue;
      if(HistoryDealGetString(ticket, DEAL_SYMBOL) != _Symbol ||
         (ulong)HistoryDealGetInteger(ticket, DEAL_MAGIC) != MagicNumber)
         continue;

      total += HistoryDealGetDouble(ticket, DEAL_PROFIT);
      total += HistoryDealGetDouble(ticket, DEAL_COMMISSION);
      total += HistoryDealGetDouble(ticket, DEAL_SWAP);
   }
   return total;
}

int TodayEntryCount()
{
   const datetime now = TimeCurrent();
   if(!HistorySelect(StartOfDay(now), now))
      return 0;

   int count = 0;
   const int deals = HistoryDealsTotal();
   for(int i = 0; i < deals; ++i)
   {
      const ulong ticket = HistoryDealGetTicket(i);
      if(ticket == 0)
         continue;
      if(HistoryDealGetString(ticket, DEAL_SYMBOL) != _Symbol ||
         (ulong)HistoryDealGetInteger(ticket, DEAL_MAGIC) != MagicNumber)
         continue;

      const ENUM_DEAL_ENTRY entry =
         (ENUM_DEAL_ENTRY)HistoryDealGetInteger(ticket, DEAL_ENTRY);
      if(entry == DEAL_ENTRY_IN || entry == DEAL_ENTRY_INOUT)
         count++;
   }
   return count;
}

int ConsecutiveLosses()
{
   if(!HistorySelect(0, TimeCurrent()))
      return 0;

   int losses = 0;
   for(int i = HistoryDealsTotal() - 1; i >= 0; --i)
   {
      const ulong ticket = HistoryDealGetTicket(i);
      if(ticket == 0)
         continue;
      if(HistoryDealGetString(ticket, DEAL_SYMBOL) != _Symbol ||
         (ulong)HistoryDealGetInteger(ticket, DEAL_MAGIC) != MagicNumber)
         continue;

      const ENUM_DEAL_ENTRY entry =
         (ENUM_DEAL_ENTRY)HistoryDealGetInteger(ticket, DEAL_ENTRY);
      if(entry != DEAL_ENTRY_OUT && entry != DEAL_ENTRY_OUT_BY &&
         entry != DEAL_ENTRY_INOUT)
         continue;

      const double pnl = HistoryDealGetDouble(ticket, DEAL_PROFIT) +
                         HistoryDealGetDouble(ticket, DEAL_COMMISSION) +
                         HistoryDealGetDouble(ticket, DEAL_SWAP);
      if(pnl < 0.0)
         losses++;
      else if(pnl > 0.0)
         break;
   }
   return losses;
}

datetime MostRecentLosingExitTime()
{
   if(!HistorySelect(0, TimeCurrent()))
      return 0;

   for(int i = HistoryDealsTotal() - 1; i >= 0; --i)
   {
      const ulong ticket = HistoryDealGetTicket(i);
      if(ticket == 0)
         continue;
      if(HistoryDealGetString(ticket, DEAL_SYMBOL) != _Symbol ||
         (ulong)HistoryDealGetInteger(ticket, DEAL_MAGIC) != MagicNumber)
         continue;

      const ENUM_DEAL_ENTRY entry =
         (ENUM_DEAL_ENTRY)HistoryDealGetInteger(ticket, DEAL_ENTRY);
      if(entry != DEAL_ENTRY_OUT && entry != DEAL_ENTRY_OUT_BY &&
         entry != DEAL_ENTRY_INOUT)
         continue;

      const double pnl = HistoryDealGetDouble(ticket, DEAL_PROFIT) +
                         HistoryDealGetDouble(ticket, DEAL_COMMISSION) +
                         HistoryDealGetDouble(ticket, DEAL_SWAP);
      if(pnl < 0.0)
         return (datetime)HistoryDealGetInteger(ticket, DEAL_TIME);
      return 0;
   }
   return 0;
}

void RefreshDayState()
{
   const datetime day_start = StartOfDay(TimeCurrent());
   if(day_start != current_day_start)
   {
      current_day_start = day_start;
      day_start_balance = AccountInfoDouble(ACCOUNT_BALANCE);
      daily_blocked = false;
      blocked_until = 0;
      LogEvent("DAY_RESET", "daily guards reset");
   }
}

void ApplyLossGuards()
{
   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   if(dt.sec == last_guard_second)
      return;
   last_guard_second = dt.sec;

   RefreshDayState();

   const double daily_pnl = TodayRealizedPnl() + OurFloatingProfit();
   double daily_loss_limit = MaximumDailyLossMoney;
   if(MaximumDailyLossPercent > 0.0 && day_start_balance > 0.0)
   {
      const double percentage_limit =
         day_start_balance * MaximumDailyLossPercent / 100.0;
      if(daily_loss_limit <= 0.0 || percentage_limit < daily_loss_limit)
         daily_loss_limit = percentage_limit;
   }

   const double equity = AccountInfoDouble(ACCOUNT_EQUITY);
   if(equity > peak_equity)
      peak_equity = equity;
   const double account_drawdown_pct =
      peak_equity > 0.0
      ? MathMax(0.0, (peak_equity - equity) / peak_equity * 100.0)
      : 0.0;
   const bool daily_limit_hit =
      daily_loss_limit > 0.0 && daily_pnl <= -daily_loss_limit;
   const bool account_limit_hit =
      MaximumAccountDrawdownPct > 0.0 &&
      account_drawdown_pct >= MaximumAccountDrawdownPct;

   if(daily_limit_hit || account_limit_hit)
   {
      if(!daily_blocked)
         LogEvent("DAILY_BLOCK",
                  StringFormat("pnl=%.2f daily_limit=-%.2f account_dd=%.2f%%",
                               daily_pnl,
                               daily_loss_limit,
                               account_drawdown_pct));
      daily_blocked = true;
      if(CancelOrdersWhenBlocked)
         DeletePendingOrders(0, "daily_loss");
      if(CloseOnDailyLossLimit)
         CloseOurPositions("daily_loss");
   }

   const int losses = ConsecutiveLosses();
   if(MaximumConsecutiveLosses > 0 && losses >= MaximumConsecutiveLosses)
   {
      const datetime loss_time = MostRecentLosingExitTime();
      const datetime until_time = loss_time + LossCooldownMinutes * 60;
      if(until_time > blocked_until)
      {
         blocked_until = until_time;
         LogEvent("LOSS_COOLDOWN",
                  StringFormat("losses=%d until=%s",
                               losses,
                               TimeToString(blocked_until,
                                            TIME_DATE | TIME_MINUTES)));
      }
      if(CancelOrdersWhenBlocked && TimeCurrent() < blocked_until)
         DeletePendingOrders(0, "loss_cooldown");
   }
}

bool IsPlacementHour(const int hour)
{
   string clean = PlacementHours;
   StringReplace(clean, " ", "");
   string values[];
   const int count = StringSplit(clean, ',', values);
   for(int i = 0; i < count; ++i)
   {
      if((int)StringToInteger(values[i]) == hour)
         return true;
   }
   return false;
}

datetime CurrentHourStart()
{
   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   dt.min = 0;
   dt.sec = 0;
   return StructToTime(dt);
}

bool AlreadyActedThisSlot(const datetime slot_start)
{
   if(last_slot_time == slot_start)
      return true;

   for(int i = OrdersTotal() - 1; i >= 0; --i)
   {
      const ulong ticket = OrderGetTicket(i);
      if(ticket == 0)
         continue;
      if(OrderGetString(ORDER_SYMBOL) == _Symbol &&
         (ulong)OrderGetInteger(ORDER_MAGIC) == MagicNumber &&
         (datetime)OrderGetInteger(ORDER_TIME_SETUP) >= slot_start)
         return true;
   }

   if(!HistorySelect(slot_start, TimeCurrent()))
      return false;
   const int total = HistoryOrdersTotal();
   for(int i = total - 1; i >= 0; --i)
   {
      const ulong ticket = HistoryOrderGetTicket(i);
      if(ticket == 0)
         continue;
      if(HistoryOrderGetString(ticket, ORDER_SYMBOL) == _Symbol &&
         (ulong)HistoryOrderGetInteger(ticket, ORDER_MAGIC) == MagicNumber &&
         (datetime)HistoryOrderGetInteger(ticket, ORDER_TIME_SETUP) >= slot_start)
         return true;
   }
   return false;
}

bool ReadIndicatorValue(const int handle,
                        const int buffer,
                        const int shift,
                        double &value)
{
   double data[];
   ArrayResize(data, 1);
   if(CopyBuffer(handle, buffer, shift, 1, data) != 1)
      return false;
   value = data[0];
   return MathIsValidNumber(value);
}

bool IsSwingHigh(const MqlRates &rates[],
                 const int index,
                 const int strength,
                 const int total)
{
   if(index - strength < 1 || index + strength >= total)
      return false;
   for(int j = 1; j <= strength; ++j)
   {
      if(rates[index].high <= rates[index - j].high ||
         rates[index].high < rates[index + j].high)
         return false;
   }
   return true;
}

bool IsSwingLow(const MqlRates &rates[],
                const int index,
                const int strength,
                const int total)
{
   if(index - strength < 1 || index + strength >= total)
      return false;
   for(int j = 1; j <= strength; ++j)
   {
      if(rates[index].low >= rates[index - j].low ||
         rates[index].low > rates[index + j].low)
         return false;
   }
   return true;
}

void SelectNearestPeakLevels(const MqlRates &rates[],
                             const int total,
                             const double ask,
                             const double bid,
                             double &breakout_high,
                             double &breakout_low)
{
   breakout_high = 0.0;
   breakout_low = 0.0;
   double nearest_buy_entry = DBL_MAX;
   double nearest_sell_entry = -DBL_MAX;

   const int last_index =
      MathMin(BreakoutLookbackBars, total - SwingStrength - 1);
   const int first_index = 1 + SwingStrength;
   for(int i = first_index; i <= last_index; ++i)
   {
      if(IsSwingHigh(rates, i, SwingStrength, total))
      {
         const double candidate = rates[i].high + EntryBufferPrice;
         if(candidate >= ask + MinimumEntryDistancePrice &&
            candidate < nearest_buy_entry)
         {
            nearest_buy_entry = candidate;
            breakout_high = rates[i].high;
         }
      }

      if(IsSwingLow(rates, i, SwingStrength, total))
      {
         const double candidate = rates[i].low - EntryBufferPrice;
         if(candidate <= bid - MinimumEntryDistancePrice &&
            candidate > nearest_sell_entry)
         {
            nearest_sell_entry = candidate;
            breakout_low = rates[i].low;
         }
      }
   }

   double range_high = rates[1].high;
   double range_low = rates[1].low;
   for(int i = 2; i <= MathMin(BreakoutLookbackBars, total - 1); ++i)
   {
      range_high = MathMax(range_high, rates[i].high);
      range_low = MathMin(range_low, rates[i].low);
   }

   if(breakout_high <= 0.0 &&
      range_high + EntryBufferPrice >= ask + MinimumEntryDistancePrice)
      breakout_high = range_high;
   if(breakout_low <= 0.0 &&
      range_low - EntryBufferPrice <= bid - MinimumEntryDistancePrice)
      breakout_low = range_low;
}

bool BuildContext(bool &buy_allowed,
                  bool &sell_allowed,
                  double &breakout_high,
                  double &breakout_low,
                  double &atr_value,
                  string &reason)
{
   buy_allowed = EnableLongs;
   sell_allowed = EnableShorts;
   breakout_high = 0.0;
   breakout_low = 0.0;
   atr_value = 0.0;
   reason = "";

   const int bars_needed =
      MathMax(BreakoutLookbackBars + SwingStrength + 5,
              MomentumLookbackBars + 5);
   MqlRates rates[];
   ArraySetAsSeries(rates, true);
   const int copied =
      CopyRates(_Symbol, SignalTimeframe, 0, bars_needed, rates);
   if(copied < bars_needed)
   {
      reason = "not_enough_bars";
      return false;
   }

   if(!ReadIndicatorValue(atr_handle, 0, 1, atr_value))
   {
      reason = "atr_unavailable";
      return false;
   }
   if(atr_value < MinimumATRPrice || atr_value > MaximumATRPrice)
   {
      reason = StringFormat("atr_outside %.2f", atr_value);
      return false;
   }

   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick))
   {
      reason = "tick_unavailable";
      return false;
   }
   SelectNearestPeakLevels(rates,
                           copied,
                           tick.ask,
                           tick.bid,
                           breakout_high,
                           breakout_low);
   buy_allowed = buy_allowed && breakout_high > 0.0;
   sell_allowed = sell_allowed && breakout_low > 0.0;

   const bool filter_longs = UseTrendSideFilter || FilterLongsWithTrend;
   const bool filter_shorts = UseTrendSideFilter || FilterShortsWithTrend;
   if(filter_longs || filter_shorts)
   {
      bool trend_buy = true;
      bool trend_sell = true;
      if(UseEMATrend)
      {
         double fast_1, fast_2, slow_1;
         if(!ReadIndicatorValue(fast_ema_handle, 0, 1, fast_1) ||
            !ReadIndicatorValue(fast_ema_handle, 0, 2, fast_2) ||
            !ReadIndicatorValue(slow_ema_handle, 0, 1, slow_1))
         {
            reason = "ema_unavailable";
            return false;
         }
         trend_buy = fast_1 > slow_1;
         trend_sell = fast_1 < slow_1;
         if(RequireFastEMASlope)
         {
            trend_buy = trend_buy && fast_1 > fast_2;
            trend_sell = trend_sell && fast_1 < fast_2;
         }
      }

      if(UseADXConfirmation)
      {
         double adx, plus_di, minus_di;
         if(!ReadIndicatorValue(adx_handle, 0, 1, adx) ||
            !ReadIndicatorValue(adx_handle, 1, 1, plus_di) ||
            !ReadIndicatorValue(adx_handle, 2, 1, minus_di))
         {
            reason = "adx_unavailable";
            return false;
         }
         trend_buy = trend_buy && adx >= MinimumADX && plus_di > minus_di;
         trend_sell = trend_sell && adx >= MinimumADX && minus_di > plus_di;
      }

      if(!trend_buy && !trend_sell && UseMomentumFallback)
      {
         const double momentum =
            rates[1].close - rates[1 + MomentumLookbackBars].close;
         trend_buy = momentum >= MinimumMomentumPrice;
         trend_sell = momentum <= -MinimumMomentumPrice;
      }
      if(filter_longs)
         buy_allowed = buy_allowed && trend_buy;
      if(filter_shorts)
         sell_allowed = sell_allowed && trend_sell;
   }

   if(!buy_allowed && !sell_allowed)
   {
      reason = "no_valid_peak_side";
      return false;
   }

   reason = StringFormat("h1_peak_levels buy=%s sell=%s",
                         buy_allowed ? "true" : "false",
                         sell_allowed ? "true" : "false");
   return true;
}

bool IsTradingBlocked(string &reason)
{
   reason = "";
   if(!IsTradingAccountAllowed())
   {
      reason = "real_account_guard";
      return true;
   }
   if(daily_blocked)
   {
      reason = "daily_loss_guard";
      return true;
   }
   if(TimeCurrent() < blocked_until)
   {
      reason = "loss_cooldown";
      return true;
   }
   if(TodayEntryCount() >= MaxTradesPerDay)
   {
      reason = "max_trades_per_day";
      return true;
   }
   if(OurPositionCount() >= MaxOpenPositions)
   {
      reason = "max_positions";
      return true;
   }
   if(OurPendingCount() >= MaxPendingOrders)
   {
      reason = "max_pending";
      return true;
   }
   if(MinimumSecondsBetweenOrders > 0 &&
      TimeCurrent() - last_order_time < MinimumSecondsBetweenOrders)
   {
      reason = "order_spacing";
      return true;
   }
   if(EnableSpreadProtection &&
      CurrentSpreadPrice() > MaximumSpreadPrice)
   {
      reason = "spread";
      return true;
   }

   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   if(dt.day_of_week == 0 || dt.day_of_week == 6)
   {
      reason = "weekend";
      return true;
   }
   if(dt.day_of_week == 5 && dt.hour >= FridayNoNewOrdersHour)
   {
      reason = "friday_cutoff";
      return true;
   }
   return false;
}

bool PlaceBreakoutOrder(const int direction,
                        const double breakout_high,
                        const double breakout_low,
                        const double atr_value,
                        const string context_reason)
{
   if(OurPendingCount() >= MaxPendingOrders)
   {
      LogEvent("SIGNAL_SKIP", "reason=max_pending_at_send");
      return false;
   }
   if(OurPositionCount() >= MaxOpenPositions)
   {
      LogEvent("SIGNAL_SKIP", "reason=max_positions_at_send");
      return false;
   }

   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick))
      return false;

   const int stops_level =
      (int)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL);
   const double minimum_distance =
      MathMax((stops_level + 1) * _Point, CurrentSpreadPrice() * 1.25);

   double entry = direction > 0
                  ? breakout_high + EntryBufferPrice
                  : breakout_low - EntryBufferPrice;
   if(direction > 0)
      entry = MathMax(entry, tick.ask + minimum_distance);
   else
      entry = MathMin(entry, tick.bid - minimum_distance);

   const double market_price = direction > 0 ? tick.ask : tick.bid;
   const double distance = MathAbs(entry - market_price);
   if(distance < MinimumEntryDistancePrice)
   {
      LogEvent("SIGNAL_SKIP",
               StringFormat("reason=entry_too_close dir=%d distance=%.2f",
                            direction,
                            distance));
      return false;
   }
   if(MaximumEntryDistanceATR > 0.0 &&
      distance > atr_value * MaximumEntryDistanceATR)
   {
      LogEvent("SIGNAL_SKIP",
               StringFormat("reason=entry_too_far dir=%d distance=%.2f atr=%.2f",
                            direction,
                            distance,
                            atr_value));
      return false;
   }

   if(OnePendingPerDirection && OurPendingCount(direction) > 0)
   {
      LogEvent("SIGNAL_SKIP",
               StringFormat("reason=direction_pending_exists dir=%d", direction));
      return false;
   }

   entry = NormalizePrice(entry);
   if(PendingNearLevel(direction, entry))
   {
      LogEvent("SIGNAL_SKIP",
               StringFormat("reason=duplicate_level dir=%d entry=%.2f",
                            direction,
                            entry));
      return false;
   }

   const double sl = NormalizePrice(direction > 0
                                    ? entry - HardStopPrice
                                    : entry + HardStopPrice);
   const double tp = NormalizePrice(direction > 0
                                    ? entry + FormalTakeProfitPrice
                                    : entry - FormalTakeProfitPrice);
   const double volume = ResolveOrderVolume();
   const datetime expiration = TimeCurrent() + PendingExpiryHours * 3600;

   bool sent = false;
   if(direction > 0)
      sent = trade.BuyStop(volume,
                           entry,
                           _Symbol,
                           sl,
                           tp,
                           ORDER_TIME_SPECIFIED,
                           expiration,
                           TradeComment + "_BUY");
   else
      sent = trade.SellStop(volume,
                            entry,
                            _Symbol,
                            sl,
                            tp,
                            ORDER_TIME_SPECIFIED,
                            expiration,
                            TradeComment + "_SELL");

   if(!sent)
   {
      LogEvent("ORDER_REJECT",
               StringFormat("dir=%d retcode=%u description=%s",
                            direction,
                            trade.ResultRetcode(),
                            trade.ResultRetcodeDescription()));
      return false;
   }

   last_order_time = TimeCurrent();
   LogEvent("ORDER_PLACED",
            StringFormat("dir=%d lot=%.2f entry=%.2f sl=%.2f tp=%.2f atr=%.2f context=%s order=%I64u",
                         direction,
                         volume,
                         entry,
                         sl,
                         tp,
                         atr_value,
                         context_reason,
                         trade.ResultOrder()));
   return true;
}

void EvaluatePlacementSlot()
{
   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   if(!IsPlacementHour(dt.hour) ||
      dt.min >= PlacementWindowMinutes)
      return;

   const datetime slot_start = CurrentHourStart();
   if(AlreadyActedThisSlot(slot_start))
   {
      last_slot_time = slot_start;
      return;
   }
   last_slot_time = slot_start;

   string block_reason;
   if(IsTradingBlocked(block_reason))
   {
      LogEvent("SLOT_SKIP", "reason=" + block_reason);
      return;
   }

   bool buy_allowed, sell_allowed;
   double breakout_high, breakout_low, atr_value;
   string context_reason;
   if(!BuildContext(buy_allowed,
                    sell_allowed,
                    breakout_high,
                    breakout_low,
                    atr_value,
                    context_reason))
   {
      LogEvent("SLOT_SKIP", "reason=" + context_reason);
      return;
   }

   bool placed = false;
   if(buy_allowed)
      placed = PlaceBreakoutOrder(1,
                                  breakout_high,
                                  breakout_low,
                                  atr_value,
                                  context_reason) || placed;
   if(sell_allowed)
      placed = PlaceBreakoutOrder(-1,
                                  breakout_high,
                                  breakout_low,
                                  atr_value,
                                  context_reason) || placed;
   if(!placed)
      LogEvent("SLOT_SKIP", "reason=no_new_level");
}

void ManagePendingContext()
{
   const datetime bar_time = iTime(_Symbol, SignalTimeframe, 0);
   if(bar_time <= 0 || bar_time == last_signal_bar)
      return;
   last_signal_bar = bar_time;

   if(!CancelPendingOnTrendFlip)
      return;

   bool buy_allowed, sell_allowed;
   double high, low, atr;
   string reason;
   if(!BuildContext(buy_allowed,
                    sell_allowed,
                    high,
                    low,
                    atr,
                    reason))
      return;

   if(buy_allowed && !sell_allowed)
      DeletePendingOrders(-1, "h1_trend_flip");
   else if(sell_allowed && !buy_allowed)
      DeletePendingOrders(1, "h1_trend_flip");
}

void ManageFridayRisk()
{
   MqlDateTime dt;
   TimeToStruct(TimeCurrent(), dt);
   if(dt.day_of_week != 5 || dt.hour < FridayCancelHour)
      return;

   DeletePendingOrders(0, "friday_cutoff");
   if(ClosePositionsOnFriday)
      CloseOurPositions("friday_cutoff");
}

void ManagePositions()
{
   MqlTick tick;
   if(!SymbolInfoTick(_Symbol, tick))
      return;

   const double spread = tick.ask - tick.bid;
   const int stops_level =
      (int)SymbolInfoInteger(_Symbol, SYMBOL_TRADE_STOPS_LEVEL);
   const double minimum_stop_distance =
      MathMax((stops_level + 1) * _Point, spread * 1.10);

   for(int i = PositionsTotal() - 1; i >= 0; --i)
   {
      const ulong ticket = PositionGetTicket(i);
      if(ticket == 0)
         continue;
      if(PositionGetString(POSITION_SYMBOL) != _Symbol ||
         (ulong)PositionGetInteger(POSITION_MAGIC) != MagicNumber)
         continue;

      const ENUM_POSITION_TYPE type =
         (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
      const double open_price = PositionGetDouble(POSITION_PRICE_OPEN);
      const double current_sl = PositionGetDouble(POSITION_SL);
      const double current_tp = PositionGetDouble(POSITION_TP);
      const datetime open_time =
         (datetime)PositionGetInteger(POSITION_TIME);

      const bool is_buy = type == POSITION_TYPE_BUY;
      const double exit_price = is_buy ? tick.bid : tick.ask;
      const double profit_distance = is_buy
                                     ? exit_price - open_price
                                     : open_price - exit_price;
      double candidate_sl = current_sl;

      if(EnableBreakEven &&
         profit_distance >= BreakEvenTriggerPrice)
      {
         const double be_sl = is_buy
                              ? open_price + BreakEvenLockPrice
                              : open_price - BreakEvenLockPrice;
         if((is_buy && (candidate_sl == 0.0 || be_sl > candidate_sl)) ||
            (!is_buy && (candidate_sl == 0.0 || be_sl < candidate_sl)))
            candidate_sl = be_sl;
      }

      if(EnableSmartTrailing &&
         profit_distance >= TrailingStartPrice)
      {
         const double trail_sl = is_buy
                                 ? exit_price - TrailingDistancePrice
                                 : exit_price + TrailingDistancePrice;
         if((is_buy && (candidate_sl == 0.0 ||
                        trail_sl > candidate_sl + TrailingStepPrice)) ||
            (!is_buy && (candidate_sl == 0.0 ||
                         trail_sl < candidate_sl - TrailingStepPrice)))
            candidate_sl = trail_sl;
      }

      if(candidate_sl > 0.0)
      {
         if(is_buy)
            candidate_sl = MathMin(candidate_sl,
                                   tick.bid - minimum_stop_distance);
         else
            candidate_sl = MathMax(candidate_sl,
                                   tick.ask + minimum_stop_distance);
         candidate_sl = NormalizePrice(candidate_sl);

         const bool improved =
            (is_buy && (current_sl == 0.0 ||
                        candidate_sl > current_sl + _Point)) ||
            (!is_buy && (current_sl == 0.0 ||
                         candidate_sl < current_sl - _Point));
         if(improved)
         {
            if(trade.PositionModify(ticket, candidate_sl, current_tp))
               LogEvent("STOP_MODIFY",
                        StringFormat("ticket=%I64u sl=%.2f profit_distance=%.2f",
                                     ticket,
                                     candidate_sl,
                                     profit_distance));
         }
      }

      if(MaximumHoldMinutes > 0 &&
         TimeCurrent() - open_time >= MaximumHoldMinutes * 60)
      {
         if(trade.PositionClose(ticket, MaximumSlippagePoints))
            LogEvent("TIME_EXIT",
                     StringFormat("ticket=%I64u minutes=%d",
                                  ticket,
                                  MaximumHoldMinutes));
      }
   }
}

int OnInit()
{
   if(RestrictToGoldSymbols && !IsGoldSymbol())
   {
      Print("EvidenceH1FastBreakout is restricted to XAU/GOLD symbols.");
      return INIT_PARAMETERS_INCORRECT;
   }

   if(!IsTradingAccountAllowed())
   {
      Print("Real-account guard blocked the EA. Use demo/tester, or intentionally enable AllowRealAccount.");
      return INIT_FAILED;
   }

   if(FixedLots <= 0.0 ||
      MaxOpenPositions < 1 ||
      MaxPendingOrders < 1 ||
      HardStopPrice <= 0.0 ||
      BreakoutLookbackBars < 2 ||
      SwingStrength < 1 ||
      MinimumEntryDistancePrice < 0.0 ||
      PendingExpiryHours < 1)
      return INIT_PARAMETERS_INCORRECT;

   if(!EnableLongs && !EnableShorts)
      return INIT_PARAMETERS_INCORRECT;

   if(UseRiskBasedLots &&
      (RiskPerTradePercent <= 0.0 ||
       MinimumRiskLots <= 0.0 ||
       MaximumRiskLots < MinimumRiskLots))
      return INIT_PARAMETERS_INCORRECT;

   fast_ema_handle =
      iMA(_Symbol, SignalTimeframe, FastEMAPeriod, 0, MODE_EMA, PRICE_CLOSE);
   slow_ema_handle =
      iMA(_Symbol, SignalTimeframe, SlowEMAPeriod, 0, MODE_EMA, PRICE_CLOSE);
   atr_handle = iATR(_Symbol, SignalTimeframe, ATRPeriod);
   adx_handle = iADX(_Symbol, SignalTimeframe, ADXPeriod);

   if(fast_ema_handle == INVALID_HANDLE ||
      slow_ema_handle == INVALID_HANDLE ||
      atr_handle == INVALID_HANDLE ||
      adx_handle == INVALID_HANDLE)
   {
      Print("Failed to create indicator handles. Error=", GetLastError());
      return INIT_FAILED;
   }

   trade.SetExpertMagicNumber(MagicNumber);
   trade.SetDeviationInPoints(MaximumSlippagePoints);
   trade.SetTypeFillingBySymbol(_Symbol);
   trade.SetAsyncMode(false);

   current_day_start = StartOfDay(TimeCurrent());
   day_start_balance = AccountInfoDouble(ACCOUNT_BALANCE);
   initial_balance = day_start_balance;
   peak_equity = AccountInfoDouble(ACCOUNT_EQUITY);
   last_signal_bar = iTime(_Symbol, SignalTimeframe, 0);

   LogEvent("START",
            StringFormat("symbol=%s timeframe=%s lot=%.2f risk_mode=%s risk=%.2f%% long=%s short=%s hours=%s",
                         _Symbol,
                         EnumToString(SignalTimeframe),
                         ResolveOrderVolume(),
                         UseRiskBasedLots ? "true" : "false",
                         RiskPerTradePercent,
                         EnableLongs ? "true" : "false",
                         EnableShorts ? "true" : "false",
                         PlacementHours));
   return INIT_SUCCEEDED;
}

void OnDeinit(const int reason)
{
   if(fast_ema_handle != INVALID_HANDLE)
      IndicatorRelease(fast_ema_handle);
   if(slow_ema_handle != INVALID_HANDLE)
      IndicatorRelease(slow_ema_handle);
   if(atr_handle != INVALID_HANDLE)
      IndicatorRelease(atr_handle);
   if(adx_handle != INVALID_HANDLE)
      IndicatorRelease(adx_handle);

   LogEvent("STOP", StringFormat("reason=%d", reason));
}

void OnTick()
{
   ApplyLossGuards();
   ManageFridayRisk();

   if(CancelPendingOnWideSpread &&
      EnableSpreadProtection &&
      CurrentSpreadPrice() > MaximumSpreadPrice)
      DeletePendingOrders(0, "wide_spread");

   ManagePositions();
   ManagePendingContext();
   EvaluatePlacementSlot();
}

void OnTradeTransaction(const MqlTradeTransaction &trans,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result)
{
   if(trans.type != TRADE_TRANSACTION_DEAL_ADD || trans.deal == 0)
      return;
   if(!HistoryDealSelect(trans.deal))
      return;
   if(HistoryDealGetString(trans.deal, DEAL_SYMBOL) != _Symbol ||
      (ulong)HistoryDealGetInteger(trans.deal, DEAL_MAGIC) != MagicNumber)
      return;

   const ENUM_DEAL_ENTRY entry =
      (ENUM_DEAL_ENTRY)HistoryDealGetInteger(trans.deal, DEAL_ENTRY);
   const double price = HistoryDealGetDouble(trans.deal, DEAL_PRICE);
   const double profit = HistoryDealGetDouble(trans.deal, DEAL_PROFIT) +
                         HistoryDealGetDouble(trans.deal, DEAL_COMMISSION) +
                         HistoryDealGetDouble(trans.deal, DEAL_SWAP);

   if(entry == DEAL_ENTRY_IN || entry == DEAL_ENTRY_INOUT)
   {
      LogEvent("FILL",
               StringFormat("deal=%I64u price=%.2f", trans.deal, price));
      if(CancelAllPendingOnEntry)
         DeletePendingOrders(0, "entry_fill_oco");
      EnforcePositionCap(trans.position);
   }
   if(entry == DEAL_ENTRY_OUT || entry == DEAL_ENTRY_OUT_BY ||
      entry == DEAL_ENTRY_INOUT)
      LogEvent("EXIT",
               StringFormat("deal=%I64u price=%.2f net=%.2f",
                            trans.deal,
                            price,
                            profit));
}
