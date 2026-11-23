import io
P = r"C:\Users\hsush\Desktop\MSU2_MINI\Open_MSU2_MINI_Console\msu2_mini.py"
L = io.open(P, encoding="utf-8").read().split("\n")
print("\n".join("%4d %s" % (k+1, L[k]) for k in range(799, 830)))
print("---- lcd_segment_payload ----")
for i, ln in enumerate(L):
    if "def lcd_segment_payload" in ln:
        print("\n".join("%4d %s" % (k+1, L[k]) for k in range(i, i+26)))
        break
print("---- show_image ----")
for i, ln in enumerate(L):
    if ln.strip().startswith("def show_image("):
        print("\n".join("%4d %s" % (k+1, L[k]) for k in range(i, i+22)))
