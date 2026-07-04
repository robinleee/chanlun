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
        # Return None instead of raising AttributeError for missing optional fields
        return None

    def __repr__(self):
        return f"QMTObject({self.__dict__})"

    def to_dict(self):
        """Recursively convert back to standard dictionary."""
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
        
        # User-defined Callbacks
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
            
            # Explicitly bypass system proxy settings for virtual machine communication
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(req, timeout=10) as response:
                body = response.read().decode('utf-8')
                return json.loads(body)
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode('utf-8')
                res = json.loads(body)
                err_msg = res.get("message", "Unknown server error")
                logger.error(f"HTTP request to bridge server failed ({method} {path}) - Status {e.code}: {err_msg}")
                # Also print directly so user can see it in terminal
                print(f"Server returned error {e.code}: {err_msg}")
                return res
            except Exception:
                logger.error(f"HTTP request to bridge server failed ({method} {path}) - Status {e.code}: {e.reason}")
                print(f"Server returned error {e.code}: {e.reason}")
                return {"status": "error", "message": f"HTTP Error {e.code}: {e.reason}"}
        except Exception as e:
            logger.error(f"HTTP request to bridge server failed ({method} {path}): {e}")
            print(f"HTTP request to bridge server failed: {e}")
            return {"status": "error", "message": str(e)}

    def connect(self, account_id, account_type="STOCK", session_id=123456, qmt_path=None):
        """Connect QMT and subscribe to the trading account."""
        data = {
            "account_id": account_id,
            "account_type": account_type,
            "session_id": session_id
        }
        if qmt_path:
            data["qmt_path"] = qmt_path
            
        res = self._request("POST", "/connect", data)
        return res.get("status") == "success"

    def order_stock(self, symbol, side, quantity, price_type=11, price=0.0, strategy_name="BridgeAPI", order_remark=""):
        """Place order. Returns order_id (int) on success, or -1 on failure."""
        data = {
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price_type": price_type,
            "price": price,
            "strategy_name": strategy_name,
            "order_remark": order_remark
        }
        res = self._request("POST", "/order", data)
        if res.get("status") == "success":
            return res.get("order_id")
        else:
            logger.error(f"Order placement failed via bridge: {res.get('message')}")
            return -1

    def cancel_order_stock(self, order_id):
        """Cancel order by ID. Returns True on success, False on failure."""
        data = {"order_id": int(order_id)}
        res = self._request("POST", "/cancel", data)
        if res.get("status") == "success":
            return res.get("result", False)
        return False

    def cancel_order_stock_sysid(self, market, order_sysid):
        """Cancel order by exchange system ID. Returns True on success, False on failure."""
        data = {"market": market, "order_sysid": str(order_sysid)}
        res = self._request("POST", "/trade/cancel_sysid", data)
        if res.get("status") == "success":
            return res.get("result", False)
        return False

    def query_stock_positions(self):
        """Query all positions. Returns list of QMTObject positions."""
        res = self._request("GET", "/positions")
        if res.get("status") == "success":
            return [QMTObject(p) for p in res.get("positions", [])]
        return []

    def query_stock_position(self, symbol):
        """Query single stock position. Returns QMTObject position."""
        data = {"symbol": symbol}
        res = self._request("POST", "/trade/position_single", data)
        if res.get("status") == "success":
            return QMTObject(res.get("position"))
        return None

    def query_stock_asset(self):
        """Query asset. Returns QMTObject asset."""
        res = self._request("GET", "/asset")
        if res.get("status") == "success":
            return QMTObject(res.get("asset"))
        return None

    def query_stock_orders(self):
        """Query today's orders. Returns list of QMTObject orders."""
        res = self._request("GET", "/orders")
        if res.get("status") == "success":
            return [QMTObject(o) for o in res.get("orders", [])]
        return []

    def query_stock_order(self, order_id):
        """Query single order details. Returns QMTObject order."""
        data = {"order_id": int(order_id)}
        res = self._request("POST", "/trade/order_single", data)
        if res.get("status") == "success":
            return QMTObject(res.get("order"))
        return None

    def query_stock_trades(self):
        """Query today's trades. Returns list of QMTObject trades."""
        res = self._request("GET", "/trades")
        if res.get("status") == "success":
            return [QMTObject(t) for t in res.get("trades", [])]
        return []

    def query_credit_detail(self):
        """Query margin asset details (两融资产). Returns list of QMTObject."""
        res = self._request("GET", "/trade/credit_detail")
        if res.get("status") == "success":
            return [QMTObject(c) for c in res.get("credit_detail", [])]
        return []

    def query_stk_compacts(self):
        """Query margin debt contracts (两融负债). Returns list of QMTObject."""
        res = self._request("GET", "/trade/stk_compacts")
        if res.get("status") == "success":
            return [QMTObject(c) for c in res.get("stk_compacts", [])]
        return []

    def query_credit_subjects(self):
        """Query margin symbols (两融标的). Returns list of QMTObject."""
        res = self._request("GET", "/trade/credit_subjects")
        if res.get("status") == "success":
            return [QMTObject(s) for s in res.get("credit_subjects", [])]
        return []

    def query_credit_slo_code(self):
        """Query available short-selling stock (可融券). Returns list of QMTObject."""
        res = self._request("GET", "/trade/credit_slo_code")
        if res.get("status") == "success":
            return [QMTObject(s) for s in res.get("credit_slo_code", [])]
        return []

    def query_credit_assure(self):
        """Query folding folding rate (担保品折算). Returns list of QMTObject."""
        res = self._request("GET", "/trade/credit_assure")
        if res.get("status") == "success":
            return [QMTObject(a) for a in res.get("credit_assure", [])]
        return []

    def query_secu_accounts(self):
        """Query shareholder account IDs (股东账号). Returns list of QMTObject."""
        res = self._request("GET", "/trade/secu_accounts")
        if res.get("status") == "success":
            return [QMTObject(a) for a in res.get("secu_accounts", [])]
        return []

    def download_history_data2(self, symbols, period="1d", start_time="", end_time=""):
        """Start asynchronous historical data downloading. Returns task_id."""
        data = {
            "symbols": symbols,
            "period": period,
            "start_time": start_time,
            "end_time": end_time
        }
        res = self._request("POST", "/data/download", data)
        if res.get("status") == "success":
            return res.get("task_id")
        return None

    def query_download_status(self, task_id):
        """Query the downloading task status. Returns status dict."""
        res = self._request("GET", f"/data/download_status?task_id={task_id}")
        if res.get("status") == "success":
            return res.get("task")
        return None

    def get_instrument_detail(self, symbol):
        """Query static instrument details. Returns QMTObject details."""
        res = self._request("GET", f"/data/detail?symbol={symbol}")
        if res.get("status") == "success":
            return QMTObject(res.get("detail"))
        return None

    def get_market_data(self, fields, symbols, period="1d", start_time="", end_time="", count=-1, dividend_type="none"):
        """Get market K-Line/Tick data from cache. Returns dict matching Pandas/numpy structured output format."""
        data = {
            "fields": fields,
            "symbols": symbols,
            "period": period,
            "start_time": start_time,
            "end_time": end_time,
            "count": count,
            "dividend_type": dividend_type
        }
        res = self._request("POST", "/data/kline", data)
        if res.get("status") == "success":
            return res.get("data", {})
        return {}

    def get_local_data(self, fields, symbols, period="1d", start_time="", end_time="", count=-1, dividend_type="none", fill_data=True):
        """Directly query local offline cache files. Returns dict matching Pandas/numpy structured output format."""
        data = {
            "fields": fields,
            "symbols": symbols,
            "period": period,
            "start_time": start_time,
            "end_time": end_time,
            "count": count,
            "dividend_type": dividend_type,
            "fill_data": fill_data
        }
        res = self._request("POST", "/data/local_data", data)
        if res.get("status") == "success":
            return res.get("data", {})
        return {}

    def get_trading_dates(self, market, start_time="", end_time="", count=-1):
        """Get market trading days timestamp list. Returns list of int."""
        data = {"market": market, "start_time": start_time, "end_time": end_time, "count": count}
        res = self._request("POST", "/data/trading_dates", data)
        if res.get("status") == "success":
            return res.get("dates", [])
        return []

    def get_sector_list(self):
        """Get system/user sector list names. Returns list of str."""
        res = self._request("GET", "/data/sector_list")
        if res.get("status") == "success":
            return res.get("sectors", [])
        return []

    def get_stock_list_in_sector(self, sector_name, real_timetag=-1):
        """Get stock symbols under specific sector name. Returns list of str."""
        data = {"sector_name": sector_name, "real_timetag": real_timetag}
        res = self._request("POST", "/data/sector_stocks", data)
        if res.get("status") == "success":
            return res.get("stocks", [])
        return []

    def get_index_weight(self, index_code):
        """Get index weights composition. Returns dict mapping stock symbol to weight value."""
        data = {"index_code": index_code}
        res = self._request("POST", "/data/index_weight", data)
        if res.get("status") == "success":
            return res.get("weights", {})
        return {}

    def get_financial_data(self, stock_list, table_list, report_type="announce_time"):
        """Get financial data (Balance Sheet, Income Statement, etc.) for stocks.

        Args:
            stock_list: List of stock codes.
            table_list: List of table names, e.g. ['Balance', 'Income'].
            report_type: Report time type, default 'announce_time'.

        Returns:
            dict: {code: {table_name: {index, columns, data}}} structured format.
                  Each table contains 'index' (report dates), 'columns' (field names),
                  and 'data' (2D array of values).
        """
        data = {
            "stock_list": stock_list,
            "table_list": table_list,
            "report_type": report_type
        }
        res = self._request("POST", "/data/financial", data)
        if res.get("status") == "success":
            return res.get("data", {})
        return {}

    def get_holidays(self):
        """Get holidays lists. Returns list of str."""
        res = self._request("GET", "/data/holidays")
        if res.get("status") == "success":
            return res.get("holidays", [])
        return []

    def start(self):
        """Start background thread listening to SSE callback stream."""
        self.stop_signal = False
        self.listener_thread = threading.Thread(target=self._listen_events)
        self.listener_thread.daemon = True
        self.listener_thread.start()
        logger.info("SSE event listener thread started.")

    def stop(self):
        """Signal and stop the background listener thread."""
        self.stop_signal = True
        if self.listener_thread:
            self.listener_thread.join(timeout=3)
        logger.info("SSE event listener thread stopped.")

    def _dispatch_event(self, event_name, data):
        """Map SSE payload to local callbacks, wrapping dicts inside QMTObjects."""
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
                # Trigger callback
                if event_name == "on_disconnected":
                    callback()
                else:
                    callback(qmt_obj)
            except Exception as e:
                logger.error(f"Error executing callback for event {event_name}: {e}")

    def _listen_events(self):
        """Loop reading Server-Sent Events from bridge server."""
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
                                    logger.info("Successfully handshaked with SSE stream.")
                                    continue
                                self._dispatch_event(event_name, event_data)
                            except json.JSONDecodeError:
                                pass
            except Exception as e:
                if not self.stop_signal:
                    logger.warning(f"SSE event connection lost: {e}. Retrying in 2 seconds...")
                    time.sleep(2)
