"""Greenhouse public job board API. cfg: {"company": "...", "token": "boardtoken"}"""
from __future__ import annotations

from ..http import get, html_to_text
from ..models import Job

ISRAEL = ("israel", "tel aviv", "haifa", "herzliya", "petah", "jerusalem", "yokneam", "netanya",
          "raanana", "ra'anana", "kfar saba", "rehovot", "beer sheva", "be'er sheva", "hod hasharon",
          "caesarea", "ramat gan", "airport city", "rosh ha", "modiin", "kiryat", "omer", "ashdod")


def is_israel(loc: str) -> bool:
    l = (loc or "").lower()
    return any(k in l for k in ISRAEL) or ", il" in l or l.endswith(" il")


def list_jobs(cfg, terms=None) -> list[Job]:
    d = get(f"https://boards-api.greenhouse.io/v1/boards/{cfg['token']}/jobs", params={"content": "true"}).json()
    out = []
    for p in d.get("jobs", []):
        loc = (p.get("location") or {}).get("name", "")
        if not is_israel(loc):
            continue
        out.append(Job(company=cfg["company"], title=p.get("title", ""), url=p.get("absolute_url", ""),
                       source="greenhouse", location=loc, req_id=str(p.get("id", "")),
                       posted=p.get("updated_at", ""), description=html_to_text(p.get("content", "")),
                       apply_url=p.get("absolute_url", "")))
    return out


def fetch_details(job, cfg):
    if not job.description and job.req_id:
        d = get(f"https://boards-api.greenhouse.io/v1/boards/{cfg['token']}/jobs/{job.req_id}").json()
        job.description = html_to_text(d.get("content", ""))
    return job
