"""Web search and page reading for the assistant (2026-11-25, the person: "we need capability
of doing a web search as the references of story sometime need analysis and agentic searches
(like vishnu cannot be golden)").

No API key: results come from DuckDuckGo's HTML endpoint, with Wikipedia's search as the
fallback. read_page fetches one public http(s) page and returns its text; private, loopback
and link-local addresses are refused (the studio runs next to WIS, Ollama and ComfyUI, none of
which may be reached this way).
"""
from __future__ import annotations

import asyncio
import html
import ipaddress
import logging
import re
import socket
from urllib.parse import parse_qs, unquote, urlparse

import httpx

log = logging.getLogger("studio.websearch")

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) StoryStudio/1.0 (research assistant)"}
MAX_PAGE_BYTES = 2_000_000


def _text(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", fragment or "")).strip()


def _real_url(href: str) -> str:
    """DuckDuckGo wraps results as //duckduckgo.com/l/?uddg=<url>."""
    if "uddg=" in href:
        q = parse_qs(urlparse(href if href.startswith("http") else "https:" + href).query)
        if q.get("uddg"):
            return unquote(q["uddg"][0])
    return href


async def _ddg(query: str, n: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=20, headers=UA, follow_redirects=True) as c:
        r = await c.post("https://html.duckduckgo.com/html/", data={"q": query})
    r.raise_for_status()
    links = re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', r.text, re.S)
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', r.text, re.S)
    out = []
    for i, (href, title) in enumerate(links):
        url = _real_url(html.unescape(href))
        if "duckduckgo.com/y.js" in url:            # ads
            continue
        out.append({"title": _text(title), "url": url,
                    "snippet": _text(snippets[i]) if i < len(snippets) else ""})
        if len(out) >= n:
            break
    return out


async def _wikipedia(query: str, n: int) -> list[dict]:
    async with httpx.AsyncClient(timeout=20, headers=UA) as c:
        r = await c.get("https://en.wikipedia.org/w/api.php",
                        params={"action": "query", "list": "search", "srsearch": query,
                                "format": "json", "srlimit": n})
    r.raise_for_status()
    return [{"title": x["title"],
             "url": "https://en.wikipedia.org/wiki/" + x["title"].replace(" ", "_"),
             "snippet": _text(x.get("snippet", ""))}
            for x in r.json().get("query", {}).get("search", [])]


async def search(query: str, n: int = 6) -> dict:
    query = " ".join(str(query or "").split())[:300]
    if not query:
        raise ValueError("give a search query")
    n = max(1, min(10, int(n or 6)))
    results, source = [], "web"
    try:
        results = await _ddg(query, n)
    except Exception as exc:                                   # noqa: BLE001
        log.warning("web search failed (%s); trying Wikipedia", exc)
    if not results:
        source = "wikipedia"
        results = await _wikipedia(query, n)
    return {"query": query, "source": source, "results": results}


def _public(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            return False
    return True


async def read_page(url: str, max_chars: int = 6000) -> dict:
    url = str(url or "").strip()
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("give an http(s) URL")
    if not await asyncio.to_thread(_public, u.hostname):
        raise ValueError("that address is not a public web page")
    async with httpx.AsyncClient(timeout=25, headers=UA, follow_redirects=False) as c:
        for _ in range(5):                                  # follow redirects, re-checking each
            r = await c.get(url)
            if r.is_redirect and r.headers.get("location"):
                url = str(r.url.join(r.headers["location"]))
                host = urlparse(url).hostname or ""
                if not await asyncio.to_thread(_public, host):
                    raise ValueError("the page redirects to an address that is not public")
                continue
            break
    r.raise_for_status()
    body = r.content[:MAX_PAGE_BYTES].decode(r.encoding or "utf-8", "replace")
    title = _text((re.search(r"<title[^>]*>(.*?)</title>", body, re.S | re.I) or [None, ""])[1])
    body = re.sub(r"(?is)<(script|style|noscript|svg|nav|footer|header)[^>]*>.*?</\1>", " ", body)
    text = re.sub(r"\s+", " ", _text(re.sub(r"(?i)<br\s*/?>|</p>|</h\d>|</li>", "\n", body)))
    max_chars = max(500, min(20000, int(max_chars or 6000)))
    return {"url": url, "title": title, "text": text[:max_chars],
            **({"truncated": True} if len(text) > max_chars else {})}
