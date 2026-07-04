#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
缠论量化交易系统 — QMT Bridge 适配版
"""

import datetime
import os
import re
import sys
import threading
import time
from collections import deque
import numpy as np

from chanlun_engine.inclusion_filter import KLineInclusionFilter, StandardKLine
from chanlun_engine.geometry_engine import (
    Fractal, Bi, StrictBiValidator, Segment, SegmentEngine,
    Zhongshu, ZhongshuFSM
)
from chanlun_engine.dynamics_engine import DynamicsDivergenceEngine, MultiTimeframeScanner, SmallToLargeGateway
from chanlun_engine.account_fsm import AccountStateMachine
from qmt_bridge_client import QMTBridgeClient


class Tee:
    def __init__(self, *files):
        self._files = files

    def write(self, data):
        for f in self._files:
            f.write(data)

    def flush(self):
        for f in self._files:
            f.flush()


class ChanEngine:
    def __init__(self, stock_code, period, tick_decimals=2, is_micro=False):
        self.stock_code = stock_code
        self.period = period
        self.tick_decimals = tick_decimals
        self.is_micro = is_micro
        self.sklines = deque(maxlen=2000)
        self.incl_filter = KLineInclusionFilter(tick_decimals, deque_len=2000)
        self.macd_engine = DynamicsDivergenceEngine(tick_decimals)
        self.segment_engine = SegmentEngine()
        self.segment_engine.stock_code = stock_code
        self.segment_engine.period = period
        self.zhongshu_fsm = ZhongshuFSM()
        self.zhongshu_fsm.stock_code = stock_code
        self.zhongshu_fsm.period = period
        self.divergence_engine = self.macd_engine
        self.confirmed_bi = []
        self._confirmed_fractals = []
        self._last_bi_end_fractal = None
        self._last_scanned_fractal_idx = 1
        self.active_segment = None
        self.current_zhongshu = None
        self._last_processed_top_idx = None
        self._last_processed_bottom_idx = None
        self._last_t0_top_idx = None
        self._last_t0_bottom_idx = None
        self._class_buy_count = 0
        self.macd_hist = []

    def push_raw_kline(self, ts, open_p, high_p, low_p, close_p):
        round_price = lambda p: round(float(p), self.tick_decimals)
        inc_result = self.incl_filter.push_bar(
            ts, round_price(open_p), round_price(high_p),
            round_price(low_p), round_price(close_p)
        )
        if inc_result is None:
            return "NO_SIGNAL"
        sk = StandardKLine(
            index=len(self.sklines),
            open=round_price(inc_result['open']),
            high=round_price(inc_result['high']),
            low=round_price(inc_result['low']),
            close=round_price(inc_result['close']),
            direction=inc_result['direction']
        )
        self.sklines.append(sk)
        self.macd_hist.append(self.macd_engine.update_macd(sk.close))
        return self._update_geometry_topology()

    def _update_geometry_topology(self):
        sklines = list(self.sklines)
        N = len(sklines)
        if N < 3:
            return "NO_SIGNAL"
        if N >= 4:
            for i in range(max(1, self._last_scanned_fractal_idx), N - 3):
                prev = sklines[i - 1]
                curr = sklines[i]
                nxt = sklines[i + 1]
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
                            print(f"[GEOMETRY] Confirmed New Bi: {self.stock_code} [{self.period}] | "
                                  f"Direction={new_bi.direction} | "
                                  f"Start Fractal={new_bi.start.index} ({new_bi.start.type}) at "
                                  f"{new_bi.start.high if new_bi.start.type == 'TOP' else new_bi.start.low:.2f} | "
                                  f"End Fractal={new_bi.end.index} ({new_bi.end.type}) at "
                                  f"{new_bi.end.high if new_bi.end.type == 'TOP' else new_bi.end.low:.2f} | "
                                  f"Confirmed Bi count={len(self.confirmed_bi)}")
                            self._last_bi_end_fractal = f
            self._last_scanned_fractal_idx = max(1, N - 3)
        temp_fractals = []
        for i in range(max(1, N - 3), N - 1):
            prev = sklines[i - 1]
            curr = sklines[i]
            nxt = sklines[i + 1]
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


class QMTBridgeTraderGateway:
    def __init__(self, client: QMTBridgeClient, controller=None, is_live=False):
        self.client = client
        self.controller = controller
        self.is_live = is_live

    def preview_order_id(self, stock_code, timestamp):
        if self.is_live or not hasattr(self, 'controller') or self.controller is None:
            ts = int(time.time() * 1000)
        else:
            ts = int(timestamp)
        return f"BT{ts:014d}"

    def submit_order(self, stock_code, price, volume, side, order_id, strategy_name="CHAN_FSM"):
        if self.is_live:
            try:
                direction = "BUY" if side == "BUY" else "SELL"
                self.client.order_stock(
                    symbol=stock_code, price=price, volume=volume,
                    direction=direction, strategy_name=strategy_name, order_remark=order_id
                )
                print(f"[LIVE ORDER] {side} {volume} shares of {stock_code} @ {price} (order_id={order_id})")
            except Exception as e:
                print(f"[LIVE ORDER ERROR] Failed to submit {side} order for {stock_code}: {e}")
        else:
            traded_time = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
            self.controller.handle_trade_callback(
                stock_code=stock_code, side=side, volume=volume, price=price,
                order_id=order_id, traded_time=traded_time
            )

    def query_available_shares(self, stock_code, sellable_shares):
        if self.is_live:
            try:
                pos = self.client.query_stock_position(symbol=stock_code)
                if pos and 'volume' in pos:
                    return pos['volume']
                return sellable_shares
            except Exception as e:
                print(f"[LIVE POSITION QUERY ERROR] {stock_code}: {e}")
                return sellable_shares
        return sellable_shares

    def stop(self):
        pass


class StrategyKernel:
    def __init__(self, stock_pool, gateway, client, tick_decimals=2,
                 strategy_config=None, macro_period="5m", micro_period="1m"):
        self.full_pool = list(stock_pool)
        self.gateway = gateway
        self.client = client
        self._default_tick_decimals = tick_decimals
        self.active_pool = set(stock_pool)
        self.locks = {code: threading.RLock() for code in stock_pool}
        self._last_bar_date = {code: None for code in stock_pool}
        self._last_signal = {code: "NO_SIGNAL" for code in stock_pool}
        self.macro_period = macro_period
        self.micro_period = micro_period
        self._tick_decimals_cache = {}
        resolved_decimals = {code: self._resolve_tick_decimals(code) for code in stock_pool}
        self.engines_5m = {code: ChanEngine(code, macro_period, resolved_decimals[code], is_micro=False)
                           for code in stock_pool}
        self.engines_1m = {code: ChanEngine(code, micro_period, resolved_decimals[code], is_micro=True)
                           for code in stock_pool}
        self.scanners = {code: MultiTimeframeScanner(code, self.engines_5m[code], self.engines_1m[code])
                         for code in stock_pool}
        self.fsm_accounts = {code: AccountStateMachine(code, total_budget=100000.0,
                             trader_gateway=self.gateway, strategy_config=strategy_config)
                             for code in stock_pool}
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
            detail = self.client.get_instrument_detail(symbol=stock_code)
            if detail and 'PriceTick' in detail:
                tick = detail['PriceTick']
                dec = max(0, len(str(tick).split('.')[-1].rstrip('0') or '0'))
                self._tick_decimals_cache[stock_code] = dec
                return dec
        except Exception:
            pass
        return self._default_tick_decimals

    def set_active_pool(self, pool_list):
        self.active_pool = set(pool_list)

    def on_bar(self, stock_code, timeframe, timestamp, open_p, high_p, low_p, close_p,
               disable_trading=False):
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
            is_meltdown = self.small_to_large_gateway.check_meltdown_trigger(
                engine_5m, engine_1m, close_1m_history, latest_skline
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
                    order_id=order_id, bar_ts=timestamp,
                )
                if timeframe == self.micro_period:
                    last_sig = self._last_signal.get(stock_code, "NO_SIGNAL")
                    if signal != last_sig:
                        self._last_signal[stock_code] = signal
                        if signal != "NO_SIGNAL":
                            print(f"[{bar_date_str} "
                                  f"{datetime.datetime.fromtimestamp(timestamp / 1000).strftime('%H:%M:%S')}] "
                                  f"Signal evaluated for {stock_code} (timeframe={timeframe}): {signal} "
                                  f"(geo_5m={geo_signal_5m}, is_meltdown={is_meltdown})")
            if timeframe == self.micro_period:
                total_portfolio_value = 0.0
                for c in self.full_pool:
                    c_fsm = self.fsm_accounts[c]
                    if self.engines_5m[c].sklines:
                        c_close = self.engines_5m[c].sklines[-1].close
                    else:
                        c_close = close_p
                    total_portfolio_value += c_fsm.cash_on_hand + c_fsm.held_shares * c_close
                self.equity_history.append(total_portfolio_value)

    def handle_trade_callback(self, stock_code, side, volume, price,
                              order_id=None, traded_time=None):
        if stock_code not in self.fsm_accounts:
            return
        lock = self.locks[stock_code]
        with lock:
            fsm = self.fsm_accounts[stock_code]
            fsm.apply_fill(order_id, side, volume, price, traded_time)
            trade_record = {
                'stock_code': stock_code, 'side': side, 'volume': volume,
                'price': price, 'order_id': order_id or "N/A",
                'traded_time': traded_time or datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                'held_shares': fsm.held_shares, 'sellable_shares': fsm.sellable_shares,
                'total_cash': fsm.cash_on_hand
            }
            self.trade_logs.append(trade_record)


class BacktestDriver:
    def __init__(self, controller, kernel, client, stock_pool, start_date, end_date,
                 macro_period="5m", micro_period="1m"):
        self.controller = controller
        self.kernel = kernel
        self.client = client
        self.stock_pool = list(stock_pool)
        self.start_date = start_date
        self.end_date = end_date
        self.preheat_start_date = None
        self.backtest_end_date = end_date
        self.macro_period = macro_period
        self.micro_period = micro_period

    def run(self):
        now_str = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        log_dir = "backtest_reports"
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
        log_path = os.path.join(log_dir, f"backtest_run_{now_str}.log")
        abs_log_path = os.path.abspath(log_path)
        original_stdout = sys.stdout
        original_stderr = sys.stderr
        log_file = open(log_path, "w", encoding="utf-8")
        sys.stdout = Tee(log_file, original_stdout)
        sys.stderr = Tee(log_file, original_stderr)
        try:
            print(f"[BACKTEST_CONFIG] start_date={self.start_date} | "
                  f"end_date={self.end_date} | "
                  f"macro_period={self.macro_period} | "
                  f"micro_period={self.micro_period}")
            print(f"[BACKTEST_POOL] 原始股票池 (共{len(self.stock_pool)}只): "
                  f"{', '.join(self.stock_pool)}")
            cfg = getattr(self.controller, 'strategy_config', None) or {}
            cfg_items = []
            for k, v in cfg.items():
                cfg_items.append(f"{k}={v}")
            print(f"[BACKTEST_STRATEGY] {' | '.join(cfg_items)}")
            self._load_history()
            all_bars_5m, all_bars_1m, all_timestamps = self._build_timeline()
            if not all_bars_5m:
                print(f"警告: 股票池内无任何有效的 {self.macro_period} 历史数据，回测终止。")
                return
            self._playback(all_bars_5m, all_bars_1m, all_timestamps)
            self._fallback_report()
            print(f"[LOG SAVED] 回测全量控制台日志已成功保存至: "
                  f"[backtest_run_{now_str}.log](file:///{abs_log_path.replace(os.sep, '/')})")
        finally:
            sys.stdout = original_stdout
            sys.stderr = original_stderr
            log_file.close()

    def _fallback_report(self):
        kernel = self.controller.kernel
        trades = kernel.trade_logs
        cost_basis_total = sum(t['volume'] * t['price'] for t in trades if t['side'] == 'BUY')
        recovery_total = sum(t['volume'] * t['price'] for t in trades if t['side'] == 'SELL')
        realized_pnl = recovery_total - cost_basis_total
        unrealized = 0.0
        for code, fsm in kernel.fsm_accounts.items():
            if fsm.held_shares > 0:
                if kernel.engines_5m[code].sklines:
                    close = kernel.engines_5m[code].sklines[-1].close
                else:
                    close = 0.0
                unrealized += fsm.held_shares * close
        total_pnl = realized_pnl + unrealized
        total_budget = sum(fsm.total_budget for fsm in kernel.fsm_accounts.values())
        return_pct = total_pnl / max(total_budget, 1.0) * 100
        max_drawdown = 0.0
        peak = -float('inf')
        for eq in kernel.equity_history:
            if eq > peak:
                peak = eq
            if peak > 0:
                dd = (peak - eq) / peak
                if dd > max_drawdown:
                    max_drawdown = dd
        print("\n" + "=" * 50)
        print("         缠论量化交易系统回测分析报告（简化版）")
        print("=" * 50)
        print(f"初始总资产: {total_budget:.2f} 元")
        print(f"已部署本金: {cost_basis_total:.2f} 元（= BUY 总量 × 价）")
        print(f"已回收金额: {recovery_total:.2f} 元（= SELL 总量 × 价）")
        print(f"剩余持仓市值: {unrealized:.2f} 元")
        print(f"实现盈亏: {realized_pnl:.2f} 元")
        print(f"总盈亏: {total_pnl:.2f} 元 ({return_pct:+.2f}%)")
        print(f"最大回撤比: {max_drawdown:.2%}")
        print(f"总成交次数: {len(trades)} 次")
        print("=" * 50)

    def _load_history(self):
        start_dt = datetime.datetime.strptime(self.start_date, "%Y%m%d")
        preheat_start_dt = start_dt - datetime.timedelta(days=45)
        self.preheat_start_date = preheat_start_dt.strftime("%Y%m%d")
        print(f"正在加载历史数据，包含预热期 [{self.preheat_start_date}] -> [{self.end_date}] ...")

    def _fetch_data_with_fallback(self, code, period, threshold, preheat_start, end_date):
        raw = self.client.get_local_data(
            fields=['open', 'high', 'low', 'close'],
            symbols=[code], period=period,
            start_time=preheat_start, end_time=end_date
        )
        if self._check_data_sufficient(raw, code, threshold):
            return raw
        print(f"警告: 本地数据库中 {code} 的 {period} K线数据不足"
              f"(当前 {self._count_data(raw, code)} 条, 需 {threshold})。进行自动下载补全...")
        _download_and_wait(self.client, [code], period, preheat_start, end_date)
        raw = self.client.get_local_data(
            fields=['open', 'high', 'low', 'close'],
            symbols=[code], period=period,
            start_time=preheat_start, end_time=end_date
        )
        if self._check_data_sufficient(raw, code, threshold):
            return raw
        print(f"[{code}] {period} 本地缓存仍不足 ({self._count_data(raw, code)} 条)，切换为在线拉取 get_market_data ...")
        online_data = self.client.get_market_data(
            fields=['open', 'high', 'low', 'close'],
            symbols=[code], period=period,
            start_time=preheat_start, end_time=end_date
        )
        if isinstance(online_data, dict) and code in online_data:
            import pandas as pd
            df = online_data[code]
            if hasattr(df, 'index') and len(df) >= threshold:
                print(f"[{code}] {period} 在线数据拉取成功，共 {len(df)} 条记录。")
                return {code: df}
        print(f"警告: {code} 的 {period} 所有数据拉取方式均失败，跳过该品种。")
        return None

    def _check_data_sufficient(self, raw, code, threshold):
        return self._count_data(raw, code) >= threshold

    def _count_data(self, raw, code):
        if isinstance(raw, dict) and code in raw:
            import pandas as pd
            df = raw[code]
            if hasattr(df, 'index'):
                return len(df)
        return 0

    def _build_timeline(self):
        preheat_dt = datetime.datetime.strptime(self.preheat_start_date, "%Y%m%d")
        end_dt = datetime.datetime.strptime(self.end_date, "%Y%m%d")
        days_needed = (end_dt - preheat_dt).days + 1
        threshold_macro = max(int(days_needed * 6.5), 500)
        threshold_micro = threshold_macro * 5
        all_bars_5m = {}
        all_bars_1m = {}
        for code in self.stock_pool:
            raw_5m = self._fetch_data_with_fallback(
                code, self.macro_period, threshold_macro,
                self.preheat_start_date, self.end_date
            )
            raw_1m = self._fetch_data_with_fallback(
                code, self.micro_period, threshold_micro,
                self.preheat_start_date, self.end_date
            )
            if raw_5m and code in raw_5m:
                all_bars_5m[code] = raw_5m[code]
            if raw_1m and code in raw_1m:
                all_bars_1m[code] = raw_1m[code]
        all_timestamps = set()
        for code in self.stock_pool:
            for bars_dict in [all_bars_5m, all_bars_1m]:
                if code in bars_dict:
                    df = bars_dict[code]
                    if hasattr(df, 'index'):
                        all_timestamps.update(df.index)
        all_timestamps = sorted(all_timestamps)
        ts_5m = sum(1 for code in self.stock_pool if code in all_bars_5m and hasattr(all_bars_5m[code], 'index'))
        ts_1m = sum(1 for code in self.stock_pool if code in all_bars_1m and hasattr(all_bars_1m[code], 'index'))
        print(f"成功加载行情。时间轴总刻度: {len(all_timestamps)} 个 Bar"
              f"（{self.macro_period}={ts_5m}, {self.micro_period}={ts_1m}）。开始时间线对齐回放...")
        return all_bars_5m, all_bars_1m, all_timestamps

    def _playback(self, all_bars_5m, all_bars_1m, all_timestamps):
        current_day = None
        for ts in all_timestamps:
            bar_date_str = datetime.datetime.fromtimestamp(ts / 1000).strftime('%Y%m%d')
            is_preheating = bar_date_str < self.start_date
            if bar_date_str != current_day:
                current_day = bar_date_str
                if not is_preheating:
                    active_pool = self.controller.recompute_active_pool(bar_date_str)
                    print(f"[{bar_date_str}] 今日活跃交易池 ({len(active_pool)}): {active_pool}")
            for code in self.kernel.active_pool:
                is_5m_ts = code in all_bars_5m and ts in all_bars_5m[code].index
                is_1m_ts = code in all_bars_1m and ts in all_bars_1m[code].index
                if is_5m_ts:
                    row = all_bars_5m[code].loc[ts]
                    self.kernel.on_bar(code, self.macro_period, ts,
                                       float(row['open']), float(row['high']),
                                       float(row['low']), float(row['close']),
                                       disable_trading=is_preheating)
                if is_1m_ts:
                    row = all_bars_1m[code].loc[ts]
                    self.kernel.on_bar(code, self.micro_period, ts,
                                       float(row['open']), float(row['high']),
                                       float(row['low']), float(row['close']),
                                       disable_trading=is_preheating)
        print("====== 极速回测回放完成 ======")


class LiveDriver:
    def __init__(self, controller, kernel, client, stock_pool,
                 macro_period="5m", micro_period="1m"):
        self.controller = controller
        self.kernel = kernel
        self.client = client
        self.stock_pool = list(stock_pool)
        self.macro_period = macro_period
        self.micro_period = micro_period
        self._stop_event = threading.Event()
        self._last_bar_ts = {}

    def run(self):
        print("Live driver started.")
        try:
            for code in self.stock_pool:
                self.client.subscribe_quote(code, period=self.macro_period)
                self.client.subscribe_quote(code, period=self.micro_period)
            while not self._stop_event.is_set():
                events = self.client.get_sse_events(timeout=1.0)
                for evt in events:
                    self._handle_sse_event(evt)
        except KeyboardInterrupt:
            print("Live driver interrupted.")
        finally:
            print("Live driver stopped.")

    def _handle_sse_event(self, event):
        etype = event.get('type', '')
        if etype == 'bar':
            code = event.get('symbol', '')
            period = event.get('period', '')
            ts = event.get('timestamp', 0)
            bar_key = f"{code}_{period}"
            last = self._last_bar_ts.get(bar_key)
            if last is not None and ts <= last:
                return
            self._last_bar_ts[bar_key] = ts
            self.kernel.on_bar(
                code, period, ts,
                event.get('open', event.get('close', 0)),
                event.get('high', event.get('close', 0)),
                event.get('low', event.get('close', 0)),
                event.get('close', 0)
            )
        elif etype == 'trade':
            self.kernel.handle_trade_callback(
                event.get('symbol', ''), event.get('side', 'BUY'),
                event.get('volume', 0), event.get('price', 0.0),
                order_id=event.get('order_id', '')
            )

    def stop(self):
        self._stop_event.set()


class SystemController:
    def __init__(self, stock_pool, execution_mode: str, client: QMTBridgeClient,
                 tick_decimals=2, strategy_config=None,
                 macro_period="5m", micro_period="1m"):
        self.stock_pool = list(stock_pool)
        self.mode = execution_mode
        self.client = client
        self._default_tick_decimals = tick_decimals
        self.strategy_config = strategy_config
        self.macro_period = macro_period
        self.micro_period = micro_period
        is_live_flag = True if execution_mode == "LIVE" else False
        self.gateway = QMTBridgeTraderGateway(
            client=client, controller=None, is_live=is_live_flag,
        )
        self.kernel = StrategyKernel(
            stock_pool=self.stock_pool, gateway=self.gateway, client=client,
            tick_decimals=tick_decimals, strategy_config=strategy_config,
            macro_period=macro_period, micro_period=micro_period
        )
        self.gateway.controller = self
        self._backtest_driver = None
        self._live_driver = None
        self._scheduler_thread = None
        self._scheduler_stop = threading.Event()
        self._fin_data_cache = None
        self._industry_cached = False
        self._sw_sectors = []
        self._industry_constituents = {}
        self._stock_to_industry = {}
        self._all_daily_closes_cache = None

    def _ensure_financial_data_loaded(self):
        if self._fin_data_cache is not None:
            return
        try:
            self._fin_data_cache = self.client.get_financial_data(self.stock_pool, report_type='annual')
            if not self._fin_data_cache or not isinstance(self._fin_data_cache, dict):
                self._fin_data_cache = {}
        except Exception as e:
            print(f"警告: 无法获取基本面数据 ({e})，跳过基本面过滤。")
            self._fin_data_cache = {}

    def _ensure_industry_info_cached(self):
        if self._industry_cached:
            return
        try:
            sectors = self.client.get_sector_list()
            sw_sectors = [s for s in sectors if '申万' in s.get('name', '') or 'SW' in s.get('name', '').upper()]
            if not sw_sectors:
                sw_sectors = sectors
            self._sw_sectors = sw_sectors
            for sector in sw_sectors:
                name = sector.get('name', sector.get('sector', str(sector)))
                try:
                    stocks = self.client.get_sector_constituents(sector_name=name)
                    if stocks:
                        self._industry_constituents[name] = [s.split('.')[0] for s in stocks]
                except Exception:
                    pass
            for stock in self.stock_pool:
                found = False
                for industry, codes in self._industry_constituents.items():
                    if stock.split('.')[0] in codes:
                        self._stock_to_industry[stock] = industry
                        found = True
                        break
                if not found:
                    self._stock_to_industry[stock] = '综合'
            self._industry_cached = True
        except Exception as e:
            print(f"警告: 未能获取行业分类 ({e})，所有股票归入'综合'。")
            for stock in self.stock_pool:
                self._stock_to_industry[stock] = '综合'
            self._industry_cached = True

    def _load_all_daily_closes(self, current_date):
        if self._all_daily_closes_cache is None:
            self._all_daily_closes_cache = {}
        for code in self.stock_pool:
            if code not in self._all_daily_closes_cache:
                raw = self.client.get_local_data(
                    fields=['close'], symbols=[code], period='1d',
                    end_time=str(current_date), count=120
                )
                import pandas as pd
                if raw and code in raw:
                    self._all_daily_closes_cache[code] = raw[code]
        return self._all_daily_closes_cache

    def _daily_trend_filter(self, pool, current_date=None):
        closes_dict = self._load_all_daily_closes(current_date)
        survivors = []
        if current_date is None:
            current_date = str(datetime.date.today()).replace('-', '')
        for code in pool:
            if code not in closes_dict or closes_dict[code].empty:
                continue
            df = closes_dict[code]
            if 'close' not in df.columns or len(df) < 25:
                continue
            sliced = df[df.index <= str(current_date)].tail(30)
            if len(sliced) < 25:
                continue
            closes = sliced['close'].astype(float)
            ma5 = closes.rolling(5).mean().iloc[-1]
            ma20 = closes.rolling(20).mean().iloc[-1]
            if ma5 > ma20 and closes.iloc[-1] >= 0.92 * ma20:
                change_20d = (closes.iloc[-1] / closes.iloc[-20] - 1)
                if change_20d > -0.08:
                    survivors.append(code)
        return survivors

    def _fundamental_filter(self, pool):
        self._ensure_financial_data_loaded()
        self._ensure_industry_info_cached()
        surviving_pool = []
        for code in pool:
            detail = self.client.get_instrument_detail(symbol=code)
            name = (detail.get('InstrumentName', '') or detail.get('name', ''))
            if '*' in name or 'ST' in name.upper():
                continue
            fin = self._fin_data_cache.get(code, {})
            if isinstance(fin, dict) and 'debt_ratio' in fin:
                debt = fin.get('debt_ratio', 100)
                try:
                    debt = float(debt)
                except (ValueError, TypeError):
                    debt = 0
                if debt > 85:
                    continue
            if isinstance(fin, dict):
                np1 = fin.get('net_profit', 0)
                try:
                    np1 = float(np1) if np1 else 0
                except (ValueError, TypeError):
                    np1 = 0
                if np1 < 0:
                    continue
            surviving_pool.append(code)
        return surviving_pool

    def recompute_active_pool(self, bar_date_str):
        try:
            current_pool = list(self.kernel.full_pool)
            if not current_pool:
                return []
            l1 = self._fundamental_filter(current_pool)
            if not l1:
                l1 = current_pool
            rs_pool = self._fallback_rs_filter(l1, current_date=str(bar_date_str))
            l2 = self._daily_trend_filter(rs_pool, current_date=str(bar_date_str))
            final_pool = l2 if l2 else rs_pool
            if not final_pool:
                final_pool = rs_pool
            prev = self.kernel.active_pool
            added = sum(1 for s in final_pool if s not in prev)
            removed = sum(1 for s in prev if s not in final_pool)
            print(f"  [recompute_active_pool] 新增 {added}, 退出 {removed}, 当前活跃 {len(final_pool)}")
            self.kernel.set_active_pool(final_pool)
            return final_pool
        except Exception as e:
            print(f"  [recompute_active_pool] 异常 {type(e).__name__}: {e}，回退为全量池")
            self.kernel.set_active_pool(self.stock_pool)
            return list(self.stock_pool)

    def handle_trade_callback(self, stock_code, side, volume, price,
                              order_id=None, traded_time=None):
        self.kernel.handle_trade_callback(stock_code, side, volume, price,
                                          order_id=order_id, traded_time=traded_time)

    def run_backtest(self, start_date, end_date):
        if self._backtest_driver is None:
            self._backtest_driver = BacktestDriver(
                controller=self, kernel=self.kernel, client=self.client,
                stock_pool=self.stock_pool, start_date=start_date, end_date=end_date,
                macro_period=self.macro_period, micro_period=self.micro_period
            )
        self._backtest_driver.run()

    def run_live(self):
        if self._live_driver is None:
            self._live_driver = LiveDriver(
                controller=self, kernel=self.kernel, client=self.client,
                stock_pool=self.stock_pool,
                macro_period=self.macro_period, micro_period=self.micro_period
            )
        self._live_driver.run()

    def stop_live(self):
        if self._live_driver is not None:
            self._live_driver.stop()
        if self.gateway is not None:
            self.gateway.stop()

    def _fallback_rs_filter(self, pool, current_date=None):
        rs_values = {}
        if pool:
            if (hasattr(self, '_all_daily_closes_cache') and self._all_daily_closes_cache is not None):
                for code in pool:
                    if (code in self._all_daily_closes_cache and not self._all_daily_closes_cache[code].empty):
                        df = self._all_daily_closes_cache[code]
                        sliced = df[df.index <= str(current_date)].tail(30)
                        if len(sliced) >= 20 and sliced['close'].iloc[-20] > 0:
                            rs_values[code] = (sliced['close'].iloc[-1] / sliced['close'].iloc[-20])
            else:
                for code in pool:
                    raw = self.client.get_local_data(
                        fields=['close'], symbols=[code], period='1d',
                        end_time=current_date, count=30
                    )
                    close_info = raw.get('close', {}) if isinstance(raw, dict) else {}
                    if close_info and code in close_info.get('index', []):
                        idx = close_info['index'].index(code)
                        closes = close_info['data'][idx]
                        if len(closes) >= 20 and closes[-20] > 0:
                            rs_values[code] = closes[-1] / closes[-20]
        sorted_pool = sorted(rs_values.items(), key=lambda x: x[1], reverse=True)
        return [x[0] for x in sorted_pool[:60]]


def _download_and_wait(client, codes, period, start, end):
    try:
        task_id = client.download_history_data2(codes, period, start, end)
        if not task_id:
            return
        for _ in range(120):
            time.sleep(1)
            try:
                status = client.query_download_status(task_id)
                if status.get('finished', False):
                    break
            except Exception:
                pass
    except Exception:
        pass


_SECTOR_TO_INDEX_CODE = {
    "沪深300": "000300.SH", "HS300": "000300.SH", "CSI300": "000300.SH",
    "上证50": "000016.SH", "SSE50": "000016.SH",
    "中证500": "000905.SH", "CSI500": "000905.SH",
    "中证1000": "000852.SH", "CSI1000": "000852.SH",
    "科创50": "000688.SH", "STAR50": "000688.SH",
    "创业板指": "399006.SZ", "深证成指": "399001.SZ",
}


def _resolve_target_sector(client, target_name):
    if not target_name or target_name in ('DEFAULT',):
        return None
    if target_name in _SECTOR_TO_INDEX_CODE:
        target_name = _SECTOR_TO_INDEX_CODE[target_name]
    if re.match(r'^\d{6}\.(SZ|SH)$', target_name):
        return _resolve_index_constituents(client, target_name)
    if target_name.endswith('.txt') or target_name.endswith('.csv'):
        return _load_pool_from_file(target_name)
    if ',' in target_name:
        return [s.strip() for s in target_name.split(',') if s.strip()]
    try:
        stocks = client.get_sector_constituents(sector_name=target_name)
        if stocks:
            return stocks
    except Exception:
        pass
    for key, code in _SECTOR_TO_INDEX_CODE.items():
        if target_name in key:
            return _resolve_index_constituents(client, code)
    if re.match(r'^[A-Za-z0-9_]+$', target_name):
        return [target_name]
    return None


def _resolve_index_constituents(client, index_code):
    try:
        stocks = client.get_sector_constituents(sector_name=index_code)
        if stocks:
            return stocks
    except Exception:
        pass
    return [index_code]


def _load_pool_from_file(path):
    if not os.path.exists(path):
        return None
    with open(path, 'r') as f:
        return [line.strip() for line in f if line.strip() and not line.startswith('#')]


if __name__ == "__main__":
    BRIDGE_HOST = os.environ.get("QMT_BRIDGE_HOST", "192.168.122.14")
    BRIDGE_PORT = int(os.environ.get("QMT_BRIDGE_PORT", "8080"))
    print(f"正在连接 QMT 桥接服务端 {BRIDGE_HOST}:{BRIDGE_PORT} ...")
    client = QMTBridgeClient(host=BRIDGE_HOST, port=BRIDGE_PORT)
    TARGET_SECTOR = "CORE"
    my_target_pool = _resolve_target_sector(client, TARGET_SECTOR)
    if not my_target_pool:
        print(f"提示：所有解析路径均失败，使用自选股池")
        my_target_pool = [
            '300390.SZ', '300475.SZ', '300620.SZ', '300672.SZ', '300757.SZ',
            '300953.SZ', '301217.SZ', '301377.SZ', '301526.SZ', '301550.SZ',
            '300255.SZ', '300432.SZ', '300548.SZ', '300718.SZ', '300972.SZ',
            '301200.SZ', '301611.SZ', '300058.SZ', '300115.SZ', '300458.SZ',
            '300857.SZ', '301165.SZ', '301498.SZ', '301536.SZ', '300024.SZ',
            '300339.SZ', '300346.SZ', '300627.SZ', '300677.SZ', '300002.SZ',
            '300017.SZ', '300765.SZ', '301301.SZ', '300567.SZ', '301358.SZ',
            '300666.SZ', '301308.SZ', '300054.SZ', '300395.SZ', '300487.SZ',
            '300604.SZ', '300373.SZ', '300919.SZ', '300763.SZ', '300751.SZ',
            '300888.SZ', '300037.SZ', '300223.SZ', '300724.SZ', '300748.SZ',
            '300012.SZ', '300454.SZ', '300285.SZ', '300474.SZ', '300699.SZ',
            '300450.SZ', '300073.SZ', '300496.SZ', '300383.SZ', '300136.SZ',
            '300253.SZ', '300207.SZ', '300144.SZ', '300001.SZ', '300003.SZ'
        ]
    else:
        print(f"成功加载 [{TARGET_SECTOR}] 成分股，共计 {len(my_target_pool)} 只股票。")
    RUN_MODE = "BACKTEST"
    my_config = {
        "hard_stop_loss_pct": 0.0,
        "take_profit_pct": 1.25,
        "partial_take_profit": False,
        "first_tranche_ratio": 1.0,
        "signal_cooldown_bars": 0
    }
    controller = SystemController(
        stock_pool=my_target_pool, execution_mode=RUN_MODE,
        client=client, strategy_config=my_config,
        macro_period="1h", micro_period="5m"
    )
    if RUN_MODE == "BACKTEST":
        controller.run_backtest(start_date="20260101", end_date="20260628")
    else:
        controller.run_live()