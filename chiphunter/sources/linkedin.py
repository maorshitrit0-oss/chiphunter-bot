"""LinkedIn public job search (the same pages you see logged-out). No account is used, so Maor's profile is never touched.

LinkedIn doesn't offer a public API and doesn't like automated reading, so this runs gently (once an hour, a handful of
searches) and may be throttled at times — the scan reports it and carries on with the other sources.

cfg: {"company": "LinkedIn", "type": "linkedin", "keywords": [...], "time_window": "r86400"}
"""
from __future__ import annotations

import html
import os
import re
import time

from ..http import get, html_to_text
from ..models import Job

SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
DETAIL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{id}"
ISRAEL_GEO = "101620260"
DEFAULT_KEYWORDS = [
    "electrical engineering student", "hardware student", "סטודנט להנדסת חשמל", "סטודנט חומרה",
    "validation student", "verification student", "FPGA student", "VLSI student", "chip design student",
    "hardware intern",
]

_CARD = re.compile(r'<li>(.*?)</li>', re.S)


def _field(rx, block):
    m = re.search(rx, block, re.S)
    return html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip() if m else ""


def list_jobs(cfg, terms=None) -> list[Job]:
    jobs: dict[str, Job] = {}
    try:
        from ..learn import learned_search_terms
        extra = learned_search_terms()
    except Exception:
        extra = []
    for kw in list(dict.fromkeys(list(cfg.get("keywords", DEFAULT_KEYWORDS)) + extra)):
        for start in (0, 25):
            try:
                h = get(SEARCH, params={"keywords": kw, "geoId": ISRAEL_GEO, "location": "Israel",
                                        "f_TPR": os.environ.get("LINKEDIN_WINDOW") or cfg.get("time_window", "r86400"),
                                        "start": start}, retries=1).text
            except Exception as ex:
                print(f"[linkedin] '{kw}' failed: {ex}")
                break
            cards = _CARD.findall(h)
            for c in cards:
                m = re.search(r'data-entity-urn="urn:li:jobPosting:(\d+)"', c) or re.search(r"/jobs/view/[^\"?]*?-(\d+)", c)
                if not m:
                    continue
                jid = m.group(1)
                title = _field(r'class="base-search-card__title"[^>]*>(.*?)</h3>', c)
                company = _field(r'class="base-search-card__subtitle"[^>]*>(.*?)</h4>', c)
                loc = _field(r'class="job-search-card__location"[^>]*>(.*?)</span>', c)
                posted = re.search(r'datetime="([\d-]+)"', c)
                url = f"https://www.linkedin.com/jobs/view/{jid}/"
                jobs[jid] = Job(company=company or "LinkedIn", title=title, url=url, source="linkedin", location=loc,
                                req_id=jid, posted=posted.group(1) if posted else "", apply_url=url,
                                extra={"linkedin_id": jid, "via": "linkedin"})
            if len(cards) < 25:
                break
            time.sleep(1.5)
        time.sleep(1.5)
    return list(jobs.values())


def _clean_title(t: str) -> tuple[str, str]:
    """'Chip Design Student at Annapurna Labs — Tel Aviv... | LinkedIn Jobs' -> (title, company)."""
    t = re.split(r"\s+\|\s+LinkedIn", t)[0]
    t = re.split(r"\s+[—–]\s+", t)[0]                       # drop " — Tel Aviv-Yafo, ..." (em/en dash only)
    m = re.match(r"(.+?)\s+at\s+(.+)$", t.strip())
    title, company = (m.group(1).strip(), m.group(2).strip()) if m else (t.strip(), "")
    parts = [x.strip() for x in title.split(",")]
    if len(parts) == 2 and parts[0] == parts[1]:            # "Chip Design Student, Chip Design Student"
        title = parts[0]
    return title, company


def job_from_link(url: str) -> Job | None:
    """A LinkedIn job link (also lnkd.in / linkedin.com/jobs/view/...currentJobId=...) -> full Job."""
    m = re.search(r"(?:jobs/view/(?:[^/?]*-)?|currentJobId=)(\d{6,})", url)
    if not m:
        return None
    jid = m.group(1)
    job = Job(company="", title="", url=f"https://www.linkedin.com/jobs/view/{jid}/", source="linkedin",
              req_id=jid, apply_url=f"https://www.linkedin.com/jobs/view/{jid}/", extra={"linkedin_id": jid, "via": "linkedin"})
    return fetch_details(job)


def fetch_details(job: Job, cfg=None) -> Job:
    h = get(DETAIL.format(id=job.extra.get("linkedin_id") or job.req_id), retries=1).text
    if not job.title or job.company in ("", "LinkedIn", "Linkedin"):
        title = _field(r'class="top-card-layout__title[^"]*"[^>]*>(.*?)</h2>', h) or _field(r"<title>(.*?)</title>", h)
        company = _field(r'class="topcard__org-name-link[^"]*"[^>]*>(.*?)</a>', h) or \
            _field(r'class="topcard__flavor"[^>]*>(.*?)</span>', h)
        t2, c2 = _clean_title(title)
        job.title = t2 or job.title
        job.company = company or c2 or job.company
        loc = _field(r'class="topcard__flavor topcard__flavor--bullet"[^>]*>(.*?)</span>', h)
        job.location = loc or job.location
    m = re.search(r'class="show-more-less-html__markup[^"]*"[^>]*>(.*?)</div>', h, re.S)
    job.description = html_to_text(m.group(1)) if m else html_to_text(h)[:8000]
    crit = re.findall(r'job-criteria__subheader[^>]*>(.*?)</h3>\s*<span[^>]*>(.*?)</span>', h, re.S)
    if crit:
        job.description += "\n\n" + "\n".join(f"{html_to_text(a)}: {html_to_text(b)}" for a, b in crit)
    return job
