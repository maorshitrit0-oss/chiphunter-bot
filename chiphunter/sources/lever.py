"""Lever public postings API. cfg: {"company": "...", "token": "leversite", "region": "eu" (optional)}"""
from __future__ import annotations

from ..http import get, html_to_text
from ..models import Job
from .greenhouse import is_israel


def list_jobs(cfg, terms=None) -> list[Job]:
    host = "api.eu.lever.co" if cfg.get("region") == "eu" else "api.lever.co"     # e.g. Mobileye is on Lever EU
    d = get(f"https://{host}/v0/postings/{cfg['token']}", params={"mode": "json"}).json()
    out = []
    for p in d:
        cat = p.get("categories", {}) or {}
        loc = cat.get("location", "") or ", ".join(cat.get("allLocations", []) or [])
        if not (is_israel(loc) or (p.get("country") or "").upper() == "IL"):
            continue
        desc = html_to_text(p.get("description", ""))
        for l in p.get("lists", []):
            desc += f"\n{l.get('text', '')}:\n{html_to_text(l.get('content', ''))}"
        out.append(Job(company=cfg["company"], title=p.get("text", ""), url=p.get("hostedUrl", ""),
                       source="lever", location=loc, req_id=p.get("id", ""),
                       description=desc, apply_url=p.get("applyUrl", "")))
    return out


def fetch_details(job, cfg):
    return job
