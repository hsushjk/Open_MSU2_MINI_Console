import io, json, os
CON = r"C:\Users\hsush\Desktop\MSU2_MINI\Open_MSU2_MINI_Console"
cap = [bytes.fromhex(h) for h in json.load(io.open(os.path.join(CON, "tools", "_cap2.json")))]
for n, c in enumerate(cap):
    if len(c) > 400:
        print(n, len(c), c[:40].hex(" "))
print("---- 统计 02 03 07 出现次数 ----")
tot = sum(c.count(b"\x02\x03\x07") for c in cap)
print("stream-begin count:", tot)
for n, c in enumerate(cap):
    i = c.find(b"\x02\x03\x07")
    if i >= 0 and len(c) > 400:
        f = c[i:i+6]
        print("seq", n, "op7 xy:", int.from_bytes(f[2:4],'big'), int.from_bytes(f[4:6],'big'))
