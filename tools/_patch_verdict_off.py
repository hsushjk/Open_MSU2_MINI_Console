import io
P = r"C:\Users\hsush\Desktop\MSU2_MINI\Open_MSU2_MINI_Console\msu2_mini.py"
s = io.open(P, encoding="utf-8").read()
old = """        if (not raw) or fw is None or fw < (13, 0):
            # 老固件（proto25 及更早）不回裁决字节，只能靠发送间隔兜底。"""
new = """        # 裁决字节默认关闭：v13~v19 的事实是，任何往像素流附近加的东西
        # （栅栏前缀、空闲探询、逐帧回执）都会引入新的错帧，而它对可见
        # 故障的贡献是零。需要定位问题时再设 MSU2_VERDICT=1 打开。
        if _os.environ.get("MSU2_VERDICT", "0") != "1":
            fw = None
        if (not raw) or fw is None or fw < (13, 0):
            # 老固件（proto25 及更早）不回裁决字节，只能靠发送间隔兜底。"""
assert old in s
s = s.replace(old, new, 1)
io.open(P, "w", encoding="utf-8", newline="\n").write(s)
print("[ok] verdict path off by default")
