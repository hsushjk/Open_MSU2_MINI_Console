import io
P = r"C:\Users\hsush\Desktop\MSU2_MINI\Open_MSU2_MINI_Console\msu2_mini.py"
L = io.open(P, encoding="utf-8").read().split("\n")
def show(name, n=60):
    for i, ln in enumerate(L):
        if ln.strip().startswith("def %s(" % name):
            print("=== %s @%d" % (name, i+1))
            print("\n".join("%4d %s" % (k+1, L[k]) for k in range(i, min(len(L), i+n))))
            return
show("read_verdicts", 30)
show("_show_image_once", 60)
