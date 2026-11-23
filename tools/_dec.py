import io, json, os, sys
from PIL import Image
sys.path.insert(0, r"C:\Users\hsush\Desktop\MSU2_MINI\Open_MSU2_MINI_Console")
CON = r"C:\Users\hsush\Desktop\MSU2_MINI\Open_MSU2_MINI_Console"
cap = [bytes.fromhex(h) for h in json.load(io.open(os.path.join(CON, "tools", "_cap2.json")))]

W, H = 160, 80
screen = [(0, 0, 0)] * (W * H)

def put(x, y, rgb):
    if 0 <= x < W and 0 <= y < H:
        screen[y * W + x] = rgb

def rgb565(hi, lo):
    v = (hi << 8) | lo
    r = (v >> 11) & 0x1F; g = (v >> 5) & 0x3F; b = v & 0x1F
    return ((r * 255) // 31, (g * 255) // 63, (b * 255) // 31)

stats = []
for wi, p in enumerate(cap):
    pos = 0
    x0 = y0 = 0; x1 = y1 = 0; px = 0; win = 0
    cx = cy = 0
    while pos + 6 <= len(p):
        f = p[pos:pos+6]; pos += 6
        if f[0] == 2 and f[1] == 0:
            x0 = (f[2] << 8) | f[3]; y0 = (f[4] << 8) | f[5]; cx, cy = x0, y0
        elif f[0] == 2 and f[1] == 1:
            x1 = (f[2] << 8) | f[3]; y1 = (f[4] << 8) | f[5]
            win = max(0, x1 - x0) * max(0, y1 - y0)
        elif f[0] == 2 and f[1] == 3 and f[2] == 7:
            x0 = (f[2] << 8) & 0
            pass
        elif f[0] == 2 and f[1] == 3 and f[2] == 0x0D:
            n = (f[3] << 8) | f[4]; mode = f[5]
            data = p[pos:pos+n]; pos += n
            if mode:
                i = 0
                while i + 3 <= len(data):
                    cnt = data[i]; hi = data[i+1]; lo = data[i+2]; i += 3
                    col = rgb565(hi, lo)
                    for _ in range(cnt):
                        put(cx, cy, col); cx += 1; px += 1
                        if cx >= x1: cx = x0; cy += 1
            else:
                for i in range(0, len(data) - 1, 2):
                    put(cx, cy, rgb565(data[i], data[i+1])); cx += 1; px += 1
                    if cx >= x1: cx = x0; cy += 1
        elif f[0] == 2 and f[1] == 3 and f[2] == 0x09:
            stats.append((wi, len(p), win, px, px == win))
        else:
            pass
big = [s for s in stats if s[1] > 400]
print("frames:", len(stats))
print("big frames:", big[:5])
print("mismatch:", [s for s in stats if not s[4]][:8])
img = Image.new("RGB", (W, H)); img.putdata(screen)
img = img.resize((640, 320), Image.NEAREST)
img.save(os.path.join(CON, "tools", "_cap_render.png"))
print("saved render")
