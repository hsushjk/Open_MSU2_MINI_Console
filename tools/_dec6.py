import io, json, os
from PIL import Image
CON = r"C:\Users\hsush\Desktop\MSU2_MINI\Open_MSU2_MINI_Console"
cap = [bytes.fromhex(h) for h in json.load(io.open(os.path.join(CON, "tools", "_cap3.json")))]
s = b"\x00".join([]) or b"".join(cap)
W, H = 160, 80
screen = [(0, 0, 0)] * (W * H)
def c565(hi, lo):
    v = (hi << 8) | lo
    return (((v >> 11) & 0x1F) * 255 // 31, ((v >> 5) & 0x3F) * 255 // 63, (v & 0x1F) * 255 // 31)
x = y = x0 = y0 = 0; x1 = W; y1 = H
pos = 0; segs = []; seg_px = 0
while pos + 6 <= len(s):
    f = s[pos:pos+6]
    if f[0] == 2 and f[1] == 0 and f[2] == 0 and f[3] == 0 or (f[0] == 2 and f[1] == 0):
        x0 = x = (f[2] << 8) | f[3]; y0 = y = (f[4] << 8) | f[5]; pos += 6; continue
    if f[0] == 2 and f[1] == 1:
        x1 = (f[2] << 8) | f[3]; y1 = (f[4] << 8) | f[5]; pos += 6; continue
    if f[0] == 2 and f[1] == 3 and f[2] == 7:
        x = x0; y = y0; pos += 6; continue
    if f[0] == 2 and f[1] == 3 and f[2] in (0x0C, 0x0D):
        n = (f[3] << 8) | f[4]; mode = f[5]; seg = s[pos+6:pos+6+n]; pos += 6 + n
        cnt = 0
        if mode == 1:
            i = 0
            while i + 3 <= len(seg):
                c, hi, lo = seg[i], seg[i+1], seg[i+2]; i += 3
                col = c565(hi, lo)
                for _ in range(c):
                    if x >= x1: x = x0; y += 1
                    if 0 <= x < W and 0 <= y < H: screen[y*W+x] = col
                    x += 1; cnt += 1
        else:
            i = 0
            while i + 1 < len(seg):
                col = c565(seg[i], seg[i+1]); i += 2
                if x >= x1: x = x0; y += 1
                if 0 <= x < W and 0 <= y < H: screen[y*W+x] = col
                x += 1; cnt += 1
        seg_px += cnt
        continue
    if f[0] == 2 and f[1] == 3 and f[2] == 9:
        segs.append(((x1-x0)*(y1-y0), seg_px)); seg_px = 0; pos += 6; continue
    pos += 1
print("segments:", len(segs))
print("first 4:", segs[:4])
bad = [t for t in segs if t[0] != t[1]]
print("mismatch:", len(bad), bad[:6])
img = Image.new("RGB", (W, H)); img.putdata(screen)
img.resize((640, 320), Image.NEAREST).save(os.path.join(CON, "tools", "_cap3_render.png"))
print("saved")
