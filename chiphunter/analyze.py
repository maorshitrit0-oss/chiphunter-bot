"""AI steps (job analysis, form answers, interview prep, chat). All go through llm.run:
Gemini free API first (24/7, independent of the Claude subscription), Claude as fallback, rules if both fail."""
from __future__ import annotations

import json
from pathlib import Path

from . import llm
from .evaluate import DOMAINS
from .models import Job

ROOT = Path(__file__).resolve().parent.parent
PROMPT = (ROOT / "prompts" / "analyze_job.md").read_text(encoding="utf-8")
FORM_PROMPT = (ROOT / "prompts" / "form_answers.md").read_text(encoding="utf-8")
PREP_PROMPT = (ROOT / "prompts" / "interview_prep.md").read_text(encoding="utf-8")
CHAT_PROMPT = (ROOT / "prompts" / "chat.md").read_text(encoding="utf-8")
DOMAIN_KEYS = {label: key for key, label, *_ in DOMAINS}


def _file(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _base_files(*rels) -> dict:
    return {Path(r).name: _file(r) for r in rels}


def _j(x) -> str:
    return json.dumps(x, ensure_ascii=False, indent=1)


def claude_available() -> bool:   # kept for callers: "is any AI configured"
    return llm.available()


def _alert_if_down():
    err = llm.last_error()
    if not err:
        return
    from .pipeline import alert_once
    if err == "gemini_auth":
        alert_once("gemini_auth", "🔐 <b>מפתח Gemini לא תקין או בוטל</b> — המערכת עובדת לפי חוקים בלבד.\n"
                   "לתיקון: מפתח חדש ב-https://aistudio.google.com/apikey → GitHub secrets → GEMINI_API_KEY.")
    elif "claude" in err and any(k in err.lower() for k in ("expired", "login", "401", "unauthorized")):
        alert_once("claude_auth", "🔐 <b>הטוקן של Claude (גיבוי) פג</b>. לחידוש: <code>claude setup-token</code> → GitHub secrets.")


def fallback(job: Job, rule: dict, why: str = "") -> dict:
    out = {
        "job_text": "", "company": job.company, "title": job.title,
        "verdict": rule["verdict"], "verdict_he": rule["verdict_he"], "chance_he": rule["chance_he"],
        "summary_he": "", "why_he": "; ".join(rule["fail_reasons"] or rule["warnings"]) or "עומד בתנאי החובה שזוהו",
        "fail_reasons": rule["fail_reasons"], "warnings": list(rule["warnings"]),
        "cv_lang": rule["cv_lang"], "transcript": rule["transcript"],
        "domains": [DOMAIN_KEYS.get(d, d) for d in rule["domains"]],
        "cv_selection": {}, "cv_tips_he": [], "form_answers": [],
        "why_us_en": "", "why_us_he": "", "location_text": job.location, "source": "rules",
    }
    if why:
        out["warnings"].append(why)
    return out


def _learned(job) -> dict:
    """What the bot learned: Maor's taste per role category + why applications go unanswered."""
    try:
        from .learn import taste_for_ai, insights_for_ai
        out = {"taste.json": _j(taste_for_ai(job.title))}
        ins = insights_for_ai()
        if ins:
            out["insights.json"] = _j(ins)
        return out
    except Exception:
        return {}


def _maor_skips():
    try:
        from .pipeline import maor_skips
        return maor_skips()
    except Exception:
        return []


def analyze(job: Job, rule: dict, image: Path | None = None, timeout=300) -> dict:
    files = {"job.json": _j(job.to_dict()), "rule_eval.json": _j(rule),
             **({"previous_attempt.json": _j(job.extra["previous_attempt"])} if job.extra.get("previous_attempt") else {}),
             **({"maor_skips.json": _j(skips)} if (skips := _maor_skips()) else {}),
             **_learned(job),
             **_base_files("profile/profile.json", "profile/cv_content.json", "data/questionnaire.json")}
    prompt = PROMPT.replace("job_image.*", "the attached image") if image else PROMPT
    out = llm.run(prompt, files, "analysis.json", image=image, timeout=timeout)
    if not out:
        _alert_if_down()
        return fallback(job, rule, "ה-AI לא זמין כרגע — הערכה לפי חוקים בלבד")
    out.setdefault("warnings", [])
    out["source"] = out.pop("_provider", "ai")
    return out


def answer_form(image: Path, timeout=240) -> dict | None:
    """Classify a screenshot: {"type": "form", answers...} | {"type": "job"} | {"type": "other", "reply_he"}."""
    files = _base_files("profile/profile.json", "profile/cv_content.json", "data/questionnaire.json")
    out = llm.run(FORM_PROMPT.replace("screenshot.*", "the attached image"), files, "result.json", image=image,
                  timeout=timeout)
    if not out:
        _alert_if_down()
    return out


def interview_prep(application: dict, job: Job | None, timeout=900) -> dict | None:
    files = {"application.json": _j(application), "job.json": _j(job.to_dict() if job else {}),
             **_base_files("profile/profile.json", "profile/cv_content.json")}
    out = llm.run(PREP_PROMPT, files, "prep.json", search=True, timeout=timeout)
    if not out:
        _alert_if_down()
    return out


def chat(message: str, history: list, applications: list, last_job: dict | None = None, images=None,
         timeout=300) -> dict | None:
    slim = [{k: a.get(k) for k in ("id", "company", "title", "status", "applied_date", "channel", "referrer",
                                    "verdict", "notes", "found_date", "url", "source")} for a in applications
            if a.get("status") != "skipped" or a.get("skipped_by") == "maor" or a.get("source") in ("manual", "screenshot")]
    try:
        from .learn import taste
        tst = {c: {k: v[k] for k in ("label_he", "applied", "skipped", "ignored", "state")} for c, v in taste().items()}
    except Exception:
        tst = {}
    files = {"message.txt": message, "history.json": _j(history[-20:]), "applications.json": _j(slim),
             "last_job.json": _j(last_job or {}), "taste.json": _j(tst),
             **_base_files("profile/profile.json", "data/questionnaire.json")}
    out = llm.run(CHAT_PROMPT, files, "reply.json", image=images or None, search=False, timeout=timeout)
    if not out:
        _alert_if_down()
    return out


WEEKLY_PROMPT = (ROOT / "prompts" / "weekly.md").read_text(encoding="utf-8")


def weekly(events: list, applications: list, today: str) -> dict | None:
    slim = [{k: a.get(k) for k in ("id", "company", "title", "status", "applied_date", "found_date", "channel", "referrer",
                                    "verdict", "notes", "cv_profile", "attempts", "reapply_of")}
            for a in applications if a.get("status") not in ("skipped",)]
    try:
        from .learn import taste, insights_for_ai
        extra = {"taste.json": _j(taste()), "insights.json": _j(insights_for_ai() or {})}
    except Exception:
        extra = {}
    files = {"today.txt": today, "events.json": _j(events), "applications.json": _j(slim), **extra,
             **_base_files("profile/profile.json", "profile/cv_content.json")}
    out = llm.run(WEEKLY_PROMPT, files, "summary.json", timeout=300)
    if not out:
        _alert_if_down()
    return out
