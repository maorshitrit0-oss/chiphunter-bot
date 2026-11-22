"""HTTP helper. Uses curl_cffi (real-browser TLS fingerprint) when available."""
from __future__ import annotations

import html
import re
import time

try:
    from curl_cffi import requests as _req
    _KW = {"impersonate": "chrome"}
except ImportError:  # pragma: no cover
    import requests as _req
    _KW = {}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def request(method: str, url: str, *, retries: int = 2, timeout: int = 25, **kw):
    headers = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9,he;q=0.8"}
    headers.update(kw.pop("headers", {}) or {})
    last = None
    for attempt in range(max(retries, 3) + 1):
        if attempt > retries and not (last is None or "429" in str(last)):
            break
        try:
            r = _req.request(method, url, headers=headers, timeout=timeout, **_KW, **kw)
            if r.status_code == 429 and attempt < max(retries, 3):    # rate-limited: back off properly
                ra = r.headers.get("Retry-After", "")
                time.sleep(min(int(ra), 30) if ra.isdigit() else 8 * (attempt + 1))
                continue
            if r.status_code in (500, 502, 503, 504) and attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            return r
        except Exception as e:  # network errors, HTTP errors
            last = e
            if attempt < retries:
                time.sleep(2 * (attempt + 1))
    raise last


def get(url, **kw):
    return request("GET", url, **kw)


def post(url, **kw):
    return request("POST", url, **kw)


_TAG = re.compile(r"<[^>]+>")


def html_to_text(s: str) -> str:
    """Rough HTML -> readable plain text (keeps line/bullet breaks)."""
    if not s:
        return ""
    s = re.sub(r"(?i)<\s*(br|/p|/li|/h\d|/div)\s*/?>", "\n", s)
    s = re.sub(r"(?i)<\s*li[^>]*>", "• ", s)
    s = _TAG.sub("", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\xa0]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()
