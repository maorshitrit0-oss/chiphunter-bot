"""Merge two versions of data/applications.json (parallel workflow runs).

python -m chiphunter.merge OURS THEIRS OUT
Entries are matched by uid (or company+title). For the same entry, the most advanced status wins
(applied > cv_built, interview > applied, ...), and non-empty fields are kept.
"""
from __future__ import annotations

import json
import sys

RANK = {"skipped": 0, "cv_built": 1, "applied": 2, "interview": 3, "rejected": 4, "offer": 5}


def key(a):
    return a.get("uid") or f"{a.get('company', '').lower()}|{a.get('title', '').lower()}"


def merge(ours: dict, theirs: dict, base: dict | None = None) -> dict:
    """3-way merge per field: a field this run changed (ours != base) wins; otherwise keep what's on GitHub (theirs).
    Without a base, falls back to 'most advanced status wins'."""
    out = {k: v for k, v in theirs.items() if k != "applications"}
    by = {key(a): dict(a) for a in theirs["applications"]}
    base_by = {key(a): a for a in (base or {}).get("applications", [])}
    for a in ours["applications"]:
        k = key(a)
        if k not in by:
            by[k] = dict(a)
            continue
        b = by[k]
        if base is not None:
            old = base_by.get(k, {})
            merged = dict(b)
            for f, v in a.items():
                if old.get(f) != v:          # changed in this run -> take it
                    merged[f] = v
            by[k] = merged
            continue
        winner, loser = (a, b) if RANK.get(a.get("status"), 0) > RANK.get(b.get("status"), 0) else (b, a)
        by[k] = {**loser, **{f: v for f, v in winner.items() if v not in (None, "", [])}}
    apps = list(by.values())
    used, nxt = set(), max([a.get("id", 0) for a in apps] + [0]) + 1
    for a in apps:                      # keep ids unique
        if a.get("id") in used:
            a["id"], nxt = nxt, nxt + 1
        used.add(a["id"])
    out["applications"] = sorted(apps, key=lambda a: a["id"])
    return out


if __name__ == "__main__":
    o, t, dst = sys.argv[1:4]
    base = json.load(open(sys.argv[4], encoding="utf-8")) if len(sys.argv) > 4 else None
    m = merge(json.load(open(o, encoding="utf-8")), json.load(open(t, encoding="utf-8")), base)
    json.dump(m, open(dst, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
