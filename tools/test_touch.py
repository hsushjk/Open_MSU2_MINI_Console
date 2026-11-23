import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from msu2_core.touch import KIND_DOUBLE, KIND_LONG, KIND_SINGLE, TouchGestures

BASE = 3925
THR = 60
DT = 0.03
PRESS = BASE - THR - 40
WIN = 0.5
ok = True


def check(name, cond, detail=""):
    global ok
    ok = ok and bool(cond)
    print(("PASS " if cond else "FAIL ") + name + (" " + detail if detail else ""))


def run(seq, g=None):
    g = g or TouchGestures(threshold=THR, double_window=WIN, min_gap=0.012,
                           release_ratio=0.3, release_debounce=1)
    events = []
    t = 0.0
    for v in seq:
        for e in g.feed(v, t):
            events.append((t, e))
        t += DT
    return g, events


def names(events):
    return [e for _, e in events]


# 1) 单点：抬手后要等满一个双击窗口才报单击
_, ev = run([BASE] * 20 + [PRESS] * 4 + [BASE] * 40)
check("单点", names(ev) == [KIND_SINGLE], str(names(ev)))
if ev:
    release_ts = 24 * DT
    fire_ts = ev[0][0]
    ok_shift = release_ts + WIN - 2 * DT <= fire_ts <= release_ts + WIN + 2 * DT
    check("单击时机", ok_shift, "fire=%.2f expect=%.2f" % (fire_ts, release_ts + WIN))

# 2) 短促双击：两下之间只隔 3 个采样，第二次按下就判
_, ev = run([BASE] * 20 + [PRESS] * 3 + [BASE] * 3 + [PRESS] * 3 + [BASE] * 20)
check("双击", names(ev) == [KIND_DOUBLE], str(names(ev)))

# 3) 长按
_, ev = run([BASE] * 20 + [PRESS] * 40 + [BASE] * 10)
check("长按", names(ev) == [KIND_LONG], str(names(ev)))

# 4) 阈值附近抖动：只按一次，读数上下跳，不应误判双击
jitter = [BASE - THR + 2, BASE - THR - 2, BASE - THR + 3, BASE - THR - 3, BASE]
_, ev = run([BASE] * 20 + jitter + [BASE] * 20)
check("抖动不误判", names(ev) == [], str(names(ev)))

# 5) 两下间隔超过双击窗口：必须是两次单击（不能吞掉第一下）
_, ev = run([BASE] * 20 + [PRESS] * 3 + [BASE] * 30 + [PRESS] * 3 + [BASE] * 40)
check("两次单击", names(ev) == [KIND_SINGLE, KIND_SINGLE], str(names(ev)))

# 6) 基准缓慢漂移仍能触发
seq = []
curve = 0.0
for _ in range(300):
    seq.append(BASE + min(30.0, curve))
    curve += 0.2
seq += [BASE + 30 - THR - 40] * 4 + [BASE + 30] * 30
_, ev = run(seq)
check("基准漂移", names(ev) == [KIND_SINGLE], str(names(ev)))

# 7) 单击之后隔开再长按：单击要补发出来，不能只剩长按
_, ev = run([BASE] * 20 + [PRESS] * 3 + [BASE] * 25 + [PRESS] * 40 + [BASE] * 10)
check("单击后长按", names(ev) == [KIND_SINGLE, KIND_LONG], str(names(ev)))

# 8) 抬手只被采样到一拍（点得快、采样慢）：仍要判成双击
near = BASE - 5
_, ev = run([BASE] * 20 + [PRESS] * 3 + [near] + [PRESS] * 3 + [BASE] * 30)
check("抬手只一拍", names(ev) == [KIND_DOUBLE], str(names(ev)))

# 9) 长按之后抬手：不能再补一个单击
_, ev = run([BASE] * 20 + [PRESS] * 40 + [BASE] * 40)
check("长按后抬手", names(ev) == [KIND_LONG], str(names(ev)))

# 10) 手指慢慢压下去再慢慢松开：只有单击，不能判成双击
slow = [BASE - THR // 3, BASE - THR - 20, BASE - THR - 40, PRESS,
        BASE - THR - 20, BASE - THR // 3, BASE - THR // 6, BASE]
_, ev = run([BASE] * 20 + slow + [BASE] * 40)
check("慢按慢放", names(ev) == [KIND_SINGLE], str(names(ev)))

# 11) 连点三次：双击之后必须还能把第三下判成单击
_, ev = run([BASE] * 20 + ([PRESS] * 3 + [BASE] * 3) * 3 + [BASE] * 40)
check("三连点", names(ev) == [KIND_DOUBLE, KIND_SINGLE], str(names(ev)))

print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)