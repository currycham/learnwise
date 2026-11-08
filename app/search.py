"""联网检索：博查 Web Search。给模型当资料，也是页面“来源”里唯一允许出现的链接。"""
import re
import time

import httpx

from .config import BOCHA_API_KEY, BOCHA_PRICE, BOCHA_URL
from .llm import record

# 下载站、网盘、商品页没有内容价值，也不该出现在来源里
JUNK = re.compile(r"(下载|网盘|pdf|epub|txt全文|淘宝|天猫|京东|拼多多|当当|包邮|题库|答案解析|代写)", re.I)


def search(queries: list[str], actor: str = "", per_query: int = 8) -> list[dict]:
    """返回去重后的结果：[{title, url, site, text}]。没有 key 时返回空列表。"""
    if not BOCHA_API_KEY:
        return []
    out, seen = [], set()
    for q in queries:
        for attempt in range(4):
            try:
                r = httpx.post(BOCHA_URL, timeout=30, headers={"Authorization": f"Bearer {BOCHA_API_KEY}"},
                               json={"query": q, "summary": True, "count": per_query, "freshness": "noLimit"})
                if r.status_code == 429:
                    time.sleep(3 * 2 ** attempt)
                    continue
                r.raise_for_status()
                break
            except httpx.HTTPError:
                if attempt == 3:
                    r = None
                    break
                time.sleep(2)
        record("search", actor, BOCHA_PRICE)
        if r is None or r.status_code != 200:
            continue
        j = r.json()
        pages = (j.get("data") or {}).get("webPages", {}).get("value") or j.get("webPages", {}).get("value") or []
        for p in pages:
            url, title = p.get("url") or "", p.get("name") or ""
            text = (p.get("summary") or p.get("snippet") or "")[:1200]
            if not url or url in seen or JUNK.search(title):
                continue
            seen.add(url)
            out.append({"title": title, "url": url, "site": p.get("siteName") or "", "text": text})
        time.sleep(1.2)
    return out


def digest(results: list[dict], limit: int = 14) -> str:
    """给模型看的资料清单，编号从 1 开始。"""
    return "\n\n".join(f"[{i}] {r['title']}（{r['site'] or r['url']}）\n{r['text']}" for i, r in enumerate(results[:limit], 1)) or "（没有检索到资料，只能依靠你自己的知识，务必保守，拿不准的不写）"
