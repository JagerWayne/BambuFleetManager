"""Ad-hoc integrity check: custom classes used in markup/JS vs. defined in CSS."""
import re
import pathlib

root = pathlib.Path(__file__).resolve().parent.parent
css = (root / "assets" / "tailwind.src.css").read_text(encoding="utf-8")
defined = set(re.findall(r"^\s*\.([a-zA-Z][\w-]*)", css, re.M))

used = set()
for rel in ("templates/index.html", "static/js/app.js"):
    txt = (root / rel).read_text(encoding="utf-8")
    for m in re.findall(r'class="([^"]*)"', txt):
        used.update(m.split())
    for m in re.findall(r"class=\\?['\"]([^'\"]*)['\"]", txt):
        used.update(m.split())
    used.update(re.findall(r"classList\.(?:add|remove|toggle|contains)\(['\"]([\w-]+)", txt))
    for m in re.findall(r"className\s*=\s*[`'\"]([^`'\"$]*)", txt):
        used.update(m.split())
    # dynamic fragments inside template literals
    for m in re.findall(r"\bcls\s*\+=\s*['\"]([^'\"]+)", txt):
        used.update(m.split())

custom = sorted(c for c in used if c not in defined)
print("DEFINED custom classes:", len(defined))
print()
print("USED but NOT defined in the stylesheet:")
for c in custom:
    print("  ", c)