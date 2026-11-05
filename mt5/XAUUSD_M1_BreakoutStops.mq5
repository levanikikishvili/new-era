#property copyright "OpenCode / NewEra Impulse Sniper v2.0"
#property version   "2.00"
#property strict

#include <Trade/Trade.mqh>

// XAUUSD M1 Impulse Breakout Sniper EA v2.0
// Combines tick-driven Buy Stop / Sell Stop breakout orders + TP=0 Trailing Stop
// with 6 institutional protection upgrades discovered from live MT5 forensic audit:
// 1. M1 EMA Trend Alignment Filter (blocks counter-trend dead-cat bounce traps)
// 2. Fast Break-Even (+85 pts -> +12 pts lock) + Tightened Runner Trail (>350 pts -> 100 pts trail)
// 3. Post-Runner Exhaustion Cooldown (blocks buying the exact top 7s after a huge impulse closes)
// 4. Post-SL Cooldown (prevents 0ms machine-gun re-entries on the same whipsaw candle)
// 5. 3-Consecutive-Loss Whipsaw Circuit Breaker (20-min pause when impulse turns to chop)
// 6. Daily High-Water Mark (Peak Equity) Profit Trailing Lock (never gives back a +$80..+$160 day)

CTrade trade;

input group "1. General"
input bool             InpRequireGoldSymbol            = true;
input ulong            InpMagic                        = 11032026;
input string           InpCommentPrefix                = "XAU_M1_SNIPER";
input int              InpDeviationPoints              = 30;
input int              InpTimerSeconds                 = 1;
input bool             InpDeletePendingOnDeinit        = true;
input bool             InpShowChartHUD                 = true;

input group "2. Pending Breakout Orders"
input double           InpEntryStepPoints              = 75.0;   // Distance from Bid/Ask (points)
input int              InpPendingRefreshMilliseconds   = 200;    // Pending refresh throttle (ms)
input double           InpRequoteStepPoints            = 15.0;   // Min price move to modify pending (points)
input double           InpMaxSpreadPoints              = 80.0;   // Max allowed spread (points)

input group "3. Volume & Risk Per Trade"
input bool             InpUseRiskBasedLots             = true;
input double           InpLots                         = 0.04;
input double           InpRiskPerTradePercent          = 1.00;
input bool             InpCapFixedLotsByRisk           = true;
input double           InpMaxRiskPerTradePercent       = 1.50;
input int              InpMaxOpenPositions             = 1;

input group "4. Commission-Proof Trailing Stop & Break-Even (No TP)"
input double           InpCommissionRoundTurnPoints    = 80.0;   // Broker commission in points ($0.08/0.01 lot = 80 pts)
input double           InpTrailingDistancePoints       = 150.0;  // Standard trailing SL distance (points)
input double           InpTrailingStepPoints           = 20.0;   // Trailing modification step (points)
input bool             InpUseBreakEven                 = true;   // Jump SL over commission zone on impulse
input double           InpBreakEvenTriggerPoints       = 125.0;  // Profit points to trigger Commission-Safe BE
input double           InpBreakEvenNetLockPoints       = 20.0;   // Net profit points locked ABOVE commission (Entry+100 pts)
input bool             InpAvoidCommissionDeadZone      = true;   // Never place trailing SL inside commission loss zone
input bool             InpUseRunnerTightTrail          = true;   // Tighten trail on big impulse runners
input double           InpRunnerTriggerPoints          = 350.0;  // Profit points to activate tight trail
input double           InpRunnerTrailDistancePoints    = 100.0;  // Tightened trailing distance (points)

input group "5. M1 Impulse & Trend Filter (Anti-Trap)"
input bool             InpUseTrendFilter               = true;   // Block counter-trend micro-bounce traps
input int              InpFastEMAPeriod                = 9;      // Fast M1 EMA period
input int              InpSlowEMAPeriod                = 21;     // Slow M1 EMA period
input double           InpTrendSlopeMinPoints          = 18.0;   // Min |EMA9-EMA21| points for 1-way bias
input bool             InpUseATRFilter                 = true;   // Require minimum M1 volatility
input int              InpATRPeriod                    = 14;     // M1 ATR period
input double           InpMinATRPoints                 = 135.0;  // Min M1 ATR in points ($1.35) to trade

input group "6. Cooldowns & Whipsaw Circuit Breaker"
input int              InpCooldownAfterSLSeconds       = 18;     // Cooldown after a Stop-Loss hit (sec)
input int              InpCooldownAfterBigWinSeconds   = 75;     // Cooldown after big runner closes (sec)
input double           InpBigWinThresholdPoints        = 220.0;  // Profit points considered a Big Runner
input int              InpMaxConsecutiveLosses         = 3;      // Consecutive losses before pause (0=off)
input int              InpConsecutiveLossPauseMinutes  = 20;     // Pause duration after loss streak (min)

input group "7. Daily Peak Profit Lock & Account Protection"
input bool             InpUseDailyProfitLock           = true;   // Lock daily profit from High-Water Mark
input double           InpProfitLockArmMoney           = 45.0;   // Arm profit lock when day profit >= $
input double           InpProfitLockArmPercent         = 6.0;    // Or when day profit >= % of start balance
input double           InpProfitLockMaxGivebackMoney   = 18.0;   // Max $ pullback allowed from peak once armed
input double           InpProfitLockGivebackPercent    = 28.0;   // Max % of peak profit allowed to give back
input double           InpMaxDailyLossPercent          = 3.00;   // Hard daily loss limit below start balance (%)
input double           InpMaxDailyLossMoney            = 0.0;    // Hard daily loss limit in $ (0=use %)
input double           InpMaxFloatingLossMoney         = 0.0;    // Max floating loss in $ (0=off)
input bool             InpClosePositionsOnDailyStop    = true;   // Close open trades when daily stop/lock hits

input group "8. Trading Session"
input bool             InpUseTradingHours              = false;
input int              InpStartHour                    = 7;
input int              InpEndHour                      = 20;

struct PendingOrderInfo
  {
   bool   found;
   ulong  ticket;
   double volume;
   double price;
   double sl;
   double tp;
  };

int      g_digits                  = 0;
double   g_point                   = 0.0;
uint     g_last_pending_refresh_ms = 0;
bool     g_risk_blocked            = false;
string   g_risk_block_reason       = "";
bool     g_processing              = false;
bool     g_rebuild_after_stop      = false;
string   g_day_key                 = "";
double   g_day_start_balance       = 0.0;
double   g_day_peak_profit         = 0.0;
int      g_consec_losses           = 0;
datetime g_cooldown_until          = 0;
string   g_cooldown_reason         = "";

int      g_ema_fast_handle         = INVALID_HANDLE;
int      g_ema_slow_handle         = INVALID_HANDLE;
int      g_atr_handle              = INVALID_HANDLE;

//+------------------------------------------------------------------+
//| Utility & Global Variable Helpers                                |
//+------------------------------------------------------------------+
bool IsGoldSymbol(const string symbol)
  {
   string upper_symbol=symbol;
   StringToUpper(upper_symbol);
   return(StringFind(upper_symbol,"XAU")>=0 || StringFind(upper_symbol,"GOLD")>=0);
  }

string CurrentDayKey()
  {
   MqlDateTime dt;
   TimeToStruct(TimeCurrent(),dt);
   return(StringFormat("%04d%02d%02d",dt.year,dt.mon,dt.day));
  }

string DailyBaselineGlobalName(const string day_key)
  {
   return("XAU_M1_DAY_BAL_"+IntegerToString((long)InpMagic)+"_"+day_key);
  }

string DailyPeakGlobalName(const string day_key)
  {
   return("XAU_M1_DAY_PEAK_"+IntegerToString((long)InpMagic)+"_"+day_key);
  }

string DailyLockGlobalName(const string day_key)
  {
   return("XAU_M1_DAY_LOCK_"+IntegerToString((long)InpMagic)+"_"+day_key);
  }

void EnsureDailyBaseline()
  {
   const string day_key=CurrentDayKey();
   if(day_key!=g_day_key || g_day_start_balance<=0.0)
     {
      g_day_key=day_key;
      const string bal_name=DailyBaselineGlobalName(day_key);
      const string peak_name=DailyPeakGlobalName(day_key);
      const string lock_name=DailyLockGlobalName(day_key);

      if(GlobalVariableCheck(bal_name))
         g_day_start_balance=GlobalVariableGet(bal_name);
      else
        {
         g_day_start_balance=AccountInfoDouble(ACCOUNT_BALANCE);
         GlobalVariableSet(bal_name,g_day_start_balance);
        }

      if(g_day_start_balance<=0.0)
         g_day_start_balance=AccountInfoDouble(ACCOUNT_EQUITY);

      if(GlobalVariableCheck(peak_name))
         g_day_peak_profit=GlobalVariableGet(peak_name);
      else
        {
         g_day_peak_profit=0.0;
         GlobalVariableSet(peak_name,0.0);
        }

      if(GlobalVariableCheck(lock_name) && GlobalVariableGet(lock_name)>0.5)
        {
         g_risk_blocked=true;
         if(g_risk_block_reason=="")
            g_risk_block_reason="Daily Stop/Profit Lock active for today";
        }
      else
        {
         g_risk_blocked=false;
         g_risk_block_reason="";
        }
      g_consec_losses=0;
      g_cooldown_until=0;
     }

   // Update High-Water Mark (Peak Daily Profit)
   const double current_day_profit=AccountInfoDouble(ACCOUNT_EQUITY)-g_day_start_balance;
   if(current_day_profit>g_day_peak_profit)
     {
      g_day_peak_profit=current_day_profit;
      GlobalVariableSet(DailyPeakGlobalName(g_day_key),g_day_peak_profit);
     }
  }

double DailyLossLimitMoney()
  {
   if(InpMaxDailyLossMoney>0.0)
      return(InpMaxDailyLossMoney);

   if(InpMaxDailyLossPercent>0.0 && g_day_start_balance>0.0)
      return(g_day_start_balance*InpMaxDailyLossPercent/100.0);

   return(0.0);
  }

double OurFloatingProfit()
  {
   double total=0.0;
   for(int i=PositionsTotal()-1;i>=0;i--)
     {
      const ulong ticket=PositionGetTicket(i);
      if(ticket==0)
         continue;
      if(PositionGetString(POSITION_SYMBOL)!=_Symbol)
         continue;
      if((ulong)PositionGetInteger(POSITION_MAGIC)!=InpMagic)
         continue;

      total+=PositionGetDouble(POSITION_PROFIT);
      total+=PositionGetDouble(POSITION_SWAP);
     }
   return(total);
  }

bool DailyProfitLockArmed(double &arm_threshold_out,double &lock_floor_out)
  {
   arm_threshold_out=0.0;
   lock_floor_out=0.0;
   if(!InpUseDailyProfitLock || g_day_start_balance<=0.0)
      return(false);

   double arm_money=InpProfitLockArmMoney;
   if(InpProfitLockArmPercent>0.0)
     {
      const double pct_money=g_day_start_balance*InpProfitLockArmPercent/100.0;
      if(arm_money<=0.0 || pct_money<arm_money)
         arm_money=pct_money;
     }
   if(arm_money<=0.0)
      return(false);

   arm_threshold_out=arm_money;
   if(g_day_peak_profit<arm_money)
      return(false);

   double allowed_giveback=InpProfitLockMaxGivebackMoney;
   if(InpProfitLockGivebackPercent>0.0)
     {
      const double pct_giveback=g_day_peak_profit*InpProfitLockGivebackPercent/100.0;
      if(allowed_giveback<=0.0 || pct_giveback<allowed_giveback)
         allowed_giveback=pct_giveback;
     }
   if(allowed_giveback<=0.0)
      allowed_giveback=15.0;

   lock_floor_out=MathMax(5.0,g_day_peak_profit-allowed_giveback);
   return(true);
  }

bool DailyRiskOrProfitLockTriggered(string &reason_out)
  {
   reason_out="";
   const double equity=AccountInfoDouble(ACCOUNT_EQUITY);
   const double current_day_profit=equity-g_day_start_balance;

   // 1. Check Daily High-Water Mark Profit Lock
   double arm_thresh=0.0;
   double lock_floor=0.0;
   if(DailyProfitLockArmed(arm_thresh,lock_floor))
     {
      if(current_day_profit<=lock_floor)
        {
         reason_out=StringFormat("DAILY PROFIT LOCKED at $%.2f (Peak was +$%.2f)",current_day_profit,g_day_peak_profit);
         return(true);
        }
     }

   // 2. Check Hard Daily Loss Limit below Start Balance
   const double daily_limit=DailyLossLimitMoney();
   if(daily_limit>0.0 && g_day_start_balance>0.0)
     {
      if(g_day_start_balance-equity>=daily_limit)
        {
         reason_out=StringFormat("MAX DAILY LOSS LIMIT hit (-$%.2f)",g_day_start_balance-equity);
         return(true);
        }
     }

   // 3. Check Max Floating Loss
   if(InpMaxFloatingLossMoney>0.0 && OurFloatingProfit()<=-InpMaxFloatingLossMoney)
     {
      reason_out=StringFormat("MAX FLOATING LOSS hit ($%.2f)",OurFloatingProfit());
      return(true);
     }

   return(false);
  }

bool IsWithinTradingHours()
  {
   if(!InpUseTradingHours)
      return(true);

   if(InpStartHour<0 || InpStartHour>23 || InpEndHour<0 || InpEndHour>24)
      return(false);

   if(InpStartHour==InpEndHour)
      return(true);

   MqlDateTime dt;
   TimeToStruct(TimeCurrent(),dt);
   const int hour=dt.hour;

   if(InpStartHour<InpEndHour)
      return(hour>=InpStartHour && hour<InpEndHour);

   return(hour>=InpStartHour || hour<InpEndHour);
  }

bool TerminalCanTrade()
  {
   if(!TerminalInfoInteger(TERMINAL_CONNECTED))
      return(false);
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED))
      return(false);
   if(!MQLInfoInteger(MQL_TRADE_ALLOWED))
      return(false);

   const ENUM_SYMBOL_TRADE_MODE mode=(ENUM_SYMBOL_TRADE_MODE)SymbolInfoInteger(_Symbol,SYMBOL_TRADE_MODE);
   if(mode==SYMBOL_TRADE_MODE_DISABLED || mode==SYMBOL_TRADE_MODE_CLOSEONLY)
      return(false);

   return(true);
  }

bool CanPlaceOrderType(const ENUM_ORDER_TYPE order_type)
  {
   const ENUM_SYMBOL_TRADE_MODE mode=(ENUM_SYMBOL_TRADE_MODE)SymbolInfoInteger(_Symbol,SYMBOL_TRADE_MODE);
   if(mode==SYMBOL_TRADE_MODE_DISABLED || mode==SYMBOL_TRADE_MODE_CLOSEONLY)
      return(false);

   if(order_type==ORDER_TYPE_BUY_STOP && mode==SYMBOL_TRADE_MODE_SHORTONLY)
      return(false);
   if(order_type==ORDER_TYPE_SELL_STOP && mode==SYMBOL_TRADE_MODE_LONGONLY)
      return(false);

   return(true);
  }

bool SpreadIsAcceptable(const MqlTick &tick)
  {
   if(InpMaxSpreadPoints<=0.0)
      return(true);
   if(g_point<=0.0 || tick.ask<=0.0 || tick.bid<=0.0 || tick.ask<tick.bid)
      return(false);

   return((tick.ask-tick.bid)/g_point<=InpMaxSpreadPoints);
  }

double MinimumProtectionDistance()
  {
   const long stops_level=SymbolInfoInteger(_Symbol,SYMBOL_TRADE_STOPS_LEVEL);
   const long freeze_level=SymbolInfoInteger(_Symbol,SYMBOL_TRADE_FREEZE_LEVEL);
   const long broker_points=MathMax(stops_level,freeze_level);
   return((double)(broker_points+1)*g_point);
  }

double RoundToTick(const double price,const bool round_up)
  {
   const double tick_size=SymbolInfoDouble(_Symbol,SYMBOL_TRADE_TICK_SIZE);
   if(tick_size<=0.0)
      return(NormalizeDouble(price,g_digits));

   double rounded=MathRound(price/tick_size)*tick_size;
   if(round_up && rounded<price)
      rounded+=tick_size;
   if(!round_up && rounded>price)
      rounded-=tick_size;

   return(NormalizeDouble(rounded,g_digits));
  }

int VolumeDigits(const double step)
  {
   for(int digits=0;digits<=8;digits++)
     {
      if(MathAbs(step-NormalizeDouble(step,digits))<1.0e-8)
         return(digits);
     }
   return(8);
  }

double NormalizeVolumeDown(const double requested)
  {
   if(requested<=0.0)
      return(0.0);

   const double volume_min=SymbolInfoDouble(_Symbol,SYMBOL_VOLUME_MIN);
   const double volume_max=SymbolInfoDouble(_Symbol,SYMBOL_VOLUME_MAX);
   const double volume_step=SymbolInfoDouble(_Symbol,SYMBOL_VOLUME_STEP);
   if(volume_min<=0.0 || volume_max<=0.0 || volume_step<=0.0)
      return(0.0);

   double volume=MathMin(requested,volume_max);
   volume=MathFloor((volume+1.0e-12)/volume_step)*volume_step;
   volume=NormalizeDouble(volume,VolumeDigits(volume_step));

   if(volume<volume_min-1.0e-12)
      return(0.0);
   return(volume);
  }

double LossPerLot(const ENUM_ORDER_TYPE pending_type,const double entry,const double sl)
  {
   if(sl<=0.0 || entry<=0.0 || g_point<=0.0)
      return(0.0);

   const ENUM_ORDER_TYPE calculation_type=(pending_type==ORDER_TYPE_BUY_STOP ? ORDER_TYPE_BUY : ORDER_TYPE_SELL);
   double profit=0.0;
   if(OrderCalcProfit(calculation_type,_Symbol,1.0,entry,sl,profit))
      return(MathAbs(profit));

   const double tick_size=SymbolInfoDouble(_Symbol,SYMBOL_TRADE_TICK_SIZE);
   const double tick_value=SymbolInfoDouble(_Symbol,SYMBOL_TRADE_TICK_VALUE);
   if(tick_size<=0.0 || tick_value<=0.0)
      return(0.0);

   return(MathAbs(entry-sl)/tick_size*tick_value);
  }

double CalculateVolume(const ENUM_ORDER_TYPE pending_type,const double entry,const double sl)
  {
   double requested=InpLots;
   double risk_budget=0.0;

   if(InpUseRiskBasedLots)
     {
      const double balance=AccountInfoDouble(ACCOUNT_BALANCE);
      risk_budget=balance*InpRiskPerTradePercent/100.0;
     }
   else if(InpCapFixedLotsByRisk)
     {
      const double balance=AccountInfoDouble(ACCOUNT_BALANCE);
      risk_budget=balance*InpMaxRiskPerTradePercent/100.0;
     }

   if(risk_budget>0.0)
     {
      const double loss_per_lot=LossPerLot(pending_type,entry,sl);
      if(loss_per_lot>0.0)
        {
         const double risk_volume=NormalizeVolumeDown(risk_budget/loss_per_lot);
         if(InpUseRiskBasedLots)
            requested=risk_volume;
         else
            requested=MathMin(requested,risk_volume);
        }
      else if(InpUseRiskBasedLots)
         requested=0.0;
     }

   return(NormalizeVolumeDown(requested));
  }

bool HasEnoughMargin(const ENUM_ORDER_TYPE pending_type,const double volume,const double entry)
  {
   double margin=0.0;
   const ENUM_ORDER_TYPE calculation_type=(pending_type==ORDER_TYPE_BUY_STOP ? ORDER_TYPE_BUY : ORDER_TYPE_SELL);
   if(!OrderCalcMargin(calculation_type,_Symbol,volume,entry,margin))
      return(true);

   if(margin<=0.0)
      return(true);

   const double free_margin=AccountInfoDouble(ACCOUNT_MARGIN_FREE);
   return(free_margin>=margin*1.05);
  }

void BuildTrailingStop(const ENUM_ORDER_TYPE pending_type,const double entry,double &sl)
  {
   sl=0.0;

   const double minimum_distance=MinimumProtectionDistance();
   const double trailing_distance=MathMax(InpTrailingDistancePoints*g_point,minimum_distance);

   if(pending_type==ORDER_TYPE_BUY_STOP)
      sl=RoundToTick(entry-trailing_distance,false);
   else
      sl=RoundToTick(entry+trailing_distance,true);
  }

void DesiredPendingPrices(const MqlTick &tick,double &buy_entry,double &sell_entry)
  {
   const double minimum_distance=MinimumProtectionDistance();
   const double requested_step=MathMax(InpEntryStepPoints*g_point,minimum_distance);

   buy_entry=RoundToTick(tick.ask+requested_step,true);
   sell_entry=RoundToTick(tick.bid-requested_step,false);
  }

string OrderComment(const string suffix)
  {
   string comment=InpCommentPrefix+"_"+suffix;
   if(StringLen(comment)>31)
      comment=StringSubstr(comment,0,31);
   return(comment);
  }

bool TradeRetcodeIsSuccess()
  {
   const uint retcode=trade.ResultRetcode();
   return(retcode==TRADE_RETCODE_DONE ||
          retcode==TRADE_RETCODE_DONE_PARTIAL ||
          retcode==TRADE_RETCODE_PLACED ||
          retcode==TRADE_RETCODE_NO_CHANGES);
  }

void LogTradeFailure(const string action)
  {
   Print(action," failed. retcode=",trade.ResultRetcode(),
         " description=",trade.ResultRetcodeDescription(),
         " last_error=",GetLastError());
  }

//+------------------------------------------------------------------+
//| M1 Trend & Volatility Evaluation                                 |
//+------------------------------------------------------------------+
void EvaluateMarketRegime(const MqlTick &tick,bool &allow_buy_out,bool &allow_sell_out,double &atr_points_out,string &regime_label_out)
  {
   allow_buy_out=true;
   allow_sell_out=true;
   atr_points_out=0.0;
   regime_label_out="BOTH (Breakout)";

   if(g_point<=0.0)
      return;

   if(InpUseATRFilter && g_atr_handle!=INVALID_HANDLE)
     {
      double atr_buf[];
      ArraySetAsSeries(atr_buf,true);
      if(CopyBuffer(g_atr_handle,0,0,2,atr_buf)>=1 && atr_buf[0]>0.0)
        {
         atr_points_out=atr_buf[0]/g_point;
         if(InpMinATRPoints>0.0 && atr_points_out<InpMinATRPoints)
           {
            allow_buy_out=false;
            allow_sell_out=false;
            regime_label_out=StringFormat("LOW VOLATILITY (ATR=%.0f < %.0f pts)",atr_points_out,InpMinATRPoints);
            return;
           }
        }
     }

   if(InpUseTrendFilter && g_ema_fast_handle!=INVALID_HANDLE && g_ema_slow_handle!=INVALID_HANDLE)
     {
      double fast_buf[],slow_buf[];
      ArraySetAsSeries(fast_buf,true);
      ArraySetAsSeries(slow_buf,true);
      if(CopyBuffer(g_ema_fast_handle,0,0,2,fast_buf)>=2 && CopyBuffer(g_ema_slow_handle,0,0,2,slow_buf)>=2)
        {
         const double ema_fast=fast_buf[0];
         const double ema_slow=slow_buf[0];
         const double diff_points=(ema_fast-ema_slow)/g_point;
         const double mid_price=(tick.bid+tick.ask)*0.5;

         if(diff_points>=InpTrendSlopeMinPoints && mid_price>=ema_slow)
           {
            // Strong Bullish M1 structure -> Block counter-trend SellStop traps
            allow_buy_out=true;
            allow_sell_out=false;
            regime_label_out=StringFormat("BULLISH IMPULSE (+%.0f pts) -> BUY ONLY",diff_points);
           }
         else if(diff_points<=-InpTrendSlopeMinPoints && mid_price<=ema_slow)
           {
            // Strong Bearish M1 structure -> Block counter-trend BuyStop traps
            allow_buy_out=false;
            allow_sell_out=true;
            regime_label_out=StringFormat("BEARISH IMPULSE (%.0f pts) -> SELL ONLY",diff_points);
           }
         else
           {
            allow_buy_out=true;
            allow_sell_out=true;
            regime_label_out=StringFormat("COILED / NEUTRAL (%.0f pts) -> BOTH",diff_points);
           }
        }
     }
  }

//+------------------------------------------------------------------+
//| Pending Order Helpers                                            |
//+------------------------------------------------------------------+
bool IsOurPendingOrder()
  {
   if(OrderGetString(ORDER_SYMBOL)!=_Symbol)
      return(false);
   if((ulong)OrderGetInteger(ORDER_MAGIC)!=InpMagic)
      return(false);

   const ENUM_ORDER_TYPE type=(ENUM_ORDER_TYPE)OrderGetInteger(ORDER_TYPE);
   return(type==ORDER_TYPE_BUY_STOP || type==ORDER_TYPE_SELL_STOP);
  }

bool FindPendingOrder(const ENUM_ORDER_TYPE wanted_type,PendingOrderInfo &info)
  {
   info.found=false;
   info.ticket=0;
   info.volume=0.0;
   info.price=0.0;
   info.sl=0.0;
   info.tp=0.0;

   for(int i=OrdersTotal()-1;i>=0;i--)
     {
      const ulong ticket=OrderGetTicket(i);
      if(ticket==0 || !IsOurPendingOrder())
         continue;

      const ENUM_ORDER_TYPE type=(ENUM_ORDER_TYPE)OrderGetInteger(ORDER_TYPE);
      if(type!=wanted_type)
         continue;

      if(!info.found)
        {
         info.found=true;
         info.ticket=ticket;
         info.volume=OrderGetDouble(ORDER_VOLUME_CURRENT);
         info.price=OrderGetDouble(ORDER_PRICE_OPEN);
         info.sl=OrderGetDouble(ORDER_SL);
         info.tp=OrderGetDouble(ORDER_TP);
        }
      else
        {
         ResetLastError();
         if(!trade.OrderDelete(ticket) || !TradeRetcodeIsSuccess())
            LogTradeFailure("OrderDelete duplicate pending");
        }
     }

   return(info.found);
  }

void DeletePendingType(const ENUM_ORDER_TYPE wanted_type)
  {
   for(int i=OrdersTotal()-1;i>=0;i--)
     {
      const ulong ticket=OrderGetTicket(i);
      if(ticket==0 || !IsOurPendingOrder())
         continue;
      if((ENUM_ORDER_TYPE)OrderGetInteger(ORDER_TYPE)!=wanted_type)
         continue;

      ResetLastError();
      if(!trade.OrderDelete(ticket) || !TradeRetcodeIsSuccess())
         LogTradeFailure("OrderDelete");
     }
  }

void DeleteOurPendingOrders()
  {
   for(int i=OrdersTotal()-1;i>=0;i--)
     {
      const ulong ticket=OrderGetTicket(i);
      if(ticket==0 || !IsOurPendingOrder())
         continue;

      ResetLastError();
      if(!trade.OrderDelete(ticket) || !TradeRetcodeIsSuccess())
         LogTradeFailure("OrderDelete");
     }
  }

int OurPositionCount()
  {
   int count=0;
   for(int i=PositionsTotal()-1;i>=0;i--)
     {
      const ulong ticket=PositionGetTicket(i);
      if(ticket==0)
         continue;
      if(PositionGetString(POSITION_SYMBOL)!=_Symbol)
         continue;
      if((ulong)PositionGetInteger(POSITION_MAGIC)!=InpMagic)
         continue;
      count++;
     }
   return(count);
  }

bool PlacePending(const ENUM_ORDER_TYPE pending_type,const double volume,const double entry,const double sl)
  {
   if(!CanPlaceOrderType(pending_type) || !HasEnoughMargin(pending_type,volume,entry))
      return(false);

   ResetLastError();
   bool sent=false;
   if(pending_type==ORDER_TYPE_BUY_STOP)
      sent=trade.BuyStop(volume,entry,_Symbol,sl,0.0,ORDER_TIME_GTC,0,OrderComment("BS"));
   else
      sent=trade.SellStop(volume,entry,_Symbol,sl,0.0,ORDER_TIME_GTC,0,OrderComment("SS"));

   if(!sent || !TradeRetcodeIsSuccess())
     {
      LogTradeFailure(pending_type==ORDER_TYPE_BUY_STOP ? "BuyStop" : "SellStop");
      return(false);
     }
   return(true);
  }

bool PendingRefreshIsDue()
  {
   if(InpPendingRefreshMilliseconds<=0)
      return(true);

   const uint now=GetTickCount();
   if(g_last_pending_refresh_ms!=0 &&
      (uint)(now-g_last_pending_refresh_ms)<(uint)InpPendingRefreshMilliseconds)
      return(false);

   g_last_pending_refresh_ms=now;
   return(true);
  }

void EnsurePendingOrder(const ENUM_ORDER_TYPE pending_type,const double desired_entry)
  {
   if(!CanPlaceOrderType(pending_type) || desired_entry<=0.0)
      return;

   double sl=0.0;
   BuildTrailingStop(pending_type,desired_entry,sl);

   const double volume=CalculateVolume(pending_type,desired_entry,sl);
   if(volume<=0.0)
      return;

   PendingOrderInfo current;
   if(FindPendingOrder(pending_type,current))
     {
      const double volume_step=SymbolInfoDouble(_Symbol,SYMBOL_VOLUME_STEP);
      const double price_step=MathMax(g_point,InpRequoteStepPoints*g_point);
      const bool volume_changed=(volume_step>0.0 && MathAbs(current.volume-volume)>volume_step/2.0);
      const bool price_changed=(MathAbs(current.price-desired_entry)>=price_step);
      const bool sl_changed=(MathAbs(current.sl-sl)>g_point/2.0);
      const bool tp_changed=(current.tp>0.0);

      if(volume_changed)
        {
         ResetLastError();
         if(trade.OrderDelete(current.ticket) && TradeRetcodeIsSuccess())
            PlacePending(pending_type,volume,desired_entry,sl);
         else
            LogTradeFailure("OrderDelete for volume refresh");
         return;
        }

      if(price_changed || sl_changed || tp_changed)
        {
         ResetLastError();
         if(!trade.OrderModify(current.ticket,desired_entry,sl,0.0,ORDER_TIME_GTC,0,0.0) || !TradeRetcodeIsSuccess())
            LogTradeFailure("OrderModify");
        }
      return;
     }

   PlacePending(pending_type,volume,desired_entry,sl);
  }

//+------------------------------------------------------------------+
//| Position Management (Break-Even + Dynamic Runner Trailing Stop)  |
//+------------------------------------------------------------------+
void ManageOpenPositions()
  {
   MqlTick tick;
   if(!SymbolInfoTick(_Symbol,tick))
      return;

   const double minimum_distance=MinimumProtectionDistance();
   const double std_trailing_distance=MathMax(InpTrailingDistancePoints*g_point,minimum_distance);
   const double tight_trailing_distance=MathMax(InpRunnerTrailDistancePoints*g_point,minimum_distance);
   const double trail_step=MathMax(1.0,InpTrailingStepPoints)*g_point;

   for(int i=PositionsTotal()-1;i>=0;i--)
     {
      const ulong ticket=PositionGetTicket(i);
      if(ticket==0)
         continue;
      if(PositionGetString(POSITION_SYMBOL)!=_Symbol)
         continue;
      if((ulong)PositionGetInteger(POSITION_MAGIC)!=InpMagic)
         continue;

      const ENUM_POSITION_TYPE position_type=(ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
      const double open_price=PositionGetDouble(POSITION_PRICE_OPEN);
      const double current_sl=PositionGetDouble(POSITION_SL);
      const double current_tp=PositionGetDouble(POSITION_TP);

      const double comm_safe_lock_points=MathMax(0.0,InpCommissionRoundTurnPoints)+MathMax(5.0,InpBreakEvenNetLockPoints);

      if(position_type==POSITION_TYPE_BUY)
        {
         const double profit_points=(tick.bid-open_price)/g_point;
         double active_trail_dist=std_trailing_distance;
         if(InpUseRunnerTightTrail && profit_points>=InpRunnerTriggerPoints)
            active_trail_dist=tight_trailing_distance;

         double candidate=tick.bid-active_trail_dist;
         const double comm_safe_sl=open_price+comm_safe_lock_points*g_point;

         // Fast Commission-Proof Break-Even lock (jumps over commission dead-zone)
         if(InpUseBreakEven && profit_points>=InpBreakEvenTriggerPoints)
           {
            if(comm_safe_sl>candidate)
               candidate=comm_safe_sl;
           }

         // Never trail SL into the commission dead-zone (between open_price - 15 pts and comm_safe_sl)
         // where a tiny pullback closes the trade for a guaranteed commission loss!
         if(InpAvoidCommissionDeadZone && candidate>open_price-15.0*g_point && candidate<comm_safe_sl)
           {
            if(profit_points>=InpBreakEvenTriggerPoints)
               candidate=comm_safe_sl;
            else
               continue; // Keep original protective SL until price clears the commission buffer
           }

         candidate=MathMin(candidate,tick.bid-minimum_distance);
         candidate=RoundToTick(candidate,false);
         if(candidate<=0.0)
            continue;

         double new_sl=current_sl;
         bool should_modify=(current_tp>0.0);
         if(current_sl<=0.0 || candidate>current_sl+trail_step)
           {
            new_sl=candidate;
            should_modify=true;
           }

         if(should_modify)
           {
            ResetLastError();
            if(!trade.PositionModify(ticket,new_sl,0.0) || !TradeRetcodeIsSuccess())
               LogTradeFailure("PositionModify BUY");
           }
        }
      else if(position_type==POSITION_TYPE_SELL)
        {
         const double profit_points=(open_price-tick.ask)/g_point;
         double active_trail_dist=std_trailing_distance;
         if(InpUseRunnerTightTrail && profit_points>=InpRunnerTriggerPoints)
            active_trail_dist=tight_trailing_distance;

         double candidate=tick.ask+active_trail_dist;
         const double comm_safe_sl=open_price-comm_safe_lock_points*g_point;

         // Fast Commission-Proof Break-Even lock (jumps over commission dead-zone)
         if(InpUseBreakEven && profit_points>=InpBreakEvenTriggerPoints)
           {
            if(comm_safe_sl<candidate)
               candidate=comm_safe_sl;
           }

         // Never trail SL into the commission dead-zone
         if(InpAvoidCommissionDeadZone && candidate<open_price+15.0*g_point && candidate>comm_safe_sl)
           {
            if(profit_points>=InpBreakEvenTriggerPoints)
               candidate=comm_safe_sl;
            else
               continue;
           }

         candidate=MathMax(candidate,tick.ask+minimum_distance);
         candidate=RoundToTick(candidate,true);
         if(candidate<=0.0)
            continue;

         double new_sl=current_sl;
         bool should_modify=(current_tp>0.0);
         if(current_sl<=0.0 || candidate<current_sl-trail_step)
           {
            new_sl=candidate;
            should_modify=true;
           }

         if(should_modify)
           {
            ResetLastError();
            if(!trade.PositionModify(ticket,new_sl,0.0) || !TradeRetcodeIsSuccess())
               LogTradeFailure("PositionModify SELL");
           }
        }
     }
  }

void CloseOurPositions()
  {
   for(int i=PositionsTotal()-1;i>=0;i--)
     {
      const ulong ticket=PositionGetTicket(i);
      if(ticket==0)
         continue;
      if(PositionGetString(POSITION_SYMBOL)!=_Symbol)
         continue;
      if((ulong)PositionGetInteger(POSITION_MAGIC)!=InpMagic)
         continue;

      ResetLastError();
      if(!trade.PositionClose(ticket,(ulong)MathMax(0,InpDeviationPoints)) || !TradeRetcodeIsSuccess())
         LogTradeFailure("PositionClose risk stop");
     }
  }

void TriggerRiskStop(const string reason)
  {
   if(!g_risk_blocked)
     {
      Print("Protection Stop activated for ",_Symbol,": ",reason,". New orders blocked until next day.");
      g_risk_block_reason=reason;
      GlobalVariableSet(DailyLockGlobalName(g_day_key),1.0);
     }

   g_risk_blocked=true;
   DeleteOurPendingOrders();
   if(InpClosePositionsOnDailyStop)
      CloseOurPositions();
  }

//+------------------------------------------------------------------+
//| Chart HUD Telemetry                                              |
//+------------------------------------------------------------------+
void UpdateChartHUD(const string regime_label,const double atr_points)
  {
   if(!InpShowChartHUD)
      return;

   const double equity=AccountInfoDouble(ACCOUNT_EQUITY);
   const double day_pnl=equity-g_day_start_balance;
   double arm_thresh=0.0,lock_floor=0.0;
   const bool lock_armed=DailyProfitLockArmed(arm_thresh,lock_floor);

   string lock_status="INACTIVE";
   if(g_risk_blocked)
      lock_status="LOCKED ("+g_risk_block_reason+")";
   else if(lock_armed)
      lock_status=StringFormat("ARMED! Floor: +$%.2f (Peak: +$%.2f)",lock_floor,g_day_peak_profit);
   else if(InpUseDailyProfitLock)
      lock_status=StringFormat("Waiting for +$%.2f (Peak: +$%.2f)",arm_thresh,g_day_peak_profit);

   string cd_status="READY";
   const datetime now=TimeCurrent();
   if(g_cooldown_until>now)
      cd_status=StringFormat("COOLDOWN (%ds left - %s)",(int)(g_cooldown_until-now),g_cooldown_reason);

   string hud=StringFormat(
      "=== XAUUSD M1 IMPULSE SNIPER v2.0 ===\n"
      "Day Start Bal : $%.2f | Equity: $%.2f | Day P/L: %s$%.2f\n"
      "Profit Lock   : %s\n"
      "Loss Streak   : %d / %d | Status: %s\n"
      "M1 Regime     : %s | M1 ATR: %.0f pts",
      g_day_start_balance,
      equity,
      (day_pnl>=0.0 ? "+" : ""),
      day_pnl,
      lock_status,
      g_consec_losses,
      InpMaxConsecutiveLosses,
      cd_status,
      regime_label,
      atr_points
   );
   Comment(hud);
  }

//+------------------------------------------------------------------+
//| Pending-order Maintenance                                        |
//+------------------------------------------------------------------+
void MaintainPendingOrders()
  {
   if(!TerminalCanTrade() || !IsWithinTradingHours())
     {
      DeleteOurPendingOrders();
      return;
     }

   const datetime now=TimeCurrent();
   if(g_cooldown_until>now)
     {
      DeleteOurPendingOrders();
      return;
     }

   MqlTick tick;
   if(!SymbolInfoTick(_Symbol,tick) || !SpreadIsAcceptable(tick))
     {
      DeleteOurPendingOrders();
      return;
     }

   if(InpMaxOpenPositions>0 && OurPositionCount()>=InpMaxOpenPositions)
     {
      DeleteOurPendingOrders();
      return;
     }

   bool allow_buy=true;
   bool allow_sell=true;
   double atr_pts=0.0;
   string regime_label="";
   EvaluateMarketRegime(tick,allow_buy,allow_sell,atr_pts,regime_label);
   UpdateChartHUD(regime_label,atr_pts);

   if(!allow_buy)
      DeletePendingType(ORDER_TYPE_BUY_STOP);
   if(!allow_sell)
      DeletePendingType(ORDER_TYPE_SELL_STOP);

   if(!allow_buy && !allow_sell)
      return;

   if(!PendingRefreshIsDue())
      return;

   double buy_entry=0.0;
   double sell_entry=0.0;
   DesiredPendingPrices(tick,buy_entry,sell_entry);

   if(allow_buy)
      EnsurePendingOrder(ORDER_TYPE_BUY_STOP,buy_entry);
   if(allow_sell)
      EnsurePendingOrder(ORDER_TYPE_SELL_STOP,sell_entry);
  }

void ProcessEA()
  {
   if(g_processing)
      return;
   g_processing=true;

   EnsureDailyBaseline();
   ManageOpenPositions();

   string stop_reason="";
   if(DailyRiskOrProfitLockTriggered(stop_reason))
     {
      TriggerRiskStop(stop_reason);
      UpdateChartHUD("DAILY STOP/LOCK",0.0);
      g_processing=false;
      return;
     }

   if(g_risk_blocked)
     {
      DeleteOurPendingOrders();
      UpdateChartHUD("DAILY STOP/LOCK",0.0);
      g_processing=false;
      return;
     }

   if(g_rebuild_after_stop && OurPositionCount()==0)
     {
      DeleteOurPendingOrders();
      g_last_pending_refresh_ms=0;
      g_rebuild_after_stop=false;
     }

   if(InpMaxOpenPositions>0 && OurPositionCount()>=InpMaxOpenPositions)
     {
      DeleteOurPendingOrders();
      g_processing=false;
      return;
     }

   MaintainPendingOrders();
   g_processing=false;
  }

//+------------------------------------------------------------------+
//| Expert Lifecycle                                                 |
//+------------------------------------------------------------------+
int OnInit()
  {
   if(InpRequireGoldSymbol && !IsGoldSymbol(_Symbol))
     {
      Print("Attach this EA to an XAU/GOLD symbol. Current symbol: ",_Symbol);
      return(INIT_FAILED);
     }

   if(!SymbolSelect(_Symbol,true))
      return(INIT_FAILED);

   g_digits=(int)SymbolInfoInteger(_Symbol,SYMBOL_DIGITS);
   g_point=SymbolInfoDouble(_Symbol,SYMBOL_POINT);
   if(g_digits<0 || g_point<=0.0)
      return(INIT_FAILED);

   if(InpEntryStepPoints<=0.0 || InpTrailingDistancePoints<=0.0 ||
      InpTrailingStepPoints<=0.0 || InpLots<=0.0 || InpMaxOpenPositions<1)
     {
      Print("Invalid EA input parameters.");
      return(INIT_PARAMETERS_INCORRECT);
     }

   g_ema_fast_handle=iMA(_Symbol,PERIOD_M1,MathMax(2,InpFastEMAPeriod),0,MODE_EMA,PRICE_CLOSE);
   g_ema_slow_handle=iMA(_Symbol,PERIOD_M1,MathMax(5,InpSlowEMAPeriod),0,MODE_EMA,PRICE_CLOSE);
   g_atr_handle=iATR(_Symbol,PERIOD_M1,MathMax(2,InpATRPeriod));

   trade.SetExpertMagicNumber(InpMagic);
   trade.SetDeviationInPoints((ulong)MathMax(0,InpDeviationPoints));
   trade.SetAsyncMode(false);
   trade.SetTypeFillingBySymbol(_Symbol);

   EnsureDailyBaseline();
   EventSetTimer(MathMax(1,InpTimerSeconds));

   Print("XAU M1 Impulse Sniper v2.0 initialized on ",_Symbol,
         ". StartBal=",DoubleToString(g_day_start_balance,2),
         ", PeakProfit=",DoubleToString(g_day_peak_profit,2));
   return(INIT_SUCCEEDED);
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
   if(g_ema_fast_handle!=INVALID_HANDLE)
      IndicatorRelease(g_ema_fast_handle);
   if(g_ema_slow_handle!=INVALID_HANDLE)
      IndicatorRelease(g_ema_slow_handle);
   if(g_atr_handle!=INVALID_HANDLE)
      IndicatorRelease(g_atr_handle);
   if(InpShowChartHUD)
      Comment("");
   if(InpDeletePendingOnDeinit)
      DeleteOurPendingOrders();
  }

void OnTick()
  {
   ProcessEA();
  }

void OnTimer()
  {
   ProcessEA();
  }

void OnTradeTransaction(const MqlTradeTransaction &transaction,
                        const MqlTradeRequest &request,
                        const MqlTradeResult &result)
  {
   if(transaction.type!=TRADE_TRANSACTION_DEAL_ADD || transaction.deal==0)
      return;
   if(!HistoryDealSelect(transaction.deal))
      return;
   if(HistoryDealGetString(transaction.deal,DEAL_SYMBOL)!=_Symbol)
      return;
   if((ulong)HistoryDealGetInteger(transaction.deal,DEAL_MAGIC)!=InpMagic)
      return;

   const long entry=HistoryDealGetInteger(transaction.deal,DEAL_ENTRY);
   if(entry==DEAL_ENTRY_IN || entry==DEAL_ENTRY_INOUT)
     {
      DeleteOurPendingOrders();
      return;
     }

   if(entry==DEAL_ENTRY_OUT || entry==DEAL_ENTRY_OUT_BY)
     {
      const double deal_profit=HistoryDealGetDouble(transaction.deal,DEAL_PROFIT)
                              +HistoryDealGetDouble(transaction.deal,DEAL_COMMISSION)
                              +HistoryDealGetDouble(transaction.deal,DEAL_SWAP);
      const double deal_vol=HistoryDealGetDouble(transaction.deal,DEAL_VOLUME);
      const double tick_val=SymbolInfoDouble(_Symbol,SYMBOL_TRADE_TICK_VALUE);
      const double tick_sz=SymbolInfoDouble(_Symbol,SYMBOL_TRADE_TICK_SIZE);

      double approx_points=0.0;
      if(deal_vol>0.0 && tick_val>0.0 && tick_sz>0.0 && g_point>0.0)
         approx_points=(deal_profit/(deal_vol*(tick_val/tick_sz)))*tick_sz/g_point;

      g_rebuild_after_stop=true;
      g_last_pending_refresh_ms=0;
      DeleteOurPendingOrders();

      const datetime now=TimeCurrent();

      if(deal_profit<0.0)
        {
         g_consec_losses++;
         if(InpMaxConsecutiveLosses>0 && g_consec_losses>=InpMaxConsecutiveLosses)
           {
            const int pause_sec=MathMax(60,InpConsecutiveLossPauseMinutes*60);
            g_cooldown_until=now+pause_sec;
            g_cooldown_reason=StringFormat("%d Consecutive Losses (Whipsaw Pause)",g_consec_losses);
            Print("Whipsaw Circuit Breaker triggered after ",g_consec_losses," losses. Pausing until ",TimeToString(g_cooldown_until));
            g_consec_losses=0;
           }
         else if(InpCooldownAfterSLSeconds>0)
           {
            g_cooldown_until=now+InpCooldownAfterSLSeconds;
            g_cooldown_reason="Post-SL Cooldown";
           }
        }
      else
        {
         g_consec_losses=0;
         if(InpCooldownAfterBigWinSeconds>0 && approx_points>=InpBigWinThresholdPoints)
           {
            g_cooldown_until=now+InpCooldownAfterBigWinSeconds;
            g_cooldown_reason=StringFormat("Post-Runner Exhaustion Guard (+%.0f pts)",approx_points);
            Print("Big Impulse Runner closed (+$",DoubleToString(deal_profit,2),"). Pausing ",InpCooldownAfterBigWinSeconds,"s to avoid top exhaustion.");
           }
         else if(InpCooldownAfterSLSeconds>0)
           {
            // Short 5s breather after normal green exit
            g_cooldown_until=now+MathMin(5,InpCooldownAfterSLSeconds);
            g_cooldown_reason="Post-Exit Breather";
           }
        }
     }
  }
