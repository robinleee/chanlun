from enum import Enum

# 默认策略参数；可在 FSM 构造时覆盖以做 A/B
DEFAULT_STRATEGY_CONFIG = {
    "hard_stop_loss_pct": 0.0,        # 0 = 禁用；>0 = 跌幅到此百分比强制 STOP_LOSS
    "take_profit_pct": 1.25,          # 涨幅到此比例触发 RECOVER（默认 1.25x = +25%）
    "partial_take_profit": False,      # True = 涨 30% 时先平 1/3 仓（搭配 take_profit_pct=1.3 用）
    "first_tranche_ratio": 1.0,        # 首笔建仓用预算的比例（0.5 = 半仓）
    "signal_cooldown_bars": 0,        # 同一标的两个 INITIAL 之间最少隔多少根 bar（0=不限制）
}


class AccountState(Enum):
    EMPTY = "EMPTY"
    NORMAL_HOLDING = "NORMAL_HOLDING"
    ZERO_COST_GAMING = "ZERO_COST_GAMING"

class AccountStateMachine:
    """
    零成本财务状态机。

    资金分账：
      - cash_uninvested: 未投入的本金（INITIAL 买入前可用，RECOVER 后回笼）
      - maneuver_cash:   10% 机动金，专门用于 T+0 套利与回接

    T+1 模拟（A 股不允许当日买入当日卖出）：
      - held_shares:     当前总持仓
      - sellable_shares: 今日可卖股数（隔夜后才解锁）
      SystemController 在日期切换时调用 on_day_rollover() 解锁。
    """

    def __init__(self, stock_code, total_budget, trader_gateway, strategy_config=None):
        self.stock_code = stock_code
        self.total_budget = total_budget
        self.gateway = trader_gateway
        # 合并默认 config 与用户覆盖；None 表示全部用默认
        self.config = dict(DEFAULT_STRATEGY_CONFIG)
        if strategy_config:
            self.config.update(strategy_config)

        self.cash_uninvested = total_budget * 0.9
        self.cash_unspent = total_budget * 0.9
        self.maneuver_cash = total_budget * 0.1

        self.state = AccountState.EMPTY
        self.entry_avg_price = 0.0
        self.held_shares = 0
        self.sellable_shares = 0

        # T+0 套利持仓缓存舱
        self.t0_sell_cache = 0  # 轨道一：已高抛待回吸的股票数量
        self.t0_buy_cache = 0   # 轨道二：已低吸待高抛的股票数量

        # 异步订单跟踪矩阵（按 order_id 索引，累积分笔成交）
        # pending_orders[order_id] = {
        #     "side", "volume", "filled", "price", "order_type", "filled_value"
        # }
        self.pending_orders = {}

        # A/B 测试用：上次 INITIAL 下单时的 bar 索引（用于信号冷却）
        self._last_buy_bar_idx = -10**9

    @property
    def cash_on_hand(self):
        """总可用现金（真实的账户剩余可用现金，含未投入本金 + 机动金）。用于组合权益估值。"""
        return self.cash_unspent + self.maneuver_cash

    def on_day_rollover(self):
        """新交易日开始：隔夜持仓解锁为可卖。"""
        self.sellable_shares = self.held_shares

    def update_state(self, signal, current_price, can_sell, small_to_large_meltdown=False, order_id=None, bar_ts=None):
        """
        基于形态信号与可用持仓驱动发单，严禁在此处直接记账，需由成交回报驱动。
        can_sell: 来自回测的 sellable_shares 或实盘 QMT 的 can_use_volume。
        order_id: 由 QMTTraderGateway 生成的订单 ID（回测为合成，实盘为柜台 ID）。
        bar_ts: 可选，当前 bar 时间戳（用于 A/B 测试中的信号冷却）
        """
        if small_to_large_meltdown and self.state != AccountState.EMPTY:
            if can_sell > 0:
                print(f"[FSM_ACTION] STOP_LOSS TRIGGERED (MELTDOWN): {self.stock_code} | Price={current_price:.2f} | Shares={can_sell}")
                self._register_order(order_id, "SELL", can_sell, current_price, "MELTDOWN")
            return

        cfg = self.config
        hard_stop_pct = cfg["hard_stop_loss_pct"]
        take_profit = cfg["take_profit_pct"]
        partial_tp = cfg["partial_take_profit"]
        first_ratio = cfg["first_tranche_ratio"]
        cooldown = cfg["signal_cooldown_bars"]

        # P0 硬止损：在任何状态机分支前检查，对 NORMAL_HOLDING 和 ZERO_COST_GAMING 都生效
        if hard_stop_pct > 0 and self.entry_avg_price > 0 and self.held_shares > 0:
            loss_pct = (current_price - self.entry_avg_price) / self.entry_avg_price
            print(f"[FSM_CHECK] Stop Loss Check: {self.stock_code} | Price={current_price:.2f} | Entry Avg Price={self.entry_avg_price:.2f} | Loss={loss_pct:.2%} | Hard Stop Threshold={hard_stop_pct:.2%}")
            if loss_pct <= -hard_stop_pct:
                print(f"[FSM_ACTION] STOP_LOSS TRIGGERED (HARD STOP): {self.stock_code} | Price={current_price:.2f} | Shares={self.held_shares}")
                self._register_order(order_id, "SELL", self.held_shares, current_price, "STOP_LOSS")
                return

        if self.state == AccountState.EMPTY:
            if signal in ["RETAIL_2BUY", "RETAIL_3BUY", "CROSS_1ClassBuy"]:
                # 信号冷却：避免短时间内重复触发 INITIAL
                if cooldown > 0 and bar_ts is not None and (bar_ts - self._last_buy_bar_idx) < cooldown:
                    return
                full_target = int(self.cash_unspent / current_price / 100) * 100
                # 首笔比例：1.0 = 满仓，0.5 = 半仓分两批
                target_shares = max(100, int(full_target * first_ratio / 100) * 100)
                target_shares = min(target_shares, full_target)
                if target_shares > 0:
                    self._register_order(order_id, "BUY", target_shares, current_price, "INITIAL")
                    if bar_ts is not None:
                        self._last_buy_bar_idx = bar_ts

        elif self.state == AccountState.NORMAL_HOLDING:
            if signal in ["3ClassSell", "CROSS_1ClassSell"]:
                if can_sell > 0:
                    print(f"[FSM_ACTION] EXIT TRIGGERED ({signal}): {self.stock_code} | Price={current_price:.2f} | Shares={can_sell}")
                    self._register_order(order_id, "SELL", can_sell, current_price, "MACRO_SELL")
            elif current_price >= self.entry_avg_price * take_profit or signal == "1ClassSell":
                # 1ClassSell + partial_tp=True：强制平 1/3（绕过 T+1，新功能）
                # 1ClassSell + partial_tp=False：按 cash_uninvested 算（标准 RECOVER，受 T+1 约束）
                if partial_tp and signal == "1ClassSell":
                    shares_to_sell = round(self.held_shares * 0.33 / 100) * 100
                    if shares_to_sell > 0:
                        # 强制离场，绕过 T+1
                        self._register_order(order_id, "SELL", shares_to_sell, current_price, "RECOVER")
                else:
                    # 标准 RECOVER，受 T+1 约束
                    shares_to_sell = int(self.cash_uninvested / current_price / 100) * 100
                    actual_sell = min(shares_to_sell, can_sell)
                    if actual_sell > 0:
                        self._register_order(order_id, "SELL", actual_sell, current_price, "RECOVER")

        elif self.state == AccountState.ZERO_COST_GAMING:
            if signal in ["Macro_1ClassSell", "CROSS_1ClassSell"]:
                if can_sell > 0:
                    self._register_order(order_id, "SELL", can_sell, current_price, "MACRO_SELL")
                return

            # T+0 套利单位数量：总持仓（包含已借出的持仓缓存）的 10%
            t0_unit_shares = int((self.held_shares + self.t0_sell_cache) * 0.1 / 100) * 100
            if t0_unit_shares <= 0:
                t0_unit_shares = 100

            # 轨道一：先卖后买（高抛低吸）—— 高抛
            if signal == "FluctuationTop" and self.t0_buy_cache == 0 and can_sell >= t0_unit_shares:
                self._register_order(order_id, "SELL", t0_unit_shares, current_price, "T0_SELL")

            # 轨道一：先卖后买 —— 低吸接回
            if signal == "FluctuationBottom" and self.t0_sell_cache > 0:
                required_cash = self.t0_sell_cache * current_price
                if self.maneuver_cash >= required_cash:
                    self._register_order(order_id, "BUY", self.t0_sell_cache, current_price, "T0_BUYback")

            # 轨道二：先买后卖 —— 低吸
            if signal == "FluctuationBottom" and self.t0_sell_cache == 0 and self.t0_buy_cache == 0:
                required_cash = t0_unit_shares * current_price
                if self.maneuver_cash >= required_cash:
                    self._register_order(order_id, "BUY", t0_unit_shares, current_price, "T0_BUY")

            # 轨道二：先买后卖 —— 高抛平仓
            # T+1 约束：t0_buy_cache 是当日买入的，必须隔夜解锁后才能卖。
            # 锁定是否解锁的判定：can_sell == held_shares（当日所有持仓都可卖）。
            if signal == "FluctuationTop" and self.t0_buy_cache > 0 \
                    and can_sell >= self.t0_buy_cache and can_sell >= self.held_shares:
                self._register_order(order_id, "SELL", self.t0_buy_cache, current_price, "T0_SELLback")

    def _register_order(self, order_id, side, volume, price, order_type):
        """委托下单：通过 gateway 发单并在本地 pending_orders 中建档。

        关键：必须先把订单加入 pending_orders，再调 gateway.submit_order。
        因为回测模式下 submit_order 同步触发 handle_trade_callback →
        fsm.on_order_trade_callback，会立刻查 pending_orders；如果是空的就
        静默错过整笔交易。
        """
        if order_id is None:
            raise ValueError("order_id is required for order registration")
        if order_id in self.pending_orders:
            # 防御性：order_id 重复使用不应发生；如发生则跳过
            return
        # 先建档（让 callback 能找到）
        self.pending_orders[order_id] = {
            "side": side,
            "volume": volume,
            "filled": 0,
            "price": price,
            "order_type": order_type,
            "filled_value": 0.0,
        }
        print(f"[FSM_ACTION] Triggering Order: {self.stock_code} | Type={order_type} | Side={side} | Volume={volume} | Price={price:.2f} | FSM State={self.state.value}")
        # 然后再发单（回测模式会同步触发 callback；实盘模式异步触发）
        self.gateway.submit_order(self.stock_code, side, volume, price, order_id=order_id)

    def on_order_trade_callback(self, side, volume, price, order_id):
        """
        订单跟踪矩阵的回调：累积单笔成交，到达目标量后才执行状态机清算。
        order_id 由 MyXtQuantCallback 透传（实盘）或 QMTTraderGateway 注入（回测）。
        """
        if order_id is None or order_id not in self.pending_orders:
            # 防御性：未知成交回报（例如延迟推送），忽略
            return

        record = self.pending_orders[order_id]
        record["filled"] += volume
        record["filled_value"] += volume * price

        if record["filled"] < record["volume"]:
            # 部分成交：保留订单记录，等待后续回报
            return

        if record["filled"] > record["volume"]:
            # 异常：累计成交超过目标量，截断处理
            record["filled"] = record["volume"]

        vwap_price = record["filled_value"] / record["volume"]
        order_type = record["order_type"]
        filled_volume = record["volume"]
        del self.pending_orders[order_id]

        self._clear_balances(side, filled_volume, vwap_price, order_type)

    def _clear_balances(self, side, volume, price, order_type):
        """
        资金划转与持仓更新的原子清算逻辑。
        注意：INITIAL/T0_BUY 等增加持仓的订单，已被 T+1 锁定，
        因此只更新 held_shares 而不更新 sellable_shares。
        只有 on_day_rollover() 才会把 held_shares 解锁为 sellable_shares。

        cash_uninvested 语义：尚未回收的原始建仓本金。
          - INITIAL: 不变（建仓后仍是"未回收"状态，由 held_shares * price 作为市值体现）
          - RECOVER: 减少（已回收部分本金），当减到 ≤0 时表示全部回收，进入零成本博弈
          - STOP_LOSS / MACRO_SELL / MELTDOWN: 归零（本金已损失或彻底退出）
          - T+0 套利：不变（仅动用 maneuver_cash）
        """
        if order_type == "INITIAL":
            self.held_shares = volume
            self.entry_avg_price = price
            self.cash_uninvested = volume * price
            self.cash_unspent = max(0.0, self.cash_unspent - volume * price)
            self.state = AccountState.NORMAL_HOLDING

        elif order_type == "STOP_LOSS":
            total_cash = self.cash_unspent + self.maneuver_cash + volume * price
            self.held_shares = 0
            self.sellable_shares = 0
            self.cash_uninvested = 0.0
            self.cash_unspent = total_cash * 0.9
            self.maneuver_cash = total_cash * 0.1
            self.state = AccountState.EMPTY

        elif order_type == "RECOVER":
            self.held_shares -= volume
            self.sellable_shares = max(0, self.sellable_shares - volume)
            self.cash_uninvested = max(0.0, self.cash_uninvested - volume * price)
            self.maneuver_cash += volume * price
            self.state = AccountState.ZERO_COST_GAMING

        elif order_type == "MACRO_SELL":
            total_cash = self.cash_unspent + self.maneuver_cash + volume * price
            self.held_shares = 0
            self.sellable_shares = 0
            self.cash_uninvested = 0.0
            self.cash_unspent = total_cash * 0.9
            self.maneuver_cash = total_cash * 0.1
            self.state = AccountState.EMPTY
            self.t0_sell_cache = 0
            self.t0_buy_cache = 0

        elif order_type == "MELTDOWN":
            total_cash = self.cash_unspent + self.maneuver_cash + volume * price
            self.held_shares -= volume
            self.sellable_shares -= volume
            if self.held_shares == 0:
                self.cash_uninvested = 0.0
                self.cash_unspent = total_cash * 0.9
                self.maneuver_cash = total_cash * 0.1
                self.state = AccountState.EMPTY
            else:
                self.cash_uninvested = max(0.0, self.cash_uninvested - volume * price)
                self.maneuver_cash += volume * price
            self.t0_sell_cache = 0
            self.t0_buy_cache = 0

        elif order_type == "T0_SELL":
            self.t0_sell_cache += volume
            self.held_shares -= volume
            self.sellable_shares -= volume
            self.maneuver_cash += volume * price

        elif order_type == "T0_BUYback":
            self.held_shares += volume
            self.sellable_shares += volume
            self.maneuver_cash -= volume * price
            self.t0_sell_cache = 0

        elif order_type == "T0_BUY":
            self.t0_buy_cache += volume
            self.held_shares += volume
            self.maneuver_cash -= volume * price

        elif order_type == "T0_SELLback":
            self.held_shares -= volume
            self.sellable_shares -= volume
            self.maneuver_cash += volume * price
            self.t0_buy_cache = 0

        print(f"[FSM_CLEAR] Balance Cleared for {order_type}: {self.stock_code} | Held Shares={self.held_shares} | Sellable Shares={self.sellable_shares} | Cash Uninvested={self.cash_uninvested:.2f} | Maneuver Cash={self.maneuver_cash:.2f}")
