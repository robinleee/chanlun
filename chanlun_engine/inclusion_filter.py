from collections import deque

class StandardKLine:
    def __init__(self, index, timestamp, high, low, close, direction):
        self.index = index
        self.timestamp = timestamp
        self.high = high
        self.low = low
        self.close = close
        self.direction = direction  # "UP" or "DOWN"

class KLineInclusionFilter:
    def __init__(self, tick_decimals=2, max_len=2000):
        self.tick_decimals = tick_decimals
        self.sklines = deque(maxlen=max_len)
        self.index_counter = 0

    def push_bar(self, timestamp, raw_high, raw_low, raw_close):
        """
        Stream raw bar, execute rounding precision and inclusion merge.
        Close is preserved on the standard K-line so downstream consumers
        (MACD engine, equity valuation) can read close prices without
        re-fetching from raw history. When two raw bars are merged via
        inclusion, the latest raw close wins.
        """
        high = round(raw_high, self.tick_decimals)
        low = round(raw_low, self.tick_decimals)
        close = round(raw_close, self.tick_decimals)

        if not self.sklines:
            self.sklines.append(StandardKLine(self.index_counter, timestamp, high, low, close, "UP"))
            self.index_counter += 1
            return self.sklines[-1]

        if len(self.sklines) == 1:
            last_sk = self.sklines[-1]
            is_inc = (high <= last_sk.high and low >= last_sk.low) or (high >= last_sk.high and low <= last_sk.low)
            if is_inc:
                last_sk.high = max(last_sk.high, high)
                last_sk.low = max(last_sk.low, low)
                last_sk.close = close
                last_sk.direction = "UP"
            else:
                direction = "UP" if high > last_sk.high else "DOWN"
                self.sklines.append(StandardKLine(self.index_counter, timestamp, high, low, close, direction))
                self.index_counter += 1
            return self.sklines[-1]

        last_sk = self.sklines[-1]
        is_inc = (high <= last_sk.high and low >= last_sk.low) or (high >= last_sk.high and low <= last_sk.low)

        if is_inc:
            if last_sk.direction == "UP":
                last_sk.high = max(last_sk.high, high)
                last_sk.low = max(last_sk.low, low)
            else:
                last_sk.high = min(last_sk.high, high)
                last_sk.low = min(last_sk.low, low)
            last_sk.close = close
        else:
            direction = "UP" if high > last_sk.high else "DOWN"
            self.sklines.append(StandardKLine(self.index_counter, timestamp, high, low, close, direction))
            self.index_counter += 1

        return self.sklines[-1]
