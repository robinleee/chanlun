from enum import Enum

class SegmentStatus(Enum):
    ACTIVE = "ACTIVE"
    PENDING_BREAK = "PENDING_BREAK"

class Fractal:
    def __init__(self, index, f_type, high, low, is_confirmed=False):
        self.index = index
        self.type = f_type  # "TOP" 或 "BOTTOM"
        self.high = high
        self.low = low
        self.is_confirmed = is_confirmed

    def __eq__(self, other):
        if not isinstance(other, Fractal):
            return False
        return self.index == other.index and self.type == other.type

class Bi:
    def __init__(self, start_fractal, end_fractal, direction):
        self.start = start_fractal  # Fractal 对象
        self.end = end_fractal      # Fractal 对象
        self.direction = direction  # "UP" (底->顶) 或 "DOWN" (顶->底)
        self.high = max(start_fractal.high, end_fractal.high)
        self.low = min(start_fractal.low, end_fractal.low)

    def __eq__(self, other):
        if not isinstance(other, Bi):
            return False
        return self.start == other.start and self.end == other.end and self.direction == other.direction

class StrictBiValidator:
    @staticmethod
    def validate(top_fractal, bottom_fractal):
        """
        验证严格笔刚性要求：顶底顶点索引差 >= 4。
        注：abs(idx_diff) >= 4 已蕴含"两分型间至少夹一根独立标准 K 线"，
        故无需额外的边缘重叠检查。
        """
        return abs(top_fractal.index - bottom_fractal.index) >= 4

class Segment:
    def __init__(self, start_bi, direction):
        self.start_bi = start_bi
        self.direction = direction       # "UP" 或 "DOWN"
        self.status = SegmentStatus.ACTIVE
        self.bi_list = [start_bi]        # 线段包含的笔列表
        self.end_bi = None               # 只有确认终结时才固化此值

    @property
    def current_end_bi(self):
        """动态追踪未完结线段的终点笔"""
        return self.end_bi if self.end_bi is not None else self.bi_list[-1]

    @property
    def extreme_price(self):
        """线段运行中的最高/最低点（极值转折点）"""
        if self.direction == "UP":
            return max(bi.end.high for bi in self.bi_list if bi.direction == "UP")
        else:
            return min(bi.end.low for bi in self.bi_list if bi.direction == "DOWN")


class SegmentEngine:
    def __init__(self):
        self.active_segment = None
        self.confirmed_segments = []
        # 已消费到 global_bi_list 的索引下标，避免重复扫描
        self._last_consumed_idx = 0

    def process_feature_sequence(self, bi_list, direction):
        """
        提取特征序列笔并消除包含关系
        """
        raw_elements = [bi for bi in bi_list if bi.direction != direction]
        if not raw_elements:
            return []

        clean_elements = []
        fs_direction = "DOWN" if direction == "UP" else "UP"
        
        for element in raw_elements:
            v_high = element.high
            v_low = element.low
            
            if not clean_elements:
                clean_elements.append({"high": v_high, "low": v_low})
                continue
                
            last_el = clean_elements[-1]
            is_inc = (v_high <= last_el["high"] and v_low >= last_el["low"]) or \
                     (v_high >= last_el["high"] and v_low <= last_el["low"])
            
            if is_inc:
                if fs_direction == "UP":
                    last_el["high"] = max(last_el["high"], v_high)
                    last_el["low"] = max(last_el["low"], v_low)
                else:
                    last_el["high"] = min(last_el["high"], v_high)
                    last_el["low"] = min(last_el["low"], v_low)
            else:
                clean_elements.append({"high": v_high, "low": v_low})
                
        return clean_elements

    def update_segments(self, global_bi_list):
        """
        流式线段破坏状态机接口：严格对齐第 71/81 课，无死锁、无污染
        """
        if len(global_bi_list) < 3:
            return

        if self.active_segment and len(global_bi_list) == self._last_consumed_idx:
            return

        if not self.active_segment:
            self.active_segment = Segment(global_bi_list[0], global_bi_list[0].direction)
            self.active_segment.bi_list = [global_bi_list[0], global_bi_list[1], global_bi_list[2]]
            return

        seg = self.active_segment

        # 实时将新确认的笔追加到当前活动线段中（仅扫描增量，避免 O(n²)）
        for i in range(self._last_consumed_idx, len(global_bi_list)):
            bi = global_bi_list[i]
            if bi.start.index >= seg.start_bi.start.index:
                seg.bi_list.append(bi)
        self._last_consumed_idx = len(global_bi_list)

        latest_bi = global_bi_list[-1]

        is_break_extreme = (seg.direction == "UP" and latest_bi.high > seg.extreme_price) or \
                           (seg.direction == "DOWN" and latest_bi.low < seg.extreme_price)
        if is_break_extreme:
            seg.status = SegmentStatus.ACTIVE
            return

        elements = self.process_feature_sequence(seg.bi_list, seg.direction)
        if len(elements) < 3:
            return

        is_reverse_established = self._check_reverse_fractal(elements, seg.direction)
        
        if is_reverse_established:
            extreme_bi = self._find_extreme_bi(seg.bi_list, seg.direction)
            seg.end_bi = extreme_bi
            self.confirmed_segments.append(seg)
            print(f"[GEOMETRY] Confirmed New Segment: {getattr(self, 'stock_code', '')} [{getattr(self, 'period', '')}] | Direction={seg.direction} | Start Bi start index={seg.start_bi.start.index} | End Bi end index={seg.current_end_bi.end.index} | Extreme Price={seg.extreme_price:.2f} | Confirmed Segment count={len(self.confirmed_segments)}")

            remaining_bis = [b for b in global_bi_list if b.start.index > extreme_bi.start.index]

            if remaining_bis:
                new_start_bi = remaining_bis[0]
                self.active_segment = Segment(new_start_bi, new_start_bi.direction)
                self.active_segment.bi_list = remaining_bis
                # 新线段从当前 bis 集合尾部开始，后续追加 of bi 都是新的
                self._last_consumed_idx = len(global_bi_list)
            else:
                self.active_segment = None
                self._last_consumed_idx = 0

    def _check_reverse_fractal(self, clean_fs_elements, direction):
        """
        特征序列双轨破坏判定算法：扫描整个特征序列，判断是否存在满足破坏条件的分型与缺口规则。
        """
        for k in range(1, len(clean_fs_elements) - 1):
            x1, x2, x3 = clean_fs_elements[k-1], clean_fs_elements[k], clean_fs_elements[k+1]
            
            if direction == "UP":
                # 向上线段结束转折向下：向下特征序列元素 x2 为局部最高点（顶分型）
                is_fractal = (x2["high"] > x1["high"] and x2["high"] > x3["high"]) and \
                             (x2["low"] > x1["low"] and x2["low"] > x3["low"])
                if is_fractal:
                    # 检查是否有缺口（第一种情况）
                    has_gap = x2["low"] > x1["high"]
                    if has_gap:
                        return True
                    else:
                        # 第二种情况：后三项不能创新高
                        if len(clean_fs_elements) >= k + 4:
                            rebound_max = max(el["high"] for el in clean_fs_elements[k+1:k+4])
                            if rebound_max <= max(x1["high"], x3["high"]):
                                return True
            else:
                # 向下线段结束转折向上：向上特征序列元素 x2 为局部最低点（底分型）
                is_fractal = (x2["high"] < x1["high"] and x2["high"] < x3["high"]) and \
                             (x2["low"] < x1["low"] and x2["low"] < x3["low"])
                if is_fractal:
                    # 检查是否有缺口（第一种情况）
                    has_gap = x2["high"] < x1["low"]
                    if has_gap:
                        return True
                    else:
                        # 第二种情况：后三项不能创新低
                        if len(clean_fs_elements) >= k + 4:
                            pullback_min = min(el["low"] for el in clean_fs_elements[k+1:k+4])
                            if pullback_min >= min(x1["low"], x3["low"]):
                                return True
        return False

    def _find_extreme_bi(self, bi_list, direction):
        """寻找线段极值点所在的笔"""
        if direction == "UP":
            max_val = -float('inf')
            extreme_bi = None
            for bi in bi_list:
                if bi.direction == "UP" and bi.end.high > max_val:
                    max_val = bi.end.high
                    extreme_bi = bi
            return extreme_bi
        else:
            min_val = float('inf')
            extreme_bi = None
            for bi in bi_list:
                if bi.direction == "DOWN" and bi.end.low < min_val:
                    min_val = bi.end.low
                    extreme_bi = bi
            return extreme_bi

class Zhongshu:
    def __init__(self, ZD, ZG, DD, GG, level="5m"):
        self.ZD = ZD
        self.ZG = ZG
        self.DD = DD
        self.GG = GG
        self.level = level

class ZhongshuFSM:
    def __init__(self):
        self.active_zhongshu = None
        self.zhongshu_history = []
        self._last_processed_seg_count = 0

    def update_zhongshu(self, segment_list):
        """
        中枢矩阵计算与状态转移（使用修正后的线段首尾极值提取逻辑）
        """
        if len(segment_list) < 3:
            return

        if len(segment_list) == self._last_processed_seg_count:
            return

        self._last_processed_seg_count = len(segment_list)

        s1, s2, s3 = segment_list[-3], segment_list[-2], segment_list[-1]
        
        # 提取线段 1 的首尾真实价格区间 (s1.start_bi.start 与 s1.end_bi.end)
        d1 = min(s1.start_bi.start.low, s1.current_end_bi.end.low)
        g1 = max(s1.start_bi.start.high, s1.current_end_bi.end.high)
        
        # 提取线段 2 的首尾真实价格区间
        d2 = min(s2.start_bi.start.low, s2.current_end_bi.end.low)
        g2 = max(s2.start_bi.start.high, s2.current_end_bi.end.high)
        
        # 提取线段 3 的首尾真实价格区间
        d3 = min(s3.start_bi.start.low, s3.current_end_bi.end.low)
        g3 = max(s3.start_bi.start.high, s3.current_end_bi.end.high)
        
        # 计算核心中枢重叠价格区间
        ZD = max(d1, d2, d3)
        ZG = min(g1, g2, g3)

        if ZD < ZG:
            # 计算波动极值
            DD = min(d1, d2, d3)
            GG = max(g1, g2, g3)
            new_zs = Zhongshu(ZD, ZG, DD, GG)
            
            if not self.active_zhongshu:
                self.active_zhongshu = new_zs
                print(f"[GEOMETRY] Confirmed New Zhongshu: {getattr(self, 'stock_code', '')} [{getattr(self, 'period', '')}] | Level={new_zs.level} | ZD={ZD:.2f} | ZG={ZG:.2f} | DD={DD:.2f} | GG={GG:.2f}")
            else:
                prev_zs = self.active_zhongshu
                # 判定中枢新生与级别扩张的物理交尾边界。
                # 注：spec §2.5 原文只列了上行趋势的 DD_2 <= GG_1；此处使用
                # [DD, GG] 双向重叠判定，等价于 spec 条件的严格超集，同时覆盖
                # 上下行两种方向的中枢扩张场景。
                is_overlap = (new_zs.DD <= prev_zs.GG) and (new_zs.GG >= prev_zs.DD)
                
                if is_overlap:
                    # 触发中枢级别扩张 (Level Expansion)
                    self.active_zhongshu = self._upgrade_level(prev_zs, new_zs)
                    print(f"[GEOMETRY] Zhongshu Level Expansion: {getattr(self, 'stock_code', '')} [{getattr(self, 'period', '')}] | Level={self.active_zhongshu.level} | ZD={self.active_zhongshu.ZD:.2f} | ZG={self.active_zhongshu.ZG:.2f} | DD={self.active_zhongshu.DD:.2f} | GG={self.active_zhongshu.GG:.2f}")
                else:
                    # 确立中枢新生 (Trend New-generation)
                    self.zhongshu_history.append(prev_zs)
                    self.active_zhongshu = new_zs
                    print(f"[GEOMETRY] Confirmed New Zhongshu (Trend New-generation): {getattr(self, 'stock_code', '')} [{getattr(self, 'period', '')}] | Level={new_zs.level} | ZD={ZD:.2f} | ZG={ZG:.2f} | DD={DD:.2f} | GG={GG:.2f}")

    def _upgrade_level(self, prev_zs, new_zs):
        """中枢合并升级为大一层级中枢"""
        new_ZD = min(prev_zs.ZD, new_zs.ZD)
        new_ZG = max(prev_zs.ZG, new_zs.ZG)
        new_DD = min(prev_zs.DD, new_zs.DD)
        new_GG = max(prev_zs.GG, new_zs.GG)
        return Zhongshu(new_ZD, new_ZG, new_DD, new_GG, level="30m")
