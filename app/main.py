"""LearnWise 网站：页面、接口、后台任务。uvicorn app.main:app --port 8400 --workers 1"""
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import auth, db, jobs, pipeline, prompts
from .config import (INVITE_CODE, MODEL_DISCLOSURE, ROOT, SITE_URL, USER_DAILY_AI, USER_DAILY_DOMAINS,
                     USER_DAILY_PAGES)
from .llm import BudgetExceeded, LLMError, chat, chat_json, spent_today
from .pipeline import LABELS, LEVELS, STAGES, TIER_OF

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("learnwise")


@asynccontextmanager
async def lifespan(app):
    db.init()
    tasks = jobs.start()
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
tpl = Jinja2Templates(directory=ROOT / "app" / "templates")
tpl.env.globals.update(STAGES=STAGES, LABELS=LABELS, LEVELS=LEVELS, MODEL_DISCLOSURE=MODEL_DISCLOSURE)
LEARN_HTML = (ROOT / "app" / "templates" / "learn.html").read_text(encoding="utf-8")
RISK_TEXT = {"medical": "本页是健康医学知识科普，不构成诊断或治疗建议。身体有状况请咨询医生。",
             "legal": "本页是法律知识科普，不构成法律意见。各地规定可能不同，具体问题请咨询律师。",
             "financial": "本页是投资理财知识科普，不构成投资建议，不推荐任何产品。投资有风险。"}


@app.exception_handler(HTTPException)
async def http_error(request: Request, exc: HTTPException):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
    if exc.status_code == 401:
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        return RedirectResponse("/login?" + urlencode({"next": target}), status_code=303)
    return page(request, "message.html", title="出错了", message=exc.detail, status_code=exc.status_code)


def page(request: Request, name: str, status_code: int = 200, **ctx):
    ctx.setdefault("user", auth.current_user(request))
    if ctx["user"] and "due" not in ctx:
        ctx["due"] = db.one("SELECT COUNT(*) AS n FROM cards WHERE user_id = ? AND due_at <= ?", ctx["user"]["id"], auth.ts(auth.now()))["n"]
    return tpl.TemplateResponse(request, name, ctx, status_code=status_code)


def bj_today_utc() -> str:
    """北京时间今天零点对应的 UTC 时间（表里存的都是 UTC）。"""
    now = datetime.now(timezone(timedelta(hours=8)))
    return (now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


def quota(user: dict, kind: str) -> None:
    if user.get("is_admin"):
        return
    since = bj_today_utc()
    if kind == "domain":
        n = db.one("SELECT COUNT(*) AS n FROM domains WHERE created_by = ? AND created_at >= ?", user["id"], since)["n"]
        limit, what = USER_DAILY_DOMAINS, "新建领域"
    elif kind == "page":
        n = db.one("SELECT COUNT(*) AS n FROM pages WHERE created_by = ? AND created_at >= ?", user["id"], since)["n"]
        limit, what = USER_DAILY_PAGES, "生成学习页"
    else:
        n = db.one("SELECT COUNT(*) AS n FROM llm_usage WHERE actor = ? AND at >= ? AND task IN ('feynman','tutor','topic')",
                   f"u{user['id']}", since)["n"]
        limit, what = USER_DAILY_AI, "AI 对话"
    if n >= limit:
        raise HTTPException(429, f"今天的{what}次数用完了（每天 {limit} 次），明天再来")


def get_domain(did: int) -> dict:
    d = db.one("SELECT * FROM domains WHERE id = ?", did)
    if not d:
        raise HTTPException(404, "没有这个领域")
    return d


def user_progress(user: dict | None, did: int) -> dict[int, str]:
    if not user:
        return {}
    return {r["concept_id"]: r["status"] for r in db.all_("SELECT concept_id, status FROM progress WHERE user_id = ? AND domain_id = ?", user["id"], did)}


def next_node(graph: dict, prog: dict[int, str]) -> dict | None:
    done = lambda i: prog.get(i) in ("known", "lit")
    todo = [n for n in graph["nodes"] if not done(n["id"])]
    ready = [n for n in todo if all(done(p) for p in n["prereqs"])]
    return (ready or todo or [None])[0]


def prefetch_next(p: dict, graph: dict, prog: dict[int, str]) -> None:
    # 预测闯关后的推荐，跳过已掌握节点；仅预生成一页，不提前修改学习进度。
    after = {**prog, p["concept_id"]: "lit"}
    nxt = next_node(graph, after)
    if nxt:
        try:
            jobs.ensure_page(p["domain_id"], nxt["id"], p["level"], prefetch=True)
        except Exception:
            log.exception("page %s 下一页预生成入队失败", p["id"])


def next_page_info(p: dict, graph: dict, prog: dict[int, str]) -> dict | None:
    nxt = next_node(graph, {**prog, p["concept_id"]: "lit"})
    if not nxt:
        return None
    future = db.one("SELECT id, status FROM pages WHERE domain_id=? AND concept_id=? AND level=?",
                    p["domain_id"], nxt["id"], p["level"])
    return {"name": nxt["name"], "url": f"/d/{p['domain_id']}/n/{nxt['id']}?" + urlencode({"level": p["level"]}),
            "status": future["status"] if future else "missing", "pageId": future["id"] if future else None}


# ---------- 页面 ----------
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    user = auth.current_user(request)
    domains = db.all_("""SELECT d.id, d.name, d.summary, d.category,
                                (SELECT COUNT(*) FROM domain_nodes n WHERE n.domain_id = d.id) AS nodes,
                                (SELECT COUNT(*) FROM pages p WHERE p.domain_id = d.id AND p.status = 'ready') AS pages
                         FROM domains d WHERE d.status = 'ready' ORDER BY pages DESC, d.id DESC LIMIT 200""")
    mine = []
    if user:
        mine = db.all_("""SELECT d.id, d.name, d.status, u.label,
                                 (SELECT COUNT(*) FROM domain_nodes n WHERE n.domain_id = d.id) AS nodes,
                                 (SELECT COUNT(*) FROM progress p WHERE p.user_id = u.user_id AND p.domain_id = d.id AND p.status = 'lit') AS lit
                          FROM user_domains u JOIN domains d ON d.id = u.domain_id WHERE u.user_id = ? ORDER BY u.updated_at DESC""", user["id"])
    due = db.one("SELECT COUNT(*) AS n FROM cards WHERE user_id = ? AND due_at <= ?", user["id"], auth.ts(auth.now()))["n"] if user else 0
    return page(request, "index.html", user=user, domains=domains, mine=mine, due=due)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    local = next.startswith("/") and not next.startswith("//") and "\\" not in next and not any(ord(c) < 32 for c in next)
    return page(request, "login.html", next=next if local else "/", need_invite=bool(INVITE_CODE))


@app.get("/d/{did}", response_class=HTMLResponse)
def domain_page(request: Request, did: int):
    user = auth.current_user(request)
    d = get_domain(did)
    if d["status"] != "ready":
        job = db.one("SELECT id FROM jobs WHERE kind = 'domain' AND ref_id = ? ORDER BY id DESC LIMIT 1", did)
        return page(request, "wait.html", user=user, kind="domain", ref=did, title=d["name"], error=d["error"],
                    what="知识地图", eta="大约 1–2 分钟", job=job)
    graph = pipeline.domain_graph(did)
    prog = user_progress(user, did)
    ud = db.one("SELECT * FROM user_domains WHERE user_id = ? AND domain_id = ?", user["id"], did) if user else None
    label = ud["label"] if ud else ""
    ready = {(r["concept_id"], r["level"]) for r in db.all_("SELECT concept_id, level FROM pages WHERE domain_id = ? AND status = 'ready'", did)}
    pending = {(r["concept_id"], r["level"]) for r in db.all_("SELECT concept_id, level FROM pages WHERE domain_id = ? AND status = 'pending'", did)}
    nxt = next_node(graph, prog)
    names = {n["id"]: n["name"] for n in graph["nodes"]}
    for n in graph["nodes"]:
        n["state"] = prog.get(n["id"], "")
        n["levels"] = [lv for lv in LEVELS if (n["id"], lv) in ready]
        n["pending_levels"] = [lv for lv in LEVELS if (n["id"], lv) in pending]
        n["prereq_names"] = [names[i] for i in n["prereqs"] if i in names]
    focus = next((n for n in graph["nodes"] if d["focus"] and pipeline.normkey(n["name"]) == pipeline.normkey(d["focus"])), None)
    diag = db.one("SELECT status FROM diagnostics WHERE domain_id = ?", did)
    lit = sum(1 for v in prog.values() if v == "lit")
    known = sum(1 for v in prog.values() if v == "known")
    return page(request, "domain.html", user=user, d=d, graph=graph, label=label, tier=TIER_OF.get(label, ""), nxt=nxt, focus=focus,
                diag=diag, lit=lit, known=known, risk=RISK_TEXT.get(d["risk"], ""))


@app.get("/d/{did}/diagnose", response_class=HTMLResponse)
def diagnose_page(request: Request, did: int, user=Depends(auth.require_user)):
    d = get_domain(did)
    if d["status"] != "ready":
        return RedirectResponse(f"/d/{did}", status_code=303)
    diag = db.one("SELECT * FROM diagnostics WHERE domain_id = ?", did)
    if not diag or diag["status"] == "failed":
        quota(user, "ai")
        if not diag:
            db.run("INSERT INTO diagnostics (domain_id) VALUES (?)", did)
        jobs.enqueue("diagnostic", did, user["id"])
        diag = {"status": "pending"}
    if diag["status"] != "ready":
        return page(request, "wait.html", user=user, kind="diagnostic", ref=did, title=f"{d['name']} · 水平诊断",
                    error=diag.get("error", ""), what="诊断题", eta="大约 30 秒")
    qs = db.loads(diag["data"], {}).get("questions", [])
    public = [{k: q[k] for k in ("stage", "node", "q", "options")} for q in qs]
    return page(request, "diagnose.html", user=user, d=d, qs=public)


@app.get("/d/{did}/n/{cid}", response_class=HTMLResponse)
def node_page(request: Request, did: int, cid: int, level: str = ""):
    user = auth.current_user(request)
    d = get_domain(did)
    node = db.one("SELECT c.name FROM domain_nodes n JOIN concepts c ON c.id = n.concept_id WHERE n.domain_id = ? AND n.concept_id = ?", did, cid)
    if not node:
        raise HTTPException(404, "这个知识点不在地图上")
    if level not in LEVELS:
        ud = db.one("SELECT label FROM user_domains WHERE user_id = ? AND domain_id = ?", user["id"], did) if user else None
        level = TIER_OF.get(ud["label"] if ud else "", "")
        if not level:   # 没选过水平：有现成的就用现成的，否则按进阶
            have = db.one("SELECT level FROM pages WHERE domain_id = ? AND concept_id = ? AND status = 'ready' ORDER BY id LIMIT 1", did, cid)
            level = have["level"] if have else "进阶"
    p = db.one("SELECT * FROM pages WHERE domain_id = ? AND concept_id = ? AND level = ?", did, cid, level)
    if p and p["status"] == "ready":
        return RedirectResponse(f"/p/{p['id']}", status_code=303)
    title = f"{node['name']}（{level}）"
    if not user and not p:
        return page(request, "message.html", title=title, message="这一页还没有人生成过。登录后就能生成，生成好之后所有人都能学。",
                    action=("登录", "/login?" + urlencode({"next": f"/d/{did}/n/{cid}?level={level}"})))
    if not p:
        quota(user, "page")
    if not p or (p["status"] == "pending" and user):
        p = jobs.ensure_page(did, cid, level, user["id"])
    if p["status"] == "ready":
        return RedirectResponse(f"/p/{p['id']}", status_code=303)
    job = db.one("SELECT id FROM jobs WHERE kind = 'page' AND ref_id = ? ORDER BY id DESC LIMIT 1", p["id"])
    return page(request, "wait.html", user=user, kind="page", ref=p["id"], title=title, error=p["error"],
                what="学习页", eta="大约 3–5 分钟：检索资料、写作、质检、修正", job=job, back=f"/d/{did}")


@app.get("/p/{pid}", response_class=HTMLResponse)
def learn_page(request: Request, pid: int):
    user = auth.current_user(request)
    p = db.one("SELECT * FROM pages WHERE id = ?", pid)
    if not p or p["status"] != "ready":
        raise HTTPException(404, "这一页还没有生成好")
    d = get_domain(p["domain_id"])
    db.run("UPDATE pages SET views = views + 1 WHERE id = ?", pid)
    data = db.loads(p["data"], {})
    graph = pipeline.domain_graph(d["id"])
    node_ids = {n["name"]: n["id"] for n in graph["nodes"]}
    tiers = {lv: f"/d/{d['id']}/n/{p['concept_id']}?level={lv}" for lv in LEVELS}
    prog = user_progress(user, d["id"])
    prefetch_next(p, graph, prog)
    cards = {}
    if user:
        cards = {r["idx"]: r["last_rating"] for r in db.all_("SELECT idx, last_rating FROM cards WHERE user_id = ? AND page_id = ?", user["id"], pid)}
    lw = {"pageId": pid, "domainId": d["id"], "conceptId": p["concept_id"], "home": "/", "domainUrl": f"/d/{d['id']}",
          "loggedIn": bool(user), "api": True, "nodeIds": node_ids, "tiers": tiers, "risk": RISK_TEXT.get(d["risk"], ""),
          "lit": prog.get(p["concept_id"]) == "lit", "litNodes": [n["name"] for n in graph["nodes"] if prog.get(n["id"]) == "lit"],
          "cards": cards, "loginUrl": f"/login?next=/p/{pid}", "disclosure": MODEL_DISCLOSURE,
          "links": [{**l, "url": f"/d/{l['other_domain']}/n/{l['other']}"} for l in pipeline.concept_links(p["concept_id"])],
          "graphUrl": f"/graph?c={p['concept_id']}", "next": next_page_info(p, graph, prog)}
    return HTMLResponse(render_learn(data, lw))


def render_learn(data: dict, lw: dict) -> str:
    from html import escape
    c = data["concept"]
    dump = lambda o: json.dumps(o, ensure_ascii=False).replace("</", "<\\/")
    return (LEARN_HTML.replace("__TITLE__", escape(f"{c['name']}（{c['level']}）"))
            .replace("__DESC__", escape(c.get("one_line", "")))
            .replace("/*__PAGE_JSON__*/null", dump(data))
            .replace("/*__LW__*/null", dump(lw)))


@app.get("/review", response_class=HTMLResponse)
def review_page(request: Request, user=Depends(auth.require_user)):
    due = db.all_("""SELECT k.id, k.q, k.a, k.page_id, c.name AS concept, d.name AS domain
                     FROM cards k JOIN pages p ON p.id = k.page_id JOIN concepts c ON c.id = p.concept_id JOIN domains d ON d.id = p.domain_id
                     WHERE k.user_id = ? AND k.due_at <= ? ORDER BY k.due_at LIMIT 60""", user["id"], auth.ts(auth.now()))
    nxt = db.one("SELECT MIN(due_at) AS t FROM cards WHERE user_id = ? AND due_at > ?", user["id"], auth.ts(auth.now()))
    total = db.one("SELECT COUNT(*) AS n FROM cards WHERE user_id = ?", user["id"])["n"]
    # 交错：不同知识点的卡片穿插着出
    by = {}
    for k in due:
        by.setdefault(k["page_id"], []).append(k)
    mixed = []
    while any(by.values()):
        for v in by.values():
            if v:
                mixed.append(v.pop(0))
    return page(request, "review.html", user=user, cards=mixed, next_due=nxt["t"] if nxt else None, total=total)


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, user=Depends(auth.require_admin)):
    stats = {
        "users": db.one("SELECT COUNT(*) AS n FROM users")["n"],
        "domains": db.one("SELECT COUNT(*) AS n FROM domains WHERE status = 'ready'")["n"],
        "pages": db.one("SELECT COUNT(*) AS n FROM pages WHERE status = 'ready'")["n"],
        "today": round(spent_today(), 2),
        "total": round(db.one("SELECT COALESCE(SUM(cost),0) AS s FROM llm_usage")["s"], 2),
    }
    by_task = db.all_("SELECT task, COUNT(*) AS n, ROUND(SUM(cost), 3) AS cost FROM llm_usage WHERE at >= datetime('now', '-7 days') GROUP BY task ORDER BY cost DESC")
    recent = db.all_("""SELECT j.*, CASE j.kind WHEN 'page' THEN (SELECT c.name || '（' || p.level || '）' FROM pages p JOIN concepts c ON c.id = p.concept_id WHERE p.id = j.ref_id)
                                   ELSE (SELECT name FROM domains WHERE id = j.ref_id) END AS label,
                               (SELECT ROUND(SUM(cost), 3) FROM llm_usage WHERE actor = 'job' || j.id) AS cost
                        FROM jobs j ORDER BY j.id DESC LIMIT 50""")
    pages_q = db.all_("""SELECT p.id, p.level, c.name, d.name AS domain, p.views, p.quality FROM pages p JOIN concepts c ON c.id = p.concept_id
                         JOIN domains d ON d.id = p.domain_id WHERE p.status = 'ready' ORDER BY p.id DESC LIMIT 50""")
    for x in pages_q:
        q = db.loads(x["quality"], {})
        x["score"], x["issues"], x["revised"] = q.get("score"), len(q.get("issues_before") or []), q.get("revised")
    fb = db.all_("SELECT f.*, u.email FROM feedback f LEFT JOIN users u ON u.id = f.user_id ORDER BY f.id DESC LIMIT 30")
    return page(request, "admin.html", user=user, stats=stats, by_task=by_task, recent=recent, pages_q=pages_q, fb=fb)


@app.get("/graph", response_class=HTMLResponse)
def graph_page(request: Request):
    return page(request, "graph.html")


@app.get("/api/graph")
def api_graph(request: Request):
    """全站知识网络：领域、知识点、领域内的前置关系、跨领域联系，以及当前用户点亮过的知识点。"""
    user = auth.current_user(request)
    domains = db.all_("""SELECT d.id, d.name, d.category, d.summary, COUNT(n.concept_id) AS size FROM domains d
                         JOIN domain_nodes n ON n.domain_id = d.id WHERE d.status = 'ready' GROUP BY d.id ORDER BY d.id""")
    ok = {d["id"] for d in domains}
    nodes = {}
    for r in db.all_("""SELECT n.concept_id AS id, c.name, n.domain_id, n.stage, n.one_line FROM domain_nodes n
                        JOIN concepts c ON c.id = n.concept_id ORDER BY n.domain_id, n.ord"""):
        if r["domain_id"] not in ok:
            continue
        x = nodes.setdefault(r["id"], {"id": r["id"], "name": r["name"], "one_line": r["one_line"], "stage": r["stage"], "domains": []})
        x["domains"].append(r["domain_id"])
    pages = {r["concept_id"]: r["n"] for r in db.all_("SELECT concept_id, COUNT(*) AS n FROM pages WHERE status = 'ready' GROUP BY concept_id")}
    for x in nodes.values():
        x["pages"] = pages.get(x["id"], 0)
    prereq = [[e["src"], e["dst"], e["domain_id"]] for e in db.all_("SELECT src, dst, domain_id FROM edges") if e["domain_id"] in ok]
    links = [l for l in db.all_("SELECT src, dst, src_domain AS sd, dst_domain AS dd, type, why, strength FROM links")
             if l["sd"] in ok and l["dd"] in ok and l["src"] in nodes and l["dst"] in nodes]
    mine = {"lit": [], "known": []}
    if user:
        for r in db.all_("SELECT DISTINCT concept_id, status FROM progress WHERE user_id = ? AND status IN ('lit', 'known')", user["id"]):
            mine[r["status"]].append(r["concept_id"])
    return {"domains": domains, "nodes": list(nodes.values()), "prereq": prereq, "links": links, "mine": mine, "loggedIn": bool(user)}


@app.get("/about", response_class=HTMLResponse)
def about(request: Request):
    return page(request, "about.html")


@app.get("/healthz")
def healthz():
    return {"ok": True}


# ---------- 接口 ----------
class CodeIn(BaseModel):
    email: str
    invite: str = ""


class VerifyIn(BaseModel):
    email: str
    code: str


@app.post("/api/login/code")
def api_code(body: CodeIn):
    auth.send_code(body.email, body.invite)
    return {"ok": True}


@app.post("/api/login/verify")
def api_verify(body: VerifyIn):
    token = auth.verify(body.email, body.code)
    r = JSONResponse({"ok": True})
    r.set_cookie(auth.COOKIE, token, max_age=180 * 86400, httponly=True, samesite="lax", secure=SITE_URL.startswith("https"))
    return r


@app.post("/api/logout")
def api_logout(request: Request):
    auth.logout(request)
    r = JSONResponse({"ok": True})
    r.delete_cookie(auth.COOKIE)
    return r


class TopicIn(BaseModel):
    topic: str
    goal: str = ""


@app.post("/api/domains")
def api_new_domain(body: TopicIn, user=Depends(auth.require_user)):
    text = body.topic.strip()
    if not text:
        raise HTTPException(400, "写一下你想学什么")
    # 先看看是不是已经有了，省一次模型调用
    hit = db.one("SELECT id FROM domains WHERE key = ? AND status != 'failed'", pipeline.normkey(text))
    if not hit:
        quota(user, "ai")
        try:
            t = pipeline.classify_topic(text, f"u{user['id']}")
        except BudgetExceeded as e:
            raise HTTPException(429, str(e))
        except LLMError:
            raise HTTPException(503, "AI 暂时没有响应，稍后再试")
        if not t.get("ok"):
            raise HTTPException(400, t.get("reason") or "这个主题暂时不能学")
        key = pipeline.normkey(t["name"])
        hit = db.one("SELECT id, status FROM domains WHERE key = ?", key)
        if hit and hit["status"] == "failed":
            quota(user, "domain")
            db.run("UPDATE domains SET focus = ?, risk = ?, category = ? WHERE id = ?", t["focus"], t["risk"], t["category"], hit["id"])
            jobs.enqueue("domain", hit["id"], user["id"])
        elif not hit:
            quota(user, "domain")
            did = db.run("INSERT INTO domains (name, key, focus, risk, category, created_by) VALUES (?,?,?,?,?,?)",
                         t["name"], key, t["focus"], t["risk"], t["category"], user["id"])
            jobs.enqueue("domain", did, user["id"])
            hit = {"id": did}
    db.run("""INSERT INTO user_domains (user_id, domain_id, goal) VALUES (?,?,?)
              ON CONFLICT (user_id, domain_id) DO UPDATE SET goal = CASE WHEN excluded.goal != '' THEN excluded.goal ELSE goal END,
              updated_at = CURRENT_TIMESTAMP""", user["id"], hit["id"], body.goal.strip()[:100])
    return {"url": f"/d/{hit['id']}"}


@app.get("/api/status/{kind}/{ref}")
def api_status(kind: str, ref: int):
    if kind not in jobs.TABLE:
        raise HTTPException(404, "")
    table, key = jobs.TABLE[kind]
    r = db.one(f"SELECT status, error FROM {table} WHERE {key} = ?", ref)
    if not r:
        raise HTTPException(404, "")
    job = db.one("SELECT id, status, priority FROM jobs WHERE kind = ? AND ref_id = ? ORDER BY id DESC LIMIT 1", kind, ref)
    pos = jobs.queue_position(job["id"]) if job and job["status"] == "queued" else 0
    paused = bool(job and job["status"] == "queued" and job["priority"] >= jobs.PREFETCH_PRIORITY and jobs.prefetch_paused())
    return {"status": r["status"], "error": r["error"], "running": bool(job and job["status"] == "running"), "ahead": pos,
            "paused": paused}


@app.post("/api/retry/{kind}/{ref}")
def api_retry(kind: str, ref: int, user=Depends(auth.require_user)):
    if kind not in jobs.TABLE:
        raise HTTPException(404, "")
    table, key = jobs.TABLE[kind]
    r = db.one(f"SELECT status FROM {table} WHERE {key} = ?", ref)
    if not r or r["status"] != "failed":
        raise HTTPException(400, "不需要重试")
    quota(user, "page" if kind == "page" else "domain")
    jobs.enqueue(kind, ref, user["id"])
    return {"ok": True}


class LevelIn(BaseModel):
    label: str


@app.post("/api/domains/{did}/level")
def api_level(did: int, body: LevelIn, user=Depends(auth.require_user)):
    if body.label not in LABELS:
        raise HTTPException(400, "水平标签不对")
    get_domain(did)
    db.run("""INSERT INTO user_domains (user_id, domain_id, label) VALUES (?,?,?)
              ON CONFLICT (user_id, domain_id) DO UPDATE SET label = excluded.label, updated_at = CURRENT_TIMESTAMP""",
           user["id"], did, body.label)
    return {"ok": True, "tier": TIER_OF[body.label]}


class DiagIn(BaseModel):
    answers: list[int]


@app.post("/api/domains/{did}/diagnose")
def api_diagnose(did: int, body: DiagIn, user=Depends(auth.require_user)):
    diag = db.one("SELECT data FROM diagnostics WHERE domain_id = ? AND status = 'ready'", did)
    if not diag:
        raise HTTPException(404, "诊断题还没准备好")
    qs = db.loads(diag["data"], {}).get("questions", [])
    if len(body.answers) != len(qs) or any(a < -1 or a >= len(q["options"]) for q, a in zip(qs, body.answers)):
        raise HTTPException(400, "请完成所有诊断题，不会的题可以选“我不知道”")
    label, known = pipeline.score_diagnostic(qs, body.answers)
    db.run("""INSERT INTO user_domains (user_id, domain_id, label, diagnosed_at) VALUES (?,?,?,CURRENT_TIMESTAMP)
              ON CONFLICT (user_id, domain_id) DO UPDATE SET label = excluded.label, diagnosed_at = CURRENT_TIMESTAMP,
              updated_at = CURRENT_TIMESTAMP""", user["id"], did, label)
    for cid in known:
        db.run("""INSERT INTO progress (user_id, domain_id, concept_id, status) VALUES (?,?,?,'known')
                  ON CONFLICT (user_id, domain_id, concept_id) DO NOTHING""", user["id"], did, cid)
    review = [{"q": q["q"], "node": q["node"], "options": q["options"], "answer": q["answer"], "why": q["why"],
               "pick": a} for q, a in zip(qs, body.answers)]
    return {"label": label, "tier": TIER_OF[label], "known": len(known), "review": review}


class ProgressIn(BaseModel):
    score: int


@app.get("/api/pages/{pid}/next")
def api_next_page(request: Request, pid: int):
    p = db.one("SELECT * FROM pages WHERE id=? AND status='ready'", pid)
    if not p:
        raise HTTPException(404, "这一页还没有生成好")
    user = auth.current_user(request)
    return {"next": next_page_info(p, pipeline.domain_graph(p["domain_id"]), user_progress(user, p["domain_id"]))}


@app.post("/api/pages/{pid}/progress")
def api_progress(pid: int, body: ProgressIn, user=Depends(auth.require_user)):
    p = db.one("SELECT id, domain_id, concept_id, level FROM pages WHERE id = ?", pid)
    if not p:
        raise HTTPException(404, "")
    status = "lit" if body.score >= 6 else "learning"
    db.run("""INSERT INTO progress (user_id, domain_id, concept_id, status, best_score) VALUES (?,?,?,?,?)
              ON CONFLICT (user_id, domain_id, concept_id) DO UPDATE SET
                status = CASE WHEN progress.status = 'lit' OR excluded.status = 'lit' THEN 'lit' ELSE excluded.status END,
                best_score = MAX(progress.best_score, excluded.best_score), updated_at = CURRENT_TIMESTAMP""",
           user["id"], p["domain_id"], p["concept_id"], status, body.score)
    db.run("""INSERT INTO user_domains (user_id, domain_id) VALUES (?,?) ON CONFLICT DO UPDATE SET updated_at = CURRENT_TIMESTAMP""",
           user["id"], p["domain_id"])
    graph = pipeline.domain_graph(p["domain_id"])
    nxt = next_node(graph, user_progress(user, p["domain_id"]))
    if status == "lit":
        prefetch_next(p, graph, user_progress(user, p["domain_id"]))
    return {"lit": status == "lit", "next": {"name": nxt["name"], "url": f"/d/{p['domain_id']}/n/{nxt['id']}"} if nxt else None}


# 间隔复习：简化的 SM-2。good 按难度系数拉长间隔，hard 小幅拉长并降低系数，again 明天再来
def schedule(card: dict | None, rating: str) -> dict:
    interval = card["interval_days"] if card else 0
    ease = card["ease"] if card else 2.5
    reps = card["reps"] if card else 0
    lapses = card["lapses"] if card else 0
    if rating == "again":
        interval, ease, lapses = 1, max(1.3, ease - 0.2), lapses + 1
    elif rating == "hard":
        interval, ease = max(1, interval * 1.2) if interval else 1, max(1.3, ease - 0.15)
    else:
        interval = 2 if not interval else max(interval + 1, interval * ease)
    due = auth.now() + timedelta(days=interval) - timedelta(hours=2)   # 提前两小时，免得第二天同一时间还没到期
    return dict(interval_days=round(interval, 2), ease=round(ease, 2), reps=reps + 1, lapses=lapses, due_at=auth.ts(due), last_rating=rating)


class CardIn(BaseModel):
    idx: str
    rating: str


@app.post("/api/pages/{pid}/cards")
def api_card(pid: int, body: CardIn, user=Depends(auth.require_user)):
    if body.rating not in ("good", "hard", "again"):
        raise HTTPException(400, "")
    p = db.one("SELECT data FROM pages WHERE id = ? AND status = 'ready'", pid)
    if not p:
        raise HTTPException(404, "")
    try:
        ci, ri = map(int, body.idx.split("-"))
        r = db.loads(p["data"], {})["chunks"][ci]["recall"][ri]
    except (ValueError, KeyError, IndexError):
        raise HTTPException(400, "")
    old = db.one("SELECT * FROM cards WHERE user_id = ? AND page_id = ? AND idx = ?", user["id"], pid, body.idx)
    s = schedule(None, body.rating) if not old else {**schedule(None, body.rating), "reps": old["reps"], "lapses": old["lapses"]}
    db.run("""INSERT INTO cards (user_id, page_id, idx, q, a, interval_days, ease, reps, lapses, due_at, last_rating)
              VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT (user_id, page_id, idx) DO UPDATE SET
              interval_days = excluded.interval_days, ease = excluded.ease, due_at = excluded.due_at,
              last_rating = excluded.last_rating, updated_at = CURRENT_TIMESTAMP""",
           user["id"], pid, body.idx, r["q"], r["a"], s["interval_days"], s["ease"], s["reps"], s["lapses"], s["due_at"], s["last_rating"])
    return {"ok": True, "due_at": s["due_at"]}


class RatingIn(BaseModel):
    rating: str


@app.post("/api/cards/{card_id}/review")
def api_review(card_id: int, body: RatingIn, user=Depends(auth.require_user)):
    if body.rating not in ("good", "hard", "again"):
        raise HTTPException(400, "")
    card = db.one("SELECT * FROM cards WHERE id = ? AND user_id = ?", card_id, user["id"])
    if not card:
        raise HTTPException(404, "")
    s = schedule(card, body.rating)
    db.run("""UPDATE cards SET interval_days = ?, ease = ?, reps = ?, lapses = ?, due_at = ?, last_rating = ?,
              updated_at = CURRENT_TIMESTAMP WHERE id = ?""", s["interval_days"], s["ease"], s["reps"], s["lapses"], s["due_at"], s["last_rating"], card_id)
    return {"ok": True, "interval_days": s["interval_days"]}


def page_data(pid: int) -> dict:
    p = db.one("SELECT data FROM pages WHERE id = ? AND status = 'ready'", pid)
    if not p:
        raise HTTPException(404, "")
    return db.loads(p["data"], {})


class FeynmanIn(BaseModel):
    text: str


@app.post("/api/pages/{pid}/feynman")
def api_feynman(pid: int, body: FeynmanIn, user=Depends(auth.require_user)):
    text = body.text.strip()
    if len(text) < 20:
        raise HTTPException(400, "再多写几句，至少讲清楚它是什么、为什么")
    quota(user, "ai")
    data = page_data(pid)
    f = data["feynman"]
    ctx = (f"知识点：{data['concept']['name']}（{data['concept']['domain']}，{data['concept']['level']}档）\n"
           f"题目：{f['prompt']}\n必须讲到的要点：\n" + "\n".join(f"- {k}" for k in f["key_points"]) +
           f"\n\n学习者的讲解：\n{text[:3000]}")
    try:
        out = chat_json([{"role": "system", "content": prompts.FEYNMAN_SYSTEM}, {"role": "user", "content": ctx}],
                        tier="flash", task="feynman", actor=f"u{user['id']}", max_tokens=4000, temperature=0.2, timeout=90)
    except BudgetExceeded as e:
        raise HTTPException(429, str(e))
    except LLMError:
        raise HTTPException(503, "AI 老师暂时没有响应，稍后再试")
    return out


class TutorIn(BaseModel):
    messages: list[dict]


@app.post("/api/pages/{pid}/tutor")
def api_tutor(pid: int, body: TutorIn, user=Depends(auth.require_user)):
    quota(user, "ai")
    data = page_data(pid)
    slim = {k: data[k] for k in ("concept", "hook", "chunks", "practice", "feynman") if k in data}
    msgs = [{"role": "system", "content": prompts.TUTOR_SYSTEM + json.dumps(slim, ensure_ascii=False)}]
    for m in body.messages[-12:]:
        if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str):
            msgs.append({"role": m["role"], "content": m["content"][:1500]})
    if len(msgs) < 2 or msgs[-1]["role"] != "user":
        raise HTTPException(400, "")
    try:
        reply = chat(msgs, tier="flash", task="tutor", actor=f"u{user['id']}", max_tokens=3000, temperature=0.6, timeout=90)
    except BudgetExceeded as e:
        raise HTTPException(429, str(e))
    except LLMError:
        raise HTTPException(503, "AI 老师暂时没有响应，稍后再试")
    return {"reply": reply.strip()}


class FeedbackIn(BaseModel):
    text: str


@app.post("/api/pages/{pid}/feedback")
def api_feedback(pid: int, body: FeedbackIn, request: Request):
    text = body.text.strip()
    if not 4 <= len(text) <= 2000:
        raise HTTPException(400, "写清楚哪里有问题（4–2000 字）")
    user = auth.current_user(request)
    db.run("INSERT INTO feedback (page_id, user_id, text) VALUES (?,?,?)", pid, user["id"] if user else None, text)
    return {"ok": True}


@app.post("/api/admin/pages/{pid}/regenerate")
def api_regen(pid: int, user=Depends(auth.require_admin)):
    if not db.one("SELECT id FROM pages WHERE id = ?", pid):
        raise HTTPException(404, "")
    jobs.enqueue("page", pid, user["id"])
    return {"ok": True}
