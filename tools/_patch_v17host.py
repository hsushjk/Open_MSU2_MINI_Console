"""v17 上位机配套：删掉“栅栏前缀 / 每帧空闲探询”，改成
    写完一帧 -> 读该帧的裁决字节 -> 按结果决定是否重发。
    这是唯一一条不往像素流里插字节的“设备已经吃完了”信号。
"""
import io
import sys

P = r"C:\Users\hsush\Desktop\MSU2_MINI\Open_MSU2_MINI_Console\msu2_mini.py"
src = io.open(P, encoding="utf-8").read()
orig = len(src)


def sub(old, new, tag):
    global src
    n = src.count(old)
    if n != 1:
        raise SystemExit("[FAIL] %s: found %d\n---\n%s" % (tag, n, old[:400]))
    src = src.replace(old, new, 1)
    print("[ok]", tag)


# 1) 不再往帧前面塞栅栏
sub("""        payload = frame
        if getattr(self.transport, "raw_mode", False):
            # 每帧开头先发同步栅栏：万一上一帧把设备留在“等像素”里，设备看到
            # 栅栏会立刻作废半开的流、回到命令模式，这一帧就不会被吃掉开头。
            payload = SYNC_FENCE + bytes(frame)
""", """        payload = frame
""", "no-fence-prefix")

# 2) resync：不再发栅栏，改成“把像素计数喂到 0”+ 清回执
sub('''    def resync(self, timeout=0.4):
        """把设备从“等像素”拉回“等命令”：连发栅栏，等它回 0x06。"""
        with self._lock:
            if not self._connected or not self._ser:
                return False
            try:
                self._ser.reset_input_buffer()
                # 先补几个收流命令（对老固件是唯一的出路：把卡在半路的
                # 像素计数喂到 0），再连发同步栅栏把设备拉回命令模式。
                for _ in range(8):
                    self._ser.write(END_FRAME)
                for _ in range(3):
                    self._ser.write(SYNC_FENCE)
                t0 = time.monotonic()
                while time.monotonic() - t0 < timeout:
                    n = self._ser.in_waiting
                    if n:
                        chunk = self._ser.read(n)
                        # v15 起栅栏回执是 0x07；老固件回 0x06，都算同步成功
                        if b"\\x07" in chunk or b"\\x06" in chunk:
                            # 栅栏发了 3 个，设备会回 3 个 0x06；剩下的如果留在
                            # 缓冲里，下一帧读回执时会被当成“这帧收齐了”。
                            time.sleep(0.02)
                            self._ser.reset_input_buffer()
                            return True
                    time.sleep(0.004)
                return False
            except Exception:
                self._connected = False; self._handshaked = False
                return False
''', '''    def resync(self, timeout=0.5):
        """把设备从“等像素”拉回“等命令”。

        v17 固件里已经没有栅栏：设备 400 ms 没有进展就会自己作废当前段。
        这里只做两件事——把半路的像素计数喂到 0（补足 2048 字节上限），
        再把迟到的回执清掉，免得被下一帧当成“收齐了”。
        """
        with self._lock:
            if not self._connected or not self._ser:
                return False
            try:
                blob = END_FRAME * 420          # 2520 B，足够填掉任何 raw_left
                self._ser.write(blob)
                time.sleep(max(0.05, len(blob) / 100000.0))
                self._ser.reset_input_buffer()
                return True
            except Exception:
                self._connected = False; self._handshaked = False
                return False
''', "resync")

# 3) 收尾：用裁决字节确认整帧，去掉 v16 的 sleeps + 0x57 探询
start = src.index('        fw = getattr(self.transport, "fw_version", None)\n        if bool(getattr(self.transport, "raw_mode", False)) and fw is not None and fw >= (16, 0):')
end = src.index('        self.tx_ms = (time.perf_counter() - t0) * 1000.0 - self.enc_ms\n        self.frame_bytes = len(frame)\n        return True\n', start)
end += len('        self.tx_ms = (time.perf_counter() - t0) * 1000.0 - self.enc_ms\n        self.frame_bytes = len(frame)\n        return True\n')
new_tail = '''        fw = getattr(self.transport, "fw_version", None)
        raw = bool(getattr(self.transport, "raw_mode", False))
        if (not raw) or fw is None or fw < (13, 0):
            # 老固件（proto25 及更早）不回裁决字节，只能靠发送间隔兜底。
            self.self_paced = False
            self.transport.read_all()
            self.tx_ms = (time.perf_counter() - t0) * 1000.0 - self.enc_ms
            self.frame_bytes = len(frame)
            return True
        # v13 起，设备每收完一个收流命令就回一个裁决字节（0x06 收齐 /
        # 0x15 丢了）。读到最后一个就等于“这一帧已经整个进设备了”。
        # 关键：这是唯一不往像素流里插字节的同步方式。以前那套“栅栏前缀”
        # 和“0x57 空闲探询”都会在设备还没吃完时把上一帧打断/把命令塞进
        # 像素里，正是花屏的来源，已经全部删掉。
        want = frame.count(END_FRAME)
        ok_ack, got = (True, want) if want == 0 else self.transport.read_verdicts(want, 0.6)
        if not ok_ack:
            self._ack_fails = int(getattr(self, "_ack_fails", 0)) + 1
            if self._ack_fails in (1, 3, 10, 30):
                log.warning("这一帧设备没收全（%d/%d 个回执），重发", got, want)
            self.encoder.reset()
            self._recover()
            return False
        self._ack_fails = 0
        self.self_paced = True
        self.tx_ms = (time.perf_counter() - t0) * 1000.0 - self.enc_ms
        self.frame_bytes = len(frame)
        return True

    def _recover(self):
        """设备可能停在“等像素”里：等它自己的 400 ms 看门狗作废该段，
        再把像素计数喂到 0。"""
        try:
            if not getattr(self.transport, "connected", False):
                return
            time.sleep(0.45)
            self.transport.resync()
        except Exception:
            pass
'''
src = src[:start] + new_tail + src[end:]
print("[ok] verdict-tail")

# 4) open()：版本查询失败不再发栅栏，也不再误报“固件偏旧”
sub('''            fwv = getattr(self.transport, "fw_version", None)
            if fwv is not None and fwv < (15, 0):
                log.warning("固件版本 %d.%d 偏旧：整屏帧超过 200 ms 会被设备判失败，"
                            "请烧 MSU2_open_v15.hex", fwv[0], fwv[1])
            if fwv is None:
                # 上手时设备可能正卡在“等像素”里（上一轮崩在半路），版本查询
                # 不理人。先发栅栏把它拉回命令模式，再问一次版本。
                try:
                    self.transport.resync()
                    self.transport.query_version()
                except Exception:
                    pass
''', '''            fwv = getattr(self.transport, "fw_version", None)
            if fwv is None:
                log.info("固件没有回版本号（proto25 或更早），按老固件处理")
            elif fwv < (13, 0):
                log.warning("固件版本 %d.%d 太旧，请烧 MSU2_open_v17.hex", fwv[0], fwv[1])
''', "open-version")

io.open(P, "w", encoding="utf-8", newline="\n").write(src)
print("[ok] written %d -> %d chars" % (orig, len(src)))

import ast
ast.parse(src)
print("[ok] ast.parse")