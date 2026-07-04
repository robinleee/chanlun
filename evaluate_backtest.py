import argparse
import os
import glob
import re

BI_RE = re.compile(
    r"\[GEOMETRY\] Confirmed New Bi:\s*(?P<code>\S+)\s*\[(?P<period>[^\]]+)\]\s*\|\s*"
    r"Direction=(?P<dir>UP|DOWN)\s*\|\s*"
    r"Start Fractal=\d+\s*\((?P<start_type>TOP|BOTTOM)\)\s*at\s*(?P<start_price>\d+\.?\d*)\s*\|\s*"
    r"End Fractal=\d+\s*\((?P<end_type>TOP|BOTTOM)\)\s*at\s*(?P<end_price>\d+\.?\d*)\s*\|\s*"
    r"Confirmed Bi count=(?P<count>\d+)"
)

SEGMENT_RE = re.compile(
    r"\[GEOMETRY\] Confirmed New Segment:\s*(?P<code>\S+)\s*\[(?P<period>[^\]]+)\]\s*\|\s*"
    r"Direction=(?P<dir>UP|DOWN)\s*\|\s*"
    r"Start Bi start index=\d+\s*\|\s*End Bi end index=\d+\s*\|\s*"
    r"Extreme Price=(?P<extreme_price>\d+\.?\d*)\s*\|\s*"
    r"Confirmed Segment count=(?P<count>\d+)"
)

GATEWAY_START_RE = re.compile(
    r"====== 启动层级 1：盘前选股过滤网关\s*\[当前基准日期:\s*(?P<date>\d+)\] ======"
)

POOL_RE = re.compile(
    r"\[recompute_active_pool\] 新增 (?P<added>\d+), 退出 (?P<removed>\d+), 当前活跃 (?P<active>\d+)"
)

ORDER_TRIGGER_RE = re.compile(
    r"\[FSM_ACTION\] Triggering Order:\s*(?P<code>\S+)\s*\|\s*"
    r"Type=(?P<type>\w+)\s*\|\s*"
    r"Side=(?P<side>BUY|SELL)\s*\|\s*"
    r"Volume=(?P<volume>\d+)\s*\|\s*"
    r"Price=(?P<price>\d+\.?\d*)\s*\|\s*"
    r"FSM State=(?P<state>\w+)"
)

CALLBACK_RE = re.compile(
    r"\[CALLBACK CLEANED\]\s*(?P<code>\S+)\s*(?P<side>BUY|SELL)\s*成交回报清算成功\s*\(order_id=(?P<order_id>\S+)\)\.\s*"
    r"当前仓位:\s*(?P<shares>\d+),\s*可卖:\s*(?P<sellable>\d+),\s*现金:\s*(?P<cash>\d+\.?\d*)"
)

DATA_WARNING_RE = re.compile(
    r"警告: 本地数据库中\s*(?P<code>\S+)\s*的\s*(?P<period>\S+)\s*K线数据不足"
)

def parse_args(args=None):
    parser = argparse.ArgumentParser(description="Evaluate Chanlun Backtest Logs")
    parser.add_argument("log_file", nargs="?", help="Path to the backtest log file")
    parser.add_argument("--output", help="Path to write the evaluation report")
    return parser.parse_args(args)

def get_target_log(args):
    if args.log_file:
        if not os.path.exists(args.log_file):
            raise FileNotFoundError(f"Specified log file not found: {args.log_file}")
        return args.log_file
    log_files = glob.glob(os.path.join("backtest_reports", "*.log"))
    if not log_files:
        raise FileNotFoundError("No log files found in backtest_reports/")
    # Return the most recently modified log file
    return max(log_files, key=os.path.getmtime)

def parse_log_line(line):
    line = line.strip()
    m = BI_RE.search(line)
    if m:
        return {
            "event_type": "BI",
            "code": m.group("code"),
            "period": m.group("period"),
            "direction": m.group("dir"),
            "start_price": float(m.group("start_price")),
            "end_price": float(m.group("end_price")),
            "count": int(m.group("count"))
        }
    m = SEGMENT_RE.search(line)
    if m:
        return {
            "event_type": "SEGMENT",
            "code": m.group("code"),
            "period": m.group("period"),
            "direction": m.group("dir"),
            "extreme_price": float(m.group("extreme_price")),
            "count": int(m.group("count"))
        }
    m = GATEWAY_START_RE.search(line)
    if m:
        return {"event_type": "GATEWAY_START", "date": m.group("date")}
    m = POOL_RE.search(line)
    if m:
        return {
            "event_type": "POOL_UPDATE",
            "added": int(m.group("added")),
            "removed": int(m.group("removed")),
            "active": int(m.group("active"))
        }
    m = ORDER_TRIGGER_RE.search(line)
    if m:
        return {
            "event_type": "ORDER_TRIGGER",
            "code": m.group("code"),
            "type": m.group("type"),
            "side": m.group("side"),
            "volume": int(m.group("volume")),
            "price": float(m.group("price")),
            "state": m.group("state")
        }
    m = CALLBACK_RE.search(line)
    if m:
        return {
            "event_type": "CALLBACK",
            "code": m.group("code"),
            "side": m.group("side"),
            "order_id": m.group("order_id"),
            "shares": int(m.group("shares")),
            "cash": float(m.group("cash"))
        }
    m = DATA_WARNING_RE.search(line)
    if m:
        return {
            "event_type": "DATA_WARNING",
            "code": m.group("code"),
            "period": m.group("period")
        }
    return None

def reconstruct_trades(lines):
    stocks = {}
    completed_trades = []
    t0_trades = []
    stats = {
        "total_lines": 0,
        "data_warnings": 0,
        "bi_count": 0,
        "segment_count": 0,
        "is_completed": False,
        "last_date": None,
        "dates_list": [],
        "pool_sizes": [],
        "orders_triggered": {}
    }

    for line in lines:
        stats["total_lines"] += 1
        if "====== 极速回测回放完成 ======" in line or "缠论量化交易系统回测分析报告" in line:
            stats["is_completed"] = True

        event = parse_log_line(line)
        if not event:
            continue

        code = event.get("code")
        if code and code not in stocks:
            stocks[code] = {
                "held_shares": 0,
                "active_buy": None,
                "latest_bi": {},
                "active_t0": None,
                "pending_orders": {}
            }

        if event["event_type"] == "DATA_WARNING":
            stats["data_warnings"] += 1

        elif event["event_type"] == "BI":
            stats["bi_count"] += 1
            stocks[code]["latest_bi"][event["direction"]] = event

        elif event["event_type"] == "SEGMENT":
            stats["segment_count"] += 1

        elif event["event_type"] == "GATEWAY_START":
            stats["last_date"] = event["date"]
            if event["date"] not in stats["dates_list"]:
                stats["dates_list"].append(event["date"])

        elif event["event_type"] == "POOL_UPDATE":
            stats["pool_sizes"].append(event["active"])

        elif event["event_type"] == "ORDER_TRIGGER":
            t = event["type"]
            side = event["side"]
            stats["orders_triggered"][t] = stats["orders_triggered"].get(t, 0) + 1
            stocks[code]["pending_orders"][side] = event
            stocks[code]["last_triggered_type"] = t

        elif event["event_type"] == "CALLBACK":
            side = event["side"]
            shares = event["shares"]
            order_info = stocks[code]["pending_orders"].get(side, {})
            order_type = order_info.get("type", "UNKNOWN")
            exec_price = order_info.get("price", 0.0)
            exec_volume = order_info.get("volume", shares)

            if side == "BUY":
                if order_type in ("INITIAL", "UNKNOWN") and stocks[code]["held_shares"] == 0:
                    stocks[code]["active_buy"] = {
                        "code": code,
                        "entry_price": exec_price,
                        "entry_time": stats["total_lines"],
                        "entry_date": stats["last_date"],
                        "volume": exec_volume,
                        "entry_bi_bottom": stocks[code]["latest_bi"].get("DOWN", {}).get("end_price", 0.0)
                    }
                elif order_type == "T0_BUY":
                    stocks[code]["active_t0"] = {
                        "side": "T0_BUY",
                        "buy_price": exec_price,
                        "buy_time": stats["total_lines"],
                        "volume": exec_volume
                    }
                stocks[code]["held_shares"] = shares

            elif side == "SELL":
                if order_type in ("MACRO_SELL", "STOP_LOSS", "MELTDOWN", "UNKNOWN") and stocks[code]["active_buy"]:
                    buy = stocks[code]["active_buy"]
                    pnl = (exec_price - buy["entry_price"]) * buy["volume"]
                    pnl_pct = ((exec_price - buy["entry_price"]) / buy["entry_price"]) * 100 if buy["entry_price"] > 0 else 0.0
                    completed_trades.append({
                        "code": code,
                        "entry_price": buy["entry_price"],
                        "exit_price": exec_price,
                        "volume": buy["volume"],
                        "pnl": pnl,
                        "pnl_pct": pnl_pct,
                        "entry_time": buy["entry_time"],
                        "exit_time": stats["total_lines"],
                        "entry_date": buy.get("entry_date"),
                        "exit_date": stats["last_date"],
                        "exit_type": order_type,
                        "bi_bottom": buy["entry_bi_bottom"],
                        "bi_top": stocks[code]["latest_bi"].get("UP", {}).get("end_price", 0.0)
                    })
                    stocks[code]["active_buy"] = None
                elif order_type == "T0_SELL":
                    stocks[code]["active_t0"] = {
                        "side": "T0_SELL",
                        "sell_price": exec_price,
                        "sell_time": stats["total_lines"],
                        "volume": exec_volume
                    }
                stocks[code]["held_shares"] = shares

                if order_type in ("T0_BUYback", "T0_SELLback") and stocks[code]["active_t0"]:
                    active_t0 = stocks[code]["active_t0"]
                    if active_t0["side"] == "T0_SELL" and order_type == "T0_BUYback":
                        t0_pnl = (active_t0["sell_price"] - exec_price) * exec_volume
                        t0_pnl_pct = ((active_t0["sell_price"] - exec_price) / active_t0["sell_price"]) * 100 if active_t0["sell_price"] > 0 else 0.0
                        t0_trades.append({
                            "code": code,
                            "type": "SELL_FIRST",
                            "entry_price": active_t0["sell_price"],
                            "exit_price": exec_price,
                            "volume": exec_volume,
                            "pnl": t0_pnl,
                            "pnl_pct": t0_pnl_pct
                        })
                    elif active_t0["side"] == "T0_BUY" and order_type == "T0_SELLback":
                        t0_pnl = (exec_price - active_t0["buy_price"]) * exec_volume
                        t0_pnl_pct = ((exec_price - active_t0["buy_price"]) / active_t0["buy_price"]) * 100 if active_t0["buy_price"] > 0 else 0.0
                        t0_trades.append({
                            "code": code,
                            "type": "BUY_FIRST",
                            "entry_price": active_t0["buy_price"],
                            "exit_price": exec_price,
                            "volume": exec_volume,
                            "pnl": t0_pnl,
                            "pnl_pct": t0_pnl_pct
                        })
                    stocks[code]["active_t0"] = None

    return {
        "completed_trades": completed_trades,
        "t0_trades": t0_trades,
        "stats": stats
    }

def calculate_metrics(recon):
    trades = recon["completed_trades"]
    t0 = recon["t0_trades"]
    stats = recon["stats"]

    winners = [t for t in trades if t["pnl"] > 0]
    losers = [t for t in trades if t["pnl"] <= 0]

    win_rate = (len(winners) / len(trades) * 100) if trades else 0.0
    
    total_gain = sum(t["pnl"] for t in winners)
    total_loss = sum(abs(t["pnl"]) for t in losers)
    profit_factor = (total_gain / total_loss) if total_loss > 0 else (float('inf') if total_gain > 0 else 1.0)
    
    avg_return = sum(t["pnl_pct"] for t in trades) / len(trades) if trades else 0.0

    avg_win_pct = sum(t["pnl_pct"] for t in winners) / len(winners) if winners else 0.0
    avg_loss_pct = sum(abs(t["pnl_pct"]) for t in losers) / len(losers) if losers else 0.0
    win_loss_ratio = (avg_win_pct / avg_loss_pct) if avg_loss_pct > 0 else float('inf')

    # Calculate holding period in trading days
    dates_list = stats.get("dates_list", [])
    winner_holds = []
    loser_holds = []
    for t in trades:
        entry_date = t.get("entry_date")
        exit_date = t.get("exit_date")
        if entry_date and exit_date and entry_date in dates_list and exit_date in dates_list:
            hold_days = dates_list.index(exit_date) - dates_list.index(entry_date)
        else:
            hold_days = 0.0
        
        if t["pnl"] > 0:
            winner_holds.append(hold_days)
        else:
            loser_holds.append(hold_days)

    avg_winner_hold = sum(winner_holds) / len(winner_holds) if winner_holds else 0.0
    avg_loser_hold = sum(loser_holds) / len(loser_holds) if loser_holds else 0.0

    # Timing quality / deviations
    buy_devs = []
    sell_devs = []
    for t in trades:
        if t["bi_bottom"] > 0:
            buy_devs.append((t["entry_price"] - t["bi_bottom"]) / t["bi_bottom"] * 100)
        if t["bi_top"] > 0:
            sell_devs.append((t["bi_top"] - t["exit_price"]) / t["bi_top"] * 100)

    buy_dev_avg = sum(buy_devs) / len(buy_devs) if buy_devs else 0.0
    sell_dev_avg = sum(sell_devs) / len(sell_devs) if sell_devs else 0.0

    # T0 stats
    t0_winners = [t for t in t0 if t["pnl"] > 0]
    t0_win_rate = (len(t0_winners) / len(t0) * 100) if t0 else 0.0
    t0_total_pnl = sum(t["pnl"] for t in t0)

    return {
        "completed_count": len(trades),
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "avg_return": avg_return,
        "win_loss_ratio": win_loss_ratio,
        "avg_winner_hold": avg_winner_hold,
        "avg_loser_hold": avg_loser_hold,
        "buy_deviation_avg": buy_dev_avg,
        "sell_deviation_avg": sell_dev_avg,
        "t0_count": len(t0),
        "t0_win_rate": t0_win_rate,
        "t0_pnl": t0_total_pnl,
        "is_completed": stats["is_completed"],
        "data_warnings": stats["data_warnings"],
        "bi_count": stats["bi_count"],
        "segment_count": stats["segment_count"],
        "last_date": stats["last_date"]
    }

def build_markdown_report(log_path, metrics, recon):
    import datetime
    date_str = datetime.datetime.now().strftime("%Y-%m-%d")
    status = "已完成" if metrics["is_completed"] else "异常中断"
    
    # Generate recommendations
    recs = []
    if metrics["win_rate"] < 40.0:
        recs.append("- **优化建仓过滤**：目前交易胜率较低 (%.2f%%)，建议收紧盘前选股的日级趋势过滤器，剔除更多空头排列股票。" % metrics["win_rate"])
    else:
        recs.append("- **胜率良好**：当前建仓交易胜率达 %.2f%%，缠论笔二买/三买选股逻辑有效。" % metrics["win_rate"])

    if metrics["profit_factor"] < 1.2:
        recs.append("- **提升盈亏比**：利润因子较低 (%.2f)，应考虑引入移动止盈，或者优化 STOP_LOSS 阈值，避免单笔大额亏损吞噬利润。" % metrics["profit_factor"])
    else:
        recs.append("- **风控有效**：利润因子达 %.2f，表示单笔亏损控制得当，整体交易具备正向数学期望。" % metrics["profit_factor"])

    if metrics["buy_deviation_avg"] > 5.0:
        recs.append("- **买入延迟警告**：买入均价偏离笔底达 %.2f%%，可能存在 5m 分型确认迟缓或小级别 1m 精确买点未及时确认的问题。建议调小小级别过滤器确认阈值。" % metrics["buy_deviation_avg"])
    else:
        recs.append("- **低吸点精准**：买入均价紧贴笔底偏离仅 %.2f%%，入场时机非常精准。" % metrics["buy_deviation_avg"])

    if metrics["sell_deviation_avg"] > 5.0:
        recs.append("- **卖出迟钝警告**：卖出价格偏离笔顶 %.2f%%，表明 MACRO_SELL 或 3ClassSell 信号确认有滞后。建议优化 Lesson-44 熔断保护以加速出场速度。" % metrics["sell_deviation_avg"])
    else:
        recs.append("- **高抛点精准**：卖出偏离笔顶仅 %.2f%%，几乎出场在局部高点。" % metrics["sell_deviation_avg"])

    rec_str = "\n".join(recs)

    # Section 4 Diagnostic Conclusion
    win_loss_ratio = metrics["win_loss_ratio"]
    winner_hold = metrics["avg_winner_hold"]
    loser_hold = metrics["avg_loser_hold"]
    
    if win_loss_ratio > 1.5 and (winner_hold >= loser_hold or winner_hold == 0):
        trend_conclusion = "具备优异的趋势捕捉能力。表现为"盈大亏小"（盈亏比额度比 > 1.5）且持仓行为符合"让利润奔跑，截断亏损"原则。"
    elif win_loss_ratio < 1.0 or (winner_hold > 0 and winner_hold < loser_hold):
        trend_conclusion = "趋势捕捉能力偏弱。存在"抗单"或"提早止盈跑路"倾向（亏损交易的平均持仓时间长于盈利交易，或平均单笔盈利小于亏损）。建议检查止损阈值和移动止盈策略。"
    else:
        trend_conclusion = f"趋势捕捉能力中等。盈亏比为 {win_loss_ratio:.2f}，盈利与亏损持仓天数对比符合基本风控规范。"

    content = f"""# 缠论引擎回测表现评估报告
**评估日期**: {date_str}
**目标日志文件**: `{log_path}`
**回测状态**: {status} (末次基准日期: {metrics["last_date"]})

## 1. 引擎健康度与完整性诊断
| 诊断指标 | 数值 | 状态 |
|---|---|---|
| 解析日志总行数 | {recon["stats"]["total_lines"]} | OK |
| K线数据短缺警告次数 | {metrics["data_warnings"]} | {"存在警告" if metrics["data_warnings"] > 10 else "OK"} |
| 确认笔（Bi）总数 | {metrics["bi_count"]} | OK |
| 确认线段（Segment）总数 | {metrics["segment_count"]} | OK |

## 2. 信号与交易触发频率
- **INITIAL (建仓买入)**: {recon["stats"]["orders_triggered"].get("INITIAL", 0)}
- **MACRO_SELL (趋势平仓卖出)**: {recon["stats"]["orders_triggered"].get("MACRO_SELL", 0)}
- **STOP_LOSS (止损卖出)**: {recon["stats"]["orders_triggered"].get("STOP_LOSS", 0)}
- **MELTDOWN (Lesson-44 熔断卖出)**: {recon["stats"]["orders_triggered"].get("MELTDOWN", 0)}
- **RECOVER (移仓减仓卖出)**: {recon["stats"]["orders_triggered"].get("RECOVER", 0)}
- **T+0 日内套利完成轮数**: {metrics["t0_count"]}

## 3. 交易表现与胜率统计
| 统计指标 | 数值 |
|---|---|
| 完成建平仓交易笔数 | {metrics["completed_count"]} |
| 交易胜率 | {metrics["win_rate"]:.2f}% |
| 利润因子 (Profit Factor) | {metrics["profit_factor"]:.2f} |
| 单笔平均收益率 % | {metrics["avg_return"]:.2f}% |
| T+0 日内套利胜率 | {metrics["t0_win_rate"]:.2f}% |
| T+0 日内套利净收益贡献 | {metrics["t0_pnl"]:.2f} 元 |

## 4. 大趋势捕捉能力诊断
- **平均盈利/平均亏损额度比**: {metrics["win_loss_ratio"]:.2f}
- **盈利交易平均持仓时长**: {metrics["avg_winner_hold"]:.2f} 交易日
- **亏损交易平均持仓时长**: {metrics["avg_loser_hold"]:.2f} 交易日
- **诊断结论**: {trend_conclusion}

## 5. 交易执行价格质量 (低吸高抛时机分析)
- **建仓买入均价偏离向下笔底的比例**: {metrics["buy_deviation_avg"]:.2f}%
- **平仓卖出均价偏离向上笔顶的比例**: {metrics["sell_deviation_avg"]:.2f}%

## 6. 针对性优化建议
{rec_str}
"""
    return content

def main():
    import sys
    args = parse_args()
    try:
        log_path = get_target_log(args)
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)

    print(f"Scanning Chanlun backtest log file: {log_path}...")
    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    recon = reconstruct_trades(lines)
    metrics = calculate_metrics(recon)
    
    # Determine output report file name
    output_report_path = args.output
    if not output_report_path:
        base_name = os.path.basename(log_path).replace(".log", "")
        output_report_path = os.path.join("backtest_reports", f"backtest_evaluation_{base_name}.md")

    report_content = build_markdown_report(log_path, metrics, recon)
    # Ensure parent dir exists
    os.makedirs(os.path.dirname(os.path.abspath(output_report_path)), exist_ok=True)
    with open(output_report_path, "w", encoding="utf-8") as f:
        f.write(report_content)

    # Print summary to console
    print("\n" + "="*50)
    print("          缠论引擎评估诊断摘要 (Console Dashboard)")
    print("="*50)
    print(f"解析日志文件: {log_path}")
    print(f"回测状态: {'已完成' if metrics['is_completed'] else '异常中断'} (末次基准日期: {metrics['last_date']})")
    print(f"数据短缺警告次数: {metrics['data_warnings']}")
    print(f"确认笔/段数量: {metrics['bi_count']} / {metrics['segment_count']}")
    print(f"建平仓交易笔数: {metrics['completed_count']} | 交易胜率: {metrics['win_rate']:.2f}%")
    print(f"交易利润因子 (Profit Factor): {metrics['profit_factor']:.2f}")
    print(f"日内 T+0 交易套利次数: {metrics['t0_count']} | T+0 胜率: {metrics['t0_win_rate']:.2f}%")
    print(f"买入贴底偏离度: {metrics['buy_deviation_avg']:.2f}% | 卖出贴顶偏离度: {metrics['sell_deviation_avg']:.2f}%")
    print(f"评估报告已成功保存至: [Markdown Report](file:///{os.path.abspath(output_report_path).replace(os.sep, '/')})")
    print("="*50 + "\n")

if __name__ == "__main__":
    main()
