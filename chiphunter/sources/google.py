"""Google Careers — results are embedded in the page (AF_initDataCallback ds:1)."""
from __future__ import annotations

import json
import re
from urllib.parse import quote

from ..http import get, html_to_text
from ..models import Job

_RX = re.compile(r"AF_initDataCallback\(\{key: 'ds:1'.*?data:(.*?), sideChannel", re.S)
BASE = "https://www.google.com/about/careers/applications/jobs/results"


def _slug(t):
    return re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")


def _txt(x):
    return html_to_text(x[1]) if isinstance(x, list) and len(x) > 1 and x[1] else ""


def list_jobs(cfg, terms=("student", "intern")) -> list[Job]:
    jobs: dict[str, Job] = {}
    for term in terms:
        for page in range(1, 4):
            h = get(f"{BASE}?location=Israel&q={quote(term)}&page={page}").text
            m = _RX.search(h)
            if not m:
                break
            rows = (json.loads(m.group(1)) or [[]])[0] or []
            for r in rows:
                jid, title = str(r[0]), r[1]
                locs = "; ".join(l[0] for l in (r[9] or []) if l)
                if "israel" not in locs.lower():
                    continue
                url = f"{BASE}/{jid}-{_slug(title)}"
                desc = "\n\n".join(filter(None, [
                    _txt(r[10]), "Responsibilities:\n" + _txt(r[3]), "Qualifications:\n" + _txt(r[4]), _txt(r[15])]))
                jobs[url] = Job(company="Google", title=title, url=url, source="google", location=locs,
                                req_id=jid, description=desc, apply_url=r[2] or url)
            if len(rows) < 20:
                break
    return list(jobs.values())


def fetch_details(job, cfg):
    return job
