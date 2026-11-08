"""隔离 SQLite 和模型调用：python -m unittest discover -s tests -v"""
import asyncio
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import httpx

from app import auth, db, jobs, main, pipeline
from app.llm import BudgetExceeded


class PrefetchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_patch = patch.object(db, "DB_PATH", str(Path(self.tmp.name) / "test.db"))
        self.db_patch.start()
        self.addCleanup(self.db_patch.stop)
        db.init()
        self.did = db.run("INSERT INTO domains (name, key, status) VALUES ('测试领域','test','ready')")
        self.ids = []
        for i in range(4):
            cid = db.run("INSERT INTO concepts (name, key) VALUES (?,?)", f"知识点{i}", str(i))
            db.run("INSERT INTO domain_nodes (domain_id, concept_id, stage, ord) VALUES (?,?,0,?)", self.did, cid, i)
            self.ids.append(cid)
        for a, b in zip(self.ids, self.ids[1:]):
            db.run("INSERT INTO edges (domain_id, src, dst) VALUES (?,?,?)", self.did, a, b)
        self.uid = db.run("INSERT INTO users (email) VALUES ('test@example.com')")
        db.run("INSERT INTO sessions (token, user_id) VALUES ('test-session',?)", self.uid)
        self.data = json.loads((main.ROOT / "fixtures" / "递归.json").read_text())

    def ready(self, index=0, level="进阶"):
        p = jobs.ensure_page(self.did, self.ids[index], level, prefetch=True)
        db.run("UPDATE pages SET status = 'ready', data = ? WHERE id = ?", json.dumps(self.data), p["id"])
        db.run("UPDATE jobs SET status = 'done' WHERE ref_id = ?", p["id"])
        return db.one("SELECT * FROM pages WHERE id = ?", p["id"])

    def count(self, table):
        return db.one(f"SELECT COUNT(*) AS n FROM {table}")["n"]

    def client(self, logged_in=False):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test",
                                 cookies={auth.COOKIE: "test-session"} if logged_in else {})

    def test_old_database_migration_is_repeatable(self):
        # 模拟上线前没有 priority 列的队列表，保留所有原任务。
        with db.db() as c:
            c.execute("DROP TABLE jobs")
            c.execute("""CREATE TABLE jobs (id INTEGER PRIMARY KEY, kind TEXT, ref_id INTEGER,
                         status TEXT DEFAULT 'queued', step TEXT DEFAULT '', error TEXT DEFAULT '',
                         user_id INTEGER, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                         started_at TEXT, finished_at TEXT)""")
            c.execute("INSERT INTO jobs (kind, ref_id) VALUES ('domain',1)")
        db.init()
        db.init()
        self.assertEqual(db.one("SELECT priority FROM jobs WHERE id=1")["priority"], 0)
        self.assertEqual(self.count("jobs"), 1)

    def test_domain_completion_prepares_three_first_pages(self):
        with patch.object(pipeline, "build_domain") as build:
            jobs._run({"id": 1, "kind": "domain", "ref_id": self.did, "user_id": self.uid})
        build.assert_called_once()
        self.assertEqual({p["level"] for p in db.all_("SELECT * FROM pages")}, set(pipeline.LEVELS))
        self.assertEqual({p["concept_id"] for p in db.all_("SELECT * FROM pages")}, {self.ids[0]})
        self.assertEqual(self.count("jobs"), 4)  # 三档首页 + 跨领域联系
        self.assertTrue(all(p["created_by"] is None for p in db.all_("SELECT * FROM pages")))

    def test_concurrent_prefetch_and_open_create_one_job(self):
        def open_page(i):
            return jobs.ensure_page(self.did, self.ids[1], "进阶", self.uid, prefetch=i % 2 == 0)["id"]
        with ThreadPoolExecutor(max_workers=8) as pool:
            pids = list(pool.map(open_page, range(24)))
        self.assertEqual(len(set(pids)), 1)
        self.assertEqual(self.count("jobs"), 1)
        self.assertEqual(db.one("SELECT priority FROM jobs")["priority"], 0)

    def test_priority_and_queue_position(self):
        jobs.prefetch_first_pages(self.did)
        active = jobs.ensure_page(self.did, self.ids[1], "进阶", self.uid)
        jid = db.one("SELECT id FROM jobs WHERE ref_id = ?", active["id"])["id"]
        self.assertEqual(jobs.queue_position(jid), 0)
        self.assertEqual(jobs._claim()["ref_id"], active["id"])
        jobs.ensure_page(self.did, self.ids[0], "深入", self.uid)
        self.assertEqual(jobs._claim()["priority"], 0)

    def test_ready_and_failed_pages_not_regenerated_automatically(self):
        p = self.ready()
        jobs.ensure_page(self.did, self.ids[0], "进阶", prefetch=True)
        self.assertEqual(self.count("jobs"), 1)
        db.run("UPDATE pages SET status = 'failed', error = '保留错误' WHERE id = ?", p["id"])
        jobs.ensure_page(self.did, self.ids[0], "进阶", prefetch=True)
        self.assertEqual(self.count("jobs"), 1)
        self.assertEqual(db.one("SELECT error FROM pages")["error"], "保留错误")
        jobs.ensure_page(self.did, self.ids[0], "进阶", self.uid)
        self.assertEqual(self.count("jobs"), 2)
        self.assertEqual(db.one("SELECT status FROM pages")["status"], "pending")

    async def test_start_recovers_running_and_backfills_once(self):
        p = jobs.ensure_page(self.did, self.ids[0], "进阶", prefetch=True)
        jobs._claim()
        self.assertEqual(db.one("SELECT status FROM jobs")["status"], "running")
        with patch.object(jobs, "JOB_CONCURRENCY", 0):
            self.assertEqual(jobs.start(), [])
            self.assertEqual(jobs.start(), [])
        self.assertEqual(self.count("pages"), 3)
        self.assertEqual(self.count("jobs"), 4)
        self.assertEqual(db.one("SELECT status FROM jobs WHERE ref_id=? AND kind='page'", p["id"])["status"], "queued")

    async def test_public_read_prefetches_one_page_without_progress(self):
        p = self.ready()
        async with self.client() as client:
            for _ in range(2):
                r = await client.get(f"/p/{p['id']}")
                self.assertEqual(r.status_code, 200)
                self.assertNotIn("/*__PAGE_JSON__*/null", r.text)
        future = db.all_("SELECT * FROM pages WHERE id != ?", p["id"])
        self.assertEqual([(r["concept_id"], r["level"]) for r in future], [(self.ids[1], "进阶")])
        self.assertEqual(self.count("jobs"), 2)
        self.assertEqual(self.count("progress"), 0)

    async def test_read_skips_known_nodes_and_preserves_tier(self):
        p = self.ready(level="深入")
        db.run("INSERT INTO progress (user_id, domain_id, concept_id, status) VALUES (?,?,?,'known')",
               self.uid, self.did, self.ids[1])
        async with self.client(True) as client:
            self.assertEqual((await client.get(f"/p/{p['id']}")).status_code, 200)
        future = db.one("SELECT * FROM pages WHERE id != ?", p["id"])
        self.assertEqual((future["concept_id"], future["level"]), (self.ids[2], "深入"))
        self.assertEqual(self.count("progress"), 1)

    def test_last_page_and_all_done_do_not_prefetch(self):
        p = self.ready(index=3)
        graph = pipeline.domain_graph(self.did)
        main.prefetch_next(p, graph, {cid: "lit" for cid in self.ids})
        self.assertEqual(self.count("pages"), 1)

    async def test_prefetch_error_does_not_break_read(self):
        p = self.ready()
        with patch.object(jobs, "ensure_page", side_effect=sqlite3.OperationalError("test")):
            async with self.client() as client:
                self.assertEqual((await client.get(f"/p/{p['id']}")).status_code, 200)

    async def test_open_promotes_pending_and_anonymous_cannot_create(self):
        p = jobs.ensure_page(self.did, self.ids[0], "进阶", prefetch=True)
        async with self.client() as client:
            self.assertEqual((await client.get(f"/d/{self.did}/n/{self.ids[2]}?level=进阶")).status_code, 200)
        self.assertEqual(self.count("pages"), 1)
        async with self.client(True) as client:
            r = await client.get(f"/d/{self.did}/n/{self.ids[0]}?level=进阶")
            self.assertEqual(r.status_code, 200)
            status = (await client.get(f"/api/status/page/{p['id']}")).json()
        self.assertEqual(status["status"], "pending")
        self.assertEqual(db.one("SELECT priority FROM jobs")["priority"], 0)

    async def test_quiz_cards_and_recommendation_still_work(self):
        p = self.ready()
        async with self.client(True) as client:
            await client.get(f"/p/{p['id']}")
            r = await client.post(f"/api/pages/{p['id']}/progress", json={"score": 6})
            self.assertTrue(r.json()["lit"])
            self.assertEqual(r.json()["next"]["url"], f"/d/{self.did}/n/{self.ids[1]}")
            r = await client.post(f"/api/pages/{p['id']}/cards", json={"idx": "0-0", "rating": "good"})
            self.assertEqual(r.status_code, 200)
            for url in ("/", f"/d/{self.did}", "/review", "/graph", "/healthz"):
                self.assertEqual((await client.get(url)).status_code, 200, url)
        self.assertEqual(self.count("pages"), 2)
        self.assertEqual(self.count("cards"), 1)
        self.assertEqual(db.one("SELECT status FROM progress")["status"], "lit")

    async def test_worker_budget_failure_is_visible_and_retryable(self):
        p = jobs.ensure_page(self.did, self.ids[0], "进阶", prefetch=True)
        with patch.object(pipeline, "build_page", side_effect=BudgetExceeded("今天额度用完了")):
            task = asyncio.create_task(jobs.worker(0))
            try:
                for _ in range(100):
                    if db.one("SELECT status FROM pages")["status"] == "failed":
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(db.one("SELECT status FROM pages")["status"], "failed")
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
        async with self.client(True) as client:
            r = await client.post(f"/api/retry/page/{p['id']}")
            self.assertEqual(r.status_code, 200)
        self.assertEqual(self.count("jobs"), 2)

    def test_prefetch_uses_original_generation_and_quality_checks(self):
        p = jobs.ensure_page(self.did, self.ids[0], "进阶", prefetch=True)
        # 使用样例页隔离模型，运行真实 build_page 的结构校验、质检和入库。
        with (patch.object(pipeline, "search", return_value=[]),
              patch.object(pipeline, "fill_page", side_effect=lambda value, **kw: value),
              patch.object(pipeline, "chat_json", side_effect=[self.data, {"safe": True, "score": 95, "issues": []}]) as chat):
            jobs._run({"id": 1, "kind": "page", "ref_id": p["id"], "user_id": None})
        result = db.one("SELECT * FROM pages WHERE id = ?", p["id"])
        self.assertEqual(result["status"], "ready")
        self.assertEqual(db.loads(result["quality"])["score"], 95)
        self.assertEqual([c.kwargs["task"] for c in chat.call_args_list], ["page", "check"])
        self.assertEqual(self.count("pages"), 1)  # 生成完成不能递归生成整条路线

    async def test_manual_page_quota_remains_enforced(self):
        with patch.object(main, "USER_DAILY_PAGES", 0):
            async with self.client(True) as client:
                r = await client.get(f"/d/{self.did}/n/{self.ids[2]}?level=进阶")
                self.assertEqual(r.status_code, 429)
        self.assertEqual(self.count("pages"), 0)

    def test_backfill_existing_domains_reuses_pages_and_explicit_retry(self):
        p = self.ready()
        counts = jobs.backfill_first_pages()
        self.assertEqual(counts, {"domains": 1, "ready": 1, "pending": 2, "failed": 0})
        jobs.backfill_first_pages()
        self.assertEqual(self.count("jobs"), 3)
        db.run("UPDATE pages SET status='failed' WHERE id=?", p["id"])
        self.assertEqual(jobs.backfill_first_pages()["failed"], 1)
        self.assertEqual(self.count("jobs"), 3)
        self.assertEqual(jobs.backfill_first_pages(retry_failed=True)["pending"], 3)
        self.assertEqual(self.count("jobs"), 4)
        self.assertEqual(db.one("SELECT priority FROM jobs ORDER BY id DESC LIMIT 1")["priority"], jobs.PREFETCH_PRIORITY)

    async def test_anonymous_wait_and_failed_page_require_explicit_retry(self):
        p = jobs.ensure_page(self.did, self.ids[0], "启蒙", prefetch=True)
        url = f"/d/{self.did}/n/{self.ids[0]}?level=启蒙"
        async with self.client() as client:
            r = await client.get(url)
            self.assertIn('id="busy"', r.text)
            self.assertNotIn('id="retry"', r.text)
        db.run("UPDATE pages SET status='failed', error='生成失败' WHERE id=?", p["id"])
        db.run("UPDATE jobs SET status='failed' WHERE ref_id=?", p["id"])
        async with self.client(True) as client:
            r = await client.get(url)
            self.assertIn('id="retry"', r.text)
            self.assertEqual(self.count("jobs"), 1)
            self.assertEqual(db.one("SELECT status FROM pages")["status"], "failed")
            self.assertEqual((await client.post(f"/api/retry/page/{p['id']}")).status_code, 200)
        self.assertEqual(self.count("jobs"), 2)

    async def test_map_shows_preparing_and_ready_levels(self):
        self.ready()
        jobs.backfill_first_pages()
        async with self.client() as client:
            r = await client.get(f"/d/{self.did}")
        self.assertIn("进阶 · 可学", r.text)
        self.assertIn("启蒙 · 准备中", r.text)

    async def test_budget_pauses_backfill_until_allowance_recovers(self):
        jobs.backfill_first_pages()
        with patch.object(jobs, "check_budget", side_effect=BudgetExceeded("今日额度已用完")):
            self.assertIsNone(jobs._claim())
            self.assertEqual(db.one("SELECT COUNT(*) AS n FROM jobs WHERE status='queued'")["n"], 3)
            async with self.client() as client:
                p = db.one("SELECT id FROM pages LIMIT 1")
                r = await client.get(f"/api/status/page/{p['id']}")
                self.assertTrue(r.json()["paused"])
        with patch.object(jobs, "check_budget"):
            self.assertIsNotNone(jobs._claim())

    async def test_diagnosis_and_review_user_journey(self):
        p = self.ready()
        questions = [{"stage": i // 3, "node": f"知识点{i % 4}", "concept_id": self.ids[i % 4],
                      "q": f"问题{i}", "options": ["正确", "错误", "不知道"], "answer": 0, "why": "原因"}
                     for i in range(9)]
        db.run("INSERT INTO diagnostics (domain_id,status,data) VALUES (?,'ready',?)", self.did,
               json.dumps({"questions": questions}))
        async with self.client(True) as client:
            self.assertEqual((await client.get(f"/d/{self.did}/diagnose")).status_code, 200)
            r = await client.post(f"/api/domains/{self.did}/diagnose", json={"answers": [-1] * 9})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["tier"], "启蒙")
            invalid = await client.post(f"/api/domains/{self.did}/diagnose", json={"answers": []})
            self.assertEqual(invalid.status_code, 400)
            expert = await client.post(f"/api/domains/{self.did}/diagnose", json={"answers": [0] * 9})
            self.assertEqual(expert.json()["tier"], "深入")
            r = await client.post(f"/api/pages/{p['id']}/cards", json={"idx": "0-0", "rating": "good"})
            self.assertEqual(r.status_code, 200)
            db.run("UPDATE cards SET due_at='2020-01-01 00:00:00'")
            self.assertIn(json.dumps(self.data["chunks"][0]["recall"][0]["q"]), (await client.get('/review')).text)
            card = db.one("SELECT id FROM cards")
            r = await client.post(f"/api/cards/{card['id']}/review", json={"rating": "good"})
            self.assertEqual(r.status_code, 200)
            self.assertGreater(r.json()["interval_days"], 2)

    async def test_login_retains_selected_level_and_rejects_external_return(self):
        from urllib.parse import parse_qs, urlsplit
        async with self.client() as client:
            r = await client.get(f"/d/{self.did}/diagnose?level=深入")
            self.assertEqual(r.status_code, 303)
            target = parse_qs(urlsplit(r.headers['location']).query)['next'][0]
            self.assertEqual(urlsplit(target).path, f"/d/{self.did}/diagnose")
            self.assertEqual(parse_qs(urlsplit(target).query)["level"], ["深入"])
            r = await client.get('/login', params={'next': '//example.com'})
            self.assertIn('location.href = "/"', r.text)

    async def test_next_page_status_updates_without_creating_more_pages(self):
        p = self.ready()
        async with self.client() as client:
            r = await client.get(f"/api/pages/{p['id']}/next")
            self.assertEqual(r.json()["next"]["status"], "missing")
            self.assertEqual(self.count("pages"), 1)
            await client.get(f"/p/{p['id']}")
            r = await client.get(f"/api/pages/{p['id']}/next")
            self.assertEqual(r.json()["next"]["status"], "pending")
            self.assertIn('level=', r.json()["next"]["url"])
            self.ready(index=1)
            r = await client.get(f"/api/pages/{p['id']}/next")
            self.assertEqual(r.json()["next"]["status"], "ready")
            self.assertEqual(self.count("pages"), 2)


if __name__ == "__main__":
    unittest.main()
