"""Public Telegram job channels, read through the public web preview t.me/s/<channel> (no account, no bot needed).

cfg: {"company": "Telegram", "type": "telegram", "channels": ["hitechjobsisrael", ...], "pages": 3}
A post may hold many jobs ("🔹 <a href=link>title</a> 🏢 company · 📍 place"); each link becomes a job.
Only student-titled jobs go on (the scan filters by title), and their details are read from the linked page.
"""
from __future__ import annotations

import html
import re

from ..http import get, html_to_text
from ..models import Job

_POST = re.compile(r'data-post="([^"]+)".*?class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
_ITEM = re.compile(r'<a href="(https?://[^"]+)"[^>]*>(.*?)</a>(.*?)(?=<a href="https?://|$)', re.S)


def _txt(s):
    return html.unescape(re.sub(r"<[^>]+>", " ", s)).replace("‎", " ").strip()


def list_jobs(cfg, terms=None) -> list[Job]:
    out, errors = [], []
    for ch in cfg.get("channels", []):
        url, n = f"https://t.me/s/{ch}", 0
        for _ in range(int(cfg.get("pages", 3))):
            try:
                h = get(url, timeout=25).text
            except Exception as ex:
                errors.append(f"{ch}: {ex}")
                break
            posts = _POST.findall(h)
            for post_id, body in posts:
                items = _ITEM.findall(body)
                if items:
                    for link, title, rest in items:
                        rest_t = _txt(rest)
                        comp = re.search(r"🏢\s*([^·📍🧑🔹\n]+)", rest_t)
                        loc = re.search(r"📍\s*([^·🧑🔹\n]+)", rest_t)
                        out.append(_mk(ch, post_id, link, _txt(title), comp.group(1).strip() if comp else "",
                                       loc.group(1).strip() if loc else "", ""))
                else:      # a free-text post: first line is the title, whole text is the description
                    text = html_to_text(body)
                    link = re.search(r'href="(https?://(?!t\.me)[^"]+)"', body)
                    first = next((l.strip() for l in text.splitlines() if l.strip()), "")
                    out.append(_mk(ch, post_id, link.group(1) if link else f"https://t.me/{post_id}",
                                   first[:140], "", "", text))
                n += 1
            before = re.findall(r'data-post="[^"/]+/(\d+)"', h)
            if not posts or not before:
                break
            url = f"https://t.me/s/{ch}?before={min(int(b) for b in before)}"
    if errors and not out:
        raise RuntimeError("; ".join(errors)[:200])
    return out


def _mk(ch, post_id, link, title, company, loc, desc) -> Job:
    return Job(company=company or "", title=title, url=link, source="telegram", location=loc,
               req_id=re.sub(r"\W+", "", link)[-40:], description=desc, apply_url=link,
               extra={"channel": ch, "post": f"https://t.me/{post_id}"})


def fetch_details(job, cfg=None):
    """Read the linked posting (LinkedIn via its public endpoint, otherwise the page text)."""
    from . import linkedin
    if "linkedin.com" in job.url:
        j = linkedin.job_from_link(job.url)
        if j:
            job.description = j.description
            job.company = job.company or j.company
            job.location = job.location or j.location
            return job
    if "t.me/" not in job.url:
        h = get(job.url, timeout=25).text
        text = html_to_text(re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", "", h))
        if len(text) > 300:
            job.description = text[:15000]
    if not job.company:
        host = job.url.split("/")[2].replace("www.", "")
        name = host.split(".")[0]
        generic = {"jobs", "careers", "career", "apply", "comeet", "t", "boards", "app", "il"}
        job.company = name.capitalize() if name not in generic else ""
    return job
