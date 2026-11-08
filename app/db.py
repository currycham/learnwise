"""SQLite。每次请求开一个短连接；WAL 模式下网站和后台任务可以同时读写。"""
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    role TEXT DEFAULT 'user',          -- user / admin
    banned INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT
);
CREATE TABLE IF NOT EXISTS login_codes (
    email TEXT PRIMARY KEY,
    code_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    tries INTEGER DEFAULT 0,
    sent_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- 领域：一张领域地图。按规范名去重，所有人共用
CREATE TABLE IF NOT EXISTS domains (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    key TEXT NOT NULL UNIQUE,          -- 规范名去掉空格标点、转小写，用来去重
    focus TEXT DEFAULT '',             -- 用户输入的是一个小知识点时，记下它，地图里重点标出
    summary TEXT DEFAULT '',
    category TEXT DEFAULT '',
    risk TEXT DEFAULT 'none',          -- none / medical / legal / financial
    status TEXT DEFAULT 'pending',     -- pending / ready / failed
    error TEXT DEFAULT '',
    created_by INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
-- 知识点：全站唯一，一个知识点可以出现在多个领域地图里
CREATE TABLE IF NOT EXISTS concepts (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    key TEXT NOT NULL UNIQUE,
    aliases TEXT DEFAULT '[]'
);
CREATE TABLE IF NOT EXISTS concept_alias (
    key TEXT PRIMARY KEY,
    concept_id INTEGER NOT NULL REFERENCES concepts(id)
);
CREATE TABLE IF NOT EXISTS domain_nodes (
    domain_id INTEGER NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
    concept_id INTEGER NOT NULL REFERENCES concepts(id),
    stage INTEGER NOT NULL,            -- 0 入门 / 1 进阶 / 2 深入
    ord INTEGER NOT NULL,
    one_line TEXT DEFAULT '',
    minutes INTEGER DEFAULT 30,
    PRIMARY KEY (domain_id, concept_id)
);
-- 知识点之间的边。现在只有领域地图里的“前置”
CREATE TABLE IF NOT EXISTS edges (
    domain_id INTEGER NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
    src INTEGER NOT NULL REFERENCES concepts(id),   -- 先学
    dst INTEGER NOT NULL REFERENCES concepts(id),   -- 后学
    type TEXT DEFAULT '前置',
    PRIMARY KEY (domain_id, src, dst, type)
);
-- 跨领域联系：两个不同领域里的知识点之间的关系，构成全站知识网络
CREATE TABLE IF NOT EXISTS links (
    id INTEGER PRIMARY KEY,
    src INTEGER NOT NULL REFERENCES concepts(id),
    dst INTEGER NOT NULL REFERENCES concepts(id),
    src_domain INTEGER NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
    dst_domain INTEGER NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
    type TEXT NOT NULL,                -- 类比 / 应用 / 基础 / 对比
    why TEXT DEFAULT '',
    strength INTEGER DEFAULT 1,        -- 1–3
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (src, dst, type)
);
-- 每个领域找跨领域联系的任务状态
CREATE TABLE IF NOT EXISTS link_runs (
    domain_id INTEGER PRIMARY KEY REFERENCES domains(id) ON DELETE CASCADE,
    status TEXT DEFAULT 'pending',
    error TEXT DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
-- 学习页：领域 × 知识点 × 档位，所有人共用
CREATE TABLE IF NOT EXISTS pages (
    id INTEGER PRIMARY KEY,
    domain_id INTEGER NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
    concept_id INTEGER NOT NULL REFERENCES concepts(id),
    level TEXT NOT NULL,               -- 启蒙 / 进阶 / 深入
    status TEXT DEFAULT 'pending',     -- pending / ready / failed
    data TEXT,                         -- 页面 JSON
    quality TEXT DEFAULT '{}',         -- 质检结果
    error TEXT DEFAULT '',
    views INTEGER DEFAULT 0,
    created_by INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (domain_id, concept_id, level)
);
-- 水平诊断题：每个领域一套
CREATE TABLE IF NOT EXISTS diagnostics (
    domain_id INTEGER PRIMARY KEY REFERENCES domains(id) ON DELETE CASCADE,
    status TEXT DEFAULT 'pending',
    data TEXT,
    error TEXT DEFAULT '',
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
-- 用户在一个领域里的水平
CREATE TABLE IF NOT EXISTS user_domains (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    domain_id INTEGER NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
    label TEXT DEFAULT '',             -- 完全不懂 / 听说过 / 入门 / 能用 / 熟练 / 专家
    goal TEXT DEFAULT '',
    diagnosed_at TEXT,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, domain_id)
);
-- 用户 × 领域 × 知识点：known 诊断时答对；lit 闯关及格点亮
CREATE TABLE IF NOT EXISTS progress (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    domain_id INTEGER NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
    concept_id INTEGER NOT NULL REFERENCES concepts(id),
    status TEXT NOT NULL,              -- known / learning / lit
    best_score INTEGER DEFAULT 0,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, domain_id, concept_id)
);
-- 复习卡片（来自学习页的主动回忆题）
CREATE TABLE IF NOT EXISTS cards (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    page_id INTEGER NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    idx TEXT NOT NULL,                 -- 块序号-题序号，如 2-1
    q TEXT NOT NULL,
    a TEXT NOT NULL,
    interval_days REAL DEFAULT 0,
    ease REAL DEFAULT 2.5,
    reps INTEGER DEFAULT 0,
    lapses INTEGER DEFAULT 0,
    due_at TEXT NOT NULL,
    last_rating TEXT DEFAULT '',
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, page_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_cards_due ON cards (user_id, due_at);
-- 后台任务：生成领域地图、诊断题、学习页
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,                -- domain / diagnostic / page
    ref_id INTEGER NOT NULL,
    status TEXT DEFAULT 'queued',      -- queued / running / done / failed
    step TEXT DEFAULT '',
    error TEXT DEFAULT '',
    user_id INTEGER,
    priority INTEGER NOT NULL DEFAULT 0, -- 0 主动请求 / 10 预生成 / 20 跨领域联系
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status, id);
-- 每次模型 / 检索调用的用量。字段和 SaveWise、InvestWise 的 llm_usage 一致，总后台 core/collector 直接能采
CREATE TABLE IF NOT EXISTS llm_usage (
    id INTEGER PRIMARY KEY,
    task TEXT NOT NULL,
    actor TEXT DEFAULT '',             -- u<用户ID>，或 job<任务ID>
    prompt_tokens INTEGER DEFAULT 0,
    cached_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    cost REAL DEFAULT 0,
    at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_at ON llm_usage (at);
CREATE INDEX IF NOT EXISTS idx_llm_usage_actor ON llm_usage (actor, at);
-- 页面上“发现内容有误”的反馈
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY,
    page_id INTEGER,
    user_id INTEGER,
    text TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


def init() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with db() as c:
        c.execute("PRAGMA journal_mode = WAL")
        c.executescript(SCHEMA)
        # 兼容已有线上数据库；不需要重建表或删除任务。
        if "priority" not in {r["name"] for r in c.execute("PRAGMA table_info(jobs)")}:
            c.execute("ALTER TABLE jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 0")
        c.execute("CREATE INDEX IF NOT EXISTS idx_jobs_priority ON jobs (status, priority, id)")


def one(sql: str, *args):
    with db() as c:
        r = c.execute(sql, args).fetchone()
        return dict(r) if r else None


def all_(sql: str, *args) -> list[dict]:
    with db() as c:
        return [dict(r) for r in c.execute(sql, args)]


def run(sql: str, *args) -> int:
    """执行一条写语句，返回 lastrowid。"""
    with db() as c:
        return c.execute(sql, args).lastrowid


def loads(s, default=None):
    try:
        return json.loads(s) if s else default
    except (TypeError, ValueError):
        return default
