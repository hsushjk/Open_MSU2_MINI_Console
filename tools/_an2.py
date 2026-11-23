import io, json, os
CON = r"C:\Users\hsush\Desktop\MSU2_MINI\Open_MSU2_MINI_Console"
cap = [bytes.fromhex(h) for h in json.load(io.open(os.path.join(CON, "tools", "_cap2.json")))]
print("writes:", len(cap), "bytes:", sum(map(len, cap)))
big = [c for c in cap if len(c) > 400]
print("writes >400B:", len(big), "sizes:", [len(c) for c in big][:12])
# 拆帧：payload 里以 02 00 / 02 01 / 02 03 07 开头的是命令帧
from collections import Counter
kinds = Counter()
for c in cap:
    i = 0
    while i + 6 <= len(c):
        cls, sub = c[i], c[i+1]
        if cls == 2 and sub in (0, 1):
            kinds[("cmd", sub)] += 1; i += 6
        elif cls == 2 and sub == 3:
            kinds[("op", c[i+2])] += 1; i += 6
        else:
            kinds[("other", cls, sub)] += 1; i += 1
print("kinds:", kinds.most_common(12))
