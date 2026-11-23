import ast, io, hashlib, sys

def collect(path):
    src = io.open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    lines = src.splitlines()
    out = {}
    def walk(node, prefix=""):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + ch.name
                body = ast.get_source_segment(src, ch) or ""
                out[name] = (hashlib.md5(body.encode()).hexdigest(), len(body.splitlines()), ch.lineno)
            elif isinstance(ch, ast.ClassDef):
                walk(ch, prefix + ch.name + ".")
            elif isinstance(ch, ast.If):
                walk(ch, prefix)
    walk(tree)
    top = {}
    for node in tree.body:
        seg = ast.get_source_segment(src, node) or ""
        top[type(node).__name__ + ":" + str(getattr(node, "name", getattr(node, "targets", [{}])[0].id if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) else "?"))] = (
            hashlib.md5(seg.encode()).hexdigest(), len(seg.splitlines()))
    return out, top, len(lines)

old_f, old_t, old_n = collect(r"C:\Users\hsush\Desktop\存档点1\host\msu2_mini.py")
new_f, new_t, new_n = collect(r"msu2_mini.py")
print("lines: old", old_n, "new", new_n)
print("\n== functions only in NEW ==")
for k in sorted(set(new_f) - set(old_f)):
    print("  +", k, new_f[k][1], "lines @", new_f[k][2])
print("\n== functions only in OLD ==")
for k in sorted(set(old_f) - set(new_f)):
    print("  -", k, old_f[k][1], "lines @", old_f[k][2])
print("\n== changed function bodies ==")
chg = []
for k in sorted(set(old_f) & set(new_f)):
    if old_f[k][0] != new_f[k][0]:
        chg.append((k, old_f[k][1], new_f[k][1], old_f[k][2], new_f[k][2]))
for k, ol, nl, oline, nline in chg:
    print(f"  ~ {k:55s} old {ol:4d} -> new {nl:4d} lines  (old@{oline} new@{nline})")
print("changed:", len(chg), "added:", len(set(new_f)-set(old_f)), "removed:", len(set(old_f)-set(new_f)))
