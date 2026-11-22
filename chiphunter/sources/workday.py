"""Workday (myworkdayjobs.com) public CXS API.

cfg: {"company": "NVIDIA", "host": "nvidia.wd5.myworkdayjobs.com", "site": "NVIDIAExternalCareerSite"}
"""
from __future__ import annotations

from ..http import post, get, html_to_text
from ..models import Job

_facet_cache: dict[str, tuple[str, str] | None] = {}


def _base(cfg):
    tenant = cfg["host"].split(".")[0]
    return f"https://{cfg['host']}/wday/cxs/{tenant}/{cfg['site']}"


def _israel_facet(cfg):
    """Find the facet (parameter, [ids]) meaning 'Israel' for this tenant.
    Prefers a country-level value; falls back to all 'Israel, <city>' location values."""
    key = cfg["host"] + cfg["site"]
    if key in _facet_cache:
        return _facet_cache[key]
    d = post(_base(cfg) + "/jobs", json={"appliedFacets": {}, "limit": 1, "offset": 0,
                                          "searchText": ""}).json()
    exact, partial = None, {}

    def scan(facets):
        nonlocal exact
        for f in facets:
            for v in f.get("values", []):
                if "values" in v:          # nested group (locationMainGroup)
                    scan([v])
                    continue
                desc = str(v.get("descriptor", "")).strip().lower()
                if desc in ("israel", "isr") and exact is None:
                    exact = (f["facetParameter"], [v["id"]])
                elif desc.startswith(("israel", "il -", "il-")) or desc.endswith((",isr", ", isr", "israel")):
                    partial.setdefault(f["facetParameter"], []).append(v["id"])
    scan(d.get("facets", []))
    found = exact or (max(partial.items(), key=lambda kv: len(kv[1])) if partial else None)
    _facet_cache[key] = found
    return found


def list_jobs(cfg, terms=("student", "intern", "סטודנט")) -> list[Job]:
    facet = _israel_facet(cfg)
    applied = {facet[0]: facet[1]} if facet else {}
    jobs: dict[str, Job] = {}
    for term in terms:
        q = term if facet else f"{term} Israel"
        offset = 0
        while True:
            d = post(_base(cfg) + "/jobs", json={"appliedFacets": applied, "limit": 20,
                                                  "offset": offset, "searchText": q}).json()
            posts = d.get("jobPostings", [])
            for p in posts:
                path = p.get("externalPath", "")
                if not path:
                    continue
                if not facet and "israel" not in (p.get("locationsText", "") + path).lower():
                    continue
                req = (p.get("bulletFields") or [""])[0]
                url = f"https://{cfg['host']}/{cfg['site']}{path}"
                jobs[url] = Job(company=cfg["company"], title=p.get("title", ""), url=url,
                                source="workday", location=p.get("locationsText", ""),
                                req_id=req, posted=p.get("postedOn", ""),
                                apply_url=url + "/apply",
                                extra={"path": path})
            offset += 20
            if offset >= min(d.get("total", 0), 200) or not posts:
                break
    return list(jobs.values())


def fetch_details(job: Job, cfg) -> Job:
    d = get(_base(cfg) + job.extra["path"], headers={"Accept": "application/json"}).json()
    info = d.get("jobPostingInfo", {})
    job.description = html_to_text(info.get("jobDescription", ""))
    job.location = info.get("location", job.location)
    job.extra["can_apply"] = info.get("canApply", True)
    job.extra["questionnaire_id"] = info.get("questionnaireId")
    job.extra["time_type"] = info.get("timeType")
    return job


def is_open(job: Job, cfg) -> bool:
    try:
        d = get(_base(cfg) + job.extra["path"], headers={"Accept": "application/json"}).json()
        return bool(d.get("jobPostingInfo", {}).get("canApply", False))
    except Exception:
        return False
