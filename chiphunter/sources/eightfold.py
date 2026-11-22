"""Eightfold PCSX career sites (e.g. Qualcomm). cfg: {"company", "host", "domain"}"""
from __future__ import annotations

from ..http import get, html_to_text
from ..models import Job


def list_jobs(cfg, terms=("intern", "student")) -> list[Job]:
    jobs: dict[str, Job] = {}
    for term in terms:
        start = 0
        while True:
            d = get(f"https://{cfg['host']}/api/pcsx/search",
                    params={"domain": cfg["domain"], "query": term, "location": "Israel", "start": start}).json()
            pos = (d.get("data") or {}).get("positions", []) or []
            for p in pos:
                locs = "; ".join(p.get("locations", []))
                if "israel" not in locs.lower():
                    continue
                url = f"https://{cfg['host']}/careers/job/{p['id']}"
                jobs[url] = Job(company=cfg["company"], title=p.get("name", ""), url=url, source="eightfold",
                                location=locs, req_id=str(p.get("displayJobId", p["id"])),
                                apply_url=url, extra={"pid": p["id"]})
            start += len(pos)
            if len(pos) < 10 or start >= 100:
                break
    return list(jobs.values())


def fetch_details(job, cfg):
    d = get(f"https://{cfg['host']}/api/pcsx/position_details",
            params={"position_id": job.extra["pid"], "domain": cfg["domain"], "hl": "en"}).json()
    job.description = html_to_text((d.get("data") or {}).get("jobDescription", ""))
    return job
