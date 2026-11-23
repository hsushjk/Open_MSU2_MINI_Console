import io, json, os
CON = r"C:\Users\hsush\Desktop\MSU2_MINI\Open_MSU2_MINI_Console"
cap = [bytes.fromhex(h) for h in json.load(io.open(os.path.join(CON, "tools", "_cap3.json")))]
s = b"".join(cap)
print("stream bytes:", len(s))
print("head:", s[:80].hex(" "))
print("count 02 03 09:", s.count(b"\x02\x03\x09"), " 02 03 07:", s.count(b"\x02\x03\x07"), " 02 03 0d:", s.count(b"\x02\x03\x0d"), " 02 03 0c:", s.count(b"\x02\x03\x0c"), " 02 03 08:", s.count(b"\x02\x03\x08"))
print("write sizes:", [len(c) for c in cap][:12])
