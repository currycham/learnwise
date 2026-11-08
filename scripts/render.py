"""把学习页 JSON 渲染成单个 HTML，并生成一个样例目录页。

用法：python3 scripts/render.py fixtures/*.json   → 输出到 out/
"""
import html
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.schema import validate  # noqa: E402

TEMPLATE = (ROOT / "app" / "templates" / "learn.html").read_text(encoding="utf-8")
OUT = ROOT / "out"


def render(page):
    c = page["concept"]
    data = json.dumps(page, ensure_ascii=False).replace("</", "<\\/")
    return (TEMPLATE
            .replace("__TITLE__", html.escape(f"{c['name']}（{c['level']}）"))
            .replace("__DESC__", html.escape(c["one_line"]))
            .replace("/*__PAGE_JSON__*/null", data))


def index(items):
    cards = "".join(
        f'<a class="c" href="{html.escape(f)}"><span class="d">{html.escape(c["domain"])} · {html.escape(c["level"])}</span>'
        f'<b>{html.escape(c["name"])}</b><span>{html.escape(c["one_line"])}</span></a>'
        for f, c in items)
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>样例库 · LearnWise</title><style>
:root{{--bg:#F5F3EE;--surface:#FDFCF9;--ink:#1D2130;--muted:#626879;--line:#DCD7CB;--accent:#3549A3}}
@media (prefers-color-scheme:dark){{:root{{--bg:#12141B;--surface:#1A1D27;--ink:#E3E5EC;--muted:#9AA0B2;--line:#2E3242;--accent:#8E9EF0}}}}
body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.7 "PingFang SC","Microsoft YaHei",system-ui,sans-serif}}
main{{max-width:760px;margin:0 auto;padding:40px 16px}}
h1{{font-family:"Songti SC","Noto Serif CJK SC",serif;margin:0 0 4px}} h1 b{{color:var(--accent)}}
.g{{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin-top:24px}}
.c{{display:flex;flex-direction:column;gap:4px;background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:16px 18px;text-decoration:none;color:inherit}}
.c:hover{{border-color:var(--accent)}} .c b{{font-size:20px}} .d{{font-size:13px;color:var(--accent)}} .c span:last-child{{color:var(--muted);font-size:14px}}
p{{color:var(--muted)}} footer{{font-size:13px;color:var(--muted);text-align:center;margin-top:40px}} footer a{{color:inherit}}
</style></head><body><main><h1>Learn<b>Wise</b> 样例库</h1>
<p>四个互不相关的领域，覆盖启蒙、进阶、深入三档。用来判断页面形态和内容质量。</p>
<div class="g">{cards}</div>
<footer>Waywise 出品 · <a href="https://beian.miit.gov.cn/" target="_blank" rel="noopener">沪ICP备2025127932号</a></footer></main></body></html>"""


if __name__ == "__main__":
    OUT.mkdir(exist_ok=True)
    items = []
    for path in map(pathlib.Path, sys.argv[1:]):
        page = json.loads(path.read_text(encoding="utf-8"))
        errs = validate(page)
        if errs:
            sys.exit(f"{path} 校验没通过：\n  - " + "\n  - ".join(errs))
        name = path.stem + ".html"
        (OUT / name).write_text(render(page), encoding="utf-8")
        items.append((name, page["concept"]))
        print("→ out/" + name)
    order = {"启蒙": 0, "进阶": 1, "深入": 2}
    items.sort(key=lambda x: order[x[1]["level"]])
    (OUT / "index.html").write_text(index(items), encoding="utf-8")
    print("→ out/index.html")
