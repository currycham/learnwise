"""模型调用：DeepSeek（OpenAI 兼容接口）。每次调用都记进 llm_usage，超过全站每日预算就拒绝。"""
import json
import re
import time

import httpx

from . import db
from .config import DAILY_BUDGET_YUAN, DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, MODELS


class LLMError(Exception):
    pass


class BudgetExceeded(LLMError):
    pass


def record(task: str, actor: str, cost: float, prompt: int = 0, cached: int = 0, completion: int = 0) -> None:
    db.run("INSERT INTO llm_usage (task, actor, prompt_tokens, cached_tokens, completion_tokens, cost) VALUES (?,?,?,?,?,?)",
           task, actor, prompt, cached, completion, cost)


def spent_today() -> float:
    """北京时间今天的花费（llm_usage.at 是 UTC）。"""
    r = db.one("SELECT COALESCE(SUM(cost), 0) AS s FROM llm_usage WHERE date(at, '+8 hours') = date('now', '+8 hours')")
    return r["s"]


def check_budget() -> None:
    if DAILY_BUDGET_YUAN > 0 and spent_today() >= DAILY_BUDGET_YUAN:
        raise BudgetExceeded("今天的 AI 额度用完了，明天再来")


def chat(messages: list[dict], *, tier: str = "pro", task: str, actor: str = "", json_mode: bool = False,
         max_tokens: int = 8000, effort: str = "low", temperature: float = 0.6, timeout: float = 600) -> str:
    if not DEEPSEEK_API_KEY:
        raise LLMError("没有配置 DEEPSEEK_API_KEY")
    check_budget()
    m = MODELS[tier]
    body = {"model": m["model"], "messages": messages, "max_tokens": max_tokens, "temperature": temperature,
            "reasoning_effort": effort}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    last = None
    for attempt in range(3):
        try:
            r = httpx.post(f"{DEEPSEEK_BASE_URL}/chat/completions", json=body, timeout=timeout,
                           headers={"Authorization": f"Bearer {DEEPSEEK_API_KEY}"})
            if r.status_code in (429, 500, 502, 503, 504):
                raise httpx.HTTPStatusError(f"{r.status_code}", request=r.request, response=r)
            r.raise_for_status()
            data = r.json()
            break
        except httpx.HTTPError as e:
            last = e
            time.sleep(3 * 2 ** attempt)
    else:
        raise LLMError(f"模型请求失败：{last}")
    u = data.get("usage") or {}
    prompt, cached, completion = u.get("prompt_tokens") or 0, u.get("prompt_cache_hit_tokens") or 0, u.get("completion_tokens") or 0
    pin, pcached, pout = m["price"]
    record(task, actor, ((prompt - cached) * pin + cached * pcached + completion * pout) / 1e6, prompt, cached, completion)
    choice = data["choices"][0]
    if choice.get("finish_reason") == "length":
        raise LLMError("模型输出被截断")
    return choice["message"].get("content") or ""


def parse_json(text: str) -> dict:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except ValueError:
        a, b = t.find("{"), t.rfind("}")
        if a >= 0 and b > a:
            return json.loads(t[a:b + 1])
        raise


def chat_json(messages: list[dict], **kw) -> dict:
    """要 JSON。格式坏了就重新生成一次。"""
    for attempt in range(2):
        text = chat(messages, json_mode=True, **kw)
        try:
            out = parse_json(text)
            if isinstance(out, dict):
                return out
        except ValueError:
            pass
    raise LLMError("模型没有返回合法的 JSON")
