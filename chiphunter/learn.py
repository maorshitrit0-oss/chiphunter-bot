"""Learning from what Maor does — so the bot gets better at what to send and how to get him hired.

1. Taste (no AI, recomputed every run): every job he got is put in role categories (systems/architecture, EMC,
   validation, production line...). Per category we count: applied (+), skipped (−), ignored for days (weak −).
   A category he keeps skipping and never applies to is muted — after a few times, not after one.
   He can always override in chat ("תחזיר לי משרות מהנדס מערכת" / "אל תשלח לי יותר EMC").

2. Insights (AI, at most once a day, only when outcomes changed): applications with no answer after 14+ days, rejections,
   interviews — what's the pattern, why aren't they calling back, what to change. Sent to him as a short message, given
   to the job analysis (so new CVs / advice use it) and to the weekly summary.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASTE = ROOT / "data" / "taste.json"
INSIGHTS = ROOT / "data" / "insights.json"
PROFILE = ROOT / "profile" / "profile.json"

IGNORED_AFTER_DAYS = 3        # sent, reminded, never answered -> counts as a weak "not interested"
MUTE_AT = 3.0                 # skips + 0.5*ignored, with no application in that category
LEAN_AT = 1.5                 # the AI is told it's probably less relevant (lower chance, say so)
NO_ANSWER_DAYS = 14
INSIGHTS_MIN = 3              # need at least this many no-answers / rejections to talk about a pattern

# (key, Hebrew label, title regex). A job can be in several categories.
CATEGORIES = [
    ("systems", "הנדסת מערכת / ארכיטקטורה", r"systems? (engineer|engineering|architect)|architect|הנדסת מערכת|מהנדס.? מערכת|ארכיטקט"),
    ("emc", "EMC / תאימות אלקטרומגנטית", r"\bemc\b|\bemi\b|electromagnetic|תאימות"),
    ("validation", "ולידציה", r"validation|ולידציה|post.?silicon|bring.?up"),
    ("verification", "ורפיקציה", r"verification|\bdv\b|אימות"),
    ("design", "דיזיין שבבים / RTL", r"\basic\b|digital (ic )?design|chip design|\brtl\b|logic design|front.?end|דיזיין|תכנון שבב"),
    ("physical", "Physical design / Backend", r"physical design|back.?end|layout|\bpd\b|place.?and.?route"),
    ("analog", "אנלוג / Mixed-signal", r"analog|mixed.?signal|אנלוג"),
    ("rf", "RF / תקשורת", r"\brf\b|radio|antenna|microwave|אנטנ|רדיו"),
    ("power_si", "Power / Signal integrity", r"power|signal integrity|\bsi\b|\bpi\b|הספק"),
    ("fpga", "FPGA", r"fpga"),
    ("board", "חומרה / כרטיסים", r"\bboard\b|\bpcb\b|hardware engineer|hardware engineering|כרטיס|מהנדס.? חומרה"),
    ("test", "בדיקות / Test", r"\btest|testing|בדיקות|בודק"),
    ("lab_tech", "טכנאי / מעבדה", r"technician|lab tech|טכנאי|לבורנט|מעבדה"),
    ("production", "ייצור / קו ייצור", r"production|manufacturing|ייצור|assembly|הרכבה"),
    ("control", "בקרה / סרוו", r"control|servo|בקרה|סרוו"),
    ("avionics", "אוויוניקה", r"avionic|אוויוניקה"),
    ("calibration", "כיול ומדידות", r"calibrat|כיול|מדידות|metrology"),
    ("algorithms", "אלגוריתמים", r"algorithm|אלגוריתם"),
    ("mechanical", "מכונות", r"mechanical|מכונות|מכני"),
    ("npi_ops", "NPI / תפעול הנדסי", r"\bnpi\b|new product introduction|operations|logistic|תפעול|לוגיסטיקה|supply chain"),
    ("integration", "אינטגרציה", r"integration|אינטגרציה"),
    ("project", "ניהול פרויקטים טכני", r"project manag|program manag|ניהול פרויקט|\btpm\b"),
]
# what to search for when Maor clearly likes a kind of role (he applied to it, especially to jobs he found himself)
SEARCH_TERMS = {"npi_ops": ["NPI student", "hardware operations student"], "integration": ["integration student"],
                "validation": ["validation student"], "verification": ["verification student"],
                "design": ["chip design student"], "physical": ["physical design student"], "rf": ["RF student"],
                "test": ["test engineering student"], "board": ["hardware student"], "lab_tech": ["lab student"],
                "project": ["technical project management student"], "power_si": ["power electronics student"],
                "fpga": ["FPGA student"], "analog": ["analog student"], "systems": ["systems engineering student"]}
_CAT_RX = [(k, he, re.compile(rx, re.I)) for k, he, rx in CATEGORIES]
CAT_HE = {k: he for k, he, _ in CATEGORIES}


def categories(title: str) -> list[str]:
    return [k for k, _, rx in _CAT_RX if rx.search(title or "")]


def _overrides() -> dict:
    try:
        return json.loads(PROFILE.read_text(encoding="utf-8")).get("taste_overrides", {})
    except Exception:
        return {}


def set_override(cat: str, mode: str) -> str | None:
    """mode: 'always' (keep sending), 'never' (stop), 'auto' (learn again)."""
    cat = next((k for k, he, _ in CATEGORIES if cat in (k, he) or cat.lower() in he.lower()), None)
    if not cat:
        return None
    prof = json.loads(PROFILE.read_text(encoding="utf-8"))
    ov = prof.setdefault("taste_overrides", {})
    if mode == "auto":
        ov.pop(cat, None)
    else:
        ov[cat] = mode
    PROFILE.write_text(json.dumps(prof, ensure_ascii=False, indent=1), encoding="utf-8")
    compute_taste()
    return cat


def compute_taste(apps: list | None = None) -> dict:
    if apps is None:
        apps = json.loads((ROOT / "data" / "applications.json").read_text(encoding="utf-8"))["applications"]
    today = date.today()
    counts: dict[str, dict] = {}
    for a in apps:
        st = a.get("status")
        if st in ("applied", "interview", "offer", "rejected"):
            sig = "applied"
            weight = 2 if a.get("source") in ("manual", "screenshot") else 1   # a job he found himself = strong signal
        elif st == "skipped" and a.get("skipped_by") == "maor":
            sig = "skipped"
        elif st in ("cv_built", "held") and a.get("found_date") and a.get("verdict") in ("go", "maybe") and \
                (today - date.fromisoformat(a["found_date"])).days >= IGNORED_AFTER_DAYS:
            sig = "ignored"
        else:
            continue
        if sig != "applied":
            weight = 1
        for c in categories(a.get("title", "")):
            d = counts.setdefault(c, {"applied": 0, "skipped": 0, "ignored": 0, "examples": []})
            d[sig] += weight
            if sig != "applied" and len(d["examples"]) < 4:
                d["examples"].append(f"{a.get('company')} · {a.get('title')}")
    ov = _overrides()
    out = {}
    for c, d in counts.items():
        neg = d["skipped"] + 0.5 * d["ignored"]
        state = "muted" if neg >= MUTE_AT and d["applied"] == 0 else \
                "lean_no" if neg >= LEAN_AT and d["applied"] == 0 else \
                "likes" if d["applied"] >= 2 and d["applied"] >= neg else "neutral"
        if ov.get(c) == "never":
            state = "muted"
        elif ov.get(c) == "always":
            state = "likes"
        out[c] = {"label_he": CAT_HE[c], **d, "state": state, "override": ov.get(c)}
    for c, mode in ov.items():
        if c not in out and c in CAT_HE:
            out[c] = {"label_he": CAT_HE[c], "applied": 0, "skipped": 0, "ignored": 0, "examples": [],
                      "state": "muted" if mode == "never" else "likes", "override": mode}
    prev = json.loads(TASTE.read_text(encoding="utf-8")) if TASTE.exists() else {}
    TASTE.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    newly = [c for c, v in out.items() if v["state"] == "muted" and (prev.get(c) or {}).get("state") != "muted"
             and not v.get("override")]
    if newly:
        _tell_muted(newly, out)
    return out


def _tell_muted(cats, taste):
    from . import telegram
    from .pipeline import e
    if not telegram.enabled():
        return
    lines = ["🧠 <b>למדתי משהו עליך</b>"]
    for c in cats:
        v = taste[c]
        n = v["skipped"] + v["ignored"]
        lines.append(f"• משרות <b>{e(v['label_he'])}</b> — דילגת / לא ענית על {n} כאלה ולא הגשת לאף אחת, "
                     f"אז אפסיק לשלוח אותן.")
    lines.append("\nאם טעיתי, כתוב לי למשל: \"תחזיר לי משרות " + e(taste[cats[0]]["label_he"].split(" /")[0]) + "\".")
    telegram.send("\n".join(lines))


def taste() -> dict:
    return json.loads(TASTE.read_text(encoding="utf-8")) if TASTE.exists() else compute_taste()


def muted_reason(title: str) -> str | None:
    """A job is held back only if ALL its specific categories are muted and none is liked."""
    cats = categories(title)
    if not cats:
        return None
    t = taste()
    states = [(t.get(c) or {}).get("state", "neutral") for c in cats]
    if all(s == "muted" for s in states):
        return " / ".join(CAT_HE[c] for c in cats)
    return None


def liked(title: str) -> str | None:
    """A category of this job that Maor applies to (so a field-only rule must not hide it)."""
    t = taste()
    good = [c for c in categories(title) if (t.get(c) or {}).get("state") == "likes" or (t.get(c) or {}).get("applied", 0) >= 2]
    return " / ".join(CAT_HE[c] for c in good) or None


def learned_search_terms() -> list[str]:
    t = taste()
    out = []
    for c, v in t.items():
        if v.get("state") == "likes" or v.get("applied", 0) >= 2:
            out += SEARCH_TERMS.get(c, [])
    return list(dict.fromkeys(out))


def taste_for_ai(title: str) -> dict:
    t = taste()
    return {"this_job_categories": {c: t.get(c, {"label_he": CAT_HE[c], "state": "neutral"}) for c in categories(title)},
            "all_categories": {c: {k: v[k] for k in ("label_he", "applied", "skipped", "ignored", "state")} for c, v in t.items()}}


# ---------------------------------------------------------------- why aren't they calling back?
def outcomes(apps: list) -> dict:
    today = date.today()
    no_answer, rejected, positive = [], [], []
    for a in apps:
        if a.get("status") == "applied" and a.get("applied_date") and \
                (today - date.fromisoformat(a["applied_date"])).days >= NO_ANSWER_DAYS:
            no_answer.append(a)
        elif a.get("status") == "rejected":
            rejected.append(a)
        elif a.get("status") in ("interview", "offer"):
            positive.append(a)
    return {"no_answer": no_answer, "rejected": rejected, "positive": positive}


def insights(force=False, send=False, min_cases=INSIGHTS_MIN) -> dict | None:
    """Re-analyze the pattern when outcomes changed (at most once a day). Maor gets it in the Saturday summary
    (send=True only when he asks for it in chat)."""
    from . import llm, telegram
    from .pipeline import e, load_apps
    apps = load_apps()["applications"]
    o = outcomes(apps)
    bad = o["no_answer"] + o["rejected"]
    if len(bad) < min_cases or not bad and not o["positive"]:
        return None
    key = hashlib.sha1(json.dumps(sorted((a["id"], a["status"]) for a in bad + o["positive"])).encode()).hexdigest()[:12]
    prev = json.loads(INSIGHTS.read_text(encoding="utf-8")) if INSIGHTS.exists() else {}
    if not force and (prev.get("key") == key or prev.get("updated") == date.today().isoformat()):
        return prev
    slim = lambda a: {k: a.get(k) for k in ("company", "title", "status", "applied_date", "found_date", "channel",
                                            "referrer", "verdict", "cv_profile", "cv_selection", "domains", "location",
                                            "source", "notes", "transcript")}
    applied_all = [a for a in apps if a.get("status") in ("applied", "interview", "offer", "rejected")]
    files = {"today.txt": date.today().isoformat(),
             "outcomes.json": json.dumps({"no_answer_14d": [slim(a) for a in o["no_answer"]],
                                          "rejected": [slim(a) for a in o["rejected"]],
                                          "interview_or_offer": [slim(a) for a in o["positive"]],
                                          "all_applications": [slim(a) for a in applied_all],
                                          "previous_insights": prev.get("insights")}, ensure_ascii=False, indent=1),
             "profile.json": PROFILE.read_text(encoding="utf-8"),
             "cv_content.json": (ROOT / "profile" / "cv_content.json").read_text(encoding="utf-8")}
    prompt = (ROOT / "prompts" / "insights.md").read_text(encoding="utf-8")
    out = llm.run(prompt, files, "insights.json", timeout=300)
    if not out:
        return prev or None
    rec = {"key": key, "updated": date.today().isoformat(), "based_on": len(bad), "positives": len(o["positive"]),
           "insights": out}
    INSIGHTS.write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
    msg = out.get("message_he")
    if send and msg and telegram.enabled():
        try:
            telegram.send(msg)
        except Exception:
            telegram.send(e(msg))
    return rec


def insights_for_ai() -> dict | None:
    if not INSIGHTS.exists():
        return None
    ins = json.loads(INSIGHTS.read_text(encoding="utf-8")).get("insights") or {}
    return {k: ins.get(k) for k in ("reasons_he", "suggestions_he", "cv_focus_en", "apply_tips_he", "confidence")}
