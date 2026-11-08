# LearnWise · 想学什么，都可以系统地学

**在线体验：<https://learnwise.waywise.tech>**（内测中）

输入一个想学的领域，LearnWise 先画出这个领域的知识地图、判断你的水平，再为每个知识点生成一页约 30 分钟的学习页。每一页都公开：一个人生成，所有人都能学。

## 一页学习页里有什么

每个模块对应一条证据扎实的学习原理（主要来自《学习之道》、费曼学习法和认知科学）：

| 模块 | 做什么 | 依据 |
|---|---|---|
| 开场 | 故事、悖论或真实问题 | 好奇心驱动记忆 |
| 先猜一猜 | 学之前先答，闯关后揭晓 | 生成效应 |
| 地图定位 | 它在领域里的位置、前置、可解锁 | 先全局后细节 |
| 精学 | 3–5 块：讲解、类比、例子、图、误区，每块 2 道主动回忆 | 组块、双重编码、主动回忆 |
| 练习 | 启蒙：完整范例；进阶：范例 → 挖空 → 独立；深入：开放题 | 渐隐范例、刻意练习 |
| 争议 | 深入档：流派之争、前沿和边界 | 专长逆转效应 |
| 闯关 | 8 题，混入前置知识和新场景，及格点亮地图 | 交错练习、检索练习 |
| 费曼 | 用大白话讲一遍，AI 老师对照要点打分 | 费曼学习法 |
| 迁移 | 用在自己的一件事上 | 精细化加工 |
| 复习 | 回忆题按遗忘曲线排期 | 间隔重复 |
| 问老师 | 苏格拉底式追问 | 苏格拉底法 |

**知识星图**（`/graph`）把所有领域的知识点画成一张网：同一个知识点出现在多个领域时是同一个节点；每画好一张新地图，模型会找出它和已有领域之间的联系（类比、应用、基础、对比），每条写明具体联系在哪。

同一个知识点分启蒙、进阶、深入三档。水平可以自选（完全不懂 / 听说过 / 入门 / 能用 / 熟练 / 专家），也可以做 9 道诊断题。

设计与决策见 [docs/调研与设计.md](docs/调研与设计.md)，学习页的数据结构见 [docs/学习页结构.md](docs/学习页结构.md)。

## 生成流程

```
用户输入 ──► 入口审核 + 规范领域名（flash）
                ▼
        联网检索 ──► 领域地图（pro）──► 校验前置关系 ──► 知识点去重入库
                ▼
地图完成后预生成首个知识点 × 三档；阅读时预生成下一推荐知识点 × 当前档位
其他知识点由用户点开时生成（已有页面或任务直接复用）
                ▼
联网检索 ──► 写学习页（pro，附同档样例）──► 结构校验，不过就带着错误重写
                ▼
        另一轮质检（pro，高推理）：事实、计算、代码、题目答案、内容安全
                ▼
        按质检意见修正 ──► 入库，所有人可学
```

- 模型只输出 JSON，页面由 `app/templates/learn.html` 渲染。
- 页面里的链接只能来自检索结果，模型编的链接会被丢掉。
- 闯关题的选项顺序在本地打乱，避免答案位置有规律。
- 每次模型和检索调用都记在 `llm_usage` 表（字段和 SaveWise、InvestWise 一致），有全站每日预算和每人每日次数上限。
- 用户主动请求优先于排队中的预生成和跨领域联系任务；已经运行的任务会继续完成。
- 预生成按现有推荐路径跳过已掌握的知识点，一次只提前一页，不修改进度、不连续生成整条路线。匿名阅读也会预生成；没有登录仍不能主动创建任意页面。
- 预生成不占个人主动生成次数，仍受全站每日预算约束；失败不会在每次阅读或重启时自动重试，可沿用页面上的手动重试。
- 实测：一张领域地图约 1 分钟、¥0.2–0.3；一页学习页约 3–4 分钟、¥0.8–1。

## 本地运行

需要 Python 3.12+。

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env          # 至少填 DEEPSEEK_API_KEY；ADMIN_EMAILS 填自己的邮箱
.venv/bin/uvicorn app.main:app --port 8400 --reload
```

没配 SMTP 时，登录验证码打印在终端里。

样例页（不起网站）：`python3 scripts/render.py fixtures/*.json`，输出到 `out/`。

## 部署

```bash
DEPLOY_HOST=ubuntu@<服务器IP> DEPLOY_KEY=~/.ssh/waywise_ed25519 deploy/deploy.sh --setup --env <服务器用的.env>   # 第一次
DEPLOY_HOST=ubuntu@<服务器IP> DEPLOY_KEY=~/.ssh/waywise_ed25519 deploy/deploy.sh                             # 以后每次
```

服务在 `127.0.0.1:8400`，Caddy 配置 `deploy/learnwise.caddy`，数据在 `/srv/learnwise/data/`，每天备份到 `/srv/learnwise/backup/`。生成任务队列在网站进程里，只能开 1 个 worker。日志：`sudo journalctl -u learnwise -f`。

本次更新启动时自动给任务表添加优先级字段，并为已有领域补齐首个知识点的三档预生成任务，无需手动迁移。已有页面直接复用，运行中任务在重启后继续排队。地图可先打开，首页在后台生成完成后即可直接学习；刚创建领域时仍可能短暂等待。每个新领域会提前生成三页（按原估算约 ¥2.4–3），阅读时间足够覆盖生成时间时，下一页通常无需等待。

已有领域也可以在服务运行时手动补齐首页（使用与网站相同的 `DB_PATH`，任务由运行中的网站进程执行）：

```bash
python -m app.cli backfill-homepages
# 需要连以前失败的首页也重新排队时：
python -m app.cli backfill-homepages --retry-failed
```

已生成的页面和已有任务会直接复用。未开始的后台预生成任务在全站预算耗尽时暂停，额度恢复后自动继续；已经运行到一半的任务仍可能因额度耗尽失败，可手动重试。上线前应备份数据库，线上默认位置为 `/srv/learnwise/data/learnwise.db`。

部署前可运行（使用临时数据库和模拟模型，不调用付费接口）：

```bash
.venv/bin/python -m unittest discover -s tests -v
node tests/test_learning_ui.cjs
# 本机已安装 Chrome 时，可检查真实浏览器的手机和桌面布局：
node tests/browser_smoke.cjs
PYTHONPATH=. .venv/bin/python tests/review_user_flow.py
.venv/bin/python tests/smoke_server.py
.venv/bin/python -m app.schema fixtures/*.json
.venv/bin/python -m pip check
bash -n deploy/deploy.sh deploy/setup-server.sh deploy/learnwise-backup
```

## 目录

```
app/          网站和内容引擎（FastAPI）
  pipeline.py   入口审核、领域地图、学习页、诊断题
  prompts.py    所有提示词
  schema.py     学习页校验
  jobs.py       后台任务队列
  templates/    页面；learn.html 是学习页
fixtures/     人工打磨的样例页：质量基准，也是生成时给模型看的范例
docs/         调研与设计、学习页结构
deploy/       部署脚本、systemd、Caddy、备份
scripts/      样例页渲染
```

内容由 AI 基于公开资料生成，可能有错误，仅供学习参考。
