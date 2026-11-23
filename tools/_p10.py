import io, ast

P = r"C:\Users\hsush\Desktop\happy\Open_MSU2_MINI_Console\msu2_mini.py"
L = io.open(P, encoding="utf-8").read().split("\n")


def find(text, start=0):
    for i in range(start, len(L)):
        if L[i] == text:
            return i
    raise AssertionError("not found: " + text)


# 1) MiniDevice.__init__ : 限速状态
i = find("        self.screen_reversed = False")
L[i + 1:i + 1] = [
    "        # 发送限速（字节/秒）。设备的消费者是主循环 + 屏 DMA，实测能吃下",
    "        # 180–230 KB/s。默认 180 KB/s：丢了就降速，连续收齐再慢慢抬头。",
    "        self._pace_bps = 180000.0",
]

# 2) 写入节奏：固定 2 ms -> 按限速算间隔
i = find("        seg_write = 2048")
assert L[i + 1] == "        for i in range(0, len(payload), seg_write):"
assert L[i + 5] == "            if i + seg_write < len(payload):"
assert L[i + 6] == "                time.sleep(0.002)"
L[i:i + 7] = [
    "        seg_write = 2048",
    "        rate = max(40000.0, min(240000.0, getattr(self, \"_pace_bps\", 180000.0)))",
    "        gap = seg_write / rate",
    "        for i in range(0, len(payload), seg_write):",
    "            if not self.transport.write(payload[i:i + seg_write]):",
    "                ok = False",
    "                break",
    "            if i + seg_write < len(payload):",
    "                time.sleep(gap)",
]

# 3) 收齐 -> 缓慢加速
i = find("            if ok_ack:")
assert L[i + 1] == "                self.self_paced = True"
L[i + 1:i + 2] = [
    "                self.self_paced = True",
    "                self._pace_bps = min(240000.0,",
    "                                     getattr(self, \"_pace_bps\", 180000.0) * 1.04)",
]

# 4) 收到 0x15 -> 降速重发
i = find('            self._ack_fails = int(getattr(self, "_ack_fails", 0)) + 1')
assert L[i + 1] == "            if self._ack_fails in (1, 3, 10, 30):"
assert 'resending the full frame",' in L[i + 3]
L[i + 1:i + 5] = [
    "            # 设备说像素不够：字节在路上真丢了。降速重发。",
    "            self._pace_bps = max(40000.0, getattr(self, \"_pace_bps\", 180000.0) * 0.6)",
    "            if self._ack_fails in (1, 3, 10, 30):",
    "                log.warning(\"device did not confirm the frame (attempt %d, got %d \"",
    "                            \"verdict bytes), resending at %.0f KB/s\",",
    "                            self._ack_fails, got, self._pace_bps / 1000.0)",
]

s = "\n".join(L)
ast.parse(s)
io.open(P, "w", encoding="utf-8", newline="\n").write(s)
print("pacing applied")