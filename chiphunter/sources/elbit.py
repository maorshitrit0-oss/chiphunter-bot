"""Elbit Systems Israel — the career site publishes all open jobs as one JSON file."""
from __future__ import annotations

from ..http import get, html_to_text
from ..models import Job

URL = "https://elbitsystemscareer.com/cron/jobs.json"
AREA_HE = {"North": "צפון", "Center": "מרכז", "South": "דרום", "Jerusalem": "ירושלים"}


def list_jobs(cfg, terms=None) -> list[Job]:
    out = []
    for j in get(URL).json():
        desc = "\n\n".join(filter(None, [html_to_text(html_to_text(j.get("description", ""))),
                                         "דרישות:\n" + html_to_text(html_to_text(j.get("requirements", ""))),
                                         html_to_text(j.get("skills", ""))]))
        props = {p.get("PropertyName"): p.get("Value") for p in j.get("extendedProperties") or []}
        area = j.get("area") or props.get("Area", "")
        code = j.get("jobCode", "")
        out.append(Job(company="Elbit Systems", title=j.get("jobTitle", ""),
                       # Exact deep-link format not confirmed yet; the briefing also shows the job code.
                       url=f"https://elbitsystemscareer.com/?jobCode={code}", source="elbit",
                       location=AREA_HE.get(area, area), req_id=str(code), posted=j.get("openDate", ""),
                       description=desc, apply_url="",
                       extra={"job_code": code, "job_id": j.get("jobId"), "city_code": props.get("Cities")}))
    return out


def fetch_details(job, cfg):
    return job
