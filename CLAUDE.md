# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

缠论 (Chan Theory) quantitative trading system. Implements 缠论 technical analysis — K线包含处理 → 分型 → 笔 → 线段 → 中枢 → 背驰 → 买卖信号, driving an account FSM that manages position entries, exits, stop losses, T+0 scalping, and zero-cost gaming.

## Two deployment variants

| File | Purpose |
|---|---|
| `chan_quant_system_windows.py` | Runs on Windows with direct `xtquant` SDK (QMT client) |
| `chan_quant_system_qmt.py` | Runs on Linux, communicates with a Windows VM QMT bridge via HTTP+SSE (`qmt_bridge_client.py`) |

Both files share **identical strategy logic** (ChanEngine, StrategyKernel, signal semantics). They differ only in data/trade transport layer.

## Project structure

```
chanlun_engine/            # Pure strategy library (no I/O dependencies)
├── inclusion_filter.py    # KLineInclusionFilter, StandardKLine — 包含关系处理
├── geometry_engine.py     # Fractal, Bi, StrictBiValidator, Segment, SegmentEngine, Zhongshu, ZhongshuFSM
├── dynamics_engine.py     # DynamicsDivergenceEngine (MACD背驰), MultiTimeframeScanner (区间套), SmallToLargeGateway (小转大)
└── account_fsm.py         # AccountStateMachine — 零成本仓位状态机 (EMPTY → NORMAL_HOLDING → ZERO_COST_GAMING)

evaluate_backtest.py       # Parses backtest logs, reconstructs trades, generates markdown performance report
qmt_bridge_client.py       # HTTP+SSE client SDK for Linux → Windows QMT bridge communication
backtest_reports/          # Backtest log output directory
```

## How to run

**Windows (direct xtquant):**
```bash
python chan_quant_system_windows.py
```
Edit `my_target_pool` and `RUN_MODE` (`"BACKTEST"` or `"LIVE"`) and the strategy config dict near the bottom of the file. Backtest date range defaults to `start_date="20260101", end_date="20260628"`. Period defaults to macro=`"1h"`, micro=`"5m"`.

**Linux (bridge):**
```bash
python chan_quant_system_qmt.py
```
Same config pattern as Windows. Requires `QMT_BRIDGE_HOST`/`QMT_BRIDGE_PORT` env vars (default `192.168.122.14:8080`).

**Evaluate a backtest run:**
```bash
python evaluate_backtest.py backtest_reports/backtest_run_<timestamp>.log
# or auto-detect latest log:
python evaluate_backtest.py
# Output: backtest_reports/backtest_evaluation_<name>.md
```

## Architecture: layered pipeline

### 1. K线 → 信号 (ChanEngine)

Each stock has two `ChanEngine` instances (macro period, e.g. 1h/5m, and micro period, e.g. 5m/1m). `push_raw_kline(ts, open, high, low, close)` drives the full chain incrementally:

1. **包含处理** (`KLineInclusionFilter.push_bar`) — merges overlapping bars into standard K-lines, assigns direction (UP/DOWN), manages a bounded `deque` of up to 2000 bars
2. **分型识别** — top/bottom fractals confirmed when index ≥ N-4 (confirmed), plus temp fractals at N-3, N-2
3. **严格笔** (`StrictBiValidator`) — requires fractals separated by ≥ 4 index points
4. **线段** (`SegmentEngine.update_segments`) — streaming state machine per 缠论 Lesson 71/81, processes feature sequences with inclusion elimination, detects reverse fractals with gap/no-gap rules
5. **中枢** (`ZhongshuFSM.update_zhongshu`) — computes overlapping price zones from 3 consecutive segments; handles level expansion (5m → 30m) when new zhongshu overlaps previous one
6. **信号产出** — `_update_geometry_topology()` returns signals in priority order: `Macro_1ClassSell` > `FluctuationTop/Bottom` > `RETAIL_2BUY/3BUY` (or `1m_1ClassBuy/1m_1ClassSell` for micro engines) > `3ClassSell`

### 2. Signal → trade (StrategyKernel)

`StrategyKernel.on_bar()` runs in `RLock` per stock. On each bar:

- T+1 day rollover detection (unlocks `sellable_shares` from `held_shares`)
- Pushes bar into appropriate engine (5m/1m)
- Runs `SmallToLargeGateway.check_meltdown_trigger()` — detects non-divergent declines per Lesson 44
- Runs `MultiTimeframeScanner.check_interval_套_convergence()` — compares MACD wave areas between entering segment (s1) and leaving segment (s3) of the 5m zhongshu; confirms with 1m signal for cross-period共振
- Falls back to single-period geometric signal when共振 preconditions aren't met
- Drives `AccountStateMachine.update_state()` which dispatches orders via the trader gateway

### 3. Account state machine (`account_fsm.py`)

```
EMPTY → (RETAIL_2BUY/3BUY/CROSS_1ClassBuy) → NORMAL_HOLDING
NORMAL_HOLDING → (take profit / 3ClassSell / STOP_LOSS / MELTDOWN) → EMPTY or ZERO_COST_GAMING
ZERO_COST_GAMING → T+0 scalping (FluctuationTop/FluctuationBottom) + MACRO_SELL to exit
```

Fund segregation: 90% core capital, 10% maneuver cash for T+0. Orders are registered in `pending_orders` dict keyed by `order_id` — FSM only dispatches; `on_order_trade_callback` handles settlement after fills accumulate to target volume.

### 4. Driver layer

- **BacktestDriver** — loads historical data (with 45-day preheat), builds merged timeline of 5m+1m bars, plays back chronologically. `disable_trading=True` during preheat. Recomputes active pool on day boundaries.
- **LiveDriver** (Windows) — subscribes to xtquant real-time bar callbacks. Daily 08:40 scheduler re-runs pool filter.
- **LiveDriver** (Linux/bridge) — polls `get_local_data` every 30s for new bars since last seen timestamp.

### 5. Pre-market pool filter (`_fundamental_filter` + `daily_trend_filter`)

Two-stage daily filter:
1. **Fundamental**: exclude ST/*ST stocks, debt ratio > 85%, consecutive negative net profit. Then SW industry classification + relative strength ranking (top 3 per industry, max 60 total).
2. **Daily trend**: MA5 > MA20, close ≥ 0.92 × MA20, 20-day change > -8%.

Pool filtering is the primary performance lever — stocks not in `active_pool` are skipped entirely in `on_bar()`.

## Key patterns

- **No external test framework** — testing is done via backtest mode: run a date range, inspect the `.log` output and evaluation report
- **Print-based logging** — all geometry confirmations, signal evaluations, order triggers, and trade callbacks print structured `[TAG]` lines consumed by `evaluate_backtest.py`
- **Thread safety**: `StrategyKernel` uses per-stock `threading.RLock` (not `Lock`) because backtest mode synchronously triggers `handle_trade_callback` within the same thread that holds the lock
- **T+1 simulation**: new buys lock shares in `held_shares`; `on_day_rollover()` transfers to `sellable_shares`
- **MACD is maintained incrementally** (`update_macd()`, O(1) per bar) with the batch `compute_macd_hist()` available for one-off calculations
- **Strategy config** passed as dict to `SystemController`/`AccountStateMachine`: `hard_stop_loss_pct`, `take_profit_pct`, `partial_take_profit`, `first_tranche_ratio`, `signal_cooldown_bars`

## Dual-mode transport

`QMTBridgeTraderGateway` (and `QMTTraderGateway`) handle both live and backtest modes:
- **Live**: submits orders via QMT API, receives fills asynchronously via SSE/callback
- **Backtest**: synthesizes order IDs, immediately injects simulated fills into `handle_trade_callback` — the FSM sees no difference

The `preview_order_id → fsm.update_state → gateway.submit_order` must happen in that exact order so `pending_orders` is populated before the callback fires (critical in backtest where callback is synchronous).
