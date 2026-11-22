"""amazon.jobs public search JSON (includes Annapurna Labs)."""
from __future__ import annotations

from ..http import get, html_to_text
from ..models import Job

URL = "https://www.amazon.jobs/en/search.json"


def list_jobs(cfg, terms=("student", "intern")) -> list[Job]:
    jobs: dict[str, Job] = {}
    for term in terms:
        offset = 0
        while True:
            d = get(URL, params={"base_query": term, "country": "ISR", "result_limit": 100,
                                 "offset": offset, "sort": "recent"}).json()
            for p in d.get("jobs", []):
                if (p.get("country_code") or "").upper() not in ("ISR", "IL"):
                    continue
                url = "https://www.amazon.jobs" + p.get("job_path", "")
                desc = "\n".join(filter(None, [
                    html_to_text(p.get("description", "")),
                    "Basic qualifications:\n" + html_to_text(p.get("basic_qualifications", "")),
                    "Preferred qualifications:\n" + html_to_text(p.get("preferred_qualifications", "")),
                ]))
                company = "Amazon Annapurna Labs" if p.get("business_category") == "annapurna" else "Amazon"
                jobs[url] = Job(company=company, title=p.get("title", ""), url=url, source="amazon",
                                location=p.get("normalized_location") or p.get("city", ""),
                                req_id=str(p.get("id_icims") or p.get("id", "")),
                                posted=p.get("posted_date", ""), description=desc,
                                apply_url=p.get("url_next_step", "") or url)
            offset += 100
            if offset >= min(d.get("hits", 0), 300):
                break
    return list(jobs.values())


def fetch_details(job: Job, cfg) -> Job:
    return job  # search result already contains full text
