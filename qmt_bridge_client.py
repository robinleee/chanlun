# -*- coding: utf-8 -*-
"""
QMT HTTP & SSE Bridge Client SDK
Runs on the Linux host (or any client machine).
Communicates with the QMT Bridge Server in the Windows VM.
Converts returned dicts into python objects with attribute access for compatibility.
Zero external dependencies (uses standard python libraries).
"""
import json
import logging
import threading
import time
import urllib.request
import urllib.parse
import urllib.error

logger = logging.getLogger("QMTBridgeClient")

class QMTObject(object):
    """
    Wrapper class to allow dot-attribute access (e.g., obj.stock_code)
    for dictionary data, ensuring compatibility with standard xtquant usages.
    """
    def __init__(self, d):
        if isinstance(d, dict):
            for k, v in d.items():
                if isinstance(v, dict):
                    setattr(self, k, QMTObject(v))
                elif isinstance(v, list):
                    setattr(self, k, [QMTObject(x) if isinstance(x, dict) else x for x in v])
                else:
                    setattr(self, k, v)

    def __getattr__(self, name):
        return None

    def __repr__(self):
        return f"QMTObject({self.__dict__})"

    def to_dict(self):
        res = {}
        for k, v in self.__dict__.items():
            if isinstance(v, QMTObject):
                res[k] = v.to_dict()
            elif isinstance(v, list):
                res[k] = [x.to_dict() if isinstance(x, QMTObject) else x for x in v]
            else:
                res[k] = v
        return res

class QMTBridgeClient(object):
    def __init__(self, host="127.0.0.1", port=8080):
        self.host = host
        self.port = port
        self.base_url = f"http://{host}:{port}"
        self.stop_signal = False
        self.listener_thread = None
        self.on_disconnected = None
        self.on_stock_order = None
        self.on_stock_trade = None
        self.on_stock_asset = None
        self.on_stock_position = None
        self.on_order_error = None
        self.on_cancel_error = None
        self.on_order_stock_async_response = None
        self.on_account_status = None

    def _request(self, method, path, data=None):
        """Sends HTTP request to the bridge server, bypassing host proxy settings."""
        url = f"{self.base_url}{path}"
        try:
            req_data = json.dumps(data).encode('utf-8') if data else None
            req = urllib.request.Request(url, data=req_data, method=method)
            if req_data:
                req.add_header('Content-Type', 'application/json')
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=10) as response:
                body = response.read().decode('utf-8')
                return json.loads(body)
        except urllib.error.HTTPError as e:
            # 404 on /data/download_status is expected transient state
            is_transient_404 = (e.code == 404 and "/data/download_status" in path)
            try:
                body = e.read().decode('utf-8')
                res = json.loads(body)
                err_msg = res.get("message", "Unknown server error")
                if not is_transient_404:
                    logger.error(f"HTTP request to bridge server failed ({method} {path}) - Status {e.code}: {err_msg}")
                    print(f"Server returned error {e.code}: {err_msg}")
                return res
            except Exception:
                if not is_transient_404:
                    logger.error(f"HTTP request to bridge server failed ({method} {path}) - Status {e.code}: {e.reason}")
                    print(f"Server returned error {e.code}: {e.reason}")
                return {"status": "error", "message": f"HTTP Error {e.code}: {e.reason}"}
        except Exception as e:
            logger.error(f"HTTP request to bridge server failed ({method} {path}): {e}")
            print(f"HTTP request to bridge server failed: {e}")
            return {"status": "error", "message": str(e)}

    def connect(self, account_id, account_type="STOCK", session_id=123456, qmt_path=None):
        data = {"account_id": account_id, "account_type": account_type, "session_id": session_id}
        if qmt_path:
            data["qmt_path"] = qmt_path
        res = self._request("POST", "/connect", data)
        return res.get("status") == "success"

    def order_stock(self, symbol, side, quantity, price_type=11, price=0.0, strategy_name="BridgeAPI", order_remark=""):
        data = {"symbol": symbol, "side": side, "quantity": quantity, "price_type": price_type,
                "price": price, "strategy_name": strategy_name, "order_remark": order_remark}
        res = self._request("POST", "/order", data)
        if res.get("status") == "success":
            return res.get("order_id")
        logger.error(f"Order placement failed via bridge: {res.get('message')}")
        return -1

    def cancel_order_stock(self, order_id):
        data = {"order_id": int(order_id)}
        res = self._request("POST", "/cancel", data)
        return res.get("result", False) if res.get("status") == "success" else False

    def query_stock_positions(self):
        res = self._request("GET", "/positions")
        if res.get("status") == "success":
            return [QMTObject(p) for p in res.get("positions", [])]
        return []

    def query_stock_position(self, symbol):
        data = {"symbol": symbol}
        res = self._request("POST", "/trade/position_single", data)
        if res.get("status") == "success":
            return QMTObject(res.get("position"))
        return None

    def query_stock_asset(self):
        res = self._request("GET", "/asset")
        if res.get("status") == "success":
            return QMTObject(res.get("asset"))
        return None

    def query_stock_orders(self):
        res = self._request("GET", "/orders")
        if res.get("status") == "success":
            return [QMTObject(o) for o in res.get("orders", [])]
        return []

    def query_stock_trades(self):
        res = self._request("GET", "/trades")
        if res.get("status") == "success":
            return [QMTObject(t) for t in res.get("trades", [])]
        return []

    def download_history_data2(self, symbols, period="1d", start_time="", end_time=""):
        data = {"symbols": symbols, "period": period, "start_time": start_time, "end_time": end_time}
        res = self._request("POST", "/data/download", data)
        if res.get("status") == "success":
            return res.get("task_id")
        return None

    def query_download_status(self, task_id):
        """Query the downloading task status. Returns status dict.

        404 (Task not found) is an expected transient state during async download
        startup — the worker thread may not have registered the task yet.
        Treated as "not ready" instead of an error.
        """
        try:
            res = self._request("GET", f"/data/download_status?task_id={task_id}")
        except Exception:
            return None
        if res.get("status") == "success":
            return res.get("task")
        return None

    def get_instrument_detail(self, symbol):
        res = self._request("GET", f"/data/detail?symbol={symbol}")
        if res.get("status") == "success":
            return QMTObject(res.get("detail"))
        return None

    def get_market_data(self, fields, symbols, period="1d", start_time="", end_time="", count=-1, dividend_type="none"):
        data = {"fields": fields, "symbols": symbols, "period": period,
                "start_time": start_time, "end_time": end_time,
                "count": count, "dividend_type": dividend_type}
        res = self._request("POST", "/data/kline", data)
        if res.get("status") == "success":
            return res.get("data", {})
        return {}

    def get_local_data(self, fields, symbols, period="1d", start_time="", end_time="", count=-1, dividend_type="none", fill_data=True):
        data = {"fields": fields, "symbols": symbols, "period": period,
                "start_time": start_time, "end_time": end_time,
                "count": count, "dividend_type": dividend_type, "fill_data": fill_data}
        res = self._request("POST", "/data/local_data", data)
        if res.get("status") == "success":
            return res.get("data", {})
        return {}

    def get_trading_dates(self, market, start_time="", end_time="", count=-1):
        data = {"market": market, "start_time": start_time, "end_time": end_time, "count": count}
        res = self._request("POST", "/data/trading_dates", data)
        if res.get("status") == "success":
            return res.get("dates", [])
        return []

    def get_sector_list(self):
        res = self._request("GET", "/data/sector_list")
        if res.get("status") == "success":
            return res.get("sectors", [])
        return []

    def get_stock_list_in_sector(self, sector_name, real_timetag=-1):
        data = {"sector_name": sector_name, "real_timetag": real_timetag}
        res = self._request("POST", "/data/sector_stocks", data)
        if res.get("status") == "success":
            return res.get("stocks", [])
        return []

    def get_index_weight(self, index_code):
        data = {"index_code": index_code}
        res = self._request("POST", "/data/index_weight", data)
        if res.get("status") == "success":
            return res.get("weights", {})
        return {}

    def get_financial_data(self, stock_list, table_list, report_type="announce_time"):
        data = {"stock_list": stock_list, "table_list": table_list, "report_type": report_type}
        res = self._request("POST", "/data/financial", data)
        if res.get("status") == "success":
            return res.get("data", {})
        return {}

    def get_holidays(self):
        res = self._request("GET", "/data/holidays")
        if res.get("status") == "success":
            return res.get("holidays", [])
        return []

    def start(self):
        self.stop_signal = False
        self.listener_thread = threading.Thread(target=self._listen_events)
        self.listener_thread.daemon = True
        self.listener_thread.start()
        logger.info("SSE event listener thread started.")

    def stop(self):
        self.stop_signal = True
        if self.listener_thread:
            self.listener_thread.join(timeout=3)
        logger.info("SSE event listener thread stopped.")

    def _dispatch_event(self, event_name, data):
        qmt_obj = QMTObject(data) if data is not None else None
        callback_mapping = {
            "on_disconnected": self.on_disconnected,
            "on_stock_order": self.on_stock_order,
            "on_stock_trade": self.on_stock_trade,
            "on_stock_asset": self.on_stock_asset,
            "on_stock_position": self.on_stock_position,
            "on_order_error": self.on_order_error,
            "on_cancel_error": self.on_cancel_error,
            "on_order_stock_async_response": self.on_order_stock_async_response,
            "on_account_status": self.on_account_status,
        }
        callback = callback_mapping.get(event_name)
        if callback:
            try:
                if event_name == "on_disconnected":
                    callback()
                else:
                    callback(qmt_obj)
            except Exception as e:
                logger.error(f"Error executing callback for event {event_name}: {e}")

    def _listen_events(self):
        url = f"{self.base_url}/events"
        while not self.stop_signal:
            try:
                req = urllib.request.Request(url)
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(req, timeout=15) as response:
                    for line in response:
                        if self.stop_signal:
                            break
                        line_str = line.decode('utf-8').strip()
                        if not line_str:
                            continue
                        if line_str.startswith("data:"):
                            payload_str = line_str[5:].strip()
                            try:
                                payload = json.loads(payload_str)
                                event_name = payload.get("event")
                                event_data = payload.get("data")
                                if event_name == "connected":
                                    continue
                                self._dispatch_event(event_name, event_data)
                            except json.JSONDecodeError:
                                pass
            except Exception as e:
                if not self.stop_signal:
                    time.sleep(2)