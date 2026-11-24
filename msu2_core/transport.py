"""串口传输层：SerialTransport、串口互斥装饰器、端口名归一化、串口波特率。

从 msu2_mini.py 拆出（行为不变）。依赖 protocol，不依赖 device/上层。
"""
from __future__ import annotations

import functools
import logging
import os
import select
import sys
import threading
import time
import zlib

import serial

from .protocol import (
    Cmd, END_FRAME, MiniProtocol, SCREEN_H, SCREEN_W, TILE_N,
)

log = logging.getLogger("msu2")


SCREEN_BAUD = 19200


def normalize_fw_version(fw):
    """把版本号统一成三段元组；空值返回 None。

    版本比较统一走这个函数，避免 (1, 0) 与 (1, 0, 0) 因长度不同被误判。
    """
    if not fw:
        return None
    parts = [int(x) for x in list(fw)[:3]]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def format_fw_version(fw):
    """把固件版本排成三段式字符串（如 "1.0.9"），空值返回"未知"。"""
    ver = normalize_fw_version(fw)
    return "%d.%d.%d" % ver if ver else "未知"


# 空闲探询。设备只有在“上一帧的字节全吃完、没有半开的段”时才能
# 解析到这条命令并回包，所以收到回包 = 设备确实空下来了。


def _serial_locked(fn):
    """串口是独占的原生资源：任何一次 read/write/close 都必须互斥，
    否则 pyserial 的 OVERLAPPED 缓冲可能被并发释放（表现为 0xc0000374 堆损坏）。"""
    @functools.wraps(fn)
    def wrapper(self, *args, **kwargs):
        lock = getattr(self, "_lock", None)
        if lock is None:
            return fn(self, *args, **kwargs)
        with lock:
            return fn(self, *args, **kwargs)
    return wrapper


_POSIX_NB = (os.name == "posix")
_NB_PIECE = 2048        # 实测最优分片：26 KB 整帧 50 ms（≈500 KB/s）


class SerialTransport:
    def __init__(self, port, baud=SCREEN_BAUD, timeout=0.05):
        self.port = port; self.baud = baud; self.timeout = timeout
        self._ser = None
        self._lock = threading.RLock()
        self.fw_version = None
        self._connected = False
        self._handshaked = False
        self.last_error = None
        self.bytes_written = 0
        self.version = ""
        self.raw_mode = False
        self.last_status = None      # 调试模式下最近一帧的 11 字节详情（px/win/ms）

    @_serial_locked
    def open(self, handshake_timeout=2.0, purge_retry=True):
        with self._lock:
            if self._connected and self._handshaked:
                return True
            if self._ser is None:
                try:
                    self._ser = serial.Serial(self.port, self.baud,
                                              timeout=self.timeout,
                                              write_timeout=1.0)
                except PermissionError:
                    if sys.platform == "win32":
                        self.last_error = f"{self.port} 被其它程序占用"
                    else:
                        self.last_error = (
                            f"{self.port} 权限不足，"
                            f"试试 sudo usermod -aG dialout $USER")
                    return False
                except FileNotFoundError:
                    self.last_error = f"{self.port} 暂时不存在"
                    return False
                except Exception as e:
                    self.last_error = f"打开 {self.port} 失败: {e}"
                    return False
            self._connected = True
            # 打开端口后先什么都不写，只听设备问候：设备可能还没准备好，
            # 抢先写会撞上 1 秒写超时。问候迟迟不来才灌字节自救。
            if not self._handshaked and not self.handshake(
                    timeout=handshake_timeout):
                # 上一次会话被强行结束时，设备可能正卡在“等像素”里：raw_left > 0，
                # 之后所有字节（包括我们的命令）都被它当像素吃掉，于是既不发问候
                # 也不回版本。灌两整帧数量的 0（必须是 6 的整数倍，才不会把 6 字节
                # 定界推错位）把这个窟窿填平，然后再握一次手。
                if not purge_retry:
                    # 探测别人的串口（蓝牙等）时不要灌 32 KB：那些口根本
                    # 不认这套协议，灌进去只会白等好几秒。
                    self.close(); return False
                self._purge_stale_stream()
                time.sleep(0.05)
                if not self.handshake(timeout=handshake_timeout):
                    # 设备可能正忙（待机淡入/写 NOR）没空发问候，但命令照收：
                    # 主动问一次版本，有回应就算连上。
                    self._purge_stale_stream(total=6)
                    fw = self.query_version(timeout=0.5)
                    if fw is None:
                        self.close(); return False
                    self.raw_mode = True
                    self._handshaked = True
                    log.info("设备没发问候包，但版本帧有回应，按整段传输继续")
                log.info("设备刚才卡在半截帧里，已灌字节冲掉，重新握手成功")
            log.info("串口已打开: %s @ %d", self.port, self.baud)
            ver = self.query_version()
            if not ver:
                # 设备可能正卡在“等像素”里（上一次写到一半就断了）：
                # 先灌几条收流帧把 raw_left 喂到 0，再问一次。
                for _ in range(3):
                    try:
                        self.resync(timeout=0.3)
                    except Exception:
                        break
                    ver = self.query_version(timeout=0.4)
                    if ver:
                        log.info("设备恢复应答: %s", self.port)
                        break
            if ver:
                major, minor, patch, touch = ver
                self.fw_version = (major, minor, patch)
                log.info("固件版本 %s，触摸读数 %d",
                         format_fw_version(self.fw_version), touch)
                if touch == 0:
                    for _ in range(6):
                        time.sleep(0.08)
                        again = self.query_version(timeout=0.25)
                        if again and again[3] > 0:
                            touch = again[3]
                            log.info("触摸读数 %d（第一拍稍晚才有）", touch)
                            break
                    if touch == 0:
                        log.warning("固件上报的触摸读数是 0：ADC 还没有产出结果"
                                    "（没有触摸模块时可以忽略这条）")
            else:
                log.warning("固件没有回应版本查询（可能是 v8 或更早的固件）")
            return True

    @_serial_locked
    def close(self):
        with self._lock:
            if self._ser:
                # 先把驱动缓冲里还没发完的字节发完再关：6 字节帧只发了一半就
                # close，设备会把剩下的当成新帧头，从此整条命令通道对不上号
                # （实测踩过一次，表现为设备还在打招呼却不再回任何命令）。
                try:
                    self._ser.flush()
                except Exception:
                    pass
                try: self._ser.close()
                except Exception: pass
            self._ser = None
            self._connected = False; self._handshaked = False

    @property
    def connected(self):
        return self._connected and self._handshaked

    def _write_raw(self, data):
        """把整段字节写进串口：POSIX 用非阻塞 os.write + select。

        usbmon 实测：Linux 的 cdc_acm 写路径在缓冲满时会丢掉 wakeup，主机
        519 ms 内一笔 OUT URB 都没提交（设备侧既没有 NAK，DMA 也没有超时），
        pyserial 的阻塞写因此每帧随机卡 1~2 次、26 KB 要 700~1600 ms。
        换成 O_NONBLOCK + select（2048 B 一片）后同样的载荷 50 ms 写完，
        约 500 KB/s，且不再出现卡顿。
        """
        if self._ser is None:
            return 0
        view = memoryview(bytes(data))
        total = len(view)
        if total <= 0:
            return 0
        if not _POSIX_NB:
            return self._ser.write(view) or 0
        fd = self._ser.fileno()
        try:
            os.set_blocking(fd, False)
        except (AttributeError, OSError):
            pass
        sent = 0
        t0 = time.monotonic()
        while sent < total:
            if time.monotonic() - t0 > 3.0:
                raise IOError("写入卡住（3 秒没有进展）")
            take = min(total - sent, _NB_PIECE)
            piece = view[sent:sent + take]
            off = 0
            while off < take:
                try:
                    n = os.write(fd, piece[off:])
                except BlockingIOError:
                    select.select([], [fd], [], 0.2)
                    continue
                if n <= 0:
                    select.select([], [fd], [], 0.2)
                    continue
                off += n
            sent += take
        return sent

    @_serial_locked
    def _purge_stale_stream(self, total=32004):
        # 灌一整帧还多一点的 0x00，把设备“等像素”的 raw_left 填平。
        # 每次写出的字节数都必须是 6 的整数倍，否则 6 字节定界会被推错位。
        if self._ser is None:
            return
        self._connected = True          # 自救期间一律允许读写
        total -= total % 6
        left = total
        while left >= 6:
            take = min(left, 3072)
            take -= take % 6
            for _attempt in range(3):
                try:
                    self._write_raw(bytes(take))
                    break
                except Exception:
                    time.sleep(0.01)
            else:
                return
            left -= take
        try:
            time.sleep(0.05)
            self._ser.reset_input_buffer()
        except Exception:
            pass

    def write(self, data):
        with self._lock:
            if not self._connected or not self._ser:
                return False
            if _POSIX_NB:
                # Linux: pyserial 的阻塞写在 cdc_acm 上会随机卡 500 ms（见 _write_raw）
                try:
                    self._write_raw(data)
                    self.bytes_written += len(data)
                    return True
                except Exception as e:
                    log.warning("写入失败: %s", e)
                    self._connected = False; self._handshaked = False
                    return False
            try:
                view = memoryview(bytes(data))
                total = len(view)
                sent = 0
                t0 = time.monotonic()
                stuck = 0
                while sent < total:
                    try:
                        n = self._write_raw(view[sent:]) or 0
                    except serial.SerialTimeoutException:
                        # 设备接收环满了（NAK）：这不是链路坏了，等一会儿再续。
                        # 只要 3 秒内还有进展就不算失败，日志降成 DEBUG 免得刷屏。
                        if time.monotonic() - t0 > 3.0:
                            raise
                        stuck += 1
                        if stuck % 50 == 1:
                            log.debug("设备接收环满，等待续写（已写 %d/%d 字节）",
                                      sent, total)
                        time.sleep(0.02)
                        continue
                    if n <= 0:
                        # 短写：给驱动喘口气，别把半帧当成整帧发出去
                        if time.monotonic() - t0 > 3.0:
                            raise IOError("写入卡住（3 秒没有进展）")
                        time.sleep(0.001)
                        continue
                    sent += n
                self.bytes_written += total
                return True
            except Exception as e:
                log.warning("写入失败: %s", e)
                self._connected = False; self._handshaked = False
                return False

    @_serial_locked
    def resync(self, timeout=0.5):
        """把设备从“等像素”拉回“等命令”。

        以前是往串口灌 25 KB 的收流帧，靠“喂饱像素计数”把设备顶出来；问题是
        那堆字节在设备凑够像素之后会被当命令解析，可能拼出 class 3（flash）之类
        的野命令。v39 固件自己会在“30 ms 没字节”或“一帧超过 400 ms”时作废
        未完成的段，所以这里只要闭嘴等一会儿，再发一帧 0 像素的空帧就够：
        设备放弃半截像素流、回到命令解析，状态的残留也一起清零。
        """
        with self._lock:
            if not self._connected or not self._ser:
                return False
            try:
                self._ser.reset_input_buffer()
                # 千万不能再发“开流(0x07)”：那等于告诉设备“准备好收 25600 个像素”，
                # 主机又不会真发，设备就一直等着吃字节、把所有命令都当像素吞掉，
                # 看起来就像“设备死了”。v43 固件自己会超时放弃半截流，这里只要
                # 安静一会儿，再补一条收流命令把状态清干净即可。
                time.sleep(0.45)
                self._ser.reset_input_buffer()
                # 对齐垫（与 open() 同一套）：先补一整帧 0x00，再等设备自己把
                # 半帧残包按“空闲 100 ms”丢掉，然后才清接收缓冲、发空帧。
                self._write_raw(b"\x00" * 6)
                time.sleep(0.25)
                self._ser.reset_input_buffer()
                self._write_raw(END_FRAME)        # 0 像素的收流：flush + 归零 + 裁决
                time.sleep(0.05)
                deadline = time.monotonic() + timeout
                got = b""
                while time.monotonic() < deadline:
                    try:
                        chunk = self._ser.read(16)
                    except Exception:
                        break
                    if chunk:
                        got += chunk
                        break
                    time.sleep(0.01)
                self._ser.reset_input_buffer()
                return bool(got)
            except Exception:
                self._connected = False; self._handshaked = False
                return False

    @_serial_locked
    def asset_info(self, timeout=1.5):
        """查设备上的资产：开机图是否有效、待机图哪几槽有效、当前设置。"""
        if self._ser is not None:
            self._ser.reset_input_buffer()
        self.write(MiniProtocol.asset_info())
        deadline = time.monotonic() + timeout
        buf = b""
        while time.monotonic() < deadline:
            chunk = self._ser.read(64) if self._ser else b""
            if chunk:
                buf += chunk
                idx = buf.find(b"\x0a\x00")
                while idx >= 0 and len(buf) - idx >= 14:
                    frame = buf[idx:idx + 14]
                    if frame[13] == 0xAA:
                        return {
                            "boot_valid": bool(frame[2]),
                            "standby_mask": frame[3] | (frame[4] << 8),
                            "boot_mode": frame[5],
                            "standby_mode": frame[6],
                            "count": frame[7],
                            "dwell": (frame[8] << 8) | frame[9],
                            "fw": frame[10],
                            "boot_hold": frame[11] or 4,
                        }
                    buf = buf[idx + 1:]
                    idx = buf.find(b"\x0a\x00")
            else:
                time.sleep(0.01)
        return None

    @_serial_locked
    def boot_report(self, timeout=1.0):
        """读开机自检结果位图（v47 起）：bit0 FLASH / bit1 TOUCH / bit2 LCD / bit3 USB / bit4 CFG"""
        if self._ser is None:
            return None
        try:
            self._ser.reset_input_buffer()
        except Exception:
            pass
        self.write(MiniProtocol.boot_report())
        deadline = time.monotonic() + float(timeout)
        buf = b""
        while time.monotonic() < deadline:
            buf += self.read_all() or b""
            for i in range(len(buf) - 2):
                if buf[i] == 0x0A and buf[i + 1] == 0x0B:
                    return int(buf[i + 2])
            time.sleep(0.01)
        return None

    def asset_write(self, kind, slot, payload, chunk=1024, gap=0.008,
                    timeout=8.0, fmt=0):
        """把一张 160x80 图写进指定槽。

        fmt=0 时 payload 是 25 600 B 裸 RGB565（压缩不划算时）；
        fmt=1 时 payload 是 RLE 压缩数据（`[cnt,hi,lo]` 流，长度更短）。
        分窗上传，一窗一确认；如果第一窗就被拒绝（比如设备刚被别的东西
        弄乱），整笔重试一次，不要一次失败就让用户手动再点。
        """
        for attempt in (0, 1):
            ok, why = self._asset_write_once(kind, slot, payload, chunk, gap,
                                             timeout, fmt=fmt)
            if ok or attempt == 1:
                return ok, why
            time.sleep(0.25)
        return False, "unknown"

    @_serial_locked
    def _asset_write_once(self, kind, slot, payload, chunk=1024, gap=0.008,
                          timeout=8.0, fmt=0):
        import zlib

        # 先打个招呼：设备要是已经卡死了，别再去按它的按钮，直接告诉用户拔插
        if self.asset_info(timeout=0.8) is None:
            return False, "设备没应答（请拔插一次 USB 再试）"

        payload = bytes(payload)
        if fmt == 0:
            if len(payload) != 160 * 80 * 2:
                raise ValueError("payload must be 160x80 RGB565")
        elif not 0 < len(payload) <= 0x6800 - 20:
            raise ValueError("RLE payload must fit in one asset slot")

        if self._ser is not None:
            self._ser.reset_input_buffer()
        t_begin = time.monotonic()
        self.write(MiniProtocol.asset_begin(kind, slot, len(payload), fmt))
        st = self._asset_reply(0x01, max(timeout, 20.0))
        if st != 0x06:
            used = time.monotonic() - t_begin
            if st is None:
                return False, ("设备没有回执（等了 %.1f 秒；可能它先要擦 7 个扇区，"
                               "也可能设备掉线了）" % used)
            return False, "设备拒绝接收（begin 回 0x%02X，用时 %.1f 秒）" % (st, used)

        # 分窗上传：每窗写完，设备把它真正吃进 flash 之后才回 ack。
        # 这样主机永远不会比设备快，也不会把 USB 接收环撑爆。
        window = 1024
        sent = 0
        while sent < len(payload):
            block = payload[sent:sent + window]
            st = None
            for attempt in (0, 1):
                self.write(MiniProtocol.asset_window(len(block)))
                self.write(block)
                st = self._asset_reply(0x08, max(timeout, 8.0))
                if st == 0x06:
                    break
                time.sleep(0.05)
            if st != 0x06:
                self.write(MiniProtocol.asset_abort())
                return False, ("第 %d 字节处设备没确认（%s），已放弃"
                               % (sent, "没回执" if st is None else "回 0x%02X" % st))
            sent += len(block)

        self.write(MiniProtocol.asset_commit(zlib.crc32(payload) & 0xFFFFFFFF))
        st = self._asset_reply(0x03, timeout)
        if st == 0x06:
            return True, "ok"
        if st is None:
            return False, "数据发完了，但设备没有回执（可能掉线）"
        return False, "设备校验没过（0x%02X），图没写进去" % st

    @_serial_locked
    def _asset_reply(self, sub, timeout=3.0):
        """读资产命令的回执。

        设备回的是 6 字节帧 `0A sub status 00 00 00`（查询是 12 字节），
        这里扫出 `sub` 对应的 status：0x06 成功 / 0x15 失败，没等到返回 None。
        早先按“单个字节回执”写，结果把帧头 0x0A 当成了回执值（日志里那个 10）。
        """
        deadline = time.monotonic() + timeout
        buf = bytearray()
        while time.monotonic() < deadline:
            if self._ser is None:
                return None
            try:
                n = self._ser.in_waiting
                chunk = self._ser.read(n if n else 1)
            except Exception:
                return None
            if chunk:
                buf.extend(chunk)
                for i in range(len(buf) - 2):
                    if buf[i] == 0x0A and buf[i + 1] == sub:
                        return buf[i + 2]
            else:
                time.sleep(0.005)
        return None

    @_serial_locked
    def _wait_byte(self, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._ser is None:
                return None
            chunk = self._ser.read(1)
            if chunk:
                return chunk[0]
            time.sleep(0.005)
        return None

    @_serial_locked
    def _reply_byte(self, cls_byte, sub, timeout=3.0):
        """Scan for a short reply frame ``cls sub status``; returns status or None."""
        end = time.monotonic() + timeout
        buf = b""
        while time.monotonic() < end:
            if self._ser is None:
                return None
            chunk = self._ser.read(self._ser.in_waiting or 1)
            if chunk:
                buf += chunk
                for i in range(len(buf) - 2):
                    if buf[i] == cls_byte and buf[i + 1] == sub:
                        return buf[i + 2]
            else:
                time.sleep(0.005)
        return None

    @_serial_locked
    def _drain_upload_window(self):
        """Feed zeros so a half-open upload window on the device closes."""
        if self._ser is None:
            return
        try:
            self._write_raw(b"\x00" * 4096)
            time.sleep(0.15)
            self._ser.reset_input_buffer()
        except Exception:
            pass

    @_serial_locked
    def flash_firmware(self, data, progress=None, chunk=1024):
        # 固件侧按 256 字节一页搬运，镜像长度必须是 256 的整数倍
        if len(data) % 256:
            data = bytes(data) + b"\xff" * (256 - len(data) % 256)

        """Upload ``data`` (raw firmware image) into the device and reboot it."""
        total = len(data)
        if total <= 0 or total > 48000:
            return False, "image size out of range (%d bytes)" % total
        self._ser.reset_input_buffer()
        # Close any half-open frame and reset the device's per-session byte budget
        # (firmware >= v59 drops everything once 40 KB has gone through without a reset).
        self._write_raw(MiniProtocol.lcd_set_xy(0, 0))
        self._write_raw(MiniProtocol.lcd_set_size(SCREEN_W, SCREEN_H))
        self._write_raw(MiniProtocol.lcd_enter_direct_mode())
        self._write_raw(MiniProtocol.lcd_raw_end())
        time.sleep(0.05)
        self._ser.reset_input_buffer()
        r = None
        for attempt in range(3):
            self._write_raw(MiniProtocol.flash_begin(total))
            r = self._reply_byte(0x0A, 0x12, 6.0)
            if r == 0x06:
                break
            # 设备可能卡在上一轮没收尾的流里：发一个 0 像素收流把它拉回来，再来一次
            log.warning("上传握手没有回应（第 %d 次，0x%02X），收尾后重试", attempt + 1,
                        r if r is not None else 0)
            self._drain_upload_window()
            self._write_raw(MiniProtocol.lcd_set_xy(0, 0))
            self._write_raw(MiniProtocol.lcd_set_size(SCREEN_W, SCREEN_H))
            self._write_raw(MiniProtocol.lcd_enter_direct_mode())
            self._write_raw(MiniProtocol.lcd_raw_end())
            time.sleep(0.4)
            self._ser.reset_input_buffer()
        if r != 0x06:
            self._drain_upload_window()
            return False, "device refused the upload (0x%02X)" % (r if r is not None else 0)
        pos = 0
        while pos < total:
            part = data[pos:pos + chunk]
            self._write_raw(MiniProtocol.flash_window(len(part)))
            self._write_raw(part)
            r = self._reply_byte(0x0A, 0x12, 6.0)
            if r != 0x06:
                self._drain_upload_window()
                return False, "device did not confirm at byte %d (0x%02X)" % (pos, r if r is not None else 0)
            pos += len(part)
            if progress:
                progress(pos, total)
        crc = zlib.crc32(data) & 0xFFFFFFFF
        self._write_raw(MiniProtocol.flash_commit(crc))
        r = self._reply_byte(0x13, 0x00, 8.0)
        if r != 0x06:
            self._drain_upload_window()
            return False, "CRC check failed on the device (0x%02X)" % (r if r is not None else 0)
        self._write_raw(MiniProtocol.flash_apply())
        # 设备收到 [OK] uploaded 后立刻重启：马上关串口，从这里起一个字节都不写，
        # 否则调度器会往正在重启的句柄里塞像素帧，Windows 上表现为写失败刷屏。
        self.close()
        log.info("固件已写入，串口已关闭，等待设备重新枚举")
        return True, "uploaded %d bytes, device is rebooting" % total

    @_serial_locked
    def nor_read(self, addr, n=16, timeout=1.2):
        """读板载 NOR 原始字节（固件 v52 起，命令 0A 0C），用于自检抽查。"""
        n = max(1, min(int(n), 48))
        self._ser.reset_input_buffer()
        self._write_raw(MiniProtocol._frame(0x0A, 0x0C,
                                            (addr >> 16) & 0xFF,
                                            (addr >> 8) & 0xFF,
                                            addr & 0xFF, n))
        end = time.monotonic() + timeout
        buf = b""
        while time.monotonic() < end:
            buf += self._ser.read(self._ser.in_waiting or 1)
            i = buf.find(b"\x0a\x0c")
            if i >= 0 and len(buf) >= i + 3 + buf[i + 2] + 1:
                return buf[i + 3:i + 3 + buf[i + 2]]
        return None

    def ota_channel_probe(self, payload, timeout=8.0):
        """OTA 通道自检：真的走一遍暂存区写入 + 回读，但**不 apply**。

        链路与真升级一致：`0A 12 01` 开始 → `0A 12 02` 分窗 → `0x13` 提交
        （设备会重读整段算 CRC 再写镜像头），只是**不发** `0x14`，所以设备
        不重启、内部 flash 一个字节都不动，当前固件照常运行。最后用
        `0A 0C` 从 NOR 暂存区 `0x080000` 读回 16 B 头，核对 `'MSU2'` 魔数、
        VALID 位、长度和 crc32。

        返回 `(ok, why, n)`；失败原因直接写清楚是哪一步、回包是什么。
        """
        data = bytes(payload)
        total = len(data)
        if total <= 0:
            return False, "测试数据为空", 0

        # 和真升级一样，先把屏流收尾（设备现在可能还停在直写模式）
        for frame in (MiniProtocol.lcd_set_xy(0, 0),
                      MiniProtocol.lcd_set_size(SCREEN_W, SCREEN_H),
                      MiniProtocol.lcd_enter_direct_mode()):
            self.write(frame)
        self.write(MiniProtocol.lcd_raw_end())
        time.sleep(0.05)
        if self._ser is not None:
            self._ser.reset_input_buffer()

        # 开始：真升级会重试 3 次，自检只重试 1 次，失败原因直接带回去
        self.write(MiniProtocol.flash_begin(total))
        st = self._reply_byte(0x0A, 0x12, 6.0)
        if st != 0x06:
            self._drain_upload_window()
            time.sleep(0.2)
            self.write(MiniProtocol.flash_begin(total))
            st = self._reply_byte(0x0A, 0x12, 6.0)
        if st != 0x06:
            return False, ("暂存区不接受（0A 12 begin 回 %s）"
                           % ("没回执" if st is None else "0x%02X" % st)), total

        sent = 0
        while sent < total:
            part = data[sent:sent + 1024]
            self.write(MiniProtocol.flash_window(len(part)))
            self.write(part)
            st = self._reply_byte(0x0A, 0x12, max(timeout, 3.0))
            if st != 0x06:
                return False, ("第 %d 字节处没确认（0A 12 回 %s）"
                               % (sent, "没回执" if st is None else "0x%02X" % st)), total
            sent += len(part)

        crc = zlib.crc32(data) & 0xFFFFFFFF
        self.write(MiniProtocol.flash_commit(crc))
        st = self._reply_byte(0x13, 0x00, max(timeout, 3.0))
        if st != 0x06:
            return False, ("提交没过（0x13 回 %s）"
                           % ("没回执" if st is None else "0x%02X" % st)), total

        hdr = self.nor_read(0x080000, 16)
        if not hdr or len(hdr) < 16:
            return False, "暂存区头读不回来（0A 0C 没回应）", total
        if hdr[0:4] != b"MSU2":
            return False, "暂存区魔数不对（%s）" % hdr[0:4].hex(), total
        hlen = hdr[6] | (hdr[7] << 8) | (hdr[8] << 16) | (hdr[9] << 24)
        hcrc = hdr[10] | (hdr[11] << 8) | (hdr[12] << 16) | (hdr[13] << 24)
        if not hdr[4] & 0x01:
            return False, "镜像头没打 VALID 位（flags=0x%02X）" % hdr[4], total
        if hlen != total:
            return False, "镜像头长度不对（%d != %d）" % (hlen, total), total
        if hcrc != crc:
            return False, "镜像头 crc 不对（%08X != %08X）" % (hcrc, crc), total
        return True, "ok", total

    def boot_trace(self, timeout=1.2, tries=3):
        """复位面包屑（固件 v53 起）：上次停在哪一步、累计复位次数。"""
        for _attempt in range(max(1, int(tries))):
            got = self._boot_trace_once(timeout)
            if got is not None:
                return got
            time.sleep(0.08)
        return None

    @_serial_locked
    def _boot_trace_once(self, timeout=1.2):
        self._ser.reset_input_buffer()
        self._write_raw(MiniProtocol.boot_trace())
        end = time.monotonic() + timeout
        buf = b""
        while time.monotonic() < end:
            buf += self._ser.read(self._ser.in_waiting or 1)
            i = buf.find(b"\x0a\x0d")
            if i >= 0 and len(buf) >= i + 6:
                return {"stage": buf[i + 2],
                        "boots": buf[i + 3] | (buf[i + 4] << 8)}
        return None

    @_serial_locked
    def tile_check(self, sigs, timeout=2.5):
        """分块签名核对（固件 v58）：命令与 100 字节签名一次写完，设备回 10 字节掩码。

        不设“开始收”的 ack：那需要一个来回，而设备的 EP3 只有一笔在途缓冲，
        心跳一来就把 ack 顶掉了；顶掉之后设备还停着等 100 字节，后面所有命令
        都被当成签名吃掉 —— 一个丢包就能让整机像死了一样。
        v58 起设备只是按序收下紧跟命令的 100 字节，收满就回包，不需要握手。
        """
        if self._ser is None or len(sigs) != TILE_N * 2:
            return None
        payload = MiniProtocol.tile_check() + bytes(sigs)
        # 设备的 EP3 只有一笔在途缓冲：上一拍心跳如果还没被主机取走，应答包
        # 可能整包丢掉。所以整条命令重发 3 次，早到的那份会被当成“多余命令”忽略
        # （设备每次都按序收下 100 字节，不会残留状态）。
        for _attempt in range(3):
            self._ser.reset_input_buffer()
            self._write_raw(payload)
            end = time.monotonic() + timeout / 3.0
            buf = b""
            while time.monotonic() < end:
                buf += self._ser.read(self._ser.in_waiting or 1)
                i = buf.find(b"\x0a\x11")
                if i >= 0 and len(buf) >= i + 10 and buf[i + 9] == 0xAA:
                    mask = 0
                    for k in range(7):
                        mask |= buf[i + 2 + k] << (8 * k)
                    return mask
            time.sleep(0.02)
        # 没等到回包：把 100 字节补齐，别让设备停在“等签名”的状态里
        try:
            self._write_raw(bytes(TILE_N * 2))
        except Exception:
            pass
        return None

    @_serial_locked
    def asset_verify_check(self, kind, slot, timeout=8.0):
        """让设备把槽里的图回读一遍算 CRC（固件 v53 起），返回 (ok, crc16)。"""
        self._ser.reset_input_buffer()
        self._write_raw(MiniProtocol.asset_verify(kind, slot))
        end = time.monotonic() + timeout
        buf = b""
        while time.monotonic() < end:
            buf += self._ser.read(self._ser.in_waiting or 1)
            i = buf.find(b"\x0a\x0e")
            if i >= 0 and len(buf) >= i + 6:
                return (buf[i + 2] == 0x06, buf[i + 3] | (buf[i + 4] << 8))
        return (False, 0)

    @_serial_locked
    def anim_probe(self, n=10, timeout=20.0):
        """让设备自己跑 n 次待机心跳（固件 v53 起），用来验证动画不会卡死。"""
        self._ser.reset_input_buffer()
        t0 = time.monotonic()
        self._write_raw(MiniProtocol.anim_probe(n))
        end = time.monotonic() + timeout
        buf = b""
        while time.monotonic() < end:
            buf += self._ser.read(self._ser.in_waiting or 1)
            i = buf.find(b"\x0a\x10")
            if i >= 0 and len(buf) >= i + 6:
                return time.monotonic() - t0
        return None

    @_serial_locked
    def asset_draw(self, kind, slot, timeout=4.0):
        if self._ser is not None:
            self._ser.reset_input_buffer()
        self.write(MiniProtocol.asset_draw(kind, slot))
        st = self._asset_reply(0x04, timeout)
        if st == 0x06:
            return True, "ok"
        if st is None:
            return False, "设备没有回执"
        return False, "设备上那个槽还没有图（先写入再预览）"

    @_serial_locked
    def asset_blit(self, kind, slot, x, y, timeout=6.0):
        """`0A 18`：把槽里的图贴到屏幕 (x, y)（有符号，允许负偏移裁剪）。

        设备端对 fmt=1 用滚动 RLE 解压后贴图；回包 `[0A,18,06/15,...]`。
        返回 `(ok, why)`。
        """
        if self._ser is not None:
            self._ser.reset_input_buffer()
        self.write(MiniProtocol._frame(0x0A, 0x18, kind & 0xFF, slot & 0xFF,
                                       x & 0xFF, y & 0xFF))
        st = self._asset_reply(0x18, timeout)
        if st == 0x06:
            return True, "ok"
        if st is None:
            return False, "设备没有回执"
        return False, "设备上那个槽还没有图（先写入再预览）"

    @_serial_locked
    def asset_erase(self, kind, slot, timeout=4.0):
        if self._ser is not None:
            self._ser.reset_input_buffer()
        self.write(MiniProtocol.asset_erase(kind, slot))
        st = self._asset_reply(0x05, timeout)
        return (st == 0x06), ("ok" if st == 0x06 else "设备没有确认擦除（%r）" % (st,))

    @_serial_locked
    def asset_wipe(self, timeout=25.0):
        """抹掉板载 NOR 里的全部图片与设置（逐槽擦 4K，约 0.3 秒）。"""
        if self._ser is not None:
            self._ser.reset_input_buffer()
        self.write(MiniProtocol.asset_wipe())
        st = self._asset_reply(0x0A, timeout)
        return (st == 0x06), ("ok" if st == 0x06 else "设备没有确认抹除（%r）" % (st,))

    @_serial_locked
    def asset_settings(self, boot_mode, standby_mode, count, dwell,
                       boot_hold=4, timeout=4.0):
        if self._ser is not None:
            self._ser.reset_input_buffer()
        self.write(MiniProtocol.asset_settings(boot_mode, standby_mode,
                                               count, dwell))
        st = self._asset_reply(0x06, timeout)
        if st != 0x06:
            return False, "设备没有确认画面设置（回 %r）" % (st,)
        self.write(MiniProtocol.asset_boot_hold(boot_hold))
        st = self._asset_reply(0x09, timeout)
        if st != 0x06:
            # v37 之前没有这条命令：画面设置已经存进去了，只是停留秒数不生效
            return True, "画面设置已保存；开机停留秒数需要固件 v37 及以上"
        return True, "ok"



    @_serial_locked
    def read_all(self):
        with self._lock:
            # 只要求串口对象还在：写超时会把 _connected 置 False，但设备其实
            # 还活着（只是暂时不收字节），这时候仍然必须能读到它的问候/回包。
            if self._ser is None:
                return b""
            try:
                n = self._ser.in_waiting
                return self._ser.read(n) if n else b""
            except Exception:
                self._connected = False; self._handshaked = False
                return b""

    @_serial_locked
    def query_version(self, timeout=0.4):
        """问固件版本；返回 (major, minor, patch, touch) 或 None。

        设备回 7 字节：56 MA MI PA TH TL AA，末两字节是当前触摸读数，所以
        一次查询就能同时回答“烧的是哪一版”和“ADC 有没有在产出”。
        """
        with self._lock:
            if not self._connected or not self._ser:
                return None
            try:
                self._ser.reset_input_buffer()
                self._write_raw(bytes([0x00, 0x56, 0, 0, 0, 0]))
                t0 = time.monotonic()
                buf = bytearray()
                while time.monotonic() - t0 < timeout:
                    n = self._ser.in_waiting
                    if n:
                        buf.extend(self._ser.read(n))
                    while len(buf) >= 7:
                        if buf[0] == 0x56 and buf[6] == 0xAA:
                            major, minor, patch = buf[1], buf[2], buf[3]
                            touch = (buf[4] << 8) | buf[5]
                            del buf[:7]
                            return major, minor, patch, touch
                        del buf[0]
                    time.sleep(0.005)
            except Exception as e:
                log.debug("查询固件版本失败: %s", e)
            return None

    @_serial_locked
    def devcfg_info(self, timeout=0.5):
        """读设备端存档（板载 NOR）。

        回包 11 字节：09 01 id0 id1 id2 valid dim orient reverse page AA。
        返回 dict 或 None（设备没有 NOR 或未响应）。
        """
        with self._lock:
            if not self._connected or not self._ser:
                return None
            try:
                self._ser.reset_input_buffer()
                self._write_raw(MiniProtocol.devcfg_info())
                t0 = time.monotonic()
                buf = bytearray()
                while time.monotonic() - t0 < timeout:
                    n = self._ser.in_waiting
                    if n:
                        buf.extend(self._ser.read(n))
                    while len(buf) >= 11:
                        if buf[0] == 0x09 and buf[1] == 0x01 and buf[10] == 0xAA:
                            fid = bytes(buf[2:5])
                            info = {
                                "flash_id": fid.hex().upper(),
                                "valid": bool(buf[5]),
                                "dim": buf[6],
                                "orient": buf[7],
                                "reverse": buf[8],
                                "flags": buf[9],
                            }
                            del buf[:11]
                            return info
                        del buf[0]
                    time.sleep(0.005)
            except Exception as e:
                log.debug("读设备存档失败: %s", e)
            return None

    @_serial_locked
    def devcfg_write(self, dim, orient, reverse, flags=0, timeout=3.0):
        """把配置写进设备 NOR；成功返回 True。擦除+编程约 20 ms，别和发帧抢串口。"""
        with self._lock:
            if not self._connected or not self._ser:
                return False
            try:
                self._ser.reset_input_buffer()
                self._write_raw(MiniProtocol.devcfg_write(dim, orient,
                                                          reverse, flags))
                t0 = time.monotonic()
                buf = bytearray()
                while time.monotonic() - t0 < timeout:
                    n = self._ser.in_waiting
                    if n:
                        buf.extend(self._ser.read(n))
                    for i, b in enumerate(buf):
                        if b in (0x06, 0x15):
                            del buf[:i + 1]
                            return b == 0x06
                    time.sleep(0.01)
            except Exception as e:
                log.warning("写设备存档失败: %s", e)
            return False

    @_serial_locked
    def read_adc(self, ch, timeout=0.05):
        with self._lock:
            if not self._connected or not self._ser:
                return None
            t0 = time.monotonic()
            buf = bytearray()
            while time.monotonic() - t0 < timeout:
                try:
                    n = self._ser.in_waiting
                    if n:
                        buf.extend(self._ser.read(n))
                except Exception:
                    self._connected = False; self._handshaked = False
                    return None
                while len(buf) >= 6:
                    if buf[0] == Cmd.ADC and buf[1] == ch:
                        val = (buf[4] << 8) | buf[5]
                        del buf[:6]
                        return val
                    del buf[0]
                time.sleep(0.002)
            return None

    def handshake(self, timeout=2.0):
        """等设备主动问候；收到后回 OKMSU（整段传输模式）。"""
        if self._handshaked:
            return True
        t0 = time.monotonic()
        buf = bytearray()
        while time.monotonic() - t0 < timeout:
            chunk = self.read_all()
            if chunk:
                buf.extend(chunk)
                idx = buf.find(b"\x00OPM")
                if idx >= 0 and len(buf) >= idx + 6:
                    # 设备问候串 \0OPMSU，主机回 \0OKMSU。串里没有版本号，
                    # 版本只从 0x56 版本帧取，所以 version 留空。
                    self.version = ""
                    self.raw_mode = True
                    log.info("设备已握手（整段传输）")
                    self.write(b"\x00OKMSU")
                    time.sleep(0.05)
                    self.read_all()
                    self._handshaked = True
                    return True
            time.sleep(0.01)
        return False


def _normalize_port(port):
    if sys.platform == "win32":
        return str(port)
    p = str(port)
    if p.startswith("/"):
        return p
    if p.startswith(("tty", "cu.", "serial")):
        return "/dev/" + p
    return p
