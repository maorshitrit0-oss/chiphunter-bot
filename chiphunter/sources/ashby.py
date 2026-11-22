"""Ashby public job board API. cfg: {"company": "...", "token": "boardname"}"""
from __future__ import annotations

from ..http import get, html_to_text
from ..models import Job
from .greenhouse import is_israel


def list_jobs(cfg, terms=None) -> list[Job]:
    d = get(f"https://api.ashbyhq.com/posting-api/job-board/{cfg['token']}", params={"includeCompensation": "false"}).json()
    out = []
    for p in d.get("jobs", []):
        loc = p.get("location", "") + " " + " ".join(x.get("location", "") for x in p.get("secondaryLocations", []) or [])
        if not is_israel(loc):
            continue
        out.append(Job(company=cfg["company"], title=p.get("title", ""), url=p.get("jobUrl", ""), source="ashby",
                       location=loc.strip(), req_id=p.get("id", ""), posted=p.get("publishedAt", ""),
                       description=html_to_text(p.get("descriptionHtml", "")), apply_url=p.get("applyUrl", "") or p.get("jobUrl", "")))
    return out


def fetch_details(job, cfg):
    return job
