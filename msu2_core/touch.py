"""触摸手势识别：迟滞 + 去抖 + 单击/双击/长按判定。

采样来自设备 class 8 的触摸通道，值越小表示越接近（电容耦合让读数下降）。
判定逻辑独立在这里，方便用假数据离线跑时序测试（tools/test_touch.py）。

双击的判定点在“第二次按下”这一拍，不等第二次抬手，所以短促连击不会漏；
抬手只需要 1 个采样就能被看见（按下要 2 个采样去抖），避免两次点击之间
手指只离屏一瞬、被当成一次长按。
"""

KIND_SINGLE = "single"
KIND_DOUBLE = "double"
KIND_LONG = "long"


class TouchGestures:
    def __init__(self, threshold=60, long_press=0.8, double_window=0.5,
                 min_gap=0.012, release_ratio=0.3, debounce=2,
                 release_debounce=1, base_track=0.002, calibrate=6):
        self.threshold = float(threshold)
        self.long_press = float(long_press)
        self.double_window = float(double_window)
        self.min_gap = float(min_gap)
        self.release_ratio = float(release_ratio)
        self.debounce = max(1, int(debounce))
        self.release_debounce = max(1, int(release_debounce))
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
        if value is None or value is False:
            return []
        self.samples += 1

        # 0 是“设备还没有转换结果”的占位值（异步取值的第一拍），
        # 不能当真实读数参与标定，否则基准会被拉到 0 附近、永远判不出按下。
        if value <= 0 and (self.baseline is None or self.baseline > 500):
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

        # 状态翻转：按下要连续 debounce 拍，松开只要 release_debounce 拍
        self._streak += 1
        if self._streak < (self.release_debounce if self.pressed else self.debounce):
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
        """返回列表：落在这个窗口外时，先把上一次的单击补发出去再算新的一下。"""
        gap = now - self._release_ts
        self._press_ts = now
        self._long_fired = False
        pending = []
        if self._waiting_double and self.min_gap <= gap <= self.double_window:
            # 第二次按下就判定双击，短促连击也不会漏
            self._second = True
            self._waiting_double = False
        else:
            if self._waiting_double:
                # 间隔太短（触点抖动）或太长（其实是两下）：都要把上一次的
                # 单击补发掉，绝不能默默丢掉——那正是“双击被吞成单击”的来源。
                if gap > self.double_window:
                    pending.append(KIND_SINGLE)
                self._waiting_double = False
            self._second = False
        return pending

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