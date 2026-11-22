"""Scan all configured sources, keep new student roles, evaluate them.

python -m chiphunter.scan               # scan + evaluate, print summary
python -m chiphunter.scan --json out.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .evaluate import evaluate, STUDENT
from .models import Job
from .sources import ADAPTERS

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / "data" / "state" / "seen.json"


def load_sources():
    cfg = json.loads((ROOT / "data" / "companies.json").read_text(encoding="utf-8"))
    return [s for s in cfg["sources"] if s["type"] in ADAPTERS and s.get("status") != "todo"]


def scan_source(src) -> tuple[dict, list[Job], str | None]:
    try:
        return src, ADAPTERS[src["type"]].list_jobs(src), None
    except Exception as e:
        return src, [], f"{type(e).__name__}: {str(e)[:150]}"


def student_title(job: Job) -> bool:
    return bool(STUDENT.search(job.title))


def run(only_new=True, details=True):
    seen = set(json.loads(STATE.read_text())) if STATE.exists() else set()
    results, health = [], {}
    srcs = load_sources()
    wd = [s for s in srcs if s["type"] == "workday"]
    other = [s for s in srcs if s["type"] != "workday"]

    def workday_one_by_one(_):   # Workday rate-limits (HTTP 429) when many tenants are hit at once
        out = []
        for s in wd:
            out.append(scan_source(s))
            time.sleep(1.5)
        return out
    with ThreadPoolExecutor(8) as ex:
        fut_wd = ex.submit(workday_one_by_one, None)
        scanned = list(ex.map(scan_source, other)) + fut_wd.result()
    candidates = []
    for src, jobs, err in scanned:
        health[src["company"]] = err or f"ok ({len(jobs)} jobs)"
        for j in jobs:
            if student_title(j) and (not only_new or j.uid not in seen):
                candidates.append((src, j))

    def detail(pair):
        src, j = pair
        if details and not j.description:
            try:
                ADAPTERS[src["type"]].fetch_details(j, src)
            except Exception as e:
                j.extra["detail_error"] = str(e)[:150]
        return src, j

    with ThreadPoolExecutor(8) as ex:
        for src, j in ex.map(detail, candidates):
            ev = evaluate(j, {**src, "company": j.company})
            results.append({"job": j.to_dict(), "eval": ev, "tier": src.get("tier", 3)})
            seen.add(j.uid)
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(sorted(seen)))
    order = {"go": 0, "maybe": 1, "skip": 2}
    results.sort(key=lambda r: (order[r["eval"]["verdict"]], -r["eval"]["score"]))
    return results, health


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="include already-seen jobs")
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    results, health = run(only_new=not a.all)
    print("== sources")
    for k, v in health.items():
        print(f"  {k:28s} {v}")
    for r in results:
        j, e = r["job"], r["eval"]
        print(f"[{e['verdict']:5s}] {e['score']:5.1f}  {j['company']} | {j['title']} | {e['location_he']}")
        for x in e["fail_reasons"]:
            print(f"          ✗ {x}")
        for x in e["warnings"]:
            print(f"          ! {x}")
        if e["domains"]:
            print(f"          תחום: {', '.join(e['domains'])} · שפה: {e['cv_lang']} · גליון: {'כן' if e['transcript'] else 'לא'}")
    if a.json:
        Path(a.json).write_text(json.dumps({"results": results, "health": health}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    sys.exit(main())
