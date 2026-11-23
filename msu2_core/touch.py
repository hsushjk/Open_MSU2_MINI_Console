"""触摸手势识别：迟滞 + 去抖 + 单击/双击/长按判定。

采样来自设备 class 8 的触摸通道，值越小表示越接近（电容耦合让读数下降）。
把判定逻辑独立出来，方便离线用假数据跑时序测试。
"""

KIND_SINGLE = "single"
KIND_DOUBLE = "double"
KIND_LONG = "long"


class TouchGestures:
    def __init__(self, threshold=60, long_press=0.8, double_window=0.35,
                 min_gap=0.04, release_ratio=0.5, debounce=2,
                 base_track=0.002, calibrate=6):
        self.threshold = float(threshold)
        self.long_press = float(long_press)
        self.double_window = float(double_window)
        self.min_gap = float(min_gap)
        self.release_ratio = float(release_ratio)
        self.debounce = max(1, int(debounce))
        self.base_track = float(base_track)
        self.calibrate = max(1, int(calibrate))

        self.baseline = None
        self._cal = []
        self.pressed = False
        self._streak = 0
        self._press_ts = 0.0
        self._long_fired = False
        self._waiting_double = False
        self._release_ts = 0.0
        self._second = False
        self.samples = 0

    # ------------------------------------------------------------------
    def reset(self):
        self._cal = []
        self.baseline = None
        self.pressed = False
        self._streak = 0
        self._press_ts = 0.0
        self._long_fired = False
        self._waiting_double = False
        self._release_ts = 0.0
        self._second = False

    @property
    def threshold_high(self):
        return self.baseline - self.threshold if self.baseline is not None else None

    def feed(self, value, now):
        """喂一个采样，返回本次产生的手势列表（0 或 1 个元素）。"""
        if value is None:
            return []
        self.samples += 1
        if value is False:
            return []

        if self.baseline is None:
            self._cal.append(value)
            if len(self._cal) >= self.calibrate:
                self.baseline = min(self._cal)
            return []

        base = self.baseline
        band = self.threshold * self.release_ratio if self.pressed else self.threshold
        raw = value < (base - band)

        if raw == self.pressed:
            self._streak = 0
            if not self.pressed:
                self._idle_track(value)
                return self._timeout(now)
            return self._hold(now)

        self._streak += 1
        if self._streak < self.debounce:
            return []
        self._streak = 0
        self.pressed = raw
        if raw:
            return self._on_press(now)
        return self._on_release(now)

    # ------------------------------------------------------------------
    def _idle_track(self, value):
        if self.baseline is None:
            return
        if value > self.baseline - self.threshold:
            self.baseline += (value - self.baseline) * self.base_track

    def _hold(self, now):
        if (not self._long_fired and
                now - self._press_ts >= self.long_press):
            self._long_fired = True
            self._second = False
            self._waiting_double = False
            return [KIND_LONG]
        return self._timeout(now)

    def _timeout(self, now):
        if (self._waiting_double and
                now - self._release_ts > self.double_window):
            self._waiting_double = False
            return [KIND_SINGLE]
        return []

    def _on_press(self, now):
        self._press_ts = now
        self._long_fired = False
        if (self._waiting_double and
                self.min_gap <= now - self._release_ts <= self.double_window):
            # 第二次按下就判定双击，短促连击也不会漏
            self._second = True
            self._waiting_double = False
        else:
            self._second = False
            if now - self._release_ts > self.double_window:
                self._waiting_double = False
        return []

    def _on_release(self, now):
        if self._long_fired:
            self._second = False
            return []
        if self._second:
            self._second = False
            return [KIND_DOUBLE]
        self._waiting_double = True
        self._release_ts = now
        return []