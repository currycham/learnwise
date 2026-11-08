"""内容生成：入口审核 → 领域地图 → 诊断题 → 学习页（检索 → 生成 → 校验 → 质检 → 修正）。"""
import json
import logging
import random
import re

from . import db, prompts
from .llm import LLMError, chat_json
from .schema import shuffle_quiz, validate
from .search import digest, search

log = logging.getLogger("learnwise.pipeline")

STAGES = ["入门", "进阶", "深入"]
LABELS = ["完全不懂", "听说过", "入门", "能用", "熟练", "专家"]
TIER_OF = {"完全不懂": "启蒙", "听说过": "启蒙", "入门": "进阶", "能用": "进阶", "熟练": "深入", "专家": "深入"}
LEVELS = ["启蒙", "进阶", "深入"]


def normkey(s: str) -> str:
    return re.sub(r"[\s\W_]+", "", str(s or "")).lower()


class PipelineError(Exception):
    """给用户看的失败原因。"""


# ---------- 入口审核 ----------
def classify_topic(text: str, actor: str) -> dict:
    out = chat_json([{"role": "system", "content": prompts.TOPIC_SYSTEM}, {"role": "user", "content": text[:200]}],
                    tier="flash", task="topic", actor=actor, max_tokens=2000, temperature=0.1)
    out["name"] = str(out.get("name") or "").strip()[:20]
    out["focus"] = str(out.get("focus") or "").strip()[:20]
    if out.get("risk") not in ("none", "medical", "legal", "financial"):
        out["risk"] = "none"
    if out.get("category") not in prompts.CATEGORIES:
        out["category"] = "其它"
    if out.get("ok") and not out["name"]:
        out["ok"], out["reason"] = False, "没看懂想学什么，换个说法试试"
    return out


# ---------- 领域地图 ----------
def check_map(m: dict) -> list[str]:
    """校验并就地清理：去掉无效的前置引用。返回无法自动修好的错误。"""
    e = []
    stages = m.get("stages")
    if not isinstance(stages, list) or len(stages) != 3:
        return ["stages 需要正好 3 个阶段"]
    seen = set()
    total = 0
    for si, s in enumerate(stages):
        nodes = s.get("nodes") or []
        if not 3 <= len(nodes) <= 12:
            e.append(f"第 {si + 1} 阶段需要 4–10 个知识点（现在 {len(nodes)}）")
        clean = []
        for n in nodes:
            name = str(n.get("name") or "").strip()
            k = normkey(name)
            if not k or k in seen:
                continue
            n["name"] = name
            n["prereqs"] = [p for p in (n.get("prereqs") or []) if normkey(p) in seen][:3]
            n["aliases"] = [str(a).strip() for a in (n.get("aliases") or []) if str(a).strip() and normkey(a) != k][:4]
            try:
                n["minutes"] = max(15, min(45, int(n.get("minutes") or 30)))
            except (TypeError, ValueError):
                n["minutes"] = 30
            seen.add(k)
            clean.append(n)
        s["nodes"] = clean
        total += len(clean)
    if not 10 <= total <= 30:
        e.append(f"知识点总数需要 14–24 个（现在 {total}）")
    return e


def concept_for(c, name: str, aliases: list[str]) -> int:
    keys = [normkey(name)] + [normkey(a) for a in aliases]
    for k in keys:
        r = c.execute("SELECT concept_id FROM concept_alias WHERE key = ?", (k,)).fetchone()
        if r:
            cid = r[0]
            break
    else:
        cid = c.execute("INSERT INTO concepts (name, key, aliases) VALUES (?,?,?)",
                        (name, keys[0], json.dumps(aliases, ensure_ascii=False))).lastrowid
    for k in keys:
        if k:
            c.execute("INSERT OR IGNORE INTO concept_alias (key, concept_id) VALUES (?,?)", (k, cid))
    return cid


def build_domain(domain_id: int, actor: str) -> None:
    d = db.one("SELECT * FROM domains WHERE id = ?", domain_id)
    focus = d.get("focus", "") if d else ""
    results = search([f"{d['name']} 知识体系 学习路线", f"{d['name']} 核心概念 入门教程"], actor)
    msgs = [{"role": "system", "content": prompts.MAP_SYSTEM},
            {"role": "user", "content": prompts.map_user(d["name"], focus, d["risk"], digest(results, 10))}]
    m = chat_json(msgs, tier="pro", task="map", actor=actor, max_tokens=16000, effort="high", temperature=0.4)
    errs = check_map(m)
    if errs:
        msgs += [{"role": "assistant", "content": json.dumps(m, ensure_ascii=False)},
                 {"role": "user", "content": "有这些问题，请修正后输出完整 JSON：\n" + "\n".join(errs)}]
        m = chat_json(msgs, tier="pro", task="map", actor=actor, max_tokens=16000, effort="high", temperature=0.4)
        errs = check_map(m)
        if errs:
            raise PipelineError("知识地图没画好：" + "；".join(errs))
    with db.db() as c:
        c.execute("BEGIN")
        c.execute("DELETE FROM domain_nodes WHERE domain_id = ?", (domain_id,))
        c.execute("DELETE FROM edges WHERE domain_id = ?", (domain_id,))
        ids, ord_ = {}, 0
        for si, s in enumerate(m["stages"]):
            for n in s["nodes"]:
                cid = concept_for(c, n["name"], n["aliases"])
                if cid in ids.values():   # 两个节点对应到同一个知识点：只留第一个
                    ids[normkey(n["name"])] = cid
                    continue
                ids[normkey(n["name"])] = cid
                c.execute("INSERT INTO domain_nodes (domain_id, concept_id, stage, ord, one_line, minutes) VALUES (?,?,?,?,?,?)",
                          (domain_id, cid, si, ord_, str(n.get("one_line") or "")[:60], n["minutes"]))
                ord_ += 1
                for p in n["prereqs"]:
                    src = ids.get(normkey(p))
                    if src and src != cid:
                        c.execute("INSERT OR IGNORE INTO edges (domain_id, src, dst) VALUES (?,?,?)", (domain_id, src, cid))
        c.execute("UPDATE domains SET summary = ?, status = 'ready', error = '' WHERE id = ?",
                  (str(m.get("summary") or "")[:120], domain_id))
        c.execute("COMMIT")


def domain_graph(domain_id: int) -> dict:
    """领域地图：节点（按阶段、顺序）和前置边。"""
    nodes = db.all_("""SELECT n.concept_id AS id, c.name, c.aliases, n.stage, n.ord, n.one_line, n.minutes
                       FROM domain_nodes n JOIN concepts c ON c.id = n.concept_id WHERE n.domain_id = ? ORDER BY n.ord""", domain_id)
    edges = db.all_("SELECT src, dst FROM edges WHERE domain_id = ?", domain_id)
    pre = {n["id"]: [] for n in nodes}
    post = {n["id"]: [] for n in nodes}
    for e in edges:
        if e["dst"] in pre and e["src"] in post:
            pre[e["dst"]].append(e["src"])
            post[e["src"]].append(e["dst"])
    for n in nodes:
        n["prereqs"], n["unlocks"] = pre[n["id"]], post[n["id"]]
    return {"nodes": nodes, "edges": edges}


def page_map(graph: dict, cid: int) -> tuple[dict, list[str], list[str]]:
    """从领域地图里截出本页需要的一小块：本知识点、前置、可解锁，再按顺序补足，每阶段最多 4 个、共 6–12 个。"""
    by_id = {n["id"]: n for n in graph["nodes"]}
    me = by_id[cid]
    prereqs = me["prereqs"][:3]
    unlocks = me["unlocks"][:3]
    if not unlocks:   # 没有直接依赖它的：取顺序上紧随其后的
        unlocks = [n["id"] for n in graph["nodes"] if n["ord"] > me["ord"]][:2]
    keep = [cid, *prereqs, *unlocks]
    stages = [[], [], []]
    for i in keep:
        if i not in stages[by_id[i]["stage"]]:
            stages[by_id[i]["stage"]].append(i)
    for n in graph["nodes"]:
        if sum(map(len, stages)) >= 12:
            break
        s = stages[n["stage"]]
        if n["id"] not in s and len(s) < 4:
            s.append(n["id"])
    for s in stages:
        s.sort(key=lambda i: by_id[i]["ord"])
    name = lambda i: by_id[i]["name"]
    m = {"stages": [{"name": STAGES[i], "nodes": [name(x) for x in s]} for i, s in enumerate(stages)],
         "prereqs": [name(x) for x in prereqs], "unlocks": [name(x) for x in unlocks]}
    return m, m["prereqs"], m["unlocks"]


# ---------- 学习页 ----------
def fill_page(p: dict, *, name: str, domain: str, level: str, minutes: int, m: dict, results: list[dict]) -> dict:
    """把系统负责的字段填上，清理来源。"""
    p["schema_version"] = 1
    c = p.get("concept") if isinstance(p.get("concept"), dict) else {}
    c.update(name=name, domain=domain, level=level, minutes=minutes)
    c.setdefault("aliases", [])
    p["concept"] = c
    why = (p.get("map") or {}).get("why_here") if isinstance(p.get("map"), dict) else ""
    p["map"] = {**m, "why_here": why or ""}
    urls = {r["url"]: r for r in results}
    src = []
    for s in p.get("sources") or []:
        if not isinstance(s, dict) or not str(s.get("title") or "").strip():
            continue
        u = s.get("url") or ""
        if u and u not in urls:      # 不在检索结果里的链接一律丢掉：模型编的链接不能出现在页面上
            continue
        src.append({"title": s["title"], **({"url": u} if u else {})})
    if not src:
        src = [{"title": r["title"], "url": r["url"]} for r in results[:3]]
    p["sources"] = src
    shuffle_quiz(p)
    return p


def build_page(page_id: int, actor: str) -> None:
    row = db.one("SELECT * FROM pages WHERE id = ?", page_id)
    d = db.one("SELECT * FROM domains WHERE id = ?", row["domain_id"])
    graph = domain_graph(d["id"])
    node = next((n for n in graph["nodes"] if n["id"] == row["concept_id"]), None)
    if not node:
        raise PipelineError("这个知识点不在领域地图里")
    m, prereqs, unlocks = page_map(graph, node["id"])
    level = row["level"]
    results = search([f"{node['name']} {d['name']} 是什么 原理", f"{node['name']} 例子 常见误区"], actor)
    fill = lambda p: fill_page(p, name=node["name"], domain=d["name"], level=level, minutes=30, m=m, results=results)

    msgs = [{"role": "system", "content": prompts.PAGE_SYSTEM},
            {"role": "user", "content": prompts.page_user(d["name"], node["name"], level, node["one_line"], prereqs, unlocks,
                                                          STAGES[node["stage"]], d["risk"], digest(results))}]
    page = fill(chat_json(msgs, tier="pro", task="page", actor=actor, max_tokens=32000, effort="low", temperature=0.7))
    errs = validate(page)
    for _ in range(2):
        if not errs:
            break
        page = fill(chat_json([{"role": "system", "content": prompts.REVISE_SYSTEM},
                               {"role": "user", "content": prompts.revise_user(page, [], errs)}],
                              tier="pro", task="page_fix", actor=actor, max_tokens=32000, effort="low", temperature=0.3))
        errs = validate(page)
    if errs:
        raise PipelineError("页面结构没通过校验：" + "；".join(errs[:5]))

    check = chat_json([{"role": "system", "content": prompts.CHECK_SYSTEM},
                       {"role": "user", "content": f"档位：{level}\n\n" + json.dumps(page, ensure_ascii=False)}],
                      tier="pro", task="check", actor=actor, max_tokens=16000, effort="high", temperature=0.2)
    issues = [i for i in (check.get("issues") or []) if isinstance(i, dict)]
    quality = {"score": check.get("score"), "issues_before": issues, "revised": False}
    if check.get("safe") is False:
        db.run("UPDATE pages SET quality = ? WHERE id = ?", json.dumps(quality, ensure_ascii=False), page_id)
        raise PipelineError("内容审核没通过")
    if issues:
        try:
            fixed = fill(chat_json([{"role": "system", "content": prompts.REVISE_SYSTEM},
                                    {"role": "user", "content": prompts.revise_user(page, issues)}],
                                   tier="pro", task="revise", actor=actor, max_tokens=32000, effort="low", temperature=0.3))
            if not validate(fixed):
                page, quality["revised"] = fixed, True
        except LLMError as e:
            log.warning("page %s 修正失败，保留修正前版本：%s", page_id, e)
    db.run("UPDATE pages SET data = ?, quality = ?, status = 'ready', error = '' WHERE id = ?",
           json.dumps(page, ensure_ascii=False), json.dumps(quality, ensure_ascii=False), page_id)


# ---------- 诊断题 ----------
def build_diagnostic(domain_id: int, actor: str) -> None:
    d = db.one("SELECT * FROM domains WHERE id = ?", domain_id)
    graph = domain_graph(domain_id)
    names = {normkey(n["name"]): n for n in graph["nodes"]}
    text = "\n".join(f"{STAGES[s]}：" + "、".join(n["name"] for n in graph["nodes"] if n["stage"] == s) for s in range(3))
    out = chat_json([{"role": "system", "content": prompts.DIAG_SYSTEM},
                     {"role": "user", "content": f"领域：{d['name']}\n\n领域地图：\n{text}"}],
                    tier="pro", task="diagnostic", actor=actor, max_tokens=12000, effort="low", temperature=0.5)
    qs = []
    for q in out.get("questions") or []:
        n = names.get(normkey(q.get("node")))
        opts = q.get("options")
        if not n or not isinstance(opts, list) or len(opts) != 4 or not isinstance(q.get("answer"), int) or not 0 <= q["answer"] <= 3:
            continue
        right = opts[q["answer"]]
        random.shuffle(opts)
        qs.append({"stage": n["stage"], "node": n["name"], "concept_id": n["id"], "q": q.get("q", ""),
                   "options": opts, "answer": opts.index(right), "why": q.get("why", "")})
    if sum(1 for q in qs if q["stage"] == 0) < 2 or len(qs) < 6:
        raise PipelineError("诊断题没出好")
    qs.sort(key=lambda q: q["stage"])
    db.run("UPDATE diagnostics SET data = ?, status = 'ready', error = '' WHERE domain_id = ?",
           json.dumps({"questions": qs}, ensure_ascii=False), domain_id)


def score_diagnostic(qs: list[dict], answers: list[int]) -> tuple[str, list[int]]:
    """按阶段答对率给出水平标签，返回（标签，答对的知识点）。answers 里 -1 表示“不知道”。"""
    right = [0, 0, 0]
    total = [0, 0, 0]
    known = []
    for q, a in zip(qs, answers):
        total[q["stage"]] += 1
        if a == q["answer"]:
            right[q["stage"]] += 1
            known.append(q["concept_id"])
    rate = [right[i] / total[i] if total[i] else 0 for i in range(3)]
    if rate[0] < 0.5:
        label = "完全不懂" if right[0] == 0 else "听说过"
    elif rate[1] < 0.5:
        label = "入门"
    elif rate[2] < 0.5:
        label = "能用"
    elif rate[2] < 1:
        label = "熟练"
    else:
        label = "专家"
    return label, known


# ---------- 跨领域联系 ----------
def build_links(domain_id: int, actor: str) -> None:
    """找这个领域和比它早的领域之间的知识点联系。新领域画好地图后自动跑一次，每对领域只比较一次。"""
    d = db.one("SELECT * FROM domains WHERE id = ?", domain_id)
    mine = db.all_("""SELECT c.id, c.name, n.one_line FROM domain_nodes n JOIN concepts c ON c.id = n.concept_id
                      WHERE n.domain_id = ? ORDER BY n.ord""", domain_id)
    others = db.all_("""SELECT d.id AS did, d.name AS dname, c.id, c.name FROM domains d JOIN domain_nodes n ON n.domain_id = d.id
                        JOIN concepts c ON c.id = n.concept_id WHERE d.status = 'ready' AND d.id < ? ORDER BY d.id, n.ord""", domain_id)
    mine_ids = {m["id"] for m in mine}
    others = [o for o in others if o["id"] not in mine_ids]   # 两个领域共有的知识点本身就连着，不用再找
    if not others:
        db.run("UPDATE link_runs SET status = 'ready', error = '' WHERE domain_id = ?", domain_id)
        return
    by_domain = {}
    for o in others:
        by_domain.setdefault((o["did"], o["dname"]), []).append(o)
    text = "\n".join(f"【{dn}】" + "、".join(o["name"] for o in os_) for (_, dn), os_ in by_domain.items())
    out = chat_json([{"role": "system", "content": prompts.LINK_SYSTEM},
                     {"role": "user", "content": f"新领域：{d['name']}\n" + "\n".join(f"- {m['name']}：{m['one_line']}" for m in mine)
                                                  + f"\n\n其他领域的知识点：\n{text}"}],
                    tier="pro", task="links", actor=actor, max_tokens=16000, effort="high", temperature=0.3)
    src_by = {normkey(m["name"]): m["id"] for m in mine}
    dst_by = {(normkey(dn), normkey(o["name"])): (o["id"], did) for (did, dn), os_ in by_domain.items() for o in os_}
    dst_any = {}
    for (dk, ck), v in dst_by.items():
        dst_any.setdefault(ck, v)
    n = 0
    with db.db() as c:
        c.execute("BEGIN")
        for l in (out.get("links") or [])[:12]:
            if l.get("type") not in prompts.LINK_TYPES:
                continue
            s = src_by.get(normkey(l.get("src")))
            t = dst_by.get((normkey(l.get("dst_domain")), normkey(l.get("dst")))) or dst_any.get(normkey(l.get("dst")))
            if not s or not t or s == t[0]:
                continue
            a, b, da, db_ = s, t[0], domain_id, t[1]
            if l.get("reverse") and l["type"] in ("应用", "基础"):
                a, b, da, db_ = b, a, db_, da
            try:
                strength = max(1, min(3, int(l.get("strength") or 1)))
            except (TypeError, ValueError):
                strength = 1
            c.execute("""INSERT OR IGNORE INTO links (src, dst, src_domain, dst_domain, type, why, strength) VALUES (?,?,?,?,?,?,?)""",
                      (a, b, da, db_, l["type"], str(l.get("why") or "")[:80], strength))
            n += 1
        c.execute("UPDATE link_runs SET status = 'ready', error = '' WHERE domain_id = ?", (domain_id,))
        c.execute("COMMIT")
    log.info("domain %s 找到 %s 条跨领域联系", domain_id, n)


def concept_links(cid: int) -> list[dict]:
    """一个知识点的跨领域联系（两个方向），给学习页和星图用。"""
    return db.all_("""SELECT l.type, l.why, l.strength, l.src = ? AS outgoing,
                             CASE WHEN l.src = ? THEN l.dst ELSE l.src END AS other,
                             CASE WHEN l.src = ? THEN l.dst_domain ELSE l.src_domain END AS other_domain,
                             c.name AS other_name, d.name AS other_domain_name
                      FROM links l JOIN concepts c ON c.id = CASE WHEN l.src = ? THEN l.dst ELSE l.src END
                      JOIN domains d ON d.id = CASE WHEN l.src = ? THEN l.dst_domain ELSE l.src_domain END
                      WHERE l.src = ? OR l.dst = ? ORDER BY l.strength DESC""", cid, cid, cid, cid, cid, cid, cid)
