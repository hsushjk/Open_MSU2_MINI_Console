"""多设备推流：一台设备一个输出通道（DeviceChannel）。

一个 Scheduler 只渲染/合成一次，然后把同一张图交给每个通道；通道自己负责
“按这台设备的显示设置后处理 + 排队 + 在独立线程里写串口”：

* 单台设备时用 sync 模式 —— submit() 直接在渲染线程里同步发完，适合单设备
  与测试路径。
* 多台设备时用 async 模式 —— 每台一个发送线程 + 一个深度 2 的队列（满了
  丢最旧的一帧，永远发最新的画面）。一台设备慢、掉线、卡住只影响它自己的
  线程，渲染循环和其它设备照常跑，所以不会互相拖住。
* 每台的亮度（display.dim）和 180° 翻转（reverse）在这里做后处理，所以
  同一份内容可以在亮度/翻转设置不同的屏上显示；朝向是内容级设置（不同
  朝向的设备没法共用同一份内容，由上层决定并告警）。
* 串口写始终走各自 MiniDevice/SerialTransport 的 _lock（show_image 内部
  已经整帧独占该锁）。不同设备之间没有共享锁，互不阻塞。
"""

from __future__ import annotations

import collections
import logging
import threading
import time

log = logging.getLogger("msu2")

# 异步通道的队列深度：2 帧足够吸收渲染抖一下，再多只是增加延迟。
QUEUE_DEPTH = 2
# 帧率统计窗口（秒）
RATE_WINDOW = 3.0
# 连不上时重试/重扫的节流间隔（秒）
REDISCOVER_INTERVAL = 3.0
RECONNECT_INTERVAL = 1.0


def _clamp_int(value, default, lo, hi):
    """把配置里的数字收进 [lo, hi]；不是数字就用 default。"""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _dim_lut(dim):
    """伪亮度 LUT：整屏按比例压暗（dim 是压暗量，0 = 不压暗）。"""
    k = 100 - int(dim)
    return [(i * k) // 100 for i in range(256)] * 3


def _rate(marks, window=RATE_WINDOW):
    """用时间戳队列算每秒帧数。"""
    now = time.monotonic()
    while marks and now - marks[0] > window:
        marks.popleft()
    if len(marks) < 2:
        return 0.0
    span = marks[-1] - marks[0]
    if span <= 0.0:
        return 0.0
    return round((len(marks) - 1) / span, 1)


class DeviceChannel:
    """一台设备的输出通道：显示设置 + 发送队列 + 发送线程 + 统计。"""

    QUEUE_DEPTH = QUEUE_DEPTH

    def __init__(self, dev_id, config, device, sync=True, discover=None):
        self.id = str(dev_id or "dev1")
        self.config = config if isinstance(config, dict) else {}
        self.device = device
        # sync=True：submit() 在调用线程里直接发（单设备/测试路径）
        # sync=False：submit() 只入队，独立线程消费（多设备路径）
        self.sync = bool(sync)
        self._discover = discover
        self._read_display()

        self._q = collections.deque()
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._thread = None

        self._submitted = 0
        self._sent = 0
        self._dropped = 0
        self._failed = 0
        self._send_ms = 0.0
        self._tx_ms = 0.0
        self._last_error = ""
        self._open_warn_at = 0.0
        self._last_scan = 0.0
        self._sent_marks = collections.deque()
        self._luts = {}

    # ---- 显示设置 -----------------------------------------------------
    def _read_display(self):
        """每台自己的显示设置。

        亮度看 display.dim（每台的权威来源）；朝向/翻转/竖屏旋转读该设备的
        顶层键，display 里的同名键只作为镜像，方便外部工具/前端一次读全。
        """
        cfg = self.config
        disp = cfg.get("display")
        if not isinstance(disp, dict):
            disp = {}
        self.dim = _clamp_int(disp.get("dim"), 0, 0, 90)
        self.reverse = bool(cfg.get("reverse", disp.get("reverse", False)))
        orient = cfg.get("orientation") or disp.get("orientation") or "landscape"
        self.orientation = str(orient).lower()
        self.portrait_rotate = _clamp_int(
            cfg.get("portrait_rotate", disp.get("portrait_rotate", -90)),
            -90, -90, 90)

    def set_dim(self, value):
        """设置这台设备的亮度（压暗量 0-90），并镜像进配置。"""
        v = _clamp_int(value, self.dim, 0, 90)
        self.dim = v
        disp = self.config.get("display")
        if not isinstance(disp, dict):
            disp = {}
            self.config["display"] = disp
        disp["dim"] = v
        return v

    def set_reverse(self, value):
        """设置这台设备的 180° 翻转，并镜像进配置。"""
        self.reverse = bool(value)
        self.config["reverse"] = self.reverse
        disp = self.config.get("display")
        if isinstance(disp, dict):
            disp["reverse"] = self.reverse
        self._apply_reverse_to_device()

    def _apply_reverse_to_device(self):
        try:
            fn = getattr(self.device, "set_reverse", None)
            if callable(fn):
                fn(bool(self.reverse))
        except Exception as e:      # 设备刚拔掉时会走到这里，不影响渲染
            log.debug("设备 %s 下发翻转失败: %s", self.id, e)

    def apply_display(self, frame):
        """按这台设备自己的亮度/翻转做后处理；没设置时原样返回（共用同一张图）。"""
        if frame is None:
            return None
        out = frame
        dim = int(self.dim)
        if dim > 0:
            lut = self._luts.get(dim)
            if lut is None:
                lut = _dim_lut(dim)
                self._luts[dim] = lut
            out = out.point(lut)
        if self.reverse:
            out = out.rotate(180)
        return out

    # ---- 设备属性 -----------------------------------------------------
    @property
    def port(self):
        return str(getattr(self.device, "port", "") or "")

    @property
    def offline(self):
        return self.port.lower() == "offline"

    @property
    def connected(self):
        try:
            return bool(getattr(self.device, "connected", False))
        except Exception:
            return False

    @property
    def version(self):
        fw = getattr(getattr(self.device, "transport", None), "fw_version", None)
        if not fw:
            return ""
        try:
            return ".".join(str(int(x)) for x in list(fw)[:3])
        except Exception:
            return str(fw)

    @property
    def busy(self):
        with self._cond:
            return bool(self._q)

    # ---- 发送 ---------------------------------------------------------
    def submit(self, frame):
        """渲染线程调用：把一帧交给这台设备。

        同步通道当场发完（返回是否成功）；异步通道只入队并返回 True，
        真正的串口写在它自己的线程里做。
        """
        if frame is None:
            return False
        self._submitted += 1
        if self.sync:
            return self._deliver(self.apply_display(frame))
        with self._cond:
            while len(self._q) >= self.QUEUE_DEPTH:
                # 队列满了丢最旧的一帧：慢设备宁可跳帧，也要发最新的画面
                self._q.popleft()
                self._dropped += 1
            self._q.append(frame)
            self._cond.notify()
        return True

    def _deliver(self, img):
        """真正写设备（同步通道在渲染线程、异步通道在发送线程里调用）。"""
        if getattr(self.device, "suspended", False):
            # 固件升级中：设备正在重启，一个字节都不能写。直接吞掉这一帧，
            # 计一次 dropped，但返回 True —— 让调度器别以为“发送失败”而重试。
            self._dropped += 1
            self._last_error = "设备正在升级固件，暂停发送"
            return True
        t0 = time.perf_counter()
        try:
            ok = bool(self.device.show_image(img))
        except Exception as e:
            self._failed += 1
            self._last_error = str(e)
            log.warning("设备 %s 发送失败：%s", self.id, e)
            return False
        cost = (time.perf_counter() - t0) * 1000.0
        if not ok:
            self._failed += 1
            self._last_error = str(
                getattr(getattr(self.device, "transport", None),
                        "last_error", "") or "设备没有确认这一帧")
            return False
        self._sent += 1
        self._send_ms = (cost if not self._send_ms
                         else self._send_ms * 0.8 + cost * 0.2)
        try:
            self._tx_ms = float(getattr(self.device, "tx_ms", 0.0) or 0.0)
        except Exception:
            pass
        self._sent_marks.append(time.monotonic())
        return True

    # ---- 连接维护 -----------------------------------------------------
    def ensure_open(self):
        """确保这台设备在线；连不上返回 False（只影响自己）。"""
        if self.connected:
            return True
        if getattr(self.device, "suspended", False):
            # 固件升级后等设备重新枚举：由 Scheduler 的升级流程负责重连并 resume，
            # 这里不能抢着 open()，否则会和轮询抢同一个 COM 句柄、还会刷警告。
            return False
        ok = False
        try:
            ok = bool(self.device.open(handshake_timeout=1.0))
        except TypeError:
            try:
                ok = bool(self.device.open())
            except Exception as e:
                self._last_error = str(e)
                ok = False
        except Exception as e:
            self._last_error = str(e)
            ok = False
        if ok:
            self._apply_reverse_to_device()
            self._open_warn_at = 0.0
            log.info("设备 %s 已连接：%s", self.id, self.port or "auto")
            return True
        # 掉线/没插时别刷屏：每 10 秒最多警告一次
        now = time.monotonic()
        if now - self._open_warn_at > 10.0:
            self._open_warn_at = now
            why = str(getattr(getattr(self.device, "transport", None),
                              "last_error", "") or "没有响应")
            log.warning("设备 %s（%s）连不上，跳过并稍后重试：%s",
                        self.id, self.port or "auto", why)
        return False

    def try_rediscover(self):
        """auto 端口的设备：每 3 秒扫一次串口，拔插后 COM 号变了也能自己找回。"""
        if self.offline or self._discover is None:
            return False
        if getattr(self.device, "suspended", False):
            # 升级流程正在自己轮询/重连，别和它抢串口。
            return False
        now = time.monotonic()
        if now - self._last_scan < REDISCOVER_INTERVAL:
            return False
        self._last_scan = now
        if self.port.lower() not in ("auto", ""):
            return False
        try:
            dev = self._discover()
        except Exception as e:
            log.debug("设备 %s 重新发现失败：%s", self.id, e)
            return False
        if dev is None:
            return False
        old = self.device
        self.device = dev
        self._apply_reverse_to_device()
        log.info("设备 %s 重新发现：%s", self.id, self.port or "auto")
        try:
            old.close()
        except Exception:
            pass
        return True

    def _reconnect_cycle(self):
        """异步通道在队列空/发不出去时的重连节流。"""
        if self._stop.is_set():
            return
        if self.ensure_open():
            return
        self.try_rediscover()
        self._pause(RECONNECT_INTERVAL)

    def _pause(self, secs):
        """等一会儿，但新帧到来/收到停止信号就提前醒。"""
        end = time.monotonic() + max(0.0, float(secs))
        while not self._stop.is_set():
            left = end - time.monotonic()
            if left <= 0.0:
                return
            with self._cond:
                if self._q:
                    return
                self._cond.wait(min(0.1, left))

    # ---- 线程生命周期 -------------------------------------------------
    def start(self):
        """启动发送线程（同步通道什么都不用做）。"""
        if self.sync:
            return False
        with self._cond:
            if self._thread is not None and self._thread.is_alive():
                return False
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, daemon=True, name="dev-%s" % self.id)
            self._thread.start()
        return True

    def stop(self, join=True):
        """停掉发送线程（可重复调用）。"""
        self._stop.set()
        with self._cond:
            self._q.clear()
            self._cond.notify_all()
        t = self._thread
        if (join and t is not None and t.is_alive()
                and t is not threading.current_thread()):
            try:
                t.join(timeout=3.0)
            except Exception:
                pass

    def close(self):
        """关掉底层串口（退出时用）。"""
        try:
            self.device.close()
        except Exception as e:
            log.debug("关闭设备 %s 失败：%s", self.id, e)

    def _run(self):
        log.debug("设备 %s 发送线程启动：%s", self.id, self.port or "auto")
        try:
            while not self._stop.is_set():
                frame = None
                with self._cond:
                    while not self._q and not self._stop.is_set():
                        self._cond.wait(0.5)
                    if self._q:
                        frame = self._q.popleft()
                if frame is None:
                    if self._stop.is_set():
                        break
                    # 队列空着的时候顺手做一次连接维护
                    if not self.connected:
                        self._reconnect_cycle()
                    continue
                if not self.connected and not self.ensure_open():
                    # 这台不在线：丢掉这一帧，别堵着队列，等它自己重连
                    self._dropped += 1
                    self._reconnect_cycle()
                    continue
                self._deliver(self.apply_display(frame))
        finally:
            with self._cond:
                self._q.clear()
            log.debug("设备 %s 发送线程退出", self.id)

    # ---- 对外快照 -----------------------------------------------------
    def snapshot(self):
        """给 WebUI / --devices 用的一台设备状态。"""
        with self._cond:
            pending = len(self._q)
        return {
            "id": self.id,
            "port": self.port or "auto",
            "connected": self.connected,
            "offline": self.offline,
            "version": self.version,
            "orientation": self.orientation,
            "reverse": bool(self.reverse),
            "dim": int(self.dim),
            "dim_label": 100 - int(self.dim),
            "fps": _rate(self._sent_marks),
            "frames": self._sent,
            "submitted": self._submitted,
            "dropped": self._dropped,
            "failed": self._failed,
            "tx_ms": round(float(self._tx_ms or 0.0), 1),
            "send_ms": round(float(self._send_ms or 0.0), 1),
            "queue": pending,
            "mode": "sync" if self.sync else "async",
            "err": self._last_error,
        }