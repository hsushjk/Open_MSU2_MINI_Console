import io, os, sys, time, json, threading
CON = r"C:\Users\hsush\Desktop\MSU2_MINI\Open_MSU2_MINI_Console"
os.chdir(CON); sys.path.insert(0, CON)
import msu2_mini as M
CAP = []
_w = M.SerialTransport.write
def w(self, data):
    b = bytes(data); CAP.append(b); return _w(self, data)
M.SerialTransport.write = w
def driver():
    time.sleep(10)
    io.open(os.path.join(CON, "tools", "_cap3.json"), "w").write(json.dumps([c.hex() for c in CAP]))
    print("captured", len(CAP), "writes, bytes", sum(len(c) for c in CAP), flush=True)
    os._exit(0)
threading.Thread(target=driver, daemon=True).start()
sys.argv = ["msu2_mini.py", "--headless", "--config", r"C:\Users\hsush\Desktop\config.json"]
M.main()
