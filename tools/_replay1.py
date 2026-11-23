import io, json, os, sys, time
import serial
CON = r"C:\Users\hsush\Desktop\MSU2_MINI\Open_MSU2_MINI_Console"
cap = [bytes.fromhex(h) for h in json.load(io.open(os.path.join(CON, "tools", "_cap2.json")))]
ser = serial.Serial("COM4", 19200, timeout=0.20)
ser.reset_input_buffer()
# 握手
best = None
for _ in range(30):
    b = ser.read(64)
    if b"\x00MSN" in b or b"\x00OPM" in b:
        best = b; break
print("greet:", None if best is None else best[:16])
ser.write(b"\x00OPMSU" if (best and b"\x00OPM" in best) else b"\x00MSNCN")
time.sleep(0.25); ser.reset_input_buffer()
# 开发调试状态
ser.write(bytes((2,3,0x5A,1,0,0))); time.sleep(0.15); ser.reset_input_buffer()

def status_of(payload, gap=0.0):
    ser.reset_input_buffer()
    i = 0
    while i < len(payload):
        ser.write(payload[i:i+8192]); i += 8192
        if gap: time.sleep(gap)
    end = time.time() + 0.6
    buf = b""
    while time.time() < end:
        buf += ser.read(256)
        ends = buf.count(b"\x02\x03\x09")
        if ends and len(buf) >= 6 * ends: break
    return buf

def count_seg(payload):
    return payload.count(b"\x02\x03\x09")

# 1) 只回放第一帧整屏
p = cap[1]
print("full frame bytes:", len(p), "segments:", count_seg(p))
raw = status_of(p)
print("status raw:", raw[:60].hex(" "))
sts = [raw[k:k+6] for k in range(0, len(raw)-5) if raw[k] == 0x55]
print("status frames:", len(sts), [s.hex(" ") for s in sts][:6])
ser.close()
