"""handshake：同时接受 OPMSU（本项目固件）与老 MSN02（proto25 及更早）。
有这条兜底，可以拿同一份上位机去配两代固件做 A/B 对照。
"""
import ast
import io

P = r"C:\Users\hsush\Desktop\MSU2_MINI\Open_MSU2_MINI_Console\msu2_mini.py"
src = io.open(P, encoding="utf-8").read()

old = '''            chunk = self.read_all()
            if chunk:
                buf.extend(chunk)
                idx = buf.find(b"\\x00OPM")
                if idx >= 0 and len(buf) >= idx + 6:
                    self.version = "02"
                    self.raw_mode = True
                    log.info("检测到 OPMSU 设备（整段传输）")
                    self.write(b"\\x00OPEND")
                    time.sleep(0.05); self.read_all()
                    self._handshaked = True; return True
'''
new = '''            chunk = self.read_all()
            if chunk:
                buf.extend(chunk)
                idx = buf.find(b"\\x00OPM")
                if idx >= 0 and len(buf) >= idx + 6:
                    self.version = "02"
                    self.raw_mode = True
                    log.info("检测到 OPMSU 设备（整段传输）")
                    self.write(b"\\x00OPEND")
                    time.sleep(0.05); self.read_all()
                    self._handshaked = True; return True
                idx = buf.find(b"\\x00MSN")
                if idx >= 0 and len(buf) >= idx + 6:
                    # 老固件（存档点/proto25）：整段传输协议一样，只是没有
                    # 版本查询、没有裁决字节、ADC 恒 0。
                    self.version = chr(buf[idx + 4]) + chr(buf[idx + 5])
                    self.raw_mode = (self.version == "02")
                    log.info("检测到 MSN 设备, 版本=%s%s", self.version,
                             "（整段传输）" if self.raw_mode else "")
                    self.write(b"\\x00MSNCN")
                    time.sleep(0.05); self.read_all()
                    self._handshaked = True; return True
'''
n = src.count(old)
if n != 1:
    raise SystemExit("[FAIL] handshake match %d" % n)
src = src.replace(old, new, 1)
io.open(P, "w", encoding="utf-8", newline="\n").write(src)
ast.parse(src)
print("[ok] handshake fallback + ast.parse")