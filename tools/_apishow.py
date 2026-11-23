import ast, io
def grab(path, names):
    src = io.open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    res = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for ch in node.body:
                if isinstance(ch, ast.FunctionDef) and f"{node.name}.{ch.name}" in names:
                    res[f"{node.name}.{ch.name}"] = ast.get_source_segment(src, ch)
    return res
names = {"FrameEncoder.__init__","FrameEncoder.encode","FrameEncoder._encode_tiles"}
old = grab(r"C:\Users\hsush\Desktop\存档点1\host\msu2_mini.py", names)
new = grab(r"msu2_mini.py", names)
for k in ["FrameEncoder.__init__","FrameEncoder.encode","FrameEncoder._encode_tiles"]:
    print("="*20, k, "OLD")
    print(old.get(k))
    print("="*20, k, "NEW")
    print(new.get(k))
