"""学习页 JSON 校验。结构说明见 docs/学习页结构.md。

用法：python3 -m app.schema fixtures/*.json
"""
import json
import random
import re
import sys

LEVELS = ("启蒙", "进阶", "深入")
HOOK_TYPES = ("story", "paradox", "problem")
PRACTICE_TYPES = ("worked", "faded", "solo", "open")
QUIZ_KINDS = ("本页", "前置", "迁移")
DIAGRAM_TYPES = ("flow", "compare", "bars")


def is_str(v):
    return isinstance(v, str) and v.strip() != ""


def strs(v, lo, hi):
    return isinstance(v, list) and lo <= len(v) <= hi and all(is_str(x) for x in v)


def validate(p):
    """返回错误列表；空列表表示通过。"""
    e = []

    def need(cond, msg):
        if not cond:
            e.append(msg)

    if not isinstance(p, dict):
        return ["不是对象"]
    need(p.get("schema_version") == 1, "schema_version 需要是 1")

    c = p.get("concept") or {}
    need(is_str(c.get("name")) and is_str(c.get("domain")) and is_str(c.get("one_line")), "concept.name / domain / one_line 缺失")
    need(c.get("level") in LEVELS, f"concept.level 需要是 {'/'.join(LEVELS)}")
    need(len(c.get("one_line") or "") <= 50, "concept.one_line 过长")
    level = c.get("level")

    m = p.get("map") or {}
    stages = m.get("stages") or []
    need(isinstance(stages, list) and len(stages) == 3, "map.stages 需要 3 个阶段")
    nodes = [n for s in stages for n in (s.get("nodes") or [])]
    need(6 <= len(nodes) <= 12, f"map 节点需要 6–12 个（现在 {len(nodes)}）")
    need(c.get("name") in nodes, "map.stages 里没有本知识点")
    for k, lo, hi in (("prereqs", 0, 3), ("unlocks", 0, 3)):
        v = m.get(k)
        need(isinstance(v, list) and lo <= len(v) <= hi, f"map.{k} 需要 {lo}–{hi} 个")
        for n in v or []:
            need(n in nodes, f"map.{k} 里的「{n}」不在 stages 里")
    need(is_str(m.get("why_here")), "map.why_here 缺失")

    h = p.get("hook") or {}
    need(h.get("type") in HOOK_TYPES and is_str(h.get("title")) and is_str(h.get("text")), "hook 缺失或 type 不合法")
    need(120 <= len(h.get("text") or "") <= 400, "hook.text 需要 150–300 字左右")

    pr = p.get("predict")
    need(isinstance(pr, list) and 1 <= len(pr) <= 2, "predict 需要 1–2 题")
    for i, q in enumerate(pr or []):
        opts = q.get("options")
        need(is_str(q.get("q")) and strs(opts, 3, 4) and is_str(q.get("why")), f"predict[{i}] 题干/选项/why 缺失")
        need(isinstance(q.get("answer"), int) and 0 <= q["answer"] < len(opts or []), f"predict[{i}].answer 越界")

    ch = p.get("chunks")
    need(isinstance(ch, list) and 3 <= len(ch) <= 5, f"chunks 需要 3–5 块（现在 {len(ch or [])}）")
    for i, k in enumerate(ch or []):
        at = f"chunks[{i}]"
        need(is_str(k.get("title")) and is_str(k.get("one")), f"{at}.title / one 缺失")
        need(strs(k.get("core"), 2, 4), f"{at}.core 需要 2–4 段")
        need(isinstance(k.get("analogy"), str) and (level == "深入" or is_str(k.get("analogy"))), f"{at}.analogy 缺失")
        need(is_str(k.get("example")) and is_str(k.get("myth")), f"{at}.example / myth 缺失")
        need(not re.match(r"^(误区|误解|例子)[:：]", k.get("myth") or "") and not re.match(r"^例子[:：]", k.get("example") or ""), f"{at}.example / myth 不要以“误区：”“例子：”开头")
        rc = k.get("recall")
        need(isinstance(rc, list) and len(rc) == 2 and all(is_str(r.get("q")) and is_str(r.get("a")) for r in rc), f"{at}.recall 需要正好 2 题")
        d = k.get("diagram")
        if d is not None:
            e.extend(f"{at}.diagram {x}" for x in check_diagram(d))
    n_chunks = len(ch or [])

    pa = p.get("practice")
    need(isinstance(pa, list) and 1 <= len(pa) <= 3, "practice 需要 1–3 题")
    for i, x in enumerate(pa or []):
        need(x.get("type") in PRACTICE_TYPES and is_str(x.get("q")) and is_str(x.get("a")) and isinstance(x.get("steps"), list), f"practice[{i}] 字段缺失或 type 不合法")
    types = {x.get("type") for x in pa or []}
    if level == "深入":
        need("open" in types, "深入档的 practice 需要 open 题")
        db = p.get("debates")
        need(isinstance(db, list) and len(db) >= 2, "深入档需要至少 2 个 debates")
    if level == "进阶":
        need("faded" in types or "solo" in types, "进阶档的 practice 需要 faded 或 solo 题")
    for i, d in enumerate(p.get("debates") or []):
        need(is_str(d.get("question")) and is_str(d.get("status")) and isinstance(d.get("views"), list) and len(d["views"]) >= 2
             and all(is_str(v.get("who")) and is_str(v.get("claim")) for v in d["views"]), f"debates[{i}] 需要问题、至少 2 个观点和 status")

    qz = p.get("quiz")
    need(isinstance(qz, list) and len(qz) == 8, f"quiz 需要正好 8 题（现在 {len(qz or [])}）")
    for i, q in enumerate(qz or []):
        need(is_str(q.get("q")) and strs(q.get("options"), 4, 4) and is_str(q.get("why")), f"quiz[{i}] 题干/4 个选项/why 缺失")
        need(isinstance(q.get("answer"), int) and 0 <= q["answer"] <= 3, f"quiz[{i}].answer 需要 0–3")
        need(isinstance(q.get("chunk"), int) and 1 <= q["chunk"] <= n_chunks, f"quiz[{i}].chunk 超出范围")
        need(q.get("kind") in QUIZ_KINDS, f"quiz[{i}].kind 需要是 {'/'.join(QUIZ_KINDS)}")
    if isinstance(qz, list) and qz:
        kinds = [q.get("kind") for q in qz]
        need(kinds.count("前置") >= 1 and kinds.count("迁移") >= 2, "quiz 需要至少 1 题前置、2 题迁移")
        counts = [sum(1 for q in qz if q.get("answer") == a) for a in range(4)]
        need(max(counts) <= 3 and min(counts) >= 1, f"quiz 正确答案分布不均：{counts}")
        seq = "".join(str(q.get("answer")) for q in qz)
        need(not re.fullmatch(r"(0123)+|(3210)+|(\d)\3*", seq) and not re.match(r"^(\d{2,4})\1{1,}", seq), f"quiz 正确答案位置有规律（{seq}）")

    f = p.get("feynman") or {}
    need(is_str(f.get("prompt")) and strs(f.get("key_points"), 3, 5) and strs(f.get("common_gaps"), 2, 3), "feynman 缺失或条数不对")
    t = p.get("transfer") or {}
    need(is_str(t.get("prompt")) and is_str(t.get("template")), "transfer 缺失")
    pc = p.get("podcast") or {}
    need(isinstance(pc.get("worth"), bool) and is_str(pc.get("reason")), "podcast.worth / reason 缺失")
    if pc.get("worth"):
        need(strs(pc.get("outline"), 3, 5), "podcast.worth=true 时需要 3–5 个 outline")
    tu = p.get("tutor") or {}
    need(is_str(tu.get("style")) and strs(tu.get("starters"), 3, 3), "tutor.style / 3 个 starters 缺失")
    cf = p.get("confidence") or {}
    need(cf.get("level") in ("high", "medium", "low") and is_str(cf.get("notes")), "confidence 缺失")
    src = p.get("sources")
    need(isinstance(src, list) and len(src) >= 1 and all(is_str(s.get("title")) for s in src), "sources 至少 1 条")
    return e


def quiz_order_ok(quiz) -> bool:
    counts = [sum(1 for q in quiz if q.get("answer") == a) for a in range(4)]
    seq = "".join(str(q.get("answer")) for q in quiz)
    return (max(counts) <= 3 and min(counts) >= 1
            and not re.fullmatch(r"(0123)+|(3210)+|(\d)\3*", seq) and not re.match(r"^(\d{2,4})\1{1,}", seq))


def shuffle_quiz(page) -> None:
    """模型排的正确答案位置常有规律：本地打乱选项顺序，直到分布均匀、没有规律。"""
    quiz = page.get("quiz") or []
    if len(quiz) != 8 or not all(isinstance(q.get("options"), list) and len(q["options"]) == 4 and isinstance(q.get("answer"), int) for q in quiz):
        return
    rng = random.Random()
    for _ in range(200):
        if quiz_order_ok(quiz):
            return
        for q in quiz:
            right = q["options"][q["answer"]]
            rng.shuffle(q["options"])
            q["answer"] = q["options"].index(right)


def check_diagram(d):
    e = []
    t = d.get("type")
    if t not in DIAGRAM_TYPES:
        return [f"type 需要是 {'/'.join(DIAGRAM_TYPES)}"]
    if not is_str(d.get("caption")):
        e.append("缺 caption")
    if t == "flow" and not strs(d.get("steps"), 2, 6):
        e.append("flow 需要 2–6 步")
    if t == "compare":
        cols = d.get("columns")
        if not (isinstance(cols, list) and 2 <= len(cols) <= 3 and all(is_str(c.get("title")) and strs(c.get("items"), 1, 6) for c in cols)):
            e.append("compare 需要 2–3 列，每列有标题和 1–6 项")
    if t == "bars":
        bars = d.get("bars")
        if not (isinstance(bars, list) and 2 <= len(bars) <= 8 and all(is_str(b.get("label")) and isinstance(b.get("value"), (int, float)) and b["value"] >= 0 for b in bars)):
            e.append("bars 需要 2–8 根，值非负")
    return e


if __name__ == "__main__":
    bad = 0
    for path in sys.argv[1:]:
        with open(path, encoding="utf-8") as fh:
            errs = validate(json.load(fh))
        print(("✓ " if not errs else "✗ ") + path)
        for x in errs:
            print("   - " + x)
        bad += bool(errs)
    sys.exit(1 if bad else 0)
