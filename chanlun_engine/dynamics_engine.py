import numpy as np

class DynamicsDivergenceEngine:
    """
    MACD 背驰引擎。

    两套 API：
      - 增量 update_macd(close_price): O(1) 增量更新，用于实盘/回测主路径
      - 全量 compute_macd_hist(close_prices): O(n) 重算，用于测试与一次性补算
    两套 API 输出数学上等价。
    """
    def __init__(self, tick_decimals=2):
        self.tick_decimals = tick_decimals
        self._reset_state()

    def _reset_state(self):
        self._ema12 = None
        self._ema26 = None
        self._dea = None
        self._count = 0

    def update_macd(self, close_price):
        """
        增量更新：每来一根 close，返回对应的 hist 值。
        与 batch compute_macd_hist 行为等价（EMA 从 bar 0 初始化为 prices[0]，hist 从 bar 0 起计算）。
        """
        self._count += 1
        alpha12 = 2.0 / 13.0
        alpha26 = 2.0 / 27.0
        alpha_dea = 2.0 / 10.0
        if self._ema12 is None:
            # 第一根：EMA 直接等于价格；DIF = 0；hist = 0
            self._ema12 = close_price
            self._ema26 = close_price
            self._dea = 0.0
            return 0.0
        self._ema12 = close_price * alpha12 + self._ema12 * (1 - alpha12)
        self._ema26 = close_price * alpha26 + self._ema26 * (1 - alpha26)
        dif = self._ema12 - self._ema26
        self._dea = dif * alpha_dea + self._dea * (1 - alpha_dea)
        return 2.0 * (dif - self._dea)

    def _calc_ema(self, prices, period):
        ema = np.zeros_like(prices)
        if len(prices) == 0:
            return ema
        ema[0] = prices[0]
        alpha = 2.0 / (period + 1)
        for i in range(1, len(prices)):
            ema[i] = prices[i] * alpha + ema[i-1] * (1 - alpha)
        return ema

    def compute_macd_hist(self, close_prices):
        """计算 MACD 柱状图 (Vectorized NumPy 实现)"""
        prices = np.array(close_prices, dtype=float)
        if len(prices) < 26:
            return np.zeros_like(prices)
        ema12 = self._calc_ema(prices, 12)
        ema26 = self._calc_ema(prices, 26)
        dif = ema12 - ema26
        dea = self._calc_ema(dif, 9)
        hist = 2.0 * (dif - dea)
        return hist.tolist()

    def calculate_wave_areas(self, hist, start_idx, end_idx):
        """
        根据标准 K 线索引，将 hist 划分为红绿交替的"单波浪（Wave）"，计算各自面积 (Vectorized NumPy)
        """
        segment_hist = hist[start_idx:end_idx+1]
        if len(segment_hist) == 0:
            return [0.0]

        waves = []
        current_wave = []
        
        # 按柱子正负号划分单波浪
        for val in segment_hist:
            if not current_wave:
                current_wave.append(val)
            else:
                if (val >= 0 and current_wave[-1] >= 0) or (val < 0 and current_wave[-1] < 0):
                    current_wave.append(val)
                else:
                    waves.append(current_wave)
                    current_wave = [val]
        if current_wave:
            waves.append(current_wave)

        wave_areas = []
        # 已绝对走完的波浪取真实面积
        for wave in waves[:-1]:
            wave_areas.append(float(np.sum(np.abs(wave))))

        # 采用线性衰减面积外推法，计算尾端未完结单波的预测面积
        last_wave = waves[-1]
        raw_last_area = np.sum(np.abs(last_wave))
        
        if len(last_wave) >= 2:
            h_p = abs(last_wave[-1])
            h_prev = abs(last_wave[-2])
            D = h_prev - h_p  # 柱体收编斜率
            
            if D > 0:
                # 预测剩余衰减三角形面积：(h_p ** 2) / (2 * 变化斜率)
                remaining_area = (h_p ** 2) / (2.0 * D)
                remaining_area = min(remaining_area, raw_last_area)
                wave_areas.append(float(raw_last_area + remaining_area))
            else:
                # 未回缩，直接使用累加面积
                wave_areas.append(float(raw_last_area))
        else:
            wave_areas.append(float(raw_last_area))

        return wave_areas


class MultiTimeframeScanner:
    def __init__(self, stock_code, engine_5m, engine_1m):
        self.stock_code = stock_code
        self.engine_5m = engine_5m  # 5m 形态引擎
        self.engine_1m = engine_1m  # 1m 形态引擎
        self.divergence_engine = DynamicsDivergenceEngine()

    def check_interval_套_convergence(self, close_5m, latest_5m_skline, geo_signal_5m="NO_SIGNAL"):
        """
        区间套联动校验：以 5m 真实中枢进入段 s1 与离开段 s3 的真实标准 K 线索引范围切片进行 MACD 面积计算。
        优先使用 engine.macd_hist（增量维护，O(1) 读取）；回退到全量 compute_macd_hist 用于无 hist 缓存的场景。

        跨周期共振条件不满足（无中枢 / 段数 < 3）时，回退到 5m 单级别信号
        （由调用方通过 geo_signal_5m 传入）。这样即便 SegmentEngine 在真实数据上
        长期卡在 active 状态（confirmed_segments 很少），FSM 仍能基于单级别
        几何信号下单，先验证信号链路。
        """
        cached_hist = getattr(self.engine_5m, "macd_hist", None)
        hist_5m = cached_hist if cached_hist else self.divergence_engine.compute_macd_hist(close_5m)

        if not self.engine_5m.active_segment or not self.engine_5m.current_zhongshu or len(self.engine_5m.confirmed_segments) < 3:
            return geo_signal_5m

        s1 = self.engine_5m.confirmed_segments[-3]  # 进入中枢前的段
        s3 = self.engine_5m.confirmed_segments[-1]  # 离开中枢的段

        b_start = s1.start_bi.start.index
        b_end = s1.current_end_bi.end.index

        c_start = s3.start_bi.start.index
        c_end = latest_5m_skline.index

        if c_end >= len(hist_5m) or b_end >= len(hist_5m):
            return "NO_SIGNAL"

        areas_b = self.divergence_engine.calculate_wave_areas(hist_5m, b_start, b_end)
        areas_c = self.divergence_engine.calculate_wave_areas(hist_5m, c_start, c_end)

        is_5m_divergent_pending = sum(areas_c) < sum(areas_b)

        if is_5m_divergent_pending:
            signal_1m = self.engine_1m.get_latest_signal()
            final_signal = "NO_SIGNAL"
            if self.engine_5m.active_segment.direction == "UP" and signal_1m == "1m_1ClassSell":
                final_signal = "CROSS_1ClassSell"
            elif self.engine_5m.active_segment.direction == "DOWN" and signal_1m == "1m_1ClassBuy":
                final_signal = "CROSS_1ClassBuy"
            
            if final_signal != "NO_SIGNAL":
                print(f"[SCANNER] 5m+1m Scanner Check: {self.stock_code} | Entering Segment s1 (start={b_start}, end={b_end}) MACD area={sum(areas_b):.4f} | Leaving Segment s3 (start={c_start}, end={c_end}) MACD area={sum(areas_c):.4f} | 5m Divergent={is_5m_divergent_pending} | 1m Signal={signal_1m} -> Final Signal={final_signal}")
                return final_signal

        return geo_signal_5m

class SmallToLargeGateway:
    def __init__(self, divergence_engine):
        self.divergence_engine = divergence_engine

    def check_meltdown_trigger(self, engine_5m, engine_1m, close_1m, latest_1m_skline):
        """
        小转大防御网关判定：对齐第 44 课，如果离开段 c 下砸的动能超过进入段 b (面积不背驰)，启动小转大平仓熔断。
        优先使用 engine.macd_hist；回退到全量 compute_macd_hist。
        """
        if not engine_5m.current_zhongshu or not engine_5m.active_segment:
            return False

        # 只在 5m 处于上升离开段时检测潜在的向下崩塌风险
        if engine_5m.active_segment.direction != "UP":
            return False

        latest_1m_signal = engine_1m.get_latest_signal()
        if latest_1m_signal not in ["1m_3ClassSell", "1m_1ClassSell"]:
            return False

        cached_hist = getattr(engine_1m, "macd_hist", None)
        hist_1m = cached_hist if cached_hist else self.divergence_engine.compute_macd_hist(close_1m)
        if len(engine_1m.confirmed_segments) < 1:
            return False

        # 提取 1m 上的前一个下行线段 (进入段 b)
        prev_down_seg = [s for s in engine_1m.confirmed_segments if s.direction == "DOWN"][-1]

        # 当前活动的 1m 下行段 (离开段 c)
        curr_down_seg = engine_1m.active_segment
        if curr_down_seg.direction != "DOWN":
            return False

        b_start = prev_down_seg.start_bi.start.index
        b_end = prev_down_seg.current_end_bi.end.index

        c_start = curr_down_seg.start_bi.start.index
        c_end = latest_1m_skline.index

        if c_end >= len(hist_1m) or b_end >= len(hist_1m):
            return False

        wave_areas_b = self.divergence_engine.calculate_wave_areas(hist_1m, b_start, b_end)
        wave_areas_c = self.divergence_engine.calculate_wave_areas(hist_1m, c_start, c_end)

        # 如果离开段 c 动能大（无背驰），判定小转大熔断成立
        triggered = sum(wave_areas_c) >= sum(wave_areas_b)
        if triggered:
            print(f"[MELTDOWN] Meltdown Guard Check: {getattr(engine_5m, 'stock_code', '')} | 1m Signal={latest_1m_signal} | 1m Entering Segment b (start={b_start}, end={b_end}) MACD area={sum(wave_areas_b):.4f} | Leaving Segment c (start={c_start}, end={c_end}) MACD area={sum(wave_areas_c):.4f} | Meltdown Triggered={triggered}")
        return triggered
