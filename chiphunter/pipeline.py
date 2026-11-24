"""End-to-end: job -> hard requirements -> Claude analysis -> CV -> Drive -> Telegram briefing -> tracker.

CLI:
  python -m chiphunter.pipeline scan                 # scan all sources, process new relevant jobs
  python -m chiphunter.pipeline url <URL>            # job sent to the bot as a link
  python -m chiphunter.pipeline text <FILE>          # job sent as pasted text
  python -m chiphunter.pipeline image <TELEGRAM_FILE_ID|PATH>
  python -m chiphunter.pipeline jobs <JSON_FILE>     # jobs found by the Cloudflare watcher
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

from . import drive, telegram
from .analyze import analyze
from .cv.build import build_cv, CVSafetyError
from .evaluate import evaluate
from .http import get, html_to_text
from .location import classify, describe_he
from .models import Job
from .sources import ADAPTERS

ROOT = Path(__file__).resolve().parent.parent
APPS = ROOT / "data" / "applications.json"
QA = json.loads((ROOT / "data" / "questionnaire.json").read_text(encoding="utf-8"))
REFS = ROOT / "data" / "private_refs.json"   # Telegram file_ids only — the PDFs themselves are never in git


def transcript_pdf(lang: str) -> Path | None:
    """Download the transcript (sent once to the bot) into a temp file. Never modified."""
    local = ROOT / "private" / f"transcript_{lang}.pdf"
    if local.exists():
        return local
    refs = json.loads(REFS.read_text()) if REFS.exists() else {}
    fid = refs.get(f"transcript_{lang}")
    dest = Path(tempfile.gettempdir()) / f"transcript_{lang}.pdf"
    if fid and telegram.enabled():
        return telegram.download_file(fid, dest)
    if drive.enabled():   # saved by the library import (CV_Library/_system)
        return drive.download(["_system"], f"transcript_{lang}.pdf", dest)
    return None


e = telegram.e


# ---------------------------------------------------------------- inputs
def _source_cfg(company: str, source: str = "") -> dict:
    cfg = json.loads((ROOT / "data" / "companies.json").read_text(encoding="utf-8"))
    for s in cfg["sources"]:
        if s["company"].lower() == (company or "").lower():
            return s
    if source:
        for s in cfg["sources"]:
            if s["type"] == source:
                return {**s, "company": company}
    return {}


def job_from_url(url: str) -> tuple[Job | None, str | None]:
    """Try known ATS formats first, then a generic page fetch. Returns (job, error)."""
    m = re.match(r"https://([\w.-]+\.myworkdayjobs\.com)/(?:[a-z]{2}-[A-Z]{2}/)?([^/]+)(/job/.+?)(?:/apply.*)?$", url)
    if m:
        host, site, path = m.groups()
        cfg = {"company": host.split(".")[0].upper(), "host": host, "site": site}
        known = [s for s in json.loads((ROOT / "data" / "companies.json").read_text())["sources"] if s.get("host") == host]
        if known:
            cfg["company"] = known[0]["company"]
        j = Job(company=cfg["company"], title="", url=url.split("/apply")[0], source="workday",
                extra={"path": path}, apply_url=url.split("/apply")[0] + "/apply")
        try:
            ADAPTERS["workday"].fetch_details(j, cfg)
            d = get(ADAPTERS["workday"]._base(cfg) + path, headers={"Accept": "application/json"}).json()
            j.title = d.get("jobPostingInfo", {}).get("title", "")
            return j, None
        except Exception as ex:
            return None, f"Workday: {ex}"
    if "linkedin.com" in url or "lnkd.in" in url:
        try:
            from .sources import linkedin
            lj = linkedin.job_from_link(url)
            if lj and lj.title:
                return lj, None
        except Exception as ex:
            print(f"[url] linkedin link failed: {ex}", file=sys.stderr)
    m = re.search(r"comeet\.(?:com|co)/jobs/([^/]+)/([0-9A-F]{2}\.[0-9A-F]{3})(?:/[^/]*/([0-9A-F]{2}\.[0-9A-F]{2,3}))?", url, re.I)
    if m:     # Comeet pages are built by JavaScript -> read the company's positions list and pick this one
        slug, cuid, puid = m.groups()
        try:
            name = next((s["company"] for s in json.loads((ROOT / "data" / "companies.json").read_text())["sources"]
                         if s.get("slug", "").lower() == slug.lower()), slug.replace("-", " ").title())
            jobs = ADAPTERS["comeet"].list_jobs({"company": name, "slug": slug, "uid": cuid, "all_countries": True})
            hit = next((j for j in jobs if puid and j.req_id.upper() == puid.upper()), None)
            if hit:
                hit.source = "manual"
                return hit, None
        except Exception as ex:
            print(f"[url] comeet lookup failed: {ex}", file=sys.stderr)
    known = _match_known_source(url)
    if known:
        return known, None
    try:
        h = get(url, timeout=20).text
    except Exception as ex:
        return None, f"הקישור לא נפתח ({type(ex).__name__})"
    t = re.search(r"<title[^>]*>(.*?)</title>", h, re.S | re.I)
    text = html_to_text(re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", "", h))
    if len(text) < 300:
        return None, "הדף נטען בלי טקסט משרה (כנראה חסום או נטען ב-JavaScript)"
    host = re.sub(r"^www\.", "", url.split("/")[2])
    labels = [x for x in host.split(".") if x not in ("jobs", "careers", "career", "apply", "boards", "join", "il", "co",
                                                       "com", "io", "net", "org", "app", "hire", "recruit")]
    return Job(company=(labels[-1] if labels else host.split(".")[0]).capitalize(), title=html_to_text(t.group(1)) if t else "", url=url,
               source="manual", description=text[:15000], apply_url=url), None


HOST_TYPES = {"linkedin.com": "linkedin", "amazon.jobs": "amazon", "jobs.apple.com": "apple", "google.com": "google",
              "careers.qualcomm.com": "eightfold", "elbitsystemscareer.com": "elbit", "greenhouse.io": "greenhouse",
              "lever.co": "lever"}


def _match_known_source(url: str) -> Job | None:
    """For sites we already scan, find the job in the source listing (full data, correct company)."""
    host = url.split("/")[2].lower()
    typ = next((t for h, t in HOST_TYPES.items() if host.endswith(h)), None)
    if not typ:
        return None
    ids = set(re.findall(r"\d{5,}", url))
    cfgs = [c for c in json.loads((ROOT / "data" / "companies.json").read_text(encoding="utf-8"))["sources"]
            if c["type"] == typ]
    for cfg in cfgs:
        try:
            for j in ADAPTERS[typ].list_jobs(cfg):
                if j.url.rstrip("/") == url.rstrip("/") or (ids and str(j.req_id) in ids) or (ids & set(re.findall(r"\d{5,}", j.url))):
                    if not j.description:
                        ADAPTERS[typ].fetch_details(j, cfg)
                    return j
        except Exception as ex:
            print(f"[url] {typ} lookup failed: {ex}", file=sys.stderr)
    return None


# ---------------------------------------------------------------- tracker
def load_apps():
    return json.loads(APPS.read_text(encoding="utf-8"))


def save_apps(d):
    APPS.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def already_known(company, title):
    norm = lambda s: re.sub(r"[^a-z0-9א-ת]+", " ", (s or "").lower()).strip()
    for a in load_apps()["applications"]:
        if same_company(a.get("company"), company) and (norm(a["title"]) in norm(title) or norm(title) in norm(a["title"])):
            return a
    return None


def record(job: Job, an: dict, cv_path: str | None, status: str | None = None):
    from .cv.build import choose_profile
    d = load_apps()
    apps = d["applications"]
    prof = None
    if cv_path:
        try:
            prof = choose_profile(an.get("cv_lang", "en"), an.get("domains", []), (an.get("cv_selection") or {}).get("profile"))["id"]
        except Exception:
            prof = None
    cv_info = {"cv_profile": prof, "cv_selection": an.get("cv_selection") or {}} if cv_path else {}
    for a in apps:
        if a.get("uid") == job.uid:
            a.update({"verdict": an["verdict"], "cv_file": cv_path or a.get("cv_file"), **cv_info})
            break
    else:
        prev = job.extra.get("previous_attempt") or {}
        if prev.get("id"):     # re-apply: keep history on the old record, mark it
            for a in apps:
                if a.get("id") == prev["id"]:
                    a.setdefault("attempts", []).append({"applied_date": a.get("applied_date"), "cv_profile": a.get("cv_profile")})
        apps.append({"id": max([a["id"] for a in apps] + [0]) + 1, "uid": job.uid, "company": job.company,
                     "title": job.title, "url": job.url, "req_id": job.req_id,
                     "status": status or ("cv_built" if cv_path else "skipped"),
                     "verdict": an["verdict"], "transcript": "כן" if an.get("transcript") else "לא",
                     "notes": an.get("why_he", ""), "found_date": date.today().isoformat(),
                     "applied_date": None, "cv_file": cv_path, "reapply_of": prev.get("id"),
                     "domains": an.get("domains", []), "location": job.location, "source": job.source,
                     "summary": (an.get("summary_he") or "")[:200], **cv_info})
    save_apps(d)


# ---------------------------------------------------------------- the message Maor gets
CAREER_SITES = {"workday", "amazon", "apple", "google", "eightfold", "elbit", "greenhouse", "lever",
                "smartrecruiters", "comeet", "ashby", "workable"}
SOURCE_HE = {"linkedin": "LinkedIn", "drushim": "אתר דרושים (drushim.co.il)", "telegram": "ערוץ משרות בטלגרם",
             "manual": "אתה שלחת לי אותה", "screenshot": "צילום מסך ששלחת לי"}


def source_label(job_or_rec) -> str:
    """Where the job was found — shown on every job (Maor wants to know)."""
    src = getattr(job_or_rec, "source", None) or (job_or_rec.get("source") if isinstance(job_or_rec, dict) else "") or ""
    company = getattr(job_or_rec, "company", None) or (job_or_rec.get("company") if isinstance(job_or_rec, dict) else "")
    extra = getattr(job_or_rec, "extra", None) or {}
    url = getattr(job_or_rec, "url", None) or (job_or_rec.get("url") if isinstance(job_or_rec, dict) else "") or ""
    if src in CAREER_SITES:
        label = f"אתר הקריירה של {company}" if company else "אתר הקריירה של החברה"
    else:
        label = SOURCE_HE.get(src) or ("LinkedIn" if "linkedin.com" in url else src or "לא ידוע")
    if src == "telegram" and extra.get("channel"):
        label += f" @{extra['channel']}"
    return label


def human_message(job: Job, an: dict, loc_he: str, cv_display: str | None, known: dict | None) -> str:
    """Readable, in paragraphs: verdict + chance, why, what the job is, where, CV."""
    icon = {"go": "🟢", "maybe": "🟡", "skip": "🔴"}[an["verdict"]]
    company = job.company or an.get("company") or ""
    title = an.get("title") or job.title
    paras = [f"{icon} <b>{e(company)}</b>\n{e(title)}\n🔎 <b>נמצאה ב:</b> {e(source_label(job))}"]
    why = an.get("why_he") or ""
    paras.append(f"<b>{e(an['verdict_he'])}</b> · סיכוי: <b>{e(an['chance_he'])}</b>"
                 + (f"\n<b>למה:</b> {e(why)}" if why else ""))
    if an.get("summary_he"):
        paras.append(f"<b>על מה המשרה:</b>\n{e(an['summary_he'])}")
    if an.get("pitch_he"):
        paras.append(f"💬 {e(an['pitch_he'])}")
    if an.get("learned_note_he"):
        paras.append(f"🧠 {e(an['learned_note_he'])}")
    paras.append(loc_he)
    if job.extra.get("reapply_days"):
        paras.append(f"🔁 הגשת לכאן לפני {job.extra['reapply_days']} ימים ולא חזרו — הפעם קו\"ח בגרסה אחרת"
                     + (f":\n{e(an['cv_change_he'])}" if an.get("cv_change_he") else "."))
    elif an["verdict"] != "skip" and an.get("cv_tips_he"):
        paras.append(f"💡 {e(an['cv_tips_he'][0])}")
    if known and known.get("status") != "skipped" and not job.extra.get("reapply_days"):
        paras.append(f"ℹ️ דומה למשהו שכבר במעקב: {e(known['title'])} ({e(STATUS_HE.get(known.get('status'), known.get('status')))})")
    if cv_display:
        paras.append(f"📄 קו\"ח ב{'אנגלית' if an['cv_lang'] == 'en' else 'עברית'}, {'עם' if an['transcript'] else 'בלי'} גליון ציונים (שמור גם ב-Drive)")
    return "\n\n".join(paras)


# ---------------------------------------------------------------- briefing
def briefing_messages(job: Job, an: dict, loc_he: str, cv_display: str | None, known: dict | None) -> list[str]:
    icon = {"go": "🟢", "maybe": "🟡", "skip": "🔴"}[an["verdict"]]
    m1 = [f"{icon} <b>{e(job.company)} | {e(an.get('title') or job.title)}</b>",
          f"<b>{e(an['verdict_he'])}</b> · סיכוי: {e(an['chance_he'])}"]
    if an.get("summary_he"):
        m1.append(f"על מה: {e(an['summary_he'])}")
    if an.get("why_he"):
        m1.append(f"למה: {e(an['why_he'])}")
    m1.append(loc_he)
    for w in an.get("warnings", [])[:4]:
        m1.append(f"⚠️ {e(w)}")
    if known:
        m1.append(f"ℹ️ כבר במעקב: {e(known['title'])} ({e(known.get('status'))})")
    if an["verdict"] != "skip" and cv_display:
        m1.append(f"📄 קו\"ח {'באנגלית' if an['cv_lang'] == 'en' else 'בעברית'}, "
                  f"{'עם' if an['transcript'] else 'בלי'} גליון ציונים:\n<code>{e(cv_display)}</code>")
    if job.extra.get("reapply_days"):
        m1.append(f"🔁 הגשת לפני {job.extra['reapply_days']} ימים — עברו חודשיים, אפשר להגיש שוב.")
    if an["verdict"] != "skip" and an.get("cv_tips_he"):
        m1.append("💡 " + "\n💡 ".join(e(t) for t in an["cv_tips_he"][:2]))
    msgs = ["\n".join(m1)]
    if an["verdict"] == "skip" or not os.environ.get("SEND_BRIEFING"):
        return msgs   # questionnaire answers are on demand: send a screenshot of the form to the bot

    # briefing: tap-to-copy answers
    lang = an["cv_lang"]
    m2 = ["📝 <b>תדריך הגשה</b> (לחיצה על טקסט מעתיקה אותו)"]
    for fa in an.get("form_answers", [])[:10]:
        m2.append(f"• {e(fa['q'])}\n<code>{e(fa['a'])}</code>")
    for q in QA["common"]:
        a = q.get(f"a_{'en' if lang == 'en' else 'he'}", "")
        if not a or a == "{generated_per_job}":
            continue
        flag = " ⚠️לאשר" if q.get("confirm") else ""
        m2.append(f"• {e(q['q_he'])}{flag}\n<code>{e(a)}</code>")
    why = an.get("why_us_en") if lang == "en" else an.get("why_us_he")
    if why:
        m2.append(f"• למה המשרה מעניינת אותך:\n<code>{e(why)}</code>")
    if an.get("cv_tips_he"):
        m2.append("💡 " + "\n💡 ".join(e(t) for t in an["cv_tips_he"][:3]))
    msgs.append("\n\n".join(m2))
    return msgs


# ---------------------------------------------------------------- main step
REAPPLY_DAYS = 30   # after this many days since applying (no answer), a re-posted job is offered again — with a different CV
EVENTS = ROOT / "data" / "events.json"


def log_event(kind: str, job_or_app, **extra):
    """Quiet log for the weekly summary (instead of pinging Maor for every little thing)."""
    ev = json.loads(EVENTS.read_text(encoding="utf-8")) if EVENTS.exists() else []
    j = job_or_app if isinstance(job_or_app, dict) else job_or_app.to_dict()
    ev.append({"date": date.today().isoformat(), "kind": kind, "company": j.get("company"), "title": j.get("title"),
               "url": j.get("url"), **extra})
    EVENTS.write_text(json.dumps(ev[-400:], ensure_ascii=False, indent=1), encoding="utf-8")
APPLIED_STATES = ("applied", "interview", "rejected")


def _norm(s):
    return re.sub(r"[^a-z0-9א-ת]+", " ", (s or "").lower()).strip()


_CO_NOISE = {"israel", "il", "ltd", "inc", "corp", "corporation", "technologies", "technology", "systems", "labs",
             "lab", "group", "co", "the", "semiconductor", "semiconductors", "בע", "מ", "ישראל"}


JOB_BOARDS = {"linkedin", "glassdoor", "indeed", "alljobs", "drushim", "jobmaster", "comeet", "greenhouse", "lever"}


_TITLE_NOISE = {"student", "students", "intern", "internship", "engineer", "engineering", "position", "job", "the", "and",
                "for", "of", "in", "a", "סטודנט", "סטודנטית", "משרת", "ית", "il", "israel"}


def similar_title(a: str, b: str) -> bool:
    """'Post Silicon and System Validation Student' ~ 'Post Silicon and System Validation Engineer Student',
    but 'Digital Design Student' is NOT 'Digital IC Design Intern' (could be a different role at the same company)."""
    ta = {w for w in _norm(a).split() if w not in _TITLE_NOISE}
    tb = {w for w in _norm(b).split() if w not in _TITLE_NOISE}
    if not ta or not tb:
        return _norm(a) == _norm(b)
    inter = len(ta & tb)
    if inter / len(ta | tb) >= 0.75:
        return True
    # one title wraps the other ("Mobileye - NPI Logistic and Operations Student - Radar") — only for specific titles
    small = min(len(ta), len(tb))
    return small >= 4 and inter == small


def same_company(a: str, b: str) -> bool:
    """'Elbit Systems Israel' == 'Elbit Systems' == 'Elbit'; 'Amazon Annapurna Labs' ~ 'Annapurna Labs'."""
    ta = [w for w in _norm(a).split() if w not in _CO_NOISE]
    tb = [w for w in _norm(b).split() if w not in _CO_NOISE]
    if not ta or not tb:
        return False
    return ta[0] == tb[0] or set(ta) <= set(tb) or set(tb) <= set(ta)


def repost_check(job: Job) -> dict | None:
    """Handle jobs we've seen before. Same posting = same company AND (same job number OR same title).

    - applied < REAPPLY_DAYS ago and it's open again -> they're still looking, Maor wasn't picked: stay quiet, log it
    - applied >= REAPPLY_DAYS ago, no answer -> offer again, with a different CV (previous attempt is passed along)
    - seen in the last REAPPLY_DAYS but not applied -> quiet duplicate (no repeated pings for the same role)
    """
    d = load_apps()
    rid = _norm(job.req_id)

    def same(a):
        if a.get("uid") == job.uid:
            return True
        if not same_company(a.get("company"), job.company):
            return False
        if rid and _norm(a.get("req_id")) == rid:
            return True
        if a.get("status") == "skipped":
            if not _maor_skip_active(a):          # a filtered job (e.g. the MSc role) must not hide a similar one
                return False
            if a.get("skip_reason") == "company":  # "not this company" -> nothing from it for a while
                return True
            return _norm(a.get("title")) == _norm(job.title)   # same role re-posted; similar ones go to the AI
        return similar_title(a.get("title"), job.title)

    prev = [a for a in d["applications"] if same(a)]
    if not prev:
        return None
    if any(_maor_skip_active(a) for a in prev) and not any(a.get("status") in APPLIED_STATES for a in prev):
        hit = next(a for a in prev if _maor_skip_active(a))
        log_event("hidden_like_maor_skip", job, like=hit.get("id"), reason=hit.get("skip_reason"))
        return {"job": job.to_dict(), "duplicate": True, "like_skipped": hit.get("id")}
    applied = [a for a in prev if a.get("status") in APPLIED_STATES and a.get("applied_date")]
    if applied:
        last = max(applied, key=lambda a: a["applied_date"])
        days = (date.today() - date.fromisoformat(last["applied_date"])).days
        if last.get("status") in ("interview", "offer"):
            return {"job": job.to_dict(), "duplicate": True}
        if days < REAPPLY_DAYS:
            if not last.get(f"repost_{date.today().isoformat()}") and job.uid != last.get("uid"):
                last[f"repost_{date.today().isoformat()}"] = True
                save_apps(d)
                log_event("reposted_after_apply", job, days_since_apply=days, app_id=last.get("id"))
            return {"job": job.to_dict(), "repost": True, "days": days}
        if last.get("reapply_offered"):
            return {"job": job.to_dict(), "duplicate": True}
        last["reapply_offered"] = date.today().isoformat()
        save_apps(d)
        job.extra["reapply_days"] = days
        job.extra["previous_attempt"] = {k: last.get(k) for k in ("id", "applied_date", "status", "cv_profile",
                                                                   "cv_selection", "channel", "referrer", "notes")}
        return None
    recent = [a for a in prev if a.get("found_date") and
              (date.today() - date.fromisoformat(a["found_date"])).days < REAPPLY_DAYS]
    if recent:
        return {"job": job.to_dict(), "duplicate": True}
    return None


def process(job: Job, src_cfg: dict | None = None, image: Path | None = None, manual=False) -> dict:
    src_cfg = src_cfg or _source_cfg(job.company)
    if not manual:
        rp = repost_check(job)
        if rp is not None:
            return rp
    if not manual and job.company and find_recent_same(job):   # same job seen on another site this month
        log_event("duplicate_job", job)
        return {"job": job.to_dict(), "duplicate": True}
    rule = evaluate(job, src_cfg)
    if not manual and rule["notify"]:
        from .learn import muted_reason
        mr = muted_reason(job.title)
        if mr:      # learned from Maor's skips / no-answers: this kind of role isn't for him (he can undo in chat)
            record(job, {**rule, "why_he": f"למדתי שמשרות {mr} פחות מעניינות אותך"}, None)
            log_event("learned_mute", job, category=mr)
            return {"job": job.to_dict(), "eval": rule, "notified": False, "learned_mute": mr}
    known = already_known(job.company, job.title)
    if not manual and not rule["notify"] and rule["fail_reasons"] and all(
            r.startswith("לא מוזכרת הנדסת חשמל") or r.startswith("תחום לא מתאים (business") or
            r.startswith("תחום לא מתאים (industrial") for r in rule["fail_reasons"]):
        from .learn import liked
        lk = liked(job.title)
        if lk:       # the rules say "wrong field", but Maor applies to exactly this kind of role -> let him decide
            rule = {**rule, "notify": True, "verdict": "maybe",
                    "warnings": rule["warnings"] + [f"שלחתי למרות שזה לא תפקיד הנדסת חשמל קלאסי — אתה מגיש למשרות {lk}"]}
            rule["fail_reasons"] = []
    if not manual and not rule["notify"]:
        record(job, {**rule, "why_he": "; ".join(rule["fail_reasons"] or rule["warnings"])}, None)
        return {"job": job.to_dict(), "eval": rule, "notified": False}

    an = analyze(job, rule, image=image)
    if not manual and an["verdict"] == "skip":
        # the AI found a hard mismatch (not EE/hardware, support/IT, completed degree...) -> don't bother Maor
        record(job, an, None, status="skipped")
        log_event("ai_skipped", job, why=an.get("why_he", ""))
        return {"job": job.to_dict(), "analysis": an, "notified": False}
    if an.get("job_text") and not job.description:
        job.description = an["job_text"]
    if an.get("company") and job.company in ("", "Unknown"):
        job.company = an["company"]
    if an.get("title") and job.source in ("manual", "screenshot") and len(an["title"]) <= 90:
        job.title = an["title"]          # pasted text: its first line is often "Locations" / "Description" — the AI knows better
    if an.get("title") and (not job.title or re.fullmatch(r"\s*(job )?description|about( the)? (job|role)|תיאור( ה?משרה| התפקיד)?|דרישות( התפקיד)?|"
                                                         r"[^|]*\|[^|]*recruit[^|]*", job.title, re.I)):
        job.title = an["title"]
    if len(job.title or "") > 90 and an.get("title") and len(an["title"]) <= 90:
        job.title = an["title"]
    dup = find_recent_same(job)
    if dup:       # same job already sent (other site / link vs text / screenshot) -> no second card
        log_event("duplicate_job", job, same_as=dup.get("id"))
        if manual:
            same_job_reply(dup["id"], job.url)
        return {"job": job.to_dict(), "duplicate": True, "same_as": dup.get("id")}
    loc = classify(an.get("location_text") or job.location or f"{job.title} {(job.description or '')[:800]}")

    cv_display, cv_err, pdf_bytes = None, None, None
    if an["verdict"] != "skip" or manual:      # a job Maor sent himself always gets a CV (+ library), even a weak fit
        if not an.get("cv_lang"):
            an["cv_lang"] = rule.get("cv_lang", "en")
        an.setdefault("transcript", rule.get("transcript", False))
        lang, tr = an["cv_lang"], an["transcript"]
        with tempfile.TemporaryDirectory() as td:
            try:
                res = build_cv(Path(td), lang, an.get("domains", []), an.get("cv_selection"), transcript_pdf(lang))
                final = res["cv_with_transcript"] if tr and res["cv_with_transcript"] else res["cv"]
                if tr and not res["cv_with_transcript"]:
                    an.setdefault("warnings", []).append("גליון ציונים לא נמצא במערכת — צרף ידנית")
                cv_display = drive.display_path(job.company, job.title, tr)
                if drive.enabled() and not drive.auth_ok():
                    alert_once("drive_auth", "🔐 <b>החיבור ל-Google Drive הפסיק לעבוד</b> (כנראה הטוקן בוטל/פג). "
                               "הקו\"ח עדיין נשלח בטלגרם. לתיקון: טוקן חדש ב-https://developers.google.com/oauthplayground "
                               "→ GitHub secrets → GDRIVE_REFRESH_TOKEN. פרטים ב-LINKS.md")
                elif drive.enabled():   # only the version to send: "1_להגשה עכשיו/<Company - Role>/Maor Shitrit CV.pdf"
                    drive.upload(final, drive.library_path(job.company, job.title), drive.CV_NAME)
                cv_file_to_send = final
                pdf_bytes = final.read_bytes()
            except CVSafetyError as ex:
                cv_err = f"⛔ בניית הקו\"ח נעצרה: {ex}"
            except Exception as ex:
                cv_err = f"⚠️ שגיאה בבניית קו\"ח: {type(ex).__name__}: {ex}"
    if cv_err:
        an.setdefault("warnings", []).append(cv_err)

    text = human_message(job, an, describe_he(loc), cv_display, known)
    sent = None
    if telegram.enabled():
        buttons = [[{"text": "🔗 פתח משרה", "url": job.apply_url or job.url}]] if (job.apply_url or job.url) else []
        if an["verdict"] != "skip" or manual:
            buttons.append([{"text": "✅ הגשתי", "callback_data": f"applied:{job.uid}"},
                            {"text": "⏭️ דלג", "callback_data": f"skip:{job.uid}"}])
        if pdf_bytes and len(text) <= 1000:      # one message: the CV with the explanation and the buttons
            with tempfile.TemporaryDirectory() as td:
                f = Path(td) / "Maor Shitrit CV.pdf"
                f.write_bytes(pdf_bytes)
                sent = telegram.send_document(f, caption=text, buttons=buttons or None, silent=an["verdict"] == "skip")
        else:
            sent = telegram.send(text, buttons or None, silent=an["verdict"] == "skip")
            if pdf_bytes:
                with tempfile.TemporaryDirectory() as td:
                    f = Path(td) / "Maor Shitrit CV.pdf"
                    f.write_bytes(pdf_bytes)
                    telegram.send_document(f, silent=True)
    else:
        print(re.sub(r"<[^>]+>", "", text))
    record(job, an, cv_display)
    msg_id = ((sent or {}).get("result") or {}).get("message_id") if isinstance(sent, dict) else None
    if msg_id:          # remember the message, so a status reported in chat / by screenshot updates its buttons too
        d = load_apps()
        for a in d["applications"]:
            if a.get("uid") == job.uid:
                a["tg_msg"], a["tg_url"] = msg_id, job.apply_url or job.url
                a["sent_at"] = datetime.now().isoformat(timespec="minutes")
        save_apps(d)
    log_event("notified", job, verdict=an["verdict"], reapply=bool(job.extra.get("reapply_days")))
    rec = next((x for x in load_apps()["applications"] if x.get("uid") == job.uid), {})
    import time as _t
    LAST_JOB.write_text(json.dumps({"id": rec.get("id"), "uid": job.uid, "company": job.company, "title": job.title,
                                    "verdict": an["verdict"], "when": date.today().isoformat(),
                                    "ts": int(_t.time() * 1000), "status": rec.get("status")},
                                   ensure_ascii=False, indent=1), encoding="utf-8")
    return {"job": job.to_dict(), "analysis": an, "notified": True}


def send_form_answers(form: dict):
    """Answers for an application-form screenshot; new questions are remembered for next time."""
    lines = ["📝 <b>תשובות לשאלון</b> (לחיצה על תשובה מעתיקה אותה)"]
    for qa in form.get("answers", [])[:25]:
        flag = " ⚠️" if qa.get("unsure") else ""
        lines.append(f"• {e(qa.get('q'))}{flag}\n<code>{e(qa.get('a'))}</code>")
    if form.get("note_he"):
        lines.append(f"ℹ️ {e(form['note_he'])}")
    msg = "\n\n".join(lines)
    telegram.send(msg) if telegram.enabled() else print(re.sub(r"<[^>]+>", "", msg))
    qf = ROOT / "data" / "questionnaire.json"
    q = json.loads(qf.read_text(encoding="utf-8"))
    known = {_norm(x.get("q")) for x in q.get("learned", [])}
    for qa in form.get("answers", []):
        if _norm(qa.get("q")) not in known and not qa.get("unsure"):
            q.setdefault("learned", []).append({"q": qa.get("q"), "a": qa.get("a"), "company": form.get("company", "")})
    qf.write_text(json.dumps(q, ensure_ascii=False, indent=1), encoding="utf-8")


HE_COMPANIES = {"גוגל": "google", "מרוול": "marvell", "מארוול": "marvell", "מרבל": "marvell", "אינטל": "intel",
                "אנבידיה": "nvidia", "אנווידיה": "nvidia", "אינווידיה": "nvidia", "נבידיה": "nvidia", "אפל": "apple",
                "אמזון": "amazon", "אנאפורנה": "amazon annapurna", "אנפורנה": "amazon annapurna", "קוואלקום": "qualcomm",
                "קואלקום": "qualcomm", "רפאל": "rafael", "אלביט": "elbit", "אלתא": "iai elta", "אלטא": "iai elta",
                "תעשייה אווירית": "iai", "אלטרה": "altera", "אסטרה": "astera", "טקסס": "texas", "רמון": "ramon",
                "חברת החשמל": "israel electric", "סנדיסק": "sandisk", "אינפיניאון": "infineon", "מובילאיי": "mobileye",
                "אבנס": "avenas", "נס ": "ness", "סיווה": "ceva", "סאמסונג": "samsung", "אפלייד": "applied"}


def find_application(text: str) -> dict | None:
    """'#12' / '12' -> by id; otherwise best company (+title words) match, most recent first."""
    apps = load_apps()["applications"]
    m = re.search(r"#?\b(\d{1,4})\b", text)
    if m:
        for a in apps:
            if str(a.get("id")) == m.group(1):
                return a
    t = _norm(text)
    for he, en in HE_COMPANIES.items():
        if he in t:
            t += f" {en}"
    best, score = None, 0
    for a in reversed(apps):
        c = _norm(a.get("company"))
        if not c or c.split()[0] not in t:
            continue
        sc = 10 + sum(1 for w in _norm(a.get("title")).split() if len(w) > 2 and w in t)
        sc += {"interview": 3, "applied": 2, "cv_built": 1}.get(a.get("status"), 0)
        if sc > score:
            best, score = a, sc
    return best


def start_interview(text: str):
    """'קיבלתי ראיון ל-Marvell DV' -> mark as interview + research and send a prep pack (PDF)."""
    from .analyze import interview_prep
    from .prep_pdf import to_pdf
    a = find_application(text)
    if not a:
        msg = ("לא מצאתי את המשרה במעקב 🤔 כתוב את שם החברה והתפקיד, או את המספר מ-/list "
               "(למשל: <code>ראיון 12</code>). אפשר גם לשלוח קישור/טקסט של המשרה קודם.")
        telegram.send(msg) if telegram.enabled() else print(msg)
        return
    d = load_apps()
    for x in d["applications"]:
        if x.get("id") == a.get("id"):
            x["status"] = "interview"
            x["interview_marked"] = x.get("interview_marked") or date.today().isoformat()  # date he told us, not the interview date
            x.setdefault("notes", "")
            a = x
    save_apps(d)
    if telegram.enabled():
        telegram.send(f"🎯 <b>ראיון: {e(a['company'])} | {e(a['title'])}</b>\nמחפש שאלות אמיתיות מראיונות בחברה "
                      f"(tech-interview, theworker, betheone ועוד) ובונה לך חוברת הכנה. 5-10 דקות ⏳")
    job = None
    if a.get("url"):
        job, _ = job_from_url(a["url"])
    prep = interview_prep(a, job)
    if not prep:
        msg = "⚠️ לא הצלחתי לבנות הכנה כרגע (ה-AI לא זמין). נסה שוב מאוחר יותר עם: ראיון " + str(a["id"])
        telegram.send(msg) if telegram.enabled() else print(msg)
        return
    with tempfile.TemporaryDirectory() as td:
        pdf = to_pdf(prep, Path(td) / f"Interview Prep - {drive.safe(a['company'])}.pdf")
        if drive.enabled():
            drive.upload(pdf, drive.library_path(a["company"], a["title"], bucket="applied"))
        reported = sum(1 for q in prep.get("questions", []) if q.get("reported"))
        top = "\n".join(f"• {e(q['q'])}" for q in prep.get("questions", [])[:5])
        cap = (f"🧠 <b>הכנה לראיון — {e(a['company'])}</b>\n{len(prep.get('questions', []))} שאלות "
               f"({reported} מדיווחים אמיתיים), כל אחת עם 🟡🔵🟢🔴.\n\nהשאלות הכי צפויות:\n{top}")
        if telegram.enabled():
            telegram.send(cap)
            telegram.send_document(pdf, caption=f"הכנה לראיון — {e(a['company'])}")
        else:
            print(re.sub(r"<[^>]+>", "", cap), "\n->", pdf)


CHAT_HISTORY = ROOT / "data" / "chat_history.json"
LAST_JOB = ROOT / "data" / "last_job.json"


def apply_record(company: str, title: str, status: str):
    """Find the application (company + title) and set its status; add it if it's not tracked. Returns (app, changed)."""
    apps = load_apps()["applications"]
    tnorm = _norm(title)
    cands = [a for a in apps if same_company(a.get("company"), company)]

    def score(a):
        at = _norm(a.get("title"))
        return (at == tnorm, len(set(at.split()) & set(tnorm.split())),
                a.get("status") in ("cv_built", "held") if status == "applied" else a.get("status") == "applied",
                a.get("found_date") or "")
    best = max(cands, key=score) if cands else None
    if best and not score(best)[0] and score(best)[1] < 2:
        best = None
    if best and best.get("status") == status:
        return best, False
    if not best:
        import hashlib
        d = load_apps()
        best = {"id": max([a["id"] for a in d["applications"]] + [0]) + 1,
                "uid": hashlib.sha1(f"{company}|{title}|manual".lower().encode()).hexdigest()[:12],
                "company": company or "?", "title": title or "?", "status": status,
                "applied_date": date.today().isoformat() if status == "applied" else None,
                "found_date": date.today().isoformat(), "notes": "נרשם מהשיחה", "verdict": None}
        d["applications"].append(best)
        save_apps(d)
        log_event(status, best)
        return best, True
    if not best.get("uid"):          # older records have no uid -> give them one so buttons can refer to them
        import hashlib
        d = load_apps()
        for a in d["applications"]:
            if a["id"] == best["id"]:
                a["uid"] = hashlib.sha1(f"{a['company']}|{a['title']}|{a['id']}".lower().encode()).hexdigest()[:12]
                best = a
        save_apps(d)
    set_status(best["uid"], status, quiet=True)
    return next(a for a in load_apps()["applications"] if a.get("uid") == best["uid"]), True


def record_from_screenshot(form: dict):
    """A 'thank you for applying' / rejection screenshot -> update the tracker (or add the job) and confirm."""
    status = "applied" if form.get("type") == "applied" else "rejected"
    company, title = (form.get("company") or "").strip(), (form.get("title") or "").strip()
    apps = load_apps()["applications"]
    tnorm = _norm(title)
    cands = [a for a in apps if same_company(a.get("company"), company)]
    # best title match; prefer an open (not yet applied) record for 'applied'
    def score(a):
        at = _norm(a.get("title"))
        overlap = len(set(at.split()) & set(tnorm.split()))
        return (at == tnorm, overlap, a.get("status") in ("cv_built", "held") if status == "applied" else a.get("status") == "applied",
                a.get("found_date") or "")
    best = max(cands, key=score) if cands else None
    if best and score(best)[1] < 2 and not score(best)[0]:
        best = None
    reply = (form.get("reply_he") or "").strip()
    if reply and telegram.enabled():
        telegram.send(e(reply))
    if not best:   # not in the tracker yet -> add it
        d = load_apps()
        new = {"id": max([a["id"] for a in d["applications"]] + [0]) + 1, "uid": None, "company": company or "?",
               "title": title or "?", "status": status, "applied_date": date.today().isoformat() if status == "applied" else None,
               "found_date": date.today().isoformat(), "notes": "נרשם מצילום מסך", "verdict": None}
        import hashlib
        new["uid"] = hashlib.sha1(f"{company}|{title}|screenshot".lower().encode()).hexdigest()[:12]
        d["applications"].append(new)
        save_apps(d)
        best = new
        log_event(status, new)
    else:
        set_status(best.get("uid") or str(best["id"]), status, quiet=True)
    if telegram.enabled():
        line = f"✅ רשמתי: {e(best['company'])} — {e(best['title'])} → <b>{STATUS_HE.get(status, status)}</b>"
        if status == "applied":
            telegram.send(line + "\n\nאיך הגשת?", CHANNEL_BUTTONS(best.get("uid") or best["id"]))
        else:
            telegram.send(line)


def _wait_for_job(since_ms: int, timeout_s: int = 240):
    """Poll GitHub until the job analysis that started at `since_ms` has saved its result, then load it."""
    import subprocess, time
    repo = ROOT / "priv" if (ROOT / "priv" / ".git").exists() else ROOT   # public code repo: data is a checkout in priv/
    end = time.time() + timeout_s
    while time.time() < end:
        try:
            subprocess.run(["git", "fetch", "-q", "origin", "main"], cwd=repo, check=True, timeout=30)
            lj = json.loads(subprocess.run(["git", "show", "origin/main:data/last_job.json"], cwd=repo,
                                           capture_output=True, text=True, check=True).stdout)
            if int(lj.get("ts") or 0) >= since_ms - 5000:
                for f in ("data/last_job.json", "data/applications.json"):
                    subprocess.run(["git", "checkout", "-q", "origin/main", "--", f], cwd=repo, check=True)
                print("[chat] picked up the job that was being analyzed", file=sys.stderr)
                return True
        except Exception as ex:
            print(f"[chat] waiting for job: {ex}", file=sys.stderr)
        time.sleep(15)
    print("[chat] gave up waiting for the pending job", file=sys.stderr)
    return False


GENERAL_PROFILE = {"en": "prof_general_general_cv", "he": "prof_rafael_electrical_engineering_student_general_hebrew"}


def send_general_cv(lang: str = "en", transcript: bool = False):
    """A real PDF of the general CV (not text): sent in Telegram + saved in Drive at CV_Library/0_כללי/."""
    from .cv.build import build_cv
    lang = "he" if lang == "he" else "en"
    with tempfile.TemporaryDirectory() as td:
        res = build_cv(Path(td), lang, [], {"profile": GENERAL_PROFILE[lang]}, transcript_pdf(lang) if transcript else None)
        f = res["cv_with_transcript"] if transcript and res.get("cv_with_transcript") else res["cv"]
        folder_name = "קורות חיים כלליים - " + ("עברית" if lang == "he" else "אנגלית")
        where = ""
        if drive.enabled():
            try:
                drive.upload(f, ["0_כללי", folder_name], drive.CV_NAME)
                where = f"\n📁 נשמר גם ב-Drive: CV_Library/0_כללי/{folder_name}"
            except Exception as ex:
                print(f"[drive] general cv: {ex}", file=sys.stderr)
        cap = (f"📄 <b>קורות חיים כלליים ב{'עברית' if lang == 'he' else 'אנגלית'}</b>"
               + (" + גליון ציונים" if transcript and res.get("cv_with_transcript") else "") + where)
        if telegram.enabled():
            telegram.send_document(f, caption=cap)
        else:
            print(cap, f)


def same_job_reply(app_id, url: str = ""):
    """Maor sent a job (link / text / screenshot) that he already got — point to it instead of a second card."""
    d = load_apps()
    a = next((x for x in d["applications"] if str(x.get("id")) == str(app_id)), None)
    if not a:
        return
    if url and not a.get("url"):
        a["url"] = url
        save_apps(d)
    st = STATUS_HE.get(a.get("status"), a.get("status"))
    msg = (f"🔁 זו אותה משרה ששלחתי לך כבר: <b>{e(a['company'])}</b> — {e(a['title'])}\n"
           f"מצב: <b>{e(st)}</b>" + ("" if a.get("status") in APPLIED_STATES else
                                     "\nהקו\"ח המותאמים נמצאים בהודעה המקורית (מסומנת למעלה) וב-Drive."))
    buttons = None
    if a.get("status") in ("cv_built", "held"):
        buttons = [[{"text": "✅ הגשתי", "callback_data": f"applied:{a['uid']}"},
                    {"text": "⏭️ דלג", "callback_data": f"skip:{a['uid']}"}]]
    telegram.send(msg, buttons, reply_to=a.get("tg_msg")) if telegram.enabled() else print(msg)


def find_recent_same(job: Job, days: int = 30) -> dict | None:
    """The same posting already in the tracker (sent / applied), even if it came from another site or as text vs link."""
    from datetime import timedelta
    since = (date.today() - timedelta(days=days)).isoformat()
    unknown = lambda c: not c or re.search(r"unknown|^jobs?$|^careers?$|^\?$|לא ידוע", c, re.I)
    for a in reversed(load_apps()["applications"]):
        if (a.get("found_date") or "") < since or a.get("uid") == job.uid:
            continue
        if a.get("status") == "skipped" and a.get("skipped_by") != "maor":
            continue
        if job.url and a.get("url") and job.url.split("?")[0].rstrip("/") == a["url"].split("?")[0].rstrip("/"):
            return a
        co_ok = same_company(a.get("company"), job.company) or unknown(a.get("company")) or unknown(job.company)
        if co_ok and (similar_title(a.get("title"), job.title) or
                      (len(_norm(job.title).split()) >= 3 and _norm(job.title) in _norm(a.get("title")))):
            return a
    return None


def chat_turn(message: str, image_paths: list | None = None):
    """Everything Maor sends (text, voice, screenshots) goes through here: one AI 'brain' with the whole context
    decides what he means, does it, and answers in ONE message."""
    from .analyze import chat
    m = re.match(r"\[pending:(\d+)\]\s*", message)
    if m:   # he sent a job a moment ago and it's still being analyzed -> wait for it (up to ~4 min)
        message = message[m.end():]
        _wait_for_job(int(m.group(1)))
    m = re.match(r"\[images:([^\]]+)\]\s*", message)
    if m:   # screenshots sent together (an album counts as one message)
        message = message[m.end():]
        image_paths = (image_paths or []) + [
            telegram.download_file(fid, Path(tempfile.gettempdir()) / f"img_{i}.jpg")
            for i, fid in enumerate(m.group(1).split(","))]
    hist = json.loads(CHAT_HISTORY.read_text(encoding="utf-8")) if CHAT_HISTORY.exists() else []
    last = json.loads(LAST_JOB.read_text(encoding="utf-8")) if LAST_JOB.exists() else None
    if last and last.get("uid"):
        cur = next((a for a in load_apps()["applications"] if a.get("uid") == last["uid"]), {})
        last["status"] = cur.get("status")
    res = chat(message, hist, load_apps()["applications"], last, images=image_paths)
    if not res:                      # the AI was busy — one more try a minute later before giving up
        import time
        time.sleep(60)
        res = chat(message, hist, load_apps()["applications"], last, images=image_paths)
    if not res:
        msg = "⚠️ שירות ה-AI של גוגל עמוס מאוד כרגע ולא הצלחתי לענות גם אחרי כמה ניסיונות. שלח שוב בעוד כמה דקות."
        telegram.send(msg) if telegram.enabled() else print(msg)
        return
    reply = res.get("reply_he", "").strip()
    done, buttons, job_card = run_actions(res.get("actions", [])[:3], image_paths)
    _send_turn(reply, done, buttons, job_card, message, image_paths)


def act_turn(payload: str):
    """The Worker already answered Maor (fast, in seconds) and decided what to do — here we only DO it
    (record, build a CV, analyze a job...) and report what was done."""
    d = json.loads(payload)
    if d.get("pending"):
        _wait_for_job(int(d["pending"]))
    done, buttons, job_card = run_actions(d.get("actions", [])[:3], None)
    # the Worker already told him what's happening -> follow up only with a question (e.g. "איך הגשת?") or a problem
    done = [x for x in done if not x.startswith("✅")] if buttons else [x for x in done if not x.startswith("✅")]
    _send_turn("", done, buttons, True, d.get("message", ""), None, reply_for_history=d.get("reply", ""))


def _send_turn(reply, done, buttons, job_card, message, image_paths, reply_for_history=""):
    text = "\n\n".join(x for x in ([reply] if not job_card else []) + (["\n".join(done)] if done else []) if x)
    if text and telegram.enabled():
        try:
            telegram.send(text, buttons)
        except Exception:
            telegram.send(e(re.sub(r"<[^>]+>", "", text)), buttons)
    elif text:
        print(text)
    hist = json.loads(CHAT_HISTORY.read_text(encoding="utf-8")) if CHAT_HISTORY.exists() else []
    now = date.today().isoformat()
    said = message + (f" [שלח {len(image_paths)} צילומי מסך]" if image_paths else "")
    hist += [{"from": "maor", "text": said[:2000], "date": now},
             {"from": "bot", "text": (reply_for_history or reply or text)[:2000], "date": now}]
    CHAT_HISTORY.write_text(json.dumps(hist[-40:], ensure_ascii=False, indent=1), encoding="utf-8")


def run_actions(actions: list, image_paths=None):
    done, buttons, job_card = [], None, False
    for act in actions:
        t = act.get("type")
        try:
            if t == "record_application":
                a, changed = apply_record(act.get("company", ""), act.get("title", ""), act.get("status", "applied"))
                if changed:
                    done.append(f"✅ רשמתי: {e(a['company'])} — {e(a['title'])} → <b>{STATUS_HE.get(a['status'], a['status'])}</b>")
                    if a["status"] == "applied" and not a.get("channel"):
                        done.append("איך הגשת?")
                        buttons = CHANNEL_BUTTONS(a["uid"])
            elif t == "status" and act.get("id"):
                a = set_status(str(act["id"]), act.get("status", "applied"), quiet=True)
                if a:
                    done.append(f"✅ רשמתי: {e(a['company'])} — {e(a['title'])} → <b>{STATUS_HE.get(act.get('status'), act.get('status'))}</b>")
                    if act.get("status") == "applied" and not a.get("channel") and a.get("uid"):
                        done.append("איך הגשת?")
                        buttons = CHANNEL_BUTTONS(a["uid"])
            elif t == "referrer" and act.get("id"):
                d = load_apps()
                for a in d["applications"]:
                    if a.get("id") == act["id"]:
                        a["channel"] = act.get("channel") or a.get("channel")
                        if act.get("name"):
                            a["referrer"] = act["name"]
                        done.append(f"✅ {e(a['company'])}: {CHANNEL_HE.get(a.get('channel'), '')}" + (f" ({e(a['referrer'])})" if a.get("referrer") else ""))
                save_apps(d)
            elif t == "job_from_images" and image_paths:
                job_card = True
                process(Job(company="", title="", url="", source="screenshot", req_id=f"img-{int(__import__('time').time())}"),
                        image=image_paths, manual=True)
            elif t == "interview":
                start_interview(act.get("text", ""))
            elif t == "job_text" and act.get("text"):
                job_card = True
                url = re.search(r"https?://\S+", act["text"])
                process(Job(company="", title="", url=url.group(0) if url else "", source="manual", description=act["text"],
                            req_id="txt-" + hashlib.sha1(act["text"].encode()).hexdigest()[:10],
                            apply_url=url.group(0) if url else ""), manual=True)
            elif t == "job_url" and act.get("url"):
                job_card = True
                main(["url", act["url"]])
            elif t == "project_week" and act.get("week") is not None:
                pf = ROOT / "profile" / "profile.json"
                prof = json.loads(pf.read_text(encoding="utf-8"))
                prof["project_week_done"] = int(act["week"])
                pf.write_text(json.dumps(prof, ensure_ascii=False, indent=1), encoding="utf-8")
                done.append(f"✅ פרויקט ה-Mini-TPU: שבוע {int(act['week'])} הושלם.")
            elif t == "show_project" and act.get("show") is not None:
                pf = ROOT / "profile" / "profile.json"
                prof = json.loads(pf.read_text(encoding="utf-8"))
                prof["show_fpga_project"] = bool(act["show"])
                pf.write_text(json.dumps(prof, ensure_ascii=False, indent=1), encoding="utf-8")
                done.append("✅ פרויקט ה-Mini-TPU " + ("יופיע מעכשיו בקו\"ח." if act["show"] else "הוסר מהקו\"ח."))
            elif t == "scan":
                main(["scan", "manual"])
            elif t == "taste" and act.get("category"):
                from .learn import set_override, CAT_HE
                cat = set_override(act["category"], act.get("mode", "auto"))
                if cat:
                    done.append({"always": f"✅ אמשיך לשלוח לך משרות {CAT_HE[cat]}.",
                                 "never": f"✅ לא אשלח יותר משרות {CAT_HE[cat]}.",
                                 "auto": f"✅ משרות {CAT_HE[cat]}: חוזר ללמוד לפי מה שתעשה."}[act.get("mode", "auto")])
            elif t == "general_cv":
                send_general_cv(act.get("lang", "en"), bool(act.get("transcript")))
                job_card = True
            elif t == "same_job" and act.get("id"):
                same_job_reply(act["id"], act.get("url") or "")
                job_card = True
            elif t == "insights":
                from .learn import insights
                insights(force=True, send=True, min_cases=1)
        except Exception as ex:
            print(f"[chat] action {t} failed: {ex}", file=sys.stderr)
    return done, buttons, job_card


def weekly_summary(force=False):
    """Saturday night (21:00 Israel time): a human summary of the week + what to do next."""
    from zoneinfo import ZoneInfo
    from datetime import datetime, timedelta
    from .analyze import weekly
    now = datetime.now(ZoneInfo("Asia/Jerusalem"))
    if not force and not (now.weekday() == 5 and now.hour >= 21):
        print(f"[weekly] not Saturday night in Israel ({now:%a %H:%M}) — skipping")
        return
    marker = ROOT / "data" / "weekly_sent.json"
    sent = json.loads(marker.read_text()) if marker.exists() else {}
    if not force and sent.get("last") == now.date().isoformat():
        return
    week_ago = (now.date() - timedelta(days=7)).isoformat()
    ev = json.loads(EVENTS.read_text(encoding="utf-8")) if EVENTS.exists() else []
    apps = load_apps()["applications"]
    try:      # fresh "why were you rejected / not answered" analysis for the weekly tips
        from .learn import insights
        insights(force=True, min_cases=1)
    except Exception as ex:
        print(f"[weekly] insights: {ex}", file=sys.stderr)
    res = weekly([x for x in ev if x["date"] >= week_ago], apps, now.date().isoformat())
    if not res or not res.get("message_he"):
        # no AI -> plain summary
        act = [a for a in apps if a.get("status") in ("applied", "interview")]
        held = [x for x in ev if x["date"] >= week_ago and x["kind"] == "held_maybe"]
        msg = (f"🗓️ <b>סיכום שבועי</b>\nהגשות פעילות: {len(act)}\n" +
               "\n".join(f"• {e(a['company'])} — {e(a['title'])} ({STATUS_HE.get(a['status'], a['status'])}, "
                         f"{a.get('applied_date') or ''})" for a in act[:15]) +
               (f"\n\nמשרות 'אולי' השבוע: {len(held)}" if held else ""))
    else:
        msg = res["message_he"]
    try:
        telegram.send(msg) if telegram.enabled() else print(msg)
    except Exception:
        telegram.send(e(msg))
    sent["last"] = now.date().isoformat()
    marker.write_text(json.dumps(sent))


STATUS_HE = {"applied": "הוגש", "skip": "דילגת", "skipped": "סונן/דילגת", "held": "אולי (בסיכום השבועי)", "cv_built": "קו\"ח מוכן, לא הוגש", "interview": "ראיון", "rejected": "נדחה", "offer": "הצעה"}


CHANNEL_HE = {"direct": "🌐 באתר החברה", "linkedin_apply": "💼 הגשה בלינקדאין", "linkedin": "👤 דרך איש קשר בלינקדאין",
              "linkedin_person": "👤 דרך איש קשר בלינקדאין", "friend": "🤝 דרך מכר"}


def CHANNEL_BUTTONS(ref):
    return [[{"text": "🌐 באתר החברה", "callback_data": f"channel:direct:{ref}"},
             {"text": "💼 הגשה בלינקדאין", "callback_data": f"channel:linkedin_apply:{ref}"}],
            [{"text": "🤝 דרך מכר", "callback_data": f"channel:friend:{ref}"},
             {"text": "👤 איש קשר בלינקדאין", "callback_data": f"channel:linkedin_person:{ref}"}]]


def set_channel(key: str):
    """key = '<channel>:<uid|id>' from the 'how did you apply?' buttons (the Worker already asked for the name)."""
    channel, ref = key.split(":", 1)
    d = load_apps()
    for a in d["applications"]:
        if a.get("uid") == ref or str(a.get("id")) == ref:
            a["channel"] = channel
            save_apps(d)
            log_event("channel", a, channel=channel)
            return a
    return None


def set_referrer(text: str):
    """'<uid|id> <name>' from a reply to the 'what is the name?' question."""
    m = re.match(r"\s*#?([0-9a-f]{12}|\d+)\s+(.+)", text, re.S)
    if not m:
        return
    d = load_apps()
    for a in d["applications"]:
        if a.get("uid") == m.group(1) or str(a.get("id")) == m.group(1):
            a["referrer"] = m.group(2).strip()[:60]
            save_apps(d)
            return a


def _channel_text(a):
    if not a.get("channel"):
        return ""
    t = CHANNEL_HE.get(a["channel"], a["channel"])
    return f" · {t}" + (f" ({a['referrer']})" if a.get("referrer") else "")


SKIP_MEMORY_DAYS = 90       # "not relevant, at least for now": Maor's skips teach the bot for ~3 months
SKIP_REASON_HE = {"field": "התחום לא מעניין", "far": "רחוק מדי", "company": "החברה לא מעניינת",
                  "reqs": "הדרישות לא מתאימות", "later": "לא עכשיו"}


def _maor_skip_active(a: dict) -> bool:
    if a.get("status") != "skipped" or a.get("skipped_by") != "maor" or not a.get("skip_date"):
        return False
    return (date.today() - date.fromisoformat(a["skip_date"])).days < SKIP_MEMORY_DAYS


def maor_skips() -> list[dict]:
    """What Maor skipped recently and why — given to the AI so it learns his taste."""
    return [{"company": a.get("company"), "title": a.get("title"), "domains": a.get("domains", []),
             "location": a.get("location"), "summary": a.get("summary"),
             "reason": SKIP_REASON_HE.get(a.get("skip_reason"), "לא צוינה סיבה"), "date": a.get("skip_date")}
            for a in load_apps()["applications"] if _maor_skip_active(a)][-40:]


def sync_job_message(a: dict):
    """Show the record's current status on the buttons of the job message Maor got (no new message)."""
    if not (a and a.get("tg_msg") and telegram.enabled()):
        return
    ref, st = a.get("uid") or a["id"], a.get("status")
    rows = [[{"text": "🔗 פתח משרה", "url": a["tg_url"]}]] if a.get("tg_url") else []
    undo = {"text": "↩️ ביטול", "callback_data": f"undo:{ref}"}
    if st in ("cv_built", "held"):
        rows.append([{"text": "✅ הגשתי", "callback_data": f"applied:{ref}"}, {"text": "⏭️ דלג", "callback_data": f"skip:{ref}"}])
    elif st == "applied":
        rows.append([{"text": "✅ הוגש", "callback_data": "noop"}, undo])
    elif st == "skipped":
        why = SKIP_REASON_HE.get(a.get("skip_reason"))
        rows.append([{"text": "⏭️ דילגת" + (f" · {why}" if why else ""), "callback_data": "noop"}, undo])
    else:
        label = {"interview": "🎤 ראיון", "rejected": "❌ נדחה", "offer": "🎉 הצעה"}.get(st, STATUS_HE.get(st, st))
        rows.append([{"text": label, "callback_data": "noop"}])
    try:
        telegram.edit_buttons(a["tg_msg"], rows)
    except Exception as ex:
        print(f"[tg] could not update job message: {ex}", file=sys.stderr)


def set_status(key: str, action: str, quiet: bool = False, from_button: bool = False):
    a = _set_status(key, action, quiet)
    if a and action not in ("channel", "why") and a.get("cv_file") and drive.enabled():
        try:      # keep the Drive library tidy: the job's folder follows its status
            b = drive.bucket_for(a.get("status"))
            if drive.move_job(a["company"], a["title"], b):
                d = load_apps()
                for x in d["applications"]:
                    if x.get("id") == a["id"]:
                        x["cv_file"] = drive.display_path(a["company"], a["title"], bucket=b)
                save_apps(d)
        except Exception as ex:
            print(f"[drive] move failed: {ex}", file=sys.stderr)
    if a and not from_button and action not in ("channel",):
        sync_job_message(a)
    return a


def _set_status(key: str, action: str, quiet: bool = False):
    if action == "channel":
        return set_channel(key)
    if action == "why":           # 'why:<reason>:<uid>' — why Maor skipped (field/far/company/reqs/later)
        reason, ref = key.split(":", 1)
        d = load_apps()
        for a in d["applications"]:
            if a.get("uid") == ref or str(a.get("id")) == ref:
                a["skip_reason"] = reason
                save_apps(d)
                log_event("skip_reason", a, reason=reason)
                return a
        return None
    if action == "undo":          # pressed "applied"/"skip" by mistake -> back to "CV ready, not applied"
        d = load_apps()
        for a in d["applications"]:
            if a.get("uid") == key or str(a.get("id")) == key:
                a.update({"status": "cv_built", "applied_date": None, "channel": None, "referrer": None,
                          "skipped_by": None, "skip_date": None, "skip_reason": None})
                save_apps(d)
                log_event("undo", a)
                return a
        return None
    d = load_apps()
    for a in d["applications"]:
        if a.get("uid") == key or str(a.get("id")) == key:
            a["status"] = "skipped" if action == "skip" else action
            if action == "skip":
                a.update({"skipped_by": "maor", "skip_date": date.today().isoformat()})
            for b in d["applications"]:      # the same job that arrived twice (another site / text + link) follows it
                if b is not a and b.get("status") in ("cv_built", "held") and \
                        (same_company(b.get("company"), a.get("company")) or not b.get("company") or
                         re.search(r"unknown|^jobs?$", b.get("company") or "", re.I)) and \
                        similar_title(b.get("title"), a.get("title")):
                    b["status"] = a["status"]
                    b["notes"] = (b.get("notes") or "") + f" · אותה משרה כמו #{a['id']}"
                    if action == "applied":
                        b["applied_date"] = a.get("applied_date")
            if action == "applied":
                a["applied_date"] = date.today().isoformat()
            a.pop("reminded", None)
            save_apps(d)
            typed = key.isdigit()          # '/status 12 interview' (typed) vs a button (uid) — buttons already answered
            if telegram.enabled() and typed and not quiet:
                telegram.send(f"📌 {e(a['company'])} | {e(a['title'])} → {STATUS_HE.get(action, action)}")
            log_event({"applied": "applied", "rejected": "rejected", "interview": "interview", "offer": "offer",
                       "skip": "skipped_by_maor"}.get(action, action), a)
            if action == "applied" and telegram.enabled() and typed and not quiet:
                ref = a.get("uid") or a["id"]
                telegram.send(f"איך הגשת ל-{e(a['company'])}?", CHANNEL_BUTTONS(ref))
            if action == "interview":
                start_interview(str(a["id"]))
            return a
    return None


def send_status_list():
    apps = [a for a in load_apps()["applications"] if a.get("status") in ("applied", "interview", "offer", "rejected")]
    if not apps:
        msg = "📋 עוד לא רשמת הגשות. כשתגיש, לחץ \"✅ הגשתי\" על המשרה או כתוב לי \"הגשתי ל-X\"."
        telegram.send(msg) if telegram.enabled() else print(msg)
        return
    groups = [("offer", "🎉 הצעות"), ("interview", "🎤 ראיונות"), ("applied", "✅ הוגשו — מחכים לתשובה"), ("rejected", "❌ נדחו")]
    act = sum(1 for a in apps if a["status"] in ("applied", "interview", "offer"))
    out = [f"📋 <b>ההגשות שלך</b> · {act} פעילות"]

    def ddmm(x):
        try:
            return date.fromisoformat(x).strftime("%d/%m")
        except Exception:
            return ""
    for st, head in groups:
        part = sorted([a for a in apps if a["status"] == st], key=lambda a: a.get("applied_date") or "", reverse=True)
        if not part:
            continue
        out.append(f"\n<b>{head} ({len(part)})</b>")
        for a in part:
            how = CHANNEL_HE.get(a.get("channel"), "") if a.get("channel") else ""
            if a.get("referrer"):
                how += f" ({a['referrer']})"
            meta = " · ".join(x for x in (f"🗓 {ddmm(a.get('applied_date'))}" if a.get("applied_date") else "", how) if x)
            out.append(f"▫️ <b>{e(a['company'])}</b>\n     {e(a['title'])}" + (f"\n     {e(meta)}" if meta else ""))
    out.append("\n💬 לעדכון פשוט כתוב לי, למשל: \"קיבלתי ראיון ל-Apple\" או \"נדחיתי מ-Marvell\".")
    text = "\n".join(out)
    telegram.send(text) if telegram.enabled() else print(text)


ALERTS = ROOT / "data" / "alerts.json"


def alert_once(key: str, text: str):
    """Send a Telegram alert at most once per day per key (e.g. an expired token)."""
    seen = json.loads(ALERTS.read_text()) if ALERTS.exists() else {}
    today = date.today().isoformat()
    if seen.get(key) == today:
        return
    seen[key] = today
    ALERTS.write_text(json.dumps(seen, indent=1))
    if telegram.enabled():
        telegram.send(text)


def check_expiry():
    f = ROOT / "data" / "expiry.json"
    if not f.exists():
        return
    for it in json.loads(f.read_text(encoding="utf-8")).get("items", []):
        left = (date.fromisoformat(it["expires"]) - date.today()).days
        if left in (30, 14, 7, 3, 1) or left <= 0:
            when = "פג היום/כבר פג" if left <= 0 else f"יפוג בעוד {left} ימים ({it['expires']})"
            alert_once(f"expiry:{it['name']}:{left}",
                       f"🔐 <b>{e(it['name'])}</b> {when}.\nאיך מחדשים: {e(it['how_he'])}")


NUDGE = ROOT / "data" / "nudge.json"
NUDGE_EVERY_HOURS = 4          # jobs Maor hasn't answered pop up again every few hours, until he answers
NUDGE_FIRST_AFTER_HOURS = 3
NUDGE_HOURS = (9, 22)          # Israel time — never at night
NUDGE_MAX = 15


def unanswered() -> list[dict]:
    """Jobs sent to Maor that he hasn't answered (no 'applied' / 'skip')."""
    now = datetime.now()
    out = []
    for a in load_apps()["applications"]:
        if a.get("status") not in ("cv_built", "held") or not a.get("uid"):
            continue
        sent = a.get("sent_at")
        if sent and (now - datetime.fromisoformat(sent)).total_seconds() < NUDGE_FIRST_AFTER_HOURS * 3600:
            continue
        if not sent and a.get("found_date") and (date.today() - date.fromisoformat(a["found_date"])).days > 30:
            continue
        out.append(a)
    return sorted(out, key=lambda a: (a.get("sent_at") or a.get("found_date") or ""), reverse=True)


def nudge(force=False):
    """One message listing the unanswered jobs, each with its own ✅/⏭️ buttons. The previous list is deleted."""
    from zoneinfo import ZoneInfo
    il = datetime.now(ZoneInfo("Asia/Jerusalem"))
    st = json.loads(NUDGE.read_text()) if NUDGE.exists() else {}
    if not force:
        if not (NUDGE_HOURS[0] <= il.hour < NUDGE_HOURS[1]):
            return
        last = st.get("last")
        if last and (datetime.now() - datetime.fromisoformat(last)).total_seconds() < NUDGE_EVERY_HOURS * 3600 - 300:
            return
    jobs = unanswered()
    if not jobs or not telegram.enabled():
        return
    if st.get("msg_id"):
        try:
            telegram._call("deleteMessage", json={"chat_id": os.environ["TELEGRAM_CHAT_ID"], "message_id": st["msg_id"]})
        except Exception:
            pass
    shown = jobs[:NUDGE_MAX]
    lines = [f"⏰ <b>עוד לא ענית על {len(jobs)} משרות</b> — אולי פספסת. לחץ על מה שמתאים:"]
    rows = []
    for i, a in enumerate(shown, 1):
        icon = {"go": "🟢", "maybe": "🟡"}.get(a.get("verdict"), "⚪")
        lines.append(f"{i}. {icon} {e(a['company'])} — {e(a['title'])}")
        if a.get("url"):
            rows.append([{"text": f"🔗 {i}. {a['company']} — {a['title']}"[:60], "url": a["url"]}])
        rows.append([{"text": f"✅ {i}. הגשתי", "callback_data": f"applied:{a['uid']}:L"},
                     {"text": f"⏭️ {i}. דלג", "callback_data": f"skip:{a['uid']}:L"}])
    if len(jobs) > NUDGE_MAX:
        lines.append(f"…ועוד {len(jobs) - NUDGE_MAX} ישנות יותר.")
    lines.append("\nאזכיר שוב בעוד כמה שעות עד שתענה. אפשר גם פשוט לכתוב לי \"הגשתי ל-X\".")
    r = telegram.send("\n".join(lines), rows)
    st = {"last": datetime.now().isoformat(timespec="minutes"),
          "msg_id": ((r or {}).get("result") or {}).get("message_id"), "count": len(jobs)}
    NUDGE.write_text(json.dumps(st, indent=1))


def remind(days=14):
    check_expiry()
    try:
        from . import learn
        learn.compute_taste()
        learn.insights()          # why no answers? kept fresh for new CVs; Maor reads it in the Saturday summary
    except Exception as ex:
        print(f"[learn] {ex}", file=sys.stderr)
    if not os.environ.get("DAILY_FOLLOWUPS"):
        return      # follow-ups are part of the Saturday-night summary
    d = load_apps()
    today = date.today()
    due = []
    for a in d["applications"]:
        if a.get("status") == "applied" and a.get("applied_date") and not a.get("reminded"):
            if (today - date.fromisoformat(a["applied_date"])).days >= days:
                due.append(a)
                a["reminded"] = today.isoformat()
    if due and telegram.enabled():
        def tip(a):
            if a.get("referrer"):
                return f"\n   ↳ שלח הודעה ל{e(a['referrer'])} ושאל אם יש עדכון"
            return "\n   ↳ אפשר לשלוח מייל קצר למגייסת / לבדוק סטטוס באתר"
        telegram.send("⏰ <b>שבועיים בלי תשובה — כדאי לעשות פולואפ:</b>\n" + "\n".join(
            f"#{a['id']} {e(a['company'])} | {e(a['title'])} (הוגש {a['applied_date']}{e(_channel_text(a))}){tip(a)}" for a in due))
    save_apps(d)


def reorg_library():
    """One-time: old deep layout (NN_Company/Role/With|Without grade sheet) -> 1_להגשה עכשיו / 2_הוגשו / 3_ארכיון."""
    root = drive.folder([])
    targets = {b: drive.folder([n]) for b, n in drive.BUCKETS.items()}
    d = load_apps()
    moved = {"todo": 0, "applied": 0, "archive": 0}
    for a in d["applications"]:
        cv = a.get("cv_file") or ""
        parts = cv.split("/")
        if len(parts) != 5 or parts[0] != drive.ROOT_NAME or parts[1] in drive.BUCKETS.values():
            continue
        fid = drive.find_path(parts[1:])
        if not fid:
            continue
        b = drive.bucket_for(a.get("status"))
        dest = drive.folder([drive.BUCKETS[b], drive.job_folder(a["company"], a["title"])])
        if drive._find(drive.CV_NAME, dest):
            continue
        drive.move(fid, dest)
        a["cv_file"] = drive.display_path(a["company"], a["title"], bucket=b)
        moved[b] += 1
    save_apps(d)
    keep = set(targets.values()) | {drive._find("_system", root, drive.FOLDER)}
    old = 0
    for f in drive.children(root):
        if f["id"] not in keep:
            drive.move(f["id"], targets["archive"], root)
            old += 1
    msg = (f"🗂 <b>סידרתי את ספריית קורות החיים ב-Drive</b>\n"
           f"📂 1_להגשה עכשיו — {moved['todo']} משרות שעוד לא הגשת\n"
           f"📂 2_הוגשו — {moved['applied']}\n"
           f"📂 3_ארכיון — {moved['archive']} משרות + {old} תיקיות מהספרייה הישנה (שום דבר לא נמחק)\n\n"
           f"💡 טיפ: באפליקציית Google Drive סמן את התיקייה <b>1_להגשה עכשיו</b> כ\"זמינה במצב לא מקוון\" — "
           f"ככה קורות החיים ייפתחו מיד גם מאפליקציית הקבצים.")
    telegram.send(msg) if telegram.enabled() else print(msg)


def _update_index(job: Job, transcript: bool):
    """Append a line to CV_Library/00_INDEX.txt in the same style as the existing index."""
    p = Path(tempfile.gettempdir()) / "00_INDEX.txt"
    if not drive.download([], "00_INDEX.txt", p):
        p.write_text("CV LIBRARY - Maor Shitrit\n", encoding="utf-8")
    line = (f"{drive.company_folder(job.company)} / {drive.safe(job.title)}: USE: "
            f"{'WITH grade sheet (required)' if transcript else 'without, unless asked'}")
    lines = p.read_text(encoding="utf-8").splitlines()
    if line not in lines:
        lines.append(line)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    drive.upload(p, [], "00_INDEX.txt", mime="text/plain")


def import_library(zip_path: Path):
    """Upload Maor's existing CV library (zip) to Drive, and save the transcripts from it (unchanged pages)."""
    import zipfile
    from pypdf import PdfReader, PdfWriter
    n = 0
    with zipfile.ZipFile(zip_path) as z, tempfile.TemporaryDirectory() as td:
        z.extractall(td)
        root = Path(td)
        if not (root / "00_INDEX.txt").exists():           # zip with a top-level folder
            sub = [d for d in root.iterdir() if d.is_dir()]
            root = sub[0] if len(sub) == 1 else root
        for f in sorted(root.rglob("*")):
            if f.is_file() and f.suffix.lower() in (".pdf", ".txt"):
                rel = f.relative_to(root).parts
                drive.upload(f, list(rel[:-1]), rel[-1], "text/plain" if f.suffix == ".txt" else "application/pdf")
                n += 1
        # transcripts = pages 2.. of a "With grade sheet" CV (English from General, Hebrew from a Hebrew CV)
        for lang, pattern in (("en", "00_General/*/With grade sheet/*.pdf"), ("he", "*/*(Hebrew)/With grade sheet/*.pdf")):
            hits = sorted(root.glob(pattern))
            if hits:
                r, w = PdfReader(str(hits[0])), PdfWriter()
                for pg in r.pages[1:]:
                    w.add_page(pg)
                out = Path(td) / f"transcript_{lang}.pdf"
                with open(out, "wb") as fh:
                    w.write(fh)
                drive.upload(out, ["_system"], out.name)
    msg = f"📚 הספרייה הועלתה ל-Drive ({n} קבצים) + גליונות הציונים נשמרו."
    telegram.send(msg) if telegram.enabled() else print(msg)


JOB_GAP_SECONDS = 6     # many jobs at once -> one every few seconds, so each one is noticed (not minutes apart)


def _pace(outcome: dict):
    if outcome.get("notified") and telegram.enabled():
        import time
        time.sleep(JOB_GAP_SECONDS)


# ---------------------------------------------------------------- CLI
def main(argv=None):
    argv = argv or sys.argv[1:]
    cmd = argv[0] if argv else "scan"
    if cmd in ("scan", "jobs", "status"):
        try:
            from .learn import compute_taste
            compute_taste()
        except Exception as ex:
            print(f"[learn] taste: {ex}", file=sys.stderr)
    if cmd == "scan":
        from .scan import run
        if len(argv) > 1 and argv[1] == "manual":
            os.environ["LINKEDIN_WINDOW"] = "r2592000"       # a manual /scan resends everything open (last month)
        results, health = run(only_new=True)
        for k, v in health.items():
            print(f"[scan] {k}: {v}", file=sys.stderr)
        print(f"[scan] student jobs to process: {len(results)}", file=sys.stderr)
        manual = len(argv) > 1 and argv[1] == "manual"
        outcomes = []
        broken = [k for k, v in health.items() if not v.startswith("ok")]
        for r in results:
            job = Job.from_dict(r["job"])
            try:          # one failing job (network hiccup) must not stop the rest of the scan
                outcomes.append(process(job, _source_cfg(job.company, job.source)) or {})
            except Exception as ex:
                print(f"[scan] {job.company} | {job.title}: {type(ex).__name__}: {ex}", file=sys.stderr)
                outcomes.append({})
            _pace(outcomes[-1])
        if manual and telegram.enabled():
            sent = sum(1 for o in outcomes if o.get("notified"))
            reposts = sum(1 for o in outcomes if o.get("repost"))
            ok = sum(1 for v in health.values() if v.startswith("ok"))
            msg = (f"✅ <b>הסריקה הסתיימה</b>\nנבדקו {len(results)} משרות סטודנט ב-{ok} מקורות.\n"
                   + (f"📬 {sent} משרות חדשות נשלחו אליך למעלה." if sent else "אין משרות חדשות מאז הסריקה הקודמת — הכל כבר אצלך.")
                   + (f"\n🔁 {reposts} משרות שכבר הגשת אליהן פורסמו שוב (יופיעו בסיכום השבועי)." if reposts else ""))
            failed = [k for k, v in health.items() if not v.startswith("ok")]
            if failed:
                msg += "\n⚠️ לא הצלחתי לסרוק: " + ", ".join(failed)
            telegram.send(msg)
        if not manual:
            nudge()
        if broken and telegram.enabled() and os.environ.get("REPORT_BROKEN") == "1":
            telegram.send("🛠 מקורות שנכשלו בסריקה: " + ", ".join(broken), silent=True)
    elif cmd == "jobs":
        for d in json.loads(Path(argv[1]).read_text()):
            job = Job.from_dict(d)
            if job.source in ("linkedin", "telegram", "drushim", "comeet"):   # use the source's reader, not the company's
                base = next((c for c in json.loads((ROOT / "data" / "companies.json").read_text(encoding="utf-8"))["sources"]
                             if c["type"] == job.source and (job.source != "comeet" or c["company"] == job.company)), {})
                cfg = {**base, "company": job.company or base.get("company", "")}
            else:
                cfg = _source_cfg(job.company)
            if not job.description and cfg.get("type") in ADAPTERS:
                try:
                    ADAPTERS[cfg["type"]].fetch_details(job, cfg)
                except Exception:
                    pass
            if not job.description:   # watcher sends titles only for some sources -> look the job up in the full listing
                full = _match_known_source(job.url)
                if full and full.description:
                    job.description, job.location = full.description, full.location or job.location
                    job.company = full.company or job.company
            try:
                _pace(process(job, cfg) or {})
            except Exception as ex:
                print(f"[jobs] {job.company} | {job.title}: {type(ex).__name__}: {ex}", file=sys.stderr)
    elif cmd == "url":
        job, err = job_from_url(argv[1])
        if not job:
            msg = f"🔗 {e(err)}\nשלח לי את טקסט המשרה (העתק-הדבק) או צילום מסך, ואמשיך משם."
            telegram.send(msg) if telegram.enabled() else print(msg)
            return
        process(job, manual=True)
    elif cmd == "text":
        text = Path(argv[1]).read_text(encoding="utf-8")
        url = re.search(r"https?://\S+", text)
        first = next((l.strip() for l in text.splitlines() if l.strip()), "")
        job = Job(company=os.environ.get("JOB_COMPANY", ""), title=first[:120], url=url.group(0) if url else "",
                  source="manual", description=text, apply_url=url.group(0) if url else "",
                  req_id="txt-" + hashlib.sha1(text.encode()).hexdigest()[:10])
        process(job, manual=True)
    elif cmd == "image":
        ref = argv[1]
        path = Path(ref) if Path(ref).exists() else telegram.download_file(ref, Path(tempfile.gettempdir()) / "job_image.jpg")
        chat_turn("", [path])
        return
        from .analyze import answer_form
        form = answer_form(path) or {}
        print(f"[image] classified as: {form.get('type')}", file=sys.stderr)
        if form.get("type") == "form":
            send_form_answers(form)
        elif form.get("type") in ("applied", "rejected"):
            record_from_screenshot(form)
        elif form.get("type") == "other":
            msg = form.get("reply_he") or "זה לא נראה כמו משרה או שאלון 🙂"
            telegram.send(msg) if telegram.enabled() else print(msg)
        elif not form and not os.environ.get("GEMINI_API_KEY") and not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"):
            msg = "⚠️ ה-AI לא מוגדר, אז אני לא יכול לקרוא צילומי מסך. שלח קישור או טקסט של המשרה."
            telegram.send(msg) if telegram.enabled() else print(msg)
        elif not form:
            msg = "⚠️ לא הצלחתי לקרוא את התמונה כרגע. נסה שוב בעוד דקה, או שלח קישור/טקסט."
            telegram.send(msg) if telegram.enabled() else print(msg)
        else:
            job = Job(company="", title="", url="", source="screenshot", req_id=f"img-{int(__import__('time').time())}")
            process(job, image=path, manual=True)
    elif cmd == "status":            # "applied:<uid>" | "skip:<uid>" | "interview:<id>" ...
        action, key = argv[1].split(":", 1)
        from_list = key.endswith(":L")
        key = key[:-2] if from_list else key
        set_status(key, action, from_button=not key.isdigit() and not from_list)   # buttons already updated their own message
    elif cmd == "transcript":        # transcript <en|he> <telegram_file_id>
        refs = json.loads(REFS.read_text()) if REFS.exists() else {}
        refs[f"transcript_{argv[1]}"] = argv[2]
        REFS.write_text(json.dumps(refs, indent=1))
        if telegram.enabled():
            telegram.send(f"✅ גליון הציונים ({'אנגלית' if argv[1] == 'en' else 'עברית'}) נשמר. "
                          "הוא יצורף כמו שהוא, בלי שינוי, למשרות שדורשות גליון.")
    elif cmd == "library":           # library <telegram_file_id|zip path>
        ref = argv[1]
        zp = Path(ref) if Path(ref).exists() else telegram.download_file(ref, Path(tempfile.gettempdir()) / "lib.zip")
        import_library(zp)
    elif cmd == "interview":         # interview <free text | id>
        start_interview(" ".join(argv[1:]))
    elif cmd == "branding":          # branding <display name>
        name = " ".join(argv[1:]).strip() or "ChipHunter"
        for method, body in (
            ("setMyName", {"name": name}),
            ("setMyShortDescription", {"short_description": "סוכן משרות סטודנט בחומרה: מוצא משרות, בודק התאמה, בונה קו\"ח ומכין לראיון."}),
            ("setMyDescription", {"description": "שלח לי קישור, טקסט או צילום מסך של משרה — ואחזיר המלצה + קו\"ח מותאם.\n"
                                                  "צילום של שאלון הגשה — ואחזיר תשובות.\nקיבלת ראיון? כתוב: קיבלתי ראיון ל-<חברה>."}),
            ("setMyCommands", {"commands": [{"command": "list", "description": "הגשות פעילות"},
                                            {"command": "scan", "description": "סריקת משרות עכשיו"},
                                            {"command": "status", "description": "עדכון סטטוס: /status 12 interview"}]})):
            r = telegram._call(method, json=body)
            print(method, r.get("ok"), r.get("description", ""))
        telegram.send(f"✨ הבוט עודכן: <b>{e(name)}</b>")
    elif cmd == "referrer":          # referrer "<id> <name>"
        set_referrer(" ".join(argv[1:]))
    elif cmd == "chat":              # chat <free text / transcribed voice>
        chat_turn(" ".join(argv[1:]))
    elif cmd == "weekly":            # weekly [force]
        weekly_summary(force=len(argv) > 1 and argv[1] == "force")
    elif cmd == "act":               # actions decided by the Worker's fast chat
        act_turn(argv[1])
    elif cmd == "health":            # one message: is every connection alive?
        from . import llm
        checks = {"טלגרם": telegram.enabled()}
        try:
            checks["Google Drive"] = drive.enabled() and drive.auth_ok()
        except Exception:
            checks["Google Drive"] = False
        try:
            checks["AI (Gemini)"] = bool(llm.run('Write ok.json with {"ok": true}', {}, "ok.json", timeout=60))
        except Exception:
            checks["AI (Gemini)"] = False
        checks["נתונים פרטיים"] = (ROOT / "data" / "applications.json").exists()
        msg = "🩺 <b>בדיקת מערכת</b>\n" + "\n".join(f"{'✅' if v else '❌'} {k}" for k, v in checks.items())
        telegram.send(msg) if telegram.enabled() else print(msg)
        print({k: bool(v) for k, v in checks.items()})
    elif cmd == "general_cv":        # general_cv en|he|both
        for lg in (["en", "he"] if len(argv) < 2 or argv[1] in ("both", "-") else [argv[1]]):
            send_general_cv(lg)
    elif cmd == "reorg":
        reorg_library()
    elif cmd == "wprobe":
        import subprocess
        sys.stdout.flush()
        subprocess.run(["node", str(ROOT / "tools" / "worker_probe.mjs"), *(argv[1].split("||") if len(argv) > 1 else [])])
    elif cmd == "probe":
        import runpy
        runpy.run_path(str(ROOT / "tools" / "probe.py"), run_name="__main__")
    elif cmd == "discover":          # find more companies with public job APIs (prints hits to the log)
        import runpy
        runpy.run_path(str(ROOT / "tools" / "discover.py"), run_name="__main__")
    elif cmd == "list":
        send_status_list()
    elif cmd == "nudge":
        nudge(force=len(argv) > 1 and argv[1] == "force")
    elif cmd == "remind":
        remind()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
