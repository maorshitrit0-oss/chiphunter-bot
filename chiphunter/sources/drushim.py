"""Drushim (drushim.co.il) — Israel's big job board. Public search pages, no account.

cfg: {"company": "Drushim", "type": "drushim", "queries": ["סטודנט הנדסת חשמל", ...]}
Each search page lists job cards; for student-titled cards we read the job page's JobPosting JSON-LD.
"""
from __future__ import annotations

import html
import json
import re
from urllib.parse import quote

from ..http import get, html_to_text
from ..models import Job

BASE = "https://www.drushim.co.il"
DEFAULT_QUERIES = ["סטודנט הנדסת חשמל", "סטודנט חשמל", "סטודנט אלקטרוניקה", "סטודנט חומרה",
                   "סטודנט VLSI", "סטודנט FPGA", "סטודנט ולידציה", "סטודנט בדיקות חומרה",
                   "סטודנט NPI", "סטודנט אינטגרציה", "סטודנט מעבדה", "סטודנט RF",
                   "electrical engineering student", "hardware student"]
_CARD = re.compile(r'aria-label="שמור משרה (.*?) למועדפים".*?href="(/job/(\d+)/[0-9a-f]+/)"', re.S)
_STUDENT = re.compile(r"סטודנט|student|intern", re.I)


def list_jobs(cfg, terms=None) -> list[Job]:
    found: dict[str, tuple[str, str]] = {}
    for q in cfg.get("queries", DEFAULT_QUERIES):
        try:
            h = get(f"{BASE}/jobs/search/{quote(q)}/", timeout=25).text
        except Exception:
            continue
        for title, path, jid in _CARD.findall(h):
            found.setdefault(jid, (html.unescape(title), path))
    out = []
    for jid, (title, path) in found.items():
        if not _STUDENT.search(title):
            continue
        try:
            out.append(_job(jid, path, title))
        except Exception:
            continue
    return out


def _job(jid, path, title) -> Job:
    url = BASE + path
    h = get(url, timeout=25).text
    ld = {}
    for s in re.findall(r'(\{"@context":"https://schema.org/?","@type":"JobPosting".*?\})\s*</script>', h, re.S):
        try:
            ld = json.loads(s)
            break
        except Exception:
            pass
    desc = ld.get("description") or ""
    loc = re.search(r"מיקום:\s*([^|\n]+)", desc)
    company = ((ld.get("hiringOrganization") or {}).get("name") or "").strip()
    return Job(company=company or "חברה דרך דרושים", title=ld.get("title") or title, url=url, source="drushim",
               location=loc.group(1).strip() if loc else "", req_id=f"drushim-{jid}",
               posted=ld.get("datePosted", ""), description=html_to_text(desc) if "<" in desc else desc,
               apply_url=url)


def fetch_details(job, cfg=None):
    """Jobs found by the 2-minute watcher come with a title only -> read the job page."""
    if job.description:
        return job
    m = re.search(r"/job/(\d+)/", job.url)
    if m:
        full = _job(m.group(1), job.url.replace(BASE, ""), job.title)
        job.company, job.title, job.description = full.company, full.title, full.description
        job.location, job.posted = full.location or job.location, full.posted
    return job
