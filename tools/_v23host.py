import io, ast
P = r"C:\Users\hsush\Desktop\MSU2_MINI\Open_MSU2_MINI_Console\msu2_mini.py"
s = io.open(P, encoding="utf-8").read()

# 1) raw 模式一律整帧（一个 0x09 一条回执）
old1 = """        if raw_mode:
            # 只发真彩 RGB565（65536 色），只发变化的 16×16 块。"""
assert old1 in s
s = s.replace(old1, """        if raw_mode:
            # v23 起：每帧整屏一个 0x09，设备回一个裁决字节，主机读到才发下一帧。
            # 差分块会让“坏掉的那一块”永远不再被重发，是花屏黏住不放的根源。
            self._last_tile_raw = None
            self.last_full = True
            return bytes(out + self._encode_full(raw))

        if False:
            # 只发真彩 RGB565（65536 色），只发变化的 16×16 块。""", 1)

# 2) 写入成功后按 v23 读一个裁决字节
old2 = """        fw = getattr(self.transport, "fw_version", None)
        raw = bool(getattr(self.transport, "raw_mode", False))"""
assert old2 in s
new2 = """        fw = getattr(self.transport, "fw_version", None)
        raw = bool(getattr(self.transport, "raw_mode", False))
        if raw and fw is not None and fw >= (23, 0):
            # 整帧写完，等设备的一个裁决字节：0x06 = 12 800 个像素都到了，
            # 0x15 = 少收了（这一帧作废，重发）。主机不等回执就不发下一帧，
            # 接收环永远不会被挤爆——这是唯一不靠“猜时序”的限速方式。
            ok_ack, got = self.transport.read_verdicts(1, 1.5)
            if ok_ack:
                self.self_paced = True
                self.tx_ms = (time.perf_counter() - t0) * 1000.0 - self.enc_ms
                self.frame_bytes = len(frame)
                return True
            self._ack_fails = int(getattr(self, "_ack_fails", 0)) + 1
            if self._ack_fails in (1, 3, 10, 30):
                log.warning("设备没有确认这一帧（第 %d 次，收到 %d 个裁决字节），"
                            "重发整帧", self._ack_fails, got)
            self.encoder.reset()
            try:
                self.transport.resync()
            except Exception:
                pass
            return False"""
s = s.replace(old2, new2, 1)
io.open(P, "w", encoding="utf-8", newline="\n").write(s)
ast.parse(s)
print("patched; lines", s.count(chr(10)) + 1)
