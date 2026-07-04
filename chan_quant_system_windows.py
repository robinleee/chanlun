import datetime
import os
import re
import sys
import threading
from enum import Enum
from collections import deque
import numpy as np
from xtquant import xtdata as _xtdata_real
xtdata = _xtdata_real

from chanlun_engine.inclusion_filter import KLineInclusionFilter, StandardKLine
from chanlun_engine.geometry_engine import Fractal, Bi, StrictBiValidator, Segment, SegmentEngine, Zhongshu, ZhongshuFSM
from chanlun_engine.dynamics_engine import DynamicsDivergenceEngine, MultiTimeframeScanner, SmallToLargeGateway
from chanlun_engine.account_fsm import AccountStateMachine, AccountState

from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
from xtquant.xttype import StockOrderContext
from xtquant.xtconstant import STOCK_BUY, STOCK_SELL

_NEXT_BACKTEST_ORDER_ID = [0]

def _next_backtest_order_id():
    _NEXT_BACKTEST_ORDER_ID[0] += 1
    return f"BT{_NEXT_BACKTEST_ORDER_ID[0]:08d}"

class MyXtQuantCallback(XtQuantTraderCallback):
    def __init__(self, controller):
        self.controller = controller

    def on_order_stock_async_response(self, response):
        print(f"QMT 异步下单确认, 订单 ID: {response.order_id}, 状态: {response.order_status}")

    def on_stock_trade(self, trade):
        print(f"QMT 收到成交回报: {trade.stock_code} | 方向: {trade.offset_flag} | 数量: {trade.traded_volume} | 价格: {trade.traded_price} | order_id: {trade.order_id}")
        side = "BUY" if trade.offset_flag == STOCK_BUY else "SELL"
        self.controller.handle_trade_callback(
            trade.stock_code, side, trade.traded_volume, trade.traded_price,
            order_id=str(trade.order_id), traded_time=getattr(trade, "traded_time", None)
        )

class QMTTraderGateway:
    def __init__(self, session_id, qmt_path, controller, is_live=False):
        self.is_live = is_live
        self.controller = controller
        if self.is_live:
            self.trader = XtQuantTrader(qmt_path, session_id)
            self.trader.start()
            self.trader.register_callback(MyXtQuantCallback(self.controller))
            self.acc = StockOrderContext(account_id="STOCK_ACC_01", account_type="STOCK")
            self.trader.connect()
        else:
            print("当前处于：回测驱动模式（订单执行已拦截为虚拟流）")

    def submit_order(self, stock_code, side, volume, price, order_id=None):
        if self.is_live:
            order_side = STOCK_BUY if side == "BUY" else STOCK_SELL
            real_id = self.trader.order_stock_async(
                self.acc, stock_code, order_side, volume, 11, price, "Chan_Engine_Auto"
            )
            return str(real_id)
        else:
            if order_id is None:
                order_id = _next_backtest_order_id()
            self.controller.handle_trade_callback(
                stock_code, side, volume, price,
                order_id=order_id, traded_time=None
            )
            return order_id

    def query_available_shares(self, stock_code, backtest_shares=0):
        if self.is_live:
            position = self.trader.query_stock_position(self.acc, stock_code)
            return position.can_use_volume if position else 0
        else:
            return backtest_shares

    def preview_order_id(self, stock_code, timestamp):
        if self.is_live:
            return f"LIVE_PREVIEW_{stock_code}_{timestamp}"
        return _next_backtest_order_id()

class ChanEngine:
    def __init__(self, stock_code, period, tick_decimals=2, is_micro=False):
        self.stock_code = stock_code
        self.period = period
        self.is_micro = is_micro
        self.inclusion_filter = KLineInclusionFilter(tick_decimals=tick_decimals)
        self.segment_engine = SegmentEngine()
        self.segment_engine.stock_code = stock_code
        self.segment_engine.period = period
        self.zhongshu_fsm = ZhongshuFSM()
        self.zhongshu_fsm.stock_code = stock_code
        self.zhongshu_fsm.period = period
        self.divergence_engine = DynamicsDivergenceEngine(tick_decimals=tick_decimals)
        self.confirmed_bi = []
        self.current_zhongshu = None
        self.sklines = self.inclusion_filter.sklines
        self.confirmed_segments = self.segment_engine.confirmed_segments
        self.active_segment = self.segment_engine.active_segment
        self.macd_hist = []
        self._class_buy_count = 0
        self._last_processed_bottom_idx = -1
        self._last_processed_top_idx = -1
        self._last_t0_top_idx = -1
        self._last_t0_bottom_idx = -1
        self._confirmed_fractals = []
        self._last_scanned_fractal_idx = 0
        self._last_bi_end_fractal = None

    def push_raw_kline(self, timestamp, open_p, high, low, close):
        self.inclusion_filter.push_bar(timestamp, high, low, close)
        prev_len = len(self.inclusion_filter.sklines)
        latest_sk = self.inclusion_filter.sklines[-1] if self.inclusion_filter.sklines else None
        if latest_sk is not None and latest_sk.index == prev_len - 1 and len(self.macd_hist) < prev_len:
            hist_val = self.divergence_engine.update_macd(latest_sk.close)
            self.macd_hist.append(hist_val)
        elif latest_sk is not None and len(self.macd_hist) < prev_len:
            while len(self.macd_hist) < prev_len - 1:
                hist_val = self.divergence_engine.update_macd(0.0)
                self.macd_hist.append(hist_val)
            hist_val = self.divergence_engine.update_macd(latest_sk.close)
            self.macd_hist.append(hist_val)
        return self._update_geometry_topology()

    def _update_geometry_topology(self):
        sklines = list(self.sklines)
        N = len(sklines)
        if N < 3:
            return "NO_SIGNAL"
        if N >= 4:
            for i in range(max(1, self._last_scanned_fractal_idx), N - 3):
                prev = sklines[i-1]
                curr = sklines[i]
                nxt = sklines[i+1]
                f = None
                if curr.high > prev.high and curr.high > nxt.high and curr.low > prev.low and curr.low > nxt.low:
                    f = Fractal(curr.index, "TOP", curr.high, curr.low, is_confirmed=True)
                elif curr.high < prev.high and curr.high < nxt.high and curr.low < prev.low and curr.low < nxt.low:
                    f = Fractal(curr.index, "BOTTOM", curr.high, curr.low, is_confirmed=True)
                if f is not None:
                    self._confirmed_fractals.append(f)
                    if not self._last_bi_end_fractal:
                        self._last_bi_end_fractal = f
                    elif f.type != self._last_bi_end_fractal.type:
                        if StrictBiValidator.validate(self._last_bi_end_fractal, f):
                            direction = "UP" if f.type == "TOP" else "DOWN"
                            new_bi = Bi(self._last_bi_end_fractal, f, direction)
                            self.confirmed_bi.append(new_bi)
                            print(f"[GEOMETRY] Confirmed New Bi: {self.stock_code} [{self.period}] | Direction={new_bi.direction} | Start Fractal={new_bi.start.index} ({new_bi.start.type}) at {new_bi.start.high if new_bi.start.type == 'TOP' else new_bi.start.low:.2f} | End Fractal={new_bi.end.index} ({new_bi.end.type}) at {new_bi.end.high if new_bi.end.type == 'TOP' else new_bi.end.low:.2f} | Confirmed Bi count={len(self.confirmed_bi)}")
                            self._last_bi_end_fractal = f
            self._last_scanned_fractal_idx = max(1, N - 3)
        temp_fractals = []
        for i in range(max(1, N - 3), N - 1):
            prev = sklines[i-1]
            curr = sklines[i]
            nxt = sklines[i+1]
            if curr.high > prev.high and curr.high > nxt.high and curr.low > prev.low and curr.low > nxt.low:
                temp_fractals.append(Fractal(curr.index, "TOP", curr.high, curr.low, is_confirmed=False))
            elif curr.high < prev.high and curr.high < nxt.high and curr.low < prev.low and curr.low < nxt.low:
                temp_fractals.append(Fractal(curr.index, "BOTTOM", curr.high, curr.low, is_confirmed=False))
        confirmed_fractals = self._confirmed_fractals
        fractals = confirmed_fractals + temp_fractals
        bis = self.confirmed_bi
        self.segment_engine.update_segments(self.confirmed_bi)
        self.active_segment = self.segment_engine.active_segment
        confirmed_segs = self.segment_engine.confirmed_segments
        self.zhongshu_fsm.update_zhongshu(confirmed_segs)
        self.current_zhongshu = self.zhongshu_fsm.active_zhongshu
        if self.current_zhongshu and self.active_segment and self.active_segment.direction == "UP":
            latest_close = sklines[-1].close
            if latest_close >= self.current_zhongshu.GG * 1.20:
                for f in confirmed_fractals:
                    if f.type == "TOP" and f.index >= len(sklines) - 4:
                        self._class_buy_count = 0
                        return "Macro_1ClassSell"
        if self.current_zhongshu and bis:
            latest_bi = bis[-1]
            latest_close = sklines[-1].close
            zg = self.current_zhongshu.ZG
            zd = self.current_zhongshu.ZD
            if zg > zd:
                if latest_close >= zd + 0.85 * (zg - zd) and latest_bi.direction == "UP" and latest_bi.end.type == "TOP":
                    if latest_bi.end.index != self._last_t0_top_idx:
                        self._last_t0_top_idx = latest_bi.end.index
                        return "FluctuationTop"
                elif latest_close <= zd + 0.15 * (zg - zd) and latest_bi.direction == "DOWN" and latest_bi.end.type == "BOTTOM":
                    if latest_bi.end.index != self._last_t0_bottom_idx:
                        self._last_t0_bottom_idx = latest_bi.end.index
                        return "FluctuationBottom"
        if self.active_segment and len(bis) >= 2:
            latest_bi = bis[-1]
            if latest_bi.direction == "DOWN" and latest_bi.end.type == "BOTTOM":
                if latest_bi.end.index != self._last_processed_bottom_idx:
                    self._last_processed_bottom_idx = latest_bi.end.index
                    self._class_buy_count += 1
                    if self.is_micro:
                        return "1m_1ClassBuy"
                    if self.current_zhongshu and latest_bi.end.low > self.current_zhongshu.ZG:
                        return "RETAIL_3BUY"
                    elif len(bis) >= 3 and latest_bi.end.low > bis[-3].end.low:
                        return "RETAIL_2BUY"
                    else:
                        return "RETAIL_2BUY" if self._class_buy_count == 1 else "RETAIL_3BUY"
            elif latest_bi.direction == "UP" and latest_bi.end.type == "TOP":
                if latest_bi.end.index != self._last_processed_top_idx:
                    self._last_processed_top_idx = latest_bi.end.index
                    self._class_buy_count = 0
                    return "1m_1ClassSell" if self.is_micro else "3ClassSell"
        return "NO_SIGNAL"

    def get_latest_signal(self):
        sklines = list(self.sklines)
        if len(sklines) < 3:
            return "NO_SIGNAL"
        return self._update_geometry_topology()

    def get_close_history(self):
        return [sk.close for sk in self.sklines]

    def get_macd_hist(self):
        return list(self.macd_hist)

class Tee:
    def __init__(self, file_obj, stream):
        self.file = file_obj
        self.stream = stream
    def write(self, data):
        self.file.write(data)
        self.stream.write(data)
        self.file.flush()
        self.stream.flush()
    def flush(self):
        self.file.flush()
        self.stream.flush()

class StrategyKernel:
    def __init__(self, stock_pool, gateway, tick_decimals=2, strategy_config=None, macro_period="5m", micro_period="1m"):
        self.full_pool = list(stock_pool)
        self.gateway = gateway
        self._default_tick_decimals = tick_decimals
        self.active_pool = set(stock_pool)
        self.locks = {code: threading.RLock() for code in stock_pool}
        self._last_bar_date = {code: None for code in stock_pool}
        self._last_signal = {code: "NO_SIGNAL" for code in stock_pool}
        self.macro_period = macro_period
        self.micro_period = micro_period
        self._tick_decimals_cache = {}
        resolved_decimals = {code: self._resolve_tick_decimals(code) for code in stock_pool}
        self.engines_5m = {code: ChanEngine(code, macro_period, resolved_decimals[code], is_micro=False) for code in stock_pool}
        self.engines_1m = {code: ChanEngine(code, micro_period, resolved_decimals[code], is_micro=True) for code in stock_pool}
        self.scanners = {code: MultiTimeframeScanner(code, self.engines_5m[code], self.engines_1m[code]) for code in stock_pool}
        self.fsm_accounts = {code: AccountStateMachine(code, total_budget=100000.0, trader_gateway=self.gateway, strategy_config=strategy_config) for code in stock_pool}
        self.small_to_large_gateway = SmallToLargeGateway(DynamicsDivergenceEngine())
        self.trade_logs = []
        self.equity_history = []
        self.current_bar_time = 0
        self._fin_data_cache = None
        self._industry_cached = False
        self._sw_sectors = []
        self._industry_constituents = {}
        self._stock_to_industry = {}
        self._all_daily_closes_cache = None

    def _resolve_tick_decimals(self, stock_code):
        if stock_code in self._tick_decimals_cache:
            return self._tick_decimals_cache[stock_code]
        try:
            detail = xtdata.get_instrument_detail(stock_code) or {}
            decimals = detail.get("PriceDecimal", detail.get("priceDecimal", self._default_tick_decimals))
            decimals = int(decimals)
        except Exception:
            decimals = self._default_tick_decimals
        self._tick_decimals_cache[stock_code] = decimals
        return decimals

    def set_active_pool(self, new_pool):
        self.active_pool = set(new_pool) if new_pool else set(self.full_pool)

    def on_bar(self, stock_code, timeframe, timestamp, open_p, high_p, low_p, close_p, disable_trading=False):
        if stock_code not in self.locks:
            return
        if not disable_trading and stock_code not in self.active_pool:
            return
        timestamp = int(timestamp)
        bar_date_str = datetime.datetime.fromtimestamp(timestamp / 1000).strftime('%Y%m%d')
        prev_date = self._last_bar_date.get(stock_code)
        lock = self.locks[stock_code]
        with lock:
            self.current_bar_time = timestamp
            if prev_date is not None and prev_date != bar_date_str:
                self.fsm_accounts[stock_code].on_day_rollover()
            self._last_bar_date[stock_code] = bar_date_str
            engine_5m = self.engines_5m[stock_code]
            engine_1m = self.engines_1m[stock_code]
            scanner = self.scanners[stock_code]
            fsm = self.fsm_accounts[stock_code]
            latest_skline = None
            geo_signal_5m = "NO_SIGNAL"
            if timeframe == self.macro_period:
                geo_signal_5m = engine_5m.push_raw_kline(timestamp, open_p, high_p, low_p, close_p)
                latest_skline = engine_5m.sklines[-1] if engine_5m.sklines else None
            elif timeframe == self.micro_period:
                engine_1m.push_raw_kline(timestamp, open_p, high_p, low_p, close_p)
                latest_skline = engine_1m.sklines[-1] if engine_1m.sklines else None
            close_1m_history = engine_1m.get_close_history()
            close_5m_history = engine_5m.get_close_history()
            latest_1m_sk = engine_1m.sklines[-1] if engine_1m.sklines else None
            is_meltdown = self.small_to_large_gateway.check_meltdown_trigger(
                engine_5m, engine_1m, close_1m_history, latest_1m_sk
            )
            signal = "NO_SIGNAL"
            if timeframe == self.macro_period and latest_skline:
                signal = scanner.check_interval_套_convergence(
                    close_5m_history, latest_skline, geo_signal_5m=geo_signal_5m
                )
            if not disable_trading:
                can_sell = self.gateway.query_available_shares(stock_code, fsm.sellable_shares)
                order_id = self.gateway.preview_order_id(stock_code, timestamp)
                fsm.update_state(
                    signal, close_p, can_sell,
                    small_to_large_meltdown=is_meltdown,
                    order_id=order_id,
                    bar_ts=timestamp,
                )
                if timeframe == "5m":
                    last_sig = self._last_signal.get(stock_code, "NO_SIGNAL")
                    if signal != last_sig:
                        self._last_signal[stock_code] = signal
                        if signal != "NO_SIGNAL":
                            print(f"[{bar_date_str} {datetime.datetime.fromtimestamp(timestamp / 1000).strftime('%H:%M:%S')}] Signal evaluated for {stock_code} (timeframe={timeframe}): {signal} (geo_5m={geo_signal_5m}, is_meltdown={is_meltdown})")
            if timeframe == "5m":
                total_portfolio_value = 0.0
                for c in self.full_pool:
                    c_fsm = self.fsm_accounts[c]
                    if self.engines_5m[c].sklines:
                        c_close = self.engines_5m[c].sklines[-1].close
                    else:
                        c_close = close_p
                    total_portfolio_value += c_fsm.cash_on_hand + c_fsm.held_shares * c_close
                self.equity_history.append(total_portfolio_value)

    def handle_trade_callback(self, stock_code, side, volume, price, order_id=None, traded_time=None):
        if stock_code not in self.locks:
            return
        lock = self.locks[stock_code]
        with lock:
            fsm = self.fsm_accounts[stock_code]
            fsm.on_order_trade_callback(side, volume, price, order_id)
            if traded_time:
                if isinstance(traded_time, (int, float)):
                    trade_time_str = datetime.datetime.fromtimestamp(int(traded_time) / 1000).strftime('%Y-%m-%d %H:%M:%S')
                else:
                    trade_time_str = str(traded_time)
            elif self.current_bar_time > 0:
                trade_time_str = datetime.datetime.fromtimestamp(self.current_bar_time / 1000).strftime('%Y-%m-%d %H:%M:%S')
            else:
                trade_time_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            current_value = fsm.cash_on_hand + fsm.held_shares * price
            self.trade_logs.append({
                "time": trade_time_str,
                "stock_code": stock_code,
                "side": side,
                "volume": volume,
                "price": price,
                "cash": fsm.cash_on_hand,
                "shares": fsm.held_shares,
                "valuation": current_value
            })
            print(f"[CALLBACK CLEANED] {stock_code} {side} 成交回报清算成功 (order_id={order_id}). 当前仓位: {fsm.held_shares}, 可卖: {fsm.sellable_shares}, 现金: {fsm.cash_on_hand:.2f}")

def daily_trend_filter(stock_pool, date_str, debug=False):
    healthy = []
    rejected = []
    for code in stock_pool:
        try:
            closes_data = xtdata.get_local_data(
                stock_list=[code], period='1d', end_time=date_str, count=60
            )
            if code not in closes_data or closes_data[code].empty:
                rejected.append((code, "no_daily_data"))
                continue
            closes = closes_data[code]['close'].tolist()
            if len(closes) < 20:
                rejected.append((code, "insufficient_history"))
                continue
            ma5 = sum(closes[-5:]) / 5
            ma20 = sum(closes[-20:]) / 20
            if ma5 < ma20:
                rejected.append((code, f"ma5<ma20 ({ma5:.2f}<{ma20:.2f})"))
                continue
            if closes[-1] < ma20 * 0.92:
                rejected.append((code, f"close远离均线 ({closes[-1]:.2f}<{ma20*0.92:.2f})"))
                continue
            change_20d = closes[-1] / closes[-20] - 1.0
            if change_20d < -0.08:
                rejected.append((code, f"急跌 ({change_20d:.2%})"))
                continue
            healthy.append(code)
        except Exception as e:
            rejected.append((code, f"exception:{type(e).__name__}"))
    if debug and (healthy or rejected):
        print(f"  [日级趋势] 通过 {len(healthy)}/{len(stock_pool)}, 剔除 {len(rejected)}")
        for code, reason in rejected[:5]:
            print(f"    - {code}: {reason}")
    return healthy

_SECTOR_TO_INDEX_CODE = {
    "沪深300": "000300.SH", "HS300": "000300.SH", "CSI300": "000300.SH",
    "上证50": "000016.SH", "SSE50": "000016.SH",
    "中证500": "000905.SH", "CSI500": "000905.SH",
    "中证800": "000906.SH", "CSI800": "000906.SH",
    "中证1000": "000852.SH", "CSI1000": "000852.SH",
    "科创50": "000688.SH", "STAR50": "000688.SH",
    "创业板指": "399006.SZ", "深证成指": "399001.SZ",
}

def _try_resolve_stock_list(target_name):
    if not target_name:
        return None
    stripped = target_name.strip()
    upper = stripped.upper()
    if upper == "DEFAULT":
        return ['513120.SH','159530.SZ','510300.SH','159885.SZ','510500.SH']
    if upper == "CORE":
        return ['688017.SH']
    if upper == "HS300_SAMPLE":
        return ["600519.SH", "601318.SH", "600036.SH", "000858.SZ", "000333.SZ", "300750.SZ", "002594.SZ", "601398.SH", "600030.SH", "600276.SH", "000651.SZ", "601857.SH", "600028.SH", "601012.SH", "002475.SZ", "300059.SZ", "600900.SH", "601888.SH", "000568.SZ", "600887.SH", "002714.SZ", "601628.SH", "600585.SH", "000938.SZ", "600809.SH", "601166.SH", "300015.SZ", "002271.SZ", "600690.SH", "601088.SH"]
    if upper == "SH50_SAMPLE":
        return ["600519.SH", "601318.SH", "600036.SH", "601398.SH", "600028.SH", "601857.SH", "601988.SH", "601288.SH", "600030.SH", "601628.SH", "601166.SH", "601328.SH", "600000.SH", "600016.SH", "600887.SH", "600585.SH", "601668.SH", "601800.SH", "601088.SH", "600050.SH", "600104.SH", "601818.SH", "601601.SH", "600690.SH", "601390.SH", "601186.SH", "600029.SH", "601111.SH", "600019.SH", "600276.SH"]
    if upper == "创业板":
        return ['300390.SZ','300475.SZ','300620.SZ','300672.SZ','300757.SZ','300953.SZ','301217.SZ','301377.SZ','301526.SZ','301550.SZ','300255.SZ','300432.SZ','300548.SZ','300718.SZ','300972.SZ','301200.SZ','301611.SZ','300058.SZ','300115.SZ','300458.SZ','300857.SZ','301165.SZ','301498.SZ','301536.SZ','300024.SZ','300339.SZ','300346.SZ','300627.SZ','300677.SZ','300002.SZ','300017.SZ','300765.SZ','301301.SZ','300567.SZ','301358.SZ','300666.SZ','301308.SZ','300054.SZ','300395.SZ','300487.SZ','300604.SZ','300373.SZ','300919.SZ','300763.SZ','300751.SZ','300888.SZ','300037.SZ','300223.SZ','300724.SZ','300748.SZ','300012.SZ','300454.SZ','300285.SZ','300474.SZ','300699.SZ','300450.SZ','300073.SZ','300496.SZ','300383.SZ','300136.SZ','300253.SZ','300207.SZ','300144.SZ','300001.SZ','300003.SZ']
    if stripped.lower().endswith((".txt", ".csv")) and os.path.exists(stripped):
        try:
            with open(stripped, "r", encoding="utf-8") as f:
                codes = []
                for line in f:
                    s = line.strip()
                    if not s or s.startswith("#"):
                        continue
                    codes.append(s.split(",")[0].strip())
            return [c for c in codes if c]
        except Exception as e:
            print(f"  [直接列表路径] 读取文件 {stripped} 失败：{e}")
            return None
    if "," in stripped:
        codes = [s.strip() for s in stripped.split(",") if s.strip()]
        valid = [c for c in codes if len(c) >= 8 and "." in c]
        if valid:
            return valid
    return None

if __name__ == "__main__":
    TARGET_SECTOR = "CORE"
    my_target_pool = _try_resolve_stock_list(TARGET_SECTOR)
    if not my_target_pool:
        print(f"提示：所有解析路径均失败，使用自选股池")
        my_target_pool = ['300390.SZ','300475.SZ','300620.SZ','300672.SZ','300757.SZ','300953.SZ','301217.SZ','301377.SZ','301526.SZ','301550.SZ','300255.SZ','300432.SZ','300548.SZ','300718.SZ','300972.SZ','301200.SZ','301611.SZ','300058.SZ','300115.SZ','300458.SZ','300857.SZ','301165.SZ','301498.SZ','301536.SZ','300024.SZ','300339.SZ','300346.SZ','300627.SZ','300677.SZ','300002.SZ','300017.SZ','300765.SZ','301301.SZ','300567.SZ','301358.SZ','300666.SZ','301308.SZ','300054.SZ','300395.SZ','300487.SZ','300604.SZ','300373.SZ','300919.SZ','300763.SZ','300751.SZ','300888.SZ','300037.SZ','300223.SZ','300724.SZ','300748.SZ','300012.SZ','300454.SZ','300285.SZ','300474.SZ','300699.SZ','300450.SZ','300073.SZ','300496.SZ','300383.SZ','300136.SZ','300253.SZ','300207.SZ','300144.SZ','300001.SZ','300003.SZ']
    else:
        print(f"成功加载 [{TARGET_SECTOR}] 成分股，共计 {len(my_target_pool)} 只股票。")
    RUN_MODE = "BACKTEST"
    my_config = {"hard_stop_loss_pct": 0.0, "take_profit_pct": 1.25, "partial_take_profit": False, "first_tranche_ratio": 1.0, "signal_cooldown_bars": 0}
    print("缠论量化交易系统启动中...")
    print(f"RUN_MODE={RUN_MODE}, pool size={len(my_target_pool)}")
