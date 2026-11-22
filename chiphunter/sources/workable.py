"""Workable public widget API. cfg: {"company": "...", "token": "account"}"""
from __future__ import annotations

from ..http import get
from ..models import Job


def list_jobs(cfg, terms=None) -> list[Job]:
    d = get(f"https://apply.workable.com/api/v1/widget/accounts/{cfg['token']}", params={"details": "true"}).json()
    out = []
    for p in d.get("jobs", []):
        if (p.get("country") or "").lower() not in ("israel", "il"):
            continue
        url = p.get("url") or p.get("shortlink") or ""
        out.append(Job(company=cfg["company"], title=p.get("title", ""), url=url, source="workable",
                       location=", ".join(filter(None, [p.get("city"), p.get("country")])), req_id=p.get("shortcode", ""),
                       posted=p.get("published_on", ""), description=p.get("description", "") or "", apply_url=url))
    return out


def fetch_details(job, cfg):
    return job
