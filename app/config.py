"""所有配置都从环境变量（.env）读，默认值适合本机开发。"""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

env = os.environ.get


def num(name: str, default: float) -> float:
    try:
        return float(env(name) or default)
    except ValueError:
        return default


DB_PATH = env("DB_PATH") or str(ROOT / "data" / "learnwise.db")
SITE_URL = (env("SITE_URL") or "http://127.0.0.1:8400").rstrip("/")

# 模型：DeepSeek，OpenAI 兼容接口。生成和质检用 pro，诊断判分、费曼打分、问老师用 flash
DEEPSEEK_API_KEY = env("DEEPSEEK_API_KEY") or ""
DEEPSEEK_BASE_URL = (env("DEEPSEEK_BASE_URL") or "https://api.deepseek.com").rstrip("/")
MODELS = {
    "pro": dict(model=env("MODEL_PRO") or "deepseek-v4-pro",
                price=(num("PRICE_PRO_INPUT", 9), num("PRICE_PRO_CACHED", 0.3), num("PRICE_PRO_OUTPUT", 27))),
    "flash": dict(model=env("MODEL_FLASH") or "deepseek-flash",
                  price=(num("PRICE_FLASH_INPUT", 2), num("PRICE_FLASH_CACHED", 0.05), num("PRICE_FLASH_OUTPUT", 8))),
}

# 联网检索：博查。没有 key 时只用模型自己的知识（仅供本机测试）
BOCHA_API_KEY = env("BOCHA_API_KEY") or ""
BOCHA_URL = env("BOCHA_URL") or "https://api.bochaai.com/v1/web-search"
BOCHA_PRICE = num("BOCHA_PRICE", 0.036)  # 元/次

# 邮件（登录验证码）。不填时验证码打印在日志里，只适合本机开发
SMTP_HOST = env("SMTP_HOST") or ""
SMTP_PORT = int(num("SMTP_PORT", 465))
SMTP_USER = env("SMTP_USER") or ""
SMTP_PASSWORD = env("SMTP_PASSWORD") or ""
SMTP_FROM = env("SMTP_FROM") or (f"LearnWise <{SMTP_USER}>" if SMTP_USER else "")

INVITE_CODE = env("INVITE_CODE") or ""          # 新用户注册要填的邀请码，留空表示开放注册
ADMIN_EMAILS = {e.strip().lower() for e in (env("ADMIN_EMAILS") or "").split(",") if e.strip()}
MODEL_DISCLOSURE = env("MODEL_DISCLOSURE") or "内容由 DeepSeek 大模型生成"

# 成本控制
DAILY_BUDGET_YUAN = num("DAILY_BUDGET_YUAN", 30)    # 全站每天模型成本上限（北京时间零点重置），0 表示不限
USER_DAILY_DOMAINS = int(num("USER_DAILY_DOMAINS", 5))   # 每人每天最多新建几个领域
USER_DAILY_PAGES = int(num("USER_DAILY_PAGES", 20))      # 每人每天最多触发生成几页
USER_DAILY_AI = int(num("USER_DAILY_AI", 80))            # 每人每天费曼打分 + 问老师 + 诊断的次数
JOB_CONCURRENCY = int(num("JOB_CONCURRENCY", 2))
