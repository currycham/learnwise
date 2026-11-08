"""后台任务队列：存在 SQLite 里，网站进程内用几个协程按顺序取任务，在线程里跑（模型调用是同步的 httpx）。
重启后把“运行中”的任务放回队列，接着跑。所以 uvicorn 只能开 1 个 worker。"""
import asyncio
import logging
import traceback

from . import db, pipeline
from .config import JOB_CONCURRENCY
from .llm import BudgetExceeded, LLMError, check_budget

log = logging.getLogger("learnwise.jobs")

TABLE = {"domain": ("domains", "id"), "page": ("pages", "id"), "diagnostic": ("diagnostics", "domain_id"),
         "links": ("link_runs", "domain_id")}
PREFETCH_PRIORITY = 10


def _enqueue(c, kind: str, ref_id: int, user_id: int | None, priority: int) -> int:
    r = c.execute("SELECT id FROM jobs WHERE kind = ? AND ref_id = ? AND status IN ('queued', 'running')",
                  (kind, ref_id)).fetchone()
    if r:
        c.execute("UPDATE jobs SET priority = MIN(priority, ?) WHERE id = ?", (priority, r["id"]))
        return r["id"]
    if kind == "links":
        c.execute("INSERT OR IGNORE INTO link_runs (domain_id) VALUES (?)", (ref_id,))
    jid = c.execute("INSERT INTO jobs (kind, ref_id, user_id, priority) VALUES (?,?,?,?)",
                    (kind, ref_id, user_id, priority)).lastrowid
    table, key = TABLE[kind]
    c.execute(f"UPDATE {table} SET status = 'pending', error = '' WHERE {key} = ?", (ref_id,))
    return jid


def enqueue(kind: str, ref_id: int, user_id: int | None) -> int:
    with db.db() as c:
        c.execute("BEGIN IMMEDIATE")
        jid = _enqueue(c, kind, ref_id, user_id, 20 if kind == "links" else 0)
        c.execute("COMMIT")
        return jid


def ensure_page(did: int, cid: int, level: str, user_id: int | None = None, *,
                prefetch: bool = False, retry_failed: bool = False) -> dict:
    """创建页面和任务在同一事务里；并发阅读、主动打开和首页补漏共用去重。
    预生成不重试失败页面，不占个人主动生成配额；仍走原有生成、质检和全站预算。
    """
    if level not in pipeline.LEVELS:
        raise ValueError("水平档位不对")
    with db.db() as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute("""INSERT INTO pages (domain_id, concept_id, level, created_by) VALUES (?,?,?,?)
                     ON CONFLICT (domain_id, concept_id, level) DO NOTHING""",
                  (did, cid, level, None if prefetch else user_id))
        p = c.execute("SELECT * FROM pages WHERE domain_id = ? AND concept_id = ? AND level = ?",
                      (did, cid, level)).fetchone()
        if p["status"] != "ready" and (p["status"] != "failed" or not prefetch or retry_failed):
            _enqueue(c, "page", p["id"], user_id, PREFETCH_PRIORITY if prefetch else 0)
        p = dict(c.execute("SELECT * FROM pages WHERE id = ?", (p["id"],)).fetchone())
        c.execute("COMMIT")
        return p


def prefetch_first_pages(did: int, *, retry_failed: bool = False) -> list[dict]:
    first = db.one("SELECT concept_id FROM domain_nodes WHERE domain_id = ? ORDER BY ord LIMIT 1", did)
    if first:
        return [ensure_page(did, first["concept_id"], level, prefetch=True, retry_failed=retry_failed)
                for level in pipeline.LEVELS]
    return []


def backfill_first_pages(*, retry_failed: bool = False) -> dict:
    """可在运行期间调用，为所有已有领域补齐首页，重复执行不会重复生成。"""
    counts = {"domains": 0, "ready": 0, "pending": 0, "failed": 0}
    for d in db.all_("SELECT id FROM domains WHERE status = 'ready' ORDER BY id"):
        counts["domains"] += 1
        for p in prefetch_first_pages(d["id"], retry_failed=retry_failed):
            counts[p["status"]] += 1
    return counts


def queue_position(job_id: int) -> int:
    r = db.one("""SELECT COUNT(*) AS n FROM jobs WHERE status = 'queued' AND
                  (priority < (SELECT priority FROM jobs WHERE id = ?) OR
                   (priority = (SELECT priority FROM jobs WHERE id = ?) AND id < ?))""", job_id, job_id, job_id)
    return r["n"]


def _run(job: dict) -> None:
    actor = f"job{job['id']}"
    fn = {"domain": pipeline.build_domain, "page": pipeline.build_page, "diagnostic": pipeline.build_diagnostic,
          "links": pipeline.build_links}[job["kind"]]
    fn(job["ref_id"], actor)
    if job["kind"] == "domain":   # 地图画好后，接着找它和其他领域的联系
        prefetch_first_pages(job["ref_id"])
        enqueue("links", job["ref_id"], job["user_id"])


def prefetch_paused() -> bool:
    try:
        check_budget()
        return False
    except BudgetExceeded:
        return True


def _claim() -> dict | None:
    paused = prefetch_paused()
    with db.db() as c:
        c.execute("BEGIN IMMEDIATE")
        # 跨领域联系要等所有地图都画完再找，否则会漏掉正在画的领域
        r = c.execute("""SELECT * FROM jobs WHERE status = 'queued' AND (? = 0 OR priority < 10)
                         AND NOT (kind = 'links' AND EXISTS (
                             SELECT 1 FROM jobs j2 WHERE j2.kind = 'domain' AND j2.status IN ('queued', 'running')))
                         ORDER BY priority, id LIMIT 1""", (paused,)).fetchone()
        if r:
            c.execute("UPDATE jobs SET status = 'running', started_at = CURRENT_TIMESTAMP WHERE id = ?", (r["id"],))
        c.execute("COMMIT")
        return dict(r) if r else None


async def worker(n: int) -> None:
    while True:
        job = await asyncio.to_thread(_claim)
        if not job:
            await asyncio.sleep(2)   # 网站的请求在别的线程里入队，这里轮询，不用跨线程唤醒
            continue
        table, key = TABLE[job["kind"]]
        log.info("worker %s 开始 job %s %s#%s", n, job["id"], job["kind"], job["ref_id"])
        try:
            await asyncio.to_thread(_run, job)
            db.run("UPDATE jobs SET status = 'done', finished_at = CURRENT_TIMESTAMP WHERE id = ?", job["id"])
            log.info("job %s 完成", job["id"])
        except Exception as e:  # noqa: BLE001 —— 任何失败都记下来，不让队列停住
            if isinstance(e, (pipeline.PipelineError, BudgetExceeded)):
                msg = str(e)
            elif isinstance(e, LLMError):
                msg = "模型暂时出错了，请稍后重试"
            else:
                msg = "生成出错了，请稍后重试"
            log.error("job %s 失败：%s\n%s", job["id"], e, traceback.format_exc())
            db.run("UPDATE jobs SET status = 'failed', error = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?", f"{e}"[:500], job["id"])
            db.run(f"UPDATE {table} SET status = 'failed', error = ? WHERE {key} = ?", msg, job["ref_id"])


def start() -> list[asyncio.Task]:
    db.run("UPDATE jobs SET status = 'queued' WHERE status = 'running'")
    # 兼容已有领域，以及地图已落库但还未来得及入队就重启的情况。
    backfill_first_pages()
    # 补漏：已经画好地图、但还没找过跨领域联系的领域
    for r in db.all_("SELECT id FROM domains WHERE status = 'ready' AND id NOT IN (SELECT domain_id FROM link_runs)"):
        enqueue("links", r["id"], None)
    return [asyncio.create_task(worker(i)) for i in range(JOB_CONCURRENCY)]
