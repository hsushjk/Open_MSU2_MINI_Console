import io, os, sys, time, json, threading
CON = r"C:\Users\hsush\Desktop\MSU2_MINI\Open_MSU2_MINI_Console"
os.chdir(CON); sys.path.insert(0, CON)
import msu2_mini as M
CAP = []
_w = M.SerialTransport.write
def w(self, data):
    b = bytes(data); CAP.append(len(b)); return _w(self, data)
M.SerialTransport.write = w
def driver():
    time.sleep(8)
    print("writes", len(CAP), "big", sum(1 for n in CAP if n > 4000), "max", max(CAP or [0]), flush=True)
    os._exit(0)
threading.Thread(target=driver, daemon=True).start()
sys.argv = ["msu2_mini.py", "--headless", "--config", r"C:\Users\hsush\Desktop\config.json"]
M.main()
