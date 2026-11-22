"""Comeet (Israeli ATS). Positions are embedded in the public careers page.

cfg: {"company": "...", "slug": "company-slug", "uid": "XX.XXX"}
Page: https://www.comeet.com/jobs/{slug}/{uid}
"""
from __future__ import annotations

import json
import re

from ..http import get, html_to_text
from ..models import Job
from .greenhouse import is_israel

_RX = re.compile(r"COMPANY_POSITIONS_DATA\s*=\s*(\[.*?\]);\s*\n", re.S)


def list_jobs(cfg, terms=None) -> list[Job]:
    h = get(f"https://www.comeet.com/jobs/{cfg['slug']}/{cfg['uid']}").text
    m = _RX.search(h)
    if not m:
        return []
    out = []
    for p in json.loads(m.group(1)):
        loc = p.get("location") or {}
        country = (loc.get("country") or "").upper()
        where = " ".join(str(loc.get(k) or "") for k in ("name", "city", "country"))
        if not cfg.get("all_countries") and country not in ("IL", "ISRAEL") and not is_israel(where):
            continue
        desc = "\n".join(f"{d.get('name', '')}:\n{html_to_text(d.get('value', ''))}"
                         for d in (p.get("custom_fields") or {}).get("details", []) or [])
        url = p.get("url_comeet_hosted_page") or p.get("url_active_page") or ""
        out.append(Job(company=cfg["company"], title=p.get("name", ""), url=url, source="comeet",
                       location=", ".join(filter(None, [loc.get("city"), loc.get("country")])),
                       req_id=p.get("uid", ""), posted=p.get("time_updated", ""), description=desc,
                       apply_url=url))
    return out


def fetch_details(job, cfg):
    return job
