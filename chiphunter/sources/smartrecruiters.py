"""SmartRecruiters public postings API. cfg: {"company": "...", "token": "CompanyIdentifier"}"""
from __future__ import annotations

from ..http import get, html_to_text
from ..models import Job


def list_jobs(cfg, terms=None) -> list[Job]:
    out, offset = [], 0
    while True:
        d = get(f"https://api.smartrecruiters.com/v1/companies/{cfg['token']}/postings",
                params={"country": "il", "limit": 100, "offset": offset}).json()
        for p in d.get("content", []):
            loc = p.get("location", {}) or {}
            url = f"https://jobs.smartrecruiters.com/{cfg['token']}/{p.get('id')}"
            out.append(Job(company=cfg["company"], title=p.get("name", ""), url=url, source="smartrecruiters",
                           location=", ".join(filter(None, [loc.get("city"), loc.get("country")])),
                           req_id=str(p.get("id", "")), posted=p.get("releasedDate", ""), apply_url=url,
                           extra={"ref": p.get("ref", "")}))
        offset += 100
        if offset >= d.get("totalFound", 0):
            break
    return out


def fetch_details(job, cfg):
    ref = job.extra.get("ref")
    if ref:
        d = get(ref).json()
        secs = (d.get("jobAd") or {}).get("sections", {}) or {}
        job.description = "\n\n".join(html_to_text((v or {}).get("text", "")) for v in secs.values())
        job.apply_url = d.get("applyUrl", job.apply_url)
    return job
