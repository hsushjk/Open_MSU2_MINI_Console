import io, ast

P = r"C:\Users\hsush\Desktop\happy\Open_MSU2_MINI_Console\msu2_mini.py"
s = io.open(P, encoding="utf-8").read()
before = s.count("180000.0")
s = s.replace("180000.0", "140000.0")
ast.parse(s)
io.open(P, "w", encoding="utf-8", newline="\n").write(s)
print("replaced", before, "occurrences of 180000.0 -> 140000.0")