import io, json
for p in (r"C:\Users\hsush\Desktop\config.json", r"C:\Users\hsush\Desktop\MSU2_MINI\Open_MSU2_MINI_Console\config.json"):
    try:
        c = json.load(io.open(p, encoding="utf-8"))
    except Exception as e:
        print(p, "ERR", e); continue
    d = c.get("devices", [{}])[0]
    print(p)
    print("   background:", json.dumps(d.get("background"), ensure_ascii=False))
    print("   display:", json.dumps(d.get("display"), ensure_ascii=False))
    print("   page:", c.get("page"), "api:", c.get("web"))
