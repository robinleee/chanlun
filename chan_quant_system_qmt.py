#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
缠论量化交易系统 — QMT Bridge 适配版
与原版 chan_quant_system_windows.py 的缠论策略逻辑 **完全一致**。
差异仅在于底层接口层：通过 QMTBridgeClient（HTTP + SSE）与 Windows VM 通信。
"""
import datetime
import os
import re
import sys
import threading
import time
from collections import deque
import numpy as np

from qmt_bridge_client import QMTBridgeClient, QMTObject

STOCK_BUY = 23
STOCK_SELL = 24

from chanlun_engine.inclusion_filter import KLineInclusionFilter, StandardKLine
from chanlun_engine.geometry_engine import Fractal, Bi, StrictBiValidator, Segment, SegmentEngine, Zhongshu, ZhongshuFSM
from chanlun_engine.dynamics_engine import DynamicsDivergenceEngine, MultiTimeframeScanner, SmallToLargeGateway
from chanlun_engine.account_fsm import AccountStateMachine, AccountState

_NEXT_BACKTEST_ORDER_ID = [0]

def _next_backtest_order_id():
    _NEXT_BACKTEST_ORDER_ID[0] += 1
    return f"BT{_NEXT_BACKTEST_ORDER_ID[0]:08d}"

def _ts_str_to_ms(ts_str):
    if not ts_str:
        return 0
    ts_str = str(ts_str).strip()
    try:
        if len(ts_str) == 8:
            dt = datetime.datetime.strptime(ts_str, '%Y%m%d')
        elif len(ts_str) == 14:
            dt = datetime.datetime.strptime(ts_str, '%Y%m%d%H%M%S')
        else:
            return int(ts_str)
        return int(dt.timestamp() * 1000)
    except (ValueError, OSError):
        return int(ts_str) if ts_str.isdigit() else 0

def _build_dataframe_from_bridge(bridge_data, stock_code):
    import pandas as pd
    records = {}
    has_data = False
    for field, field_info in bridge_data.items():
        if isinstance(field_info, dict) and "data" in field_info:
            stocks = field_info.get("index", [])
            if stock_code in stocks:
                idx = stocks.index(stock_code)
                values = field_info["data"][idx]
                if field == 'time':
                    records[field] = [_ts_str_to_ms(v) for v in values]
                else:
                    records[field] = list(values)
                has_data = True
    if not has_data:
        return None
    df = pd.DataFrame(records)
    if 'time' in df.columns:
        df.index = df['time'].astype(int)
    df = df.sort_index()
    return df

def _build_financial_df(table_dict):
    import pandas as pd
    if not table_dict or not table_dict.get("data"):
        return pd.DataFrame()
    idx = table_dict.get("index", [])
    cols = table_dict.get("columns", [])
    data = table_dict.get("data", [])
    df = pd.DataFrame(data, index=idx, columns=cols)
    return df

def _download_and_wait(client, symbols, period, start_time="", end_time="", timeout=120):
    task_id = client.download_history_data2(
        symbols=symbols, period=period, start_time=start_time, end_time=end_time
    )
    if not task_id:
        print(f"  [下载] {period} 任务启动失败")
        return False
    print(f"  [下载] {period} 任务已启动 (task_id={task_id})，等待完成...")
    elapsed = 0
    while elapsed < timeout:
        status = client.query_download_status(task_id)
        if status:
            progress = status.get("progress", 0)
            done = status.get("done", False)
            if done:
                if status.get("status") == "success":
                    print(f"  [下载] {period} 完成")
                    return True
                else:
                    print(f"  [下载] {period} 失败: {status.get('error')}")
                    return False
            if elapsed % 10 == 0:
                print(f"  [下载] {period} 进度: {progress}%")
        time.sleep(1)
        elapsed += 1
    print(f"  [下载] {period} 超时 ({timeout}s)")
    return False

class QMTBridgeTraderGateway:
    def __init__(self, client, controller, is_live=False):
        self.client = client
        self.is_live = is_live
        self.controller = controller
        if self.is_live:
            self.client.on_stock_trade = self._on_stock_trade
            self.client.on_order_stock_async_response = self._on_order_response
            self.client.start()
            print("实盘交易网关已初始化（SSE 事件流监听中）")
        else:
            print("当前处于：回测驱动模式（订单执行已拦截为虚拟流）")

    def _on_stock_trade(self, trade):
        side = "BUY" if trade.offset_flag == STOCK_BUY else "SELL"
        self.controller.handle_trade_callback(
            trade.stock_code, side, trade.traded_volume, trade.traded_price,
            order_id=str(getattr(trade, "order_id", "")),
            traded_time=getattr(trade, "traded_time", None)
        )

    def _on_order_response(self, response):
        print(f"QMT 异步下单确认, 订单 ID: {response.order_id}, 状态: {response.order_status}")

    def submit_order(self, stock_code, side, volume, price, order_id=None):
        if self.is_live:
            order_side = STOCK_BUY if side == "BUY" else STOCK_SELL
            real_id = self.client.order_stock(
                symbol=stock_code, side=order_side, quantity=volume,
                price=price, price_type=11, strategy_name="Chan_Engine_Auto"
            )
            if real_id and real_id > 0:
                return str(real_id)
            else:
                print(f"  [下单失败] {stock_code} {side} {volume}@{price}")
                return str(-1)
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
            position = self.client.query_stock_position(stock_code)
            if position is not None:
                return getattr(position, "can_use_volume", 0)
            return 0
        else:
            return backtest_shares

    def preview_order_id(self, stock_code, timestamp):
        if self.is_live:
            return f"LIVE_PREVIEW_{stock_code}_{timestamp}"
        return _next_backtest_order_id()

    def stop(self):
        if self.is_live:
            self.client.stop()

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

if __name__ == "__main__":
    BRIDGE_HOST = os.environ.get("QMT_BRIDGE_HOST", "192.168.122.14")
    BRIDGE_PORT = int(os.environ.get("QMT_BRIDGE_PORT", "8080"))
    print(f"正在连接 QMT 桥接服务端 {BRIDGE_HOST}:{BRIDGE_PORT} ...")
    client = QMTBridgeClient(host=BRIDGE_HOST, port=BRIDGE_PORT)
    TARGET_SECTOR = "CORE"
    RUN_MODE = "BACKTEST"
    my_config = {"hard_stop_loss_pct": 0.0, "take_profit_pct": 1.25, "partial_take_profit": False, "first_tranche_ratio": 1.0, "signal_cooldown_bars": 0}
    print(f"缠论量化交易系统 (Bridge版) 配置加载完成。RUN_MODE={RUN_MODE}")
