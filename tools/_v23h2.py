import io, ast
P = r"C:\Users\hsush\Desktop\MSU2_MINI\Open_MSU2_MINI_Console\msu2_mini.py"
L = io.open(P, encoding="utf-8").read().split("\n")
i = None
for k, ln in enumerate(L):
    if ln == "        if raw_mode:":
        i = k; break
assert i is not None
j = None
for k in range(i, i + 12):
    if "_encode_tiles(raw)" in L[k]:
        j = k; break
assert j is not None, L[i:i+12]
new = [
"        if raw_mode:",
"            # v23: one full-screen 0x09 per frame, device answers one verdict",
"            # byte and we only send the next frame after reading it. Tile deltas",
"            # leave a broken tile broken forever - that is why tearing stuck.",
"            self._last_tile_raw = None",
"            self.last_full = True",
"            return bytes(out + self._encode_full(raw))",
]
L[i:j+1] = new
s = "\n".join(L)

old2 = """        fw = getattr(self.transport, "fw_version", None)
        raw = bool(getattr(self.transport, "raw_mode", False))"""
assert old2 in s
new2 = """        fw = getattr(self.transport, "fw_version", None)
        raw = bool(getattr(self.transport, "raw_mode", False))
        if raw and fw is not None and fw >= (23, 0):
            # Full frame written; wait for the device verdict byte. 0x06 = all
            # 12800 pixels arrived, 0x15 = short. We never send the next frame
            # before the ack, so the receive ring can never overflow.
            ok_ack, got = self.transport.read_verdicts(1, 1.5)
            if ok_ack:
                self.self_paced = True
                self.tx_ms = (time.perf_counter() - t0) * 1000.0 - self.enc_ms
                self.frame_bytes = len(frame)
                return True
            self._ack_fails = int(getattr(self, "_ack_fails", 0)) + 1
            if self._ack_fails in (1, 3, 10, 30):
                log.warning("device did not confirm the frame (attempt %d, got %d "
                            "verdict bytes), resending the full frame",
                            self._ack_fails, got)
            self.encoder.reset()
            try:
                self.transport.resync()
            except Exception:
                pass
            return False"""
s = s.replace(old2, new2, 1)
io.open(P, "w", encoding="utf-8", newline="\n").write(s)
ast.parse(s)
print("patched; lines", s.count("\n") + 1)
