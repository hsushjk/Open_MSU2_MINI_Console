"""设备层：帧编码（差分/RLE）、MiniDevice 器件指令封装、OfflineDevice 离线模拟、屏幕分块常量。

从 msu2_mini.py 拆出（行为不变）。依赖 protocol 与 transport，不依赖 Scheduler/WebUI。
"""
from __future__ import annotations

import logging
import time

from .protocol import (
    MiniProtocol, SCREEN_H, SCREEN_W, TILE_N, lcd_segment_payload,
)
from .transport import (
    SerialTransport, _normalize_port, normalize_fw_version,
)

try:
    import numpy as _np
except Exception:  # pragma: no cover - 没装 numpy 时退化为原样发送
    _np = None

log = logging.getLogger("msu2")

# 触摸 ADC 通道（板上固定接 9 号脚）
TOUCH_CH = 9


def _tile_signatures(raw):
    """按固件的算法算 50 个块的签名：sum(hi*3+lo+1) ^ xor(hi<<8|lo)，折成 16 位。"""
    sig = bytearray(TILE_N * 2)
    w = SCREEN_W * 2
    for ty in range(SCREEN_H // 16):
        for tx in range(SCREEN_W // 16):
            ssum = 0
            sxor = 0
            for y in range(ty * 16, ty * 16 + 16):
                base = y * w + tx * 32
                row = raw[base:base + 32]
                ssum += 3 * sum(row[0::2]) + sum(row[1::2]) + 16
                for k in range(0, 32, 2):
                    sxor ^= (row[k] << 8) | row[k + 1]
            v = (ssum ^ sxor) & 0xFFFF
            t = ty * (SCREEN_W // 16) + tx
            sig[t * 2] = v & 0xFF
            sig[t * 2 + 1] = (v >> 8) & 0xFF
    return bytes(sig)


def _full_frame(raw, encoder):
    """把整帧 RGB565 包装成 0x07..0x09 报文（整帧重发用）。"""
    out = bytearray()
    out += MiniProtocol.lcd_set_xy(0, 0)
    out += MiniProtocol.lcd_set_size(SCREEN_W, SCREEN_H)
    out += MiniProtocol.lcd_enter_direct_mode()
    out += encoder._encode_full(bytes(raw))
    return bytes(out)


def _tile_frame(raw, tx, ty):
    """只画一个 16x16 块的帧：整块 512 字节，一次段事务。"""
    rows = bytearray()
    w = SCREEN_W * 2
    for y in range(ty, ty + 16):
        base = y * w + tx * 2
        rows += raw[base:base + 32]
    out = bytearray()
    out += MiniProtocol.lcd_set_xy(tx, ty)
    out += MiniProtocol.lcd_set_size(tx + 16, ty + 16)
    out += MiniProtocol.lcd_enter_direct_mode()
    out += lcd_segment_payload(bytes(rows))
    out += MiniProtocol.lcd_raw_end()
    return bytes(out)


class FrameEncoder:
    SEG = 256
    TILE = 16                   # 高清模式的差分块，16×16 像素 = 512 字节 RGB565
    TILE_FULL_THRESHOLD = 32    # 变化块超过这个数就直接整帧发

    def __init__(self):
        self._last_raw = None
        self._last_tile_raw = None
        self.last_full = False
        self._force_full = True
        self.last_raw = None
        self.last_tile_sig = None

    def reset(self):
        self._last_raw = None
        self._last_tile_raw = None
        self._force_full = True
        self.last_raw = None
        self.last_tile_sig = None

    @staticmethod
    def _to_rgb565(img):
        data = img.tobytes()
        if _np is not None:
            a = _np.frombuffer(data, dtype=_np.uint8).reshape(-1, 3)
            a = a.astype(_np.uint16)
            v = ((a[:, 0] >> 3) << 11) | ((a[:, 1] >> 2) << 5) | (a[:, 2] >> 3)
            return v.astype(">u2").tobytes()
        r = data[0::3]
        g = data[1::3]
        b = data[2::3]
        n = len(r)
        raw = bytearray(n * 2)
        for k in range(n):
            v = ((r[k] >> 3) << 11) | ((g[k] >> 2) << 5) | (b[k] >> 3)
            raw[2 * k] = v >> 8
            raw[2 * k + 1] = v & 0xFF
        return bytes(raw)

    def encode(self, img):
        """把一帧画面编码成串口报文；画面没有变化时返回空字节串。

        始终按 16x16 块做差分：首帧或变化块过多时整屏重发，其余情况只发
        变化的块。整帧报文带全屏窗口头，差分报文由每块自带窗口。
        """
        if img.size != (SCREEN_W, SCREEN_H):
            img = img.resize((SCREEN_W, SCREEN_H))
        if img.mode != "RGB":
            img = img.convert("RGB")
        raw = self._to_rgb565(img)
        self.last_full = False
        if raw == self._last_raw and not self._force_full:
            return b""
        self._last_raw = raw
        self._force_full = False
        self.last_raw = raw
        self.last_tile_sig = _tile_signatures(raw)
        body = self._encode_tiles(raw)
        if not body:
            return b""
        if self.last_full:
            header = (MiniProtocol.lcd_set_xy(0, 0)
                      + MiniProtocol.lcd_set_size(SCREEN_W, SCREEN_H)
                      + MiniProtocol.lcd_enter_direct_mode())
            return bytes(header + body)
        return bytes(body)

    def _encode_tiles(self, raw):
        """逐块比较，只发相对上一帧变化的 16x16 块（每块 512 字节）。"""
        T = self.TILE
        prev = self._last_tile_raw
        raw = bytes(raw)

        if prev is None or len(prev) != len(raw):
            self._last_tile_raw = raw
            self.last_full = True
            return self._encode_full(raw)

        tiles = []
        for ty in range(0, SCREEN_H, T):
            for tx in range(0, SCREEN_W, T):
                for r in range(ty, min(ty + T, SCREEN_H)):
                    off = (r * SCREEN_W + tx) * 2
                    if raw[off:off + T * 2] != prev[off:off + T * 2]:
                        tiles.append((tx, ty))
                        break

        self._last_tile_raw = raw
        if not tiles:
            return b""
        if len(tiles) > self.TILE_FULL_THRESHOLD:
            self.last_full = True
            return self._encode_full(raw)

        out = bytearray()
        for tx, ty in tiles:
            out += MiniProtocol.lcd_set_xy(tx, ty)
            out += MiniProtocol.lcd_set_size(tx + T, ty + T)
            out += MiniProtocol.lcd_enter_direct_mode()
            pre = bytearray()
            for r in range(ty, ty + T):
                off = (r * SCREEN_W + tx) * 2
                pre += raw[off:off + T * 2]
            out += lcd_segment_payload(bytes(pre))
            out += MiniProtocol.lcd_raw_end()
        return bytes(out)

    def _encode_full(self, raw):
        out = bytearray()
        for si in range(0, len(raw), 512):
            out += lcd_segment_payload(raw[si:si + 512])
        out += MiniProtocol.lcd_raw_end()
        return bytes(out)


class MiniDevice:
    def __init__(self, port):
        port = _normalize_port(port)
        self.port = port
        self.transport = SerialTransport(port)
        self.encoder = FrameEncoder()
        self.screen_reversed = False
        # 发送限速（字节/秒）：设备的消费者是主循环与屏 DMA。收齐就缓慢提速，
        # 收到裁决失败就降速，下限见 _write_paced()。
        self._pace_bps = 140000.0
        # 学到的安全上限：撞过一次 0x15 就把上限压到失败速率的 85%，
        # 之后慢慢往上试，但不再直接冲回失败过的那个速率。
        self._pace_max = 240000.0
        # 分块自检：每帧发完让设备回一张“哪些块没画对”的掩码，
        # 只补画坏块。整帧重绘要 25 KB，补一块只要 0.5 KB。
        self.tile_repairs = 0
        self._full_resends = 0
        self._last_repair_warn = 0.0
        self._tiles_ok = True
        # 固件升级期间挂起：设备正在重启/重新枚举，通道不能再往里写像素帧，
        # 否则 Windows 会抛 WriteFile failed (PermissionError(13, ...))。
        self._suspended = False

    def open(self, handshake_timeout=2.0, purge_retry=True):
        ok = self.transport.open(handshake_timeout=handshake_timeout,
                                 purge_retry=purge_retry)
        if ok:
            self.encoder.reset()
            self._ack_fails = 0
            self._zero_acks = 0
            if getattr(self.transport, "fw_version", None) is None:
                log.info("固件没有回版本号")
        return ok

    def close(self):  self.transport.close()

    @property
    def connected(self): return self.transport.connected

    def suspend(self):
        """固件升级用：恢复之前拒绝发送任何像素帧、也不再自动重连。"""
        self._suspended = True

    def resume(self):
        self._suspended = False

    @property
    def suspended(self):
        return self._suspended

    def show_image(self, img):
        if self._suspended:
            # 设备在重启：直接丢掉这一帧，别往不存在的句柄里写。
            return False
        ok = self._show_image_once(img)
        if ok:
            return True
        # 设备报告缺像素（或整段作废）：缓存已清，立刻整帧补一次，
        # 用户看到的是“偶尔卡一下”，而不是半屏花掉的内容。
        log.debug("整帧校验失败，重发一次")
        return self._show_image_once(img)

    def _show_image_once(self, img):
        # 一帧要分好几笔写（2 KB 一次）+ 等设备回执。这段时间里如果别的
        # 线程往串口写 6 字节命令（改亮度、读存档、开关调试），设备会把
        # 它们当成像素吃进去 —— 整帧从此错位，就是那种“局部一块花掉”。
        # 所以整帧独占串口锁（RLock，write() 内部同名锁可重入）。
        _lock = getattr(self.transport, "_lock", None)
        if _lock is not None:
            _lock.acquire()
        try:
            return self._show_image_once_locked(img)
        finally:
            if _lock is not None:
                _lock.release()

    def _write_paced(self, payload):
        """把整帧按 2 KB 分块、带间隔写进串口（限速可自适应）。

        间隔不是可省的“等待”，而是数据完整性的前提：设备端 USB 环只有 8 KB，
        主机灌得比它消化得快时尾部像素会被丢掉（Windows 上实测 2 KB/块、间隔
        6.5 ms 起稳定，7 ms 仍偶发丢块，单块放大到 4 KB 以上任何间隔都丢）。
        取 8 ms 留出余量，线上速率约 160 KB/s。Linux 写路径不同但同一固件，
        用同一上限。
        """
        seg_write = 2048
        rate = max(120000.0, min(240000.0, getattr(self, "_pace_bps", 140000.0)))
        gap = min(0.008, seg_write / rate)
        for i in range(0, len(payload), seg_write):
            if not self.transport.write(payload[i:i + seg_write]):
                return False
            if i + seg_write < len(payload):
                time.sleep(gap)
        return True

    def _show_image_once_locked(self, img):
        t0 = time.perf_counter()
        frame = self.encoder.encode(img)
        self.enc_ms = (time.perf_counter() - t0) * 1000.0
        if not frame:
            return True
        # 整帧一次写：分段写会让别的线程（触摸轮询）把请求字节插进帧中间，
        # 设备按“上一段的剩余字节数”消费，从此整条流错位。
        payload = frame
        # 按 8192 字节分块写（与存档点一致）：一旦某一块写不进去，
        # 立即停下来，不会出现“写了一半、剩下的在超时后才报错”。
        # Write in 2 KB pieces with a tiny gap: the device ring is 8 KB and its
        # consumer is the main loop, so never push a whole 25 KB frame in one
        # burst. This costs ~25 ms per frame and removes the overflow entirely.
        ok = self._write_paced(payload)
        if not ok:
            # 写失败/截断：设备可能停在“等像素”中间，此时再发任何命令都会被
            # 当作像素吃掉。用同步栅栏把它拉回命令模式，再整帧重发。
            self.encoder.reset()
            try:
                if getattr(self.transport, "connected", False):
                    self.transport.resync()
            except Exception:
                pass
            return False
        # 整帧已经写进串口了：不再等任何回执。
        # 为什么：每帧都是整屏全量重绘（encode() 恒走 _encode_full），
        # 某一帧在路上丢了几个字节，下一帧也会把整屏重新盖一遍，坏帧最多闪一下。
        # 相反，逐帧等回执会把“设备忙得没空回话”误判成“设备坏了”，接着重连，
        # 而重连发生在设备正停在半帧里的时刻，于是后续命令全被当成像素吃掉 ——
        # 那才是真正把设备搞哑的链路。宁可不确认，也绝不打断它。
        self.self_paced = False
        try:
            self.transport.read_all()
        except Exception:
            pass
        self._quiet_frames = 0
        self._ack_fails = 0
        self.tx_ms = (time.perf_counter() - t0) * 1000.0 - self.enc_ms
        self.frame_bytes = len(frame)
        self._repair_tiles()
        return True

    def _repair_tiles(self, rounds=2):
        """分块自检：让设备报出没画对的 16x16 块，只补画这些块。

        超过六成的块都不对，说明整帧根本没落地，逐块补不划算 —— 直接整帧重发。
        逐块补画也要限速：50 块约 26 KB 一口气灌进 8 KB 接收环，主机被 NAK
        卡住超过固件那 30 ms 段超时，正在收的块就会被作废（表现为底部几行反复坏）。
        """
        if not self._tiles_ok:
            return True
        fw = normalize_fw_version(
            getattr(self.transport, "fw_version", None))
        if not fw or fw < (1, 0, 0):
            return True
        if getattr(self.encoder, "last_raw", None) is None:
            return True
        raw = getattr(self.encoder, "last_raw", None)
        sig = getattr(self.encoder, "last_tile_sig", None)
        if not raw or not sig:
            return True
        raw = bytes(raw)
        told = False
        total = max(1, int(rounds))
        for _round in range(total):
            mask = self.transport.tile_check(sig)
            if mask is None or mask == 0:
                return True
            bad = [bit for bit in range(TILE_N) if (mask >> bit) & 1]
            if not told:
                told = True
                log.warning("画面不同步：%d B / %.0f ms / 坏块 %d/%d（掩码 %0*x）",
                            getattr(self, "frame_bytes", 0),
                            getattr(self, "tx_ms", 0.0),
                            len(bad), TILE_N, (TILE_N + 3) // 4, mask)
            if len(bad) * 100 >= TILE_N * 60:
                log.warning("整屏不同步，改为整帧重发（第 %d 轮）", _round + 1)
                if not self._write_paced(_full_frame(raw, self.encoder)):
                    return False
                self._full_resends = getattr(self, "_full_resends", 0) + 1
                time.sleep(0.02)
                continue
            fixed = 0
            for n, bit in enumerate(bad):
                tx = (bit % (SCREEN_W // 16)) * 16
                ty = (bit // (SCREEN_W // 16)) * 16
                if not self.transport.write(_tile_frame(raw, tx, ty)):
                    return False
                fixed += 1
                if (n + 1) % 8 == 0:
                    time.sleep(0.010)
            self.tile_repairs += 1
            log.info("分块自检：补画 %d 块（掩码 %0*x）", fixed, (TILE_N + 3) // 4, mask)
            time.sleep(0.01)
        left = self.transport.tile_check(sig)
        if left:
            log.warning("分块自检：%d 轮后仍有 %d 块不一致，整帧重发一次",
                        total, len([b for b in range(TILE_N) if (left >> b) & 1]))
            if self._write_paced(_full_frame(raw, self.encoder)):
                self._full_resends = getattr(self, "_full_resends", 0) + 1
                time.sleep(0.03)
                left = self.transport.tile_check(sig)
        if left:
            self.encoder.reset()  # 下一帧整帧重画
            now = time.monotonic()
            if now - getattr(self, "_last_repair_warn", 0.0) > 30.0:
                self._last_repair_warn = now
                log.error("画面仍然不同步（掩码 %0*x），已让下一帧整屏重画",
                          (TILE_N + 3) // 4, left)
        return True


    def set_reverse(self, reverse):
        self.screen_reversed = bool(reverse)

    def heartbeat(self):
        ok = self.transport.write(MiniProtocol.read_adc(15))
        time.sleep(0.01)
        self.transport.read_all()
        return ok

    def devcfg_info(self):
        return self.transport.devcfg_info()

    def devcfg_write(self, dim, orient, reverse, flags=0):
        return self.transport.devcfg_write(dim, orient, reverse, flags)


    def asset_info(self):
        return self.transport.asset_info()

    def boot_report(self):
        return self.transport.boot_report()

    def asset_write(self, kind, slot, payload, fmt=0):
        return self.transport.asset_write(kind, slot, payload, fmt=fmt)

    def asset_draw(self, kind, slot):
        return self.transport.asset_draw(kind, slot)

    def asset_blit(self, kind, slot, x, y):
        return self.transport.asset_blit(kind, slot, x, y)

    def asset_erase(self, kind, slot):
        return self.transport.asset_erase(kind, slot)

    def asset_settings(self, boot_mode, standby_mode, count, dwell, boot_hold=4):
        return self.transport.asset_settings(boot_mode, standby_mode,
                                             count, dwell, boot_hold)

    def asset_wipe(self):
        return self.transport.asset_wipe()


    def flash_firmware(self, data, progress=None):
        # 上传期间先挂起：设备收完 flash_apply 就重启，串口会消失；
        # transport.flash_firmware 收到 [OK] uploaded 后自己 close()。
        self._suspended = True
        return self.transport.flash_firmware(data, progress=progress)

    def boot_trace(self):
        return self.transport.boot_trace()

    def nor_read(self, addr, n=16):
        return self.transport.nor_read(addr, n)

    def ota_channel_probe(self, payload):
        return self.transport.ota_channel_probe(payload)

    def asset_verify_check(self, kind, slot):
        return self.transport.asset_verify_check(kind, slot)

    def anim_probe(self, n=10):
        return self.transport.anim_probe(n)

    def read_touch(self):
        if not self.transport.write(MiniProtocol.read_adc(TOUCH_CH)):
            return None
        return self.transport.read_adc(TOUCH_CH, timeout=0.008)


class OfflineDevice:
    def __init__(self):
        self.port = "offline"
        self.connected = True
        self.last = None
        self.frames = 0
        self.transport = type("T", (), {"bytes_written": 0})()
        self._suspended = False

    def open(self, handshake_timeout=2.0):
        self.connected = True
        return True

    def close(self):
        self.connected = False

    def suspend(self):
        self._suspended = True

    def resume(self):
        self._suspended = False

    @property
    def suspended(self):
        return self._suspended

    def show_image(self, img):
        if self._suspended:
            return False
        self.last = img.copy()
        self.frames += 1
        self.transport.bytes_written += img.size[0] * img.size[1] * 2
        return True

    def set_reverse(self, reverse):
        pass

    def read_touch(self):
        return 0

    def heartbeat(self):
        return True
