import io
P = r"C:\Users\hsush\Desktop\MSU2_MINI\Open_MSU2_MINI_Console\msu2_mini.py"
L = io.open(P, encoding="utf-8").read().split("\n")
for i, ln in enumerate(L):
    if ln.strip().startswith("def encode(") or ln.strip().startswith("def _encode_tiles("):
        print("=== @%d" % (i+1))
        print("\n".join("%4d %s" % (k+1, L[k]) for k in range(i, min(len(L), i+42))))
