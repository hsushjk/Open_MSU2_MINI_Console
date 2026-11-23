import io, re
P = r"C:\Users\hsush\Desktop\MSU2_MINI\Open_MSU2_MINI_Console\msu2_mini.py"
L = io.open(P, encoding="utf-8").read().split("\n")
for i, ln in enumerate(L):
    if "def handshake" in ln:
        print("\n".join(L[i:i+40]))
        break
