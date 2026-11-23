import io, json, os, sys
from PIL import Image
CON = r"C:\Users\hsush\Desktop\MSU2_MINI\Open_MSU2_MINI_Console"
cap = [bytes.fromhex(h) for h in json.load(io.open(os.path.join(CON, "tools", "_cap2.json")))]
W, H = 160, 80
screen = [(0, 0, 0)] * (W * H)

def rgb565(hi, lo):
    v = (hi << 8) | lo
    return (((v >> 11) & 0x1F) * 255 // 31, ((v >> 5) & 0x3F) * 255 // 63, (v & 0x1F) * 255 // 31)

class Cur:
    def __init__(s): s.x = s.y = 0; s.x0 = s.y0 = s.x1 = s.y1 = 0
    def put(s, rgb):
        if s.x >= s.x1: s.x = s.x0; s.y += 1
        if 0 <= s.x < W and 0 <= s.y < H: screen[s.y * W + s.x] = rgb
        s.x += 1

cur = Cur()
for p in cap:
    pos = 0
    while pos + 6 <= len(p):
        f = p[pos:pos+6]; pos += 6
        if f[0] == 2 and f[1] == 0:
            cur.x0 = cur.x = (f[2] << 8) | f[3]; cur.y0 = cur.y = (f[4] << 8) | f[5]
        elif f[0] == 2 and f[1] == 1:
            cur.x1 = (f[2] << 8) | f[3]; cur.y1 = (f[4] << 8) | f[5]
        elif f[0] == 2 and f[1] == 3 and f[2] == 7:
            cur.x = cur.x0; cur.y = cur.y0
        elif f[0] == 2 and f[1] == 3 and f[2] == 0x0D:
            n = (f[3] << 8) | f[4]; mode = f[5]
            data = p[pos:pos+n]; pos += n
            if mode:
                i = 0
                while i + 3 <= len(data):
                    cnt, hi, lo = data[i], data[i+1], data[i+2]; i += 3
                    col = rgb565(hi, lo)
                    for _ in range(cnt): cur.put(col)
            else:
                for i in range(0, len(data) - 1, 2): cur.put(rgb565(data[i], data[i+1]))
        elif f[0] == 2 and f[1] == 3 and f[2] == 0x09:
            pass
img = Image.new("RGB", (W, H)); img.putdata(screen)
img.resize((640, 320), Image.NEAREST).save(os.path.join(CON, "tools", "_cap_render.png"))
print("ok")
