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
        区间套联动校验：两级共振独立尝试，任意一级检测到背驰+方向确认即返回共振信号。

        两级共振策略：
          1. 宏级别引擎（engine_5m）：线段/中枢做背驰检测，micro 引擎做精确确认
          2. 微级别引擎（engine_1m）：线段/中枢做背驰检测，macro 引擎做方向确认
          两级独立尝试，互不阻塞。两级都失败时才回退到单级别几何信号。

        设计要点：强单边趋势中宏级别线段形成缓慢且背驰难以触发，
        微级别数据更丰富，可作为独立共振来源。
        """
        # 提取 helper：对给定引擎和确认方向做一次背驰检测
        def _try_resonance(seg_engine, confirm_engine, close_history, latest_skline, tag):
            """返回共振信号或 None"""
            if (seg_engine.active_segment is None
                    or seg_engine.current_zhongshu is None
                    or len(seg_engine.confirmed_segments) < 3):
                return None

            cached_hist = getattr(seg_engine, "macd_hist", None)
            hist = cached_hist if cached_hist else self.divergence_engine.compute_macd_hist(close_history)

            s1 = seg_engine.confirmed_segments[-3]
            s3 = seg_engine.confirmed_segments[-1]

            b_start = s1.start_bi.start.index
            b_end = s1.current_end_bi.end.index
            c_start = s3.start_bi.start.index
            c_end = latest_skline.index

            if c_end >= len(hist) or b_end >= len(hist):
                return None

            areas_b = self.divergence_engine.calculate_wave_areas(hist, b_start, b_end)
            areas_c = self.divergence_engine.calculate_wave_areas(hist, c_start, c_end)

            is_divergent = sum(areas_c) < sum(areas_b)
            if not is_divergent:
                return None

            confirm_signal = confirm_engine.get_latest_signal()
            final_signal = None
            if seg_engine.active_segment.direction == "UP" and confirm_signal in ("1m_1ClassSell", "3ClassSell"):
                final_signal = "CROSS_1ClassSell"
            elif seg_engine.active_segment.direction == "DOWN" and confirm_signal in ("1m_1ClassBuy", "RETAIL_2BUY", "RETAIL_3BUY"):
                final_signal = "CROSS_1ClassBuy"

            if final_signal:
                se = "macro" if seg_engine is self.engine_5m else "micro"
                print(f"[SCANNER] [{tag}] {self.stock_code} | "
                      f"{se}-level s1 (bi {b_start}→{b_end}) MACD area={sum(areas_b):.4f} | "
                      f"s3 (bi {c_start}→{c_end}) MACD area={sum(areas_c):.4f} | "
                      f"Divergent={is_divergent} | confirm_signal={confirm_signal} -> {final_signal}")
            return final_signal

        # ──── 第 1 级：宏级别引擎共振 ────
        result = _try_resonance(
            self.engine_5m, self.engine_1m,
            close_5m, latest_5m_skline, "MACRO"
        )
        if result:
            return result

        # ──── 第 2 级：微级别引擎共振（独立尝试，不依赖宏级别结果） ────
        close_1m = self.engine_1m.get_close_history()
        latest_1m = self.engine_1m.sklines[-1] if self.engine_1m.sklines else None
        if latest_1m is not None:
            result = _try_resonance(
                self.engine_1m, self.engine_5m,
                close_1m, latest_1m, "MICRO"
            )
            if result:
                return result

        # 两级都失败 → 回退单级别几何信号
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
        # TODO: 1m_3ClassSell 是计划中的微级别三卖信号（反弹无力回到中枢下方），
        # 比 1m_1ClassSell 更适合作为熔断触发条件。当前 ChanEngine 卖点端尚未
        # 实现微级别一卖/三卖的区分逻辑（参见 _update_geometry_topology:483），
        # 仅产出 1m_1ClassSell。实现后可将熔断门槛从"任意微级别卖点"收紧为
        # "仅三卖"，减少假熔断。
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