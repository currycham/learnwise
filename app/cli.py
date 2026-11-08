"""运维命令。任务写进队列，由正在运行的网站进程去跑。

    python -m app.cli seed 概率论 机器学习 认知心理学     # 预先建几个领域（审核 + 画地图 + 找跨领域联系）
    python -m app.cli relink                            # 所有领域重新找一遍跨领域联系
    python -m app.cli backfill-homepages                # 补齐已有领域三档首页，无需重启服务
    python -m app.cli backfill-homepages --retry-failed # 同时重试之前失败的首页
"""
import sys

from . import db, jobs, pipeline


def seed(topics: list[str]) -> None:
    for text in topics:
        t = pipeline.classify_topic(text, "cli")
        if not t.get("ok"):
            print(f"✗ {text}：{t.get('reason')}")
            continue
        key = pipeline.normkey(t["name"])
        hit = db.one("SELECT id FROM domains WHERE key = ?", key)
        if hit:
            print(f"· {text} → 已有「{t['name']}」#{hit['id']}")
            continue
        did = db.run("INSERT INTO domains (name, key, focus, risk, category) VALUES (?,?,?,?,?)",
                     t["name"], key, t["focus"], t["risk"], t["category"])
        jobs.enqueue("domain", did, None)
        print(f"✓ {text} → 「{t['name']}」#{did} 已排队")


def relink() -> None:
    db.run("DELETE FROM links")
    for r in db.all_("SELECT id FROM domains WHERE status = 'ready' ORDER BY id"):
        jobs.enqueue("links", r["id"], None)
    print("已排队")


if __name__ == "__main__":
    db.init()
    cmd, args = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ("", [])
    if cmd == "seed" and args:
        seed(args)
    elif cmd == "relink":
        relink()
    elif cmd == "backfill-homepages" and args in ([], ["--retry-failed"]):
        result = jobs.backfill_first_pages(retry_failed="--retry-failed" in args)
        print(f"已检查 {result['domains']} 个领域：{result['ready']} 页可直接学习，"
              f"{result['pending']} 页正在排队或生成，{result['failed']} 页此前失败。")
        print("任务由网站后台执行，受每日预算限制；重复运行不会重复生成。")
    else:
        sys.exit(__doc__)
