import io, json, os, time
import serial
CON = r"C:\Users\hsush\Desktop\MSU2_MINI\Open_MSU2_MINI_Console"
cap = [bytes.fromhex(h) for h in json.load(io.open(os.path.join(CON, "tools", "_cap3.json")))]
ser = serial.Serial("COM4", 19200, timeout=0.05)
ser.reset_input_buffer()
buf = b""
t0 = time.time()
while time.time() - t0 < 2:
    buf += ser.read(256)
    if b"\x00MSN" in buf: break
print("greet:", buf[:12])
ser.write(b"\x00MSNCN"); time.sleep(0.2); ser.reset_input_buffer()
ser.write(bytes((0, 0x5A, 1, 0, 0, 0))); time.sleep(0.2)
print("debug ack:", ser.read(32).hex(" "))
ser.reset_input_buffer()

# 原样回放：写顺序、写大小都与 app 一致
t0 = time.time()
for c in cap:
    ser.write(c)
write_ms = (time.time() - t0) * 1000
time.sleep(0.8)
raw = ser.read(65536)
elapsed = (time.time() - t0) * 1000
win = cap[1:5]
sts = [raw[k:k+6] for k in range(len(raw) - 5) if raw[k] == 0x55]
print("writes:", len(cap), "write_ms:", round(write_ms, 1))
print("status frames:", len(sts))
for s in sts[:14]:
    print("  done=%d drop=%d ms=%d.%d" % (s[1] | (s[2] << 8), s[3], s[4], s[5]))
ser.close()
