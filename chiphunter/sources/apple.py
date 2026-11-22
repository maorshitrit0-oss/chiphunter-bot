"""jobs.apple.com — search results are embedded in the page as hydration JSON."""
from __future__ import annotations

import json
import re

from ..http import get, html_to_text
from ..models import Job

_RX = re.compile(r'__staticRouterHydrationData = JSON\.parse\((".*?")\);', re.S)


def _loader(url):
    h = get(url).text
    m = _RX.search(h)
    if not m:
        return {}
    return json.loads(json.loads(m.group(1))).get("loaderData", {})


def list_jobs(cfg, terms=("student", "intern")) -> list[Job]:
    jobs: dict[str, Job] = {}
    for term in terms:
        for page in range(1, 6):
            s = _loader(f"https://jobs.apple.com/en-il/search?location=israel-ISR&search={term}&page={page}") \
                .get("search", {})
            res = s.get("searchResults", []) or []
            for r in res:
                pid = r.get("positionId") or r.get("id", "")
                slug = r.get("transformedPostingTitle") or ""
                url = f"https://jobs.apple.com/en-il/details/{pid}/{slug}"
                locs = ", ".join(l.get("name", "") for l in r.get("locations", []) if isinstance(l, dict))
                jobs[url] = Job(company="Apple", title=r.get("postingTitle", ""), url=url, source="apple",
                                location=locs, req_id=str(pid), posted=r.get("postDateInGMT", "") or r.get("postingDate", ""),
                                description=html_to_text(r.get("jobSummary", "")), apply_url=url)
            if len(res) < 20:
                break
    return list(jobs.values())


def fetch_details(job: Job, cfg) -> Job:
    ld = _loader(job.url)
    d = ld.get("jobDetails", {}) or {}
    jd = d.get("jobsData") or d
    parts = []
    for k in ("jobSummary", "description", "responsibilities", "minimumQualifications",
              "preferredQualifications", "educationAndExperience", "additionalRequirements"):
        v = jd.get(k) if isinstance(jd, dict) else None
        if v:
            parts.append(f"{k}:\n{html_to_text(v)}")
    if parts:
        job.description = "\n\n".join(parts)
    return job
