"""Rule-based evaluation: hard requirements first, then fit. Works in Hebrew and English.

Result verdict:
  "go"    – passes hard requirements and is in a relevant field
  "maybe" – passes, but a warning (low fit, borderline requirement, car-only location)
  "skip"  – a hard requirement fails (never notify, never build a CV)
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from .location import classify, describe_he
from .models import Job

ROOT = Path(__file__).resolve().parent.parent
PROFILE = json.loads((ROOT / "profile" / "profile.json").read_text(encoding="utf-8"))


def _rx(*alts):
    return re.compile("|".join(alts), re.I)


# ---------- student / degree ----------
STUDENT = _rx(r"\bstudents?\b", r"\bintern(ship)?s?\b", r"סטודנט", r"משרת סטודנט", r"\bco-?op\b")
NOT_STUDENT_TITLE = _rx(r"\bnew grad", r"\bgraduate (engineer|program)", r"\bjunior\b", r"בוגר", r"\bsenior\b",
                        r"\bmanager\b", r"\blead\b", r"\bstaff\b", r"\bprincipal\b")
PRACTICAL_ENG = _rx(r"practical engineer", r"הנדסאי")
ADV_DEGREE_TITLE = _rx(r"\bph\.?\s?d\b", r"\bm\.?\s?sc\b", r"\bmaster'?s\b", r"\bdoctoral\b", r"תואר שני", r"דוקטורט")
BSC_MENTION = _rx(r"\bb\.?\s?sc\b", r"\bbachelor", r"\bbs/ms\b", r"\bundergrad", r"תואר ראשון", r"\bb\.?s\.?\b")
ADV_REQUIRED = _rx(r"(m\.?\s?sc|master'?s|ph\.?\s?d)\s*(student|candidate)s?\s*(only|required)",
                   r"(pursuing|enrolled in)\s+(an?\s+)?(m\.?\s?sc|master'?s|ph\.?\s?d)",
                   r"סטודנט(ית)?\s*(ל)?תואר שני", r"לימודי תואר שני", r"(advanced|graduate) degree students")

# ---------- degree status ----------
COMPLETED_DEGREE = _rx(r"(hold(s|ing)?|completed|graduated? (with|from)|possess(es|ing)?)\s+(an?\s+)?(b\.?\s?sc|bachelor)",
                       r"(b\.?\s?sc|bachelor'?s?)( degree)?\s*(\(|-|–)?\s*(completed|required|graduate)",
                       r"(b\.?\s?sc\.?|bachelor'?s?)[^.\n]{0,60}?\b(completed|graduated)\b",
                       r"\bgraduates? (of|with)\b", r"recent graduates?", r"בעל(ת|/ת|\.ת)?\s+תואר", r"בוגר(ת|/ת|\.ת)?\s+תואר",
                       r"תואר ראשון (חובה|גמור|מלא)", r"סיים(ה)? תואר")
STUDENT_REQ = _rx(r"(currently|actively)\s+(pursuing|enrolled|studying)", r"\b(b\.?\s?sc|bachelor'?s?|undergrad\w*)\s+students?\b",
                  r"students?\s+(in|of|for|pursuing)\b", r"\d(st|nd|rd|th)[- ]year", r"סטודנט(ית|/ית|\.ית)?\s+(ל|ב|שנה)",
                  r"semesters?\s+(left|remaining)", r"סמסטרים")
FULL_TIME = _rx(r"full[- ]time", r"\binternship\b", r"משרה מלאה", r"התמחות")

# ---------- out-of-scope roles ----------
SW_DEV_TITLE = _rx(r"software", r"\bsw\b", r"firmware", r"embedded", r"developer", r"\bdev\b", r"programmer",
                   r"support", r"help ?desk", r"\bit\b", r"devops", r"cloud", r"cyber", r"security", r"\bnoc\b",
                   r"תמיכה", r"סייבר", r"מערכות מידע",
                   r"automation", r"\bml\b", r"machine learning", r"data scien", r"\bai\b", r"solutions architect",
                   r"applied scientist", r"תוכנה", r"מפתח", r"משובץ", r"אמבדד",
                   r"full ?-?stack", r"front ?-?end", r"back ?-?end", r"web\b", r"mobile", r"android", r"\bios\b")
HW_TITLE = _rx(r"hardware", r"\bhw\b", r"chip", r"silicon", r"validation", r"verification", r"\bdv\b", r"fpga", r"asic",
               r"vlsi", r"rtl", r"design", r"analog", r"board", r"חומרה", r"שבב", r"ולידציה", r"וריפיקציה",
               r"\brf\b", r"\bphy\b", r"electronic", r"electrical", r"אלקטרוני", r"חשמל", r"\bemc\b", r"power", r"circuit")
# the job is clearly hardware even when the text never says "electrical engineering" (short Comeet / Drushim posts)
HW_TEXT = _rx(r"verilog", r"\brtl\b", r"\buvm\b", r"system ?verilog", r"\bfpga\b", r"\basic\b", r"\bsoc\b", r"silicon",
              r"oscilloscope", r"אוסצילוסקופ", r"\bpcb\b", r"schematic", r"digital design", r"logic design", r"\bvlsi\b",
              r"post.?silicon", r"lab equipment", r"spectrum analy", r"signal integrity", r"\brf\b")

# ---------- field of study ----------
EE = _rx(r"electrical", r"electronics?", r"\bee\b", r"computer engineering", r"\bece\b", r"hardware",
         r"הנדסת חשמל", r"חשמל", r"אלקטרוניקה", r"הנדסת מחשבים", r"vlsi")
OTHER_FIELD_ONLY = {
    "physics": _rx(r"\bphysics\b", r"פיזיקה"),
    "mechanical": _rx(r"mechanical", r"מכונות"),
    "chemical/materials": _rx(r"chemical", r"chemistry", r"materials", r"כימי", r"חומרים"),
    "computer science": _rx(r"computer science", r"מדעי המחשב", r"\bcs\b"),
    "industrial": _rx(r"industrial engineering", r"תעשייה וניהול", r"quality engineer", r"הנדסת איכות", r"\bqa\b"),
    "business/marketing/legal/finance": _rx(r"\blegal\b", r"marketing", r"business", r"finance", r"accounting",
                                            r"\bhr\b", r"משפט", r"שיווק", r"מנהל עסקים", r"כספים", r"רכש",
                                            r"planner", r"procurement", r"logistic", r"פיננס", r"כלכל", r"פלנר"),
    "not engineering": _rx(r"אחזקה", r"תחזוק", r"מעונות", r"מאבטח", r"אבטחה", r"נציג", r"מכירות", r"שירות לקוחות",
                           r"מוקד", r"בק ?אופיס", r"back ?office", r"\bsales\b", r"customer service", r"קופאי",
                           r"מלצר", r"שליח", r"מחסנ", r"לבורנט", r"יועצ", r"דקנט", r"גיוס", r"recruit"),
    "aero/civil/bio": _rx(r"aerodynamic", r"aeromechanic", r"aerospace engineering", r"\bndi\b", r"civil engineering",
                          r"biomedical", r"אווירונאוטיקה", r"אזרחית", r"ביו-?רפואית"),
}

# ---------- domains (fit) ----------
DOMAINS = [  # (key, Hebrew label, weight, regex)
    ("digital_design", "דיגיטל דיזיין / RTL", 10, _rx(r"digital (ic )?design", r"\brtl\b", r"logic design", r"\basic\b", r"\bsoc\b",
                                                      r"chip design", r"micro-?architecture", r"\bcpu\b", r"תכנון לוגי")),
    ("fpga", "FPGA", 10, _rx(r"\bfpga", r"vivado", r"xilinx")),
    ("verification", "וריפיקציה", 8, _rx(r"verification", r"\bdv\b", r"וריפיקציה")),
    ("validation", "ולידציה / Post-Silicon", 9, _rx(r"validation", r"post[- ]?si", r"bring[- ]?up", r"ולידציה")),
    ("vlsi_pd", "VLSI / Physical Design / DFT / CAD", 8, _rx(r"vlsi", r"physical design", r"\bdft\b", r"\bcad\b", r"timing", r"\bsta\b")),
    ("hw_board", "חומרה / כרטיסים", 7, _rx(r"hardware", r"board design", r"\bpcb\b", r"schematic", r"חומרה", r"כרטיס")),
    ("analog", "אנלוגי / Mixed-Signal / SI-PI", 5, _rx(r"analog", r"mixed[- ]signal", r"signal integrity", r"power integrity",
                                                       r"serdes", r"\brf\b", r"אנלוגי")),
    ("embedded", "Embedded / Firmware", 5, _rx(r"embedded", r"firmware", r"משובץ")),
    ("test_lab", "בדיקות / מעבדה", 4, _rx(r"\btest\b", r"\blab\b", r"מעבדה", r"בדיקות", r"calibration", r"כיול")),
    ("systems", "הנדסת מערכת / אלגוריתמים", 3, _rx(r"system(s)? engineer", r"algorithm", r"\bdsp\b", r"הנדסת מערכת", r"אלגוריתמ")),
    ("power", "חשמל / אנרגיה / הספק", 3, _rx(r"power electronics", r"\bpower\b", r"הספק", r"אנרגיה")),
    ("electro_optics", "אלקטרו-אופטיקה / פוטוניקה", 2, _rx(r"electro[- ]?optic", r"photonic", r"optical", r"אלקטרו-?אופטי")),
    ("software", "תוכנה", 1, _rx(r"software", r"תוכנה", r"\bml\b", r"machine learning", r"data")),
]

# ---------- hard requirement parsers ----------
WORDNUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
           "אחת": 1, "אחד": 1, "שתיים": 2, "שניים": 2, "שנתיים": 2, "שלוש": 3, "שלושה": 3, "ארבע": 4, "ארבעה": 4, "חמישה": 5}
NUM = r"(\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight|אחת|אחד|שתיים|שניים|שלוש|שלושה|ארבע|ארבעה|חמישה)"


def _n(s):
    s = s.lower()
    return float(WORDNUM.get(s, s)) if s in WORDNUM or re.fullmatch(r"\d+(\.\d+)?", s) else None


DAYS = [re.compile(p, re.I) for p in [
    NUM + r"\s*(?:-|to|–)?\s*(?:\d\s*)?(?:full\s+)?(?:work(?:ing)?\s+)?days?\s*(?:a|per|/|in a)\s*week",
    r"at least\s+" + NUM + r"\s*(?:full\s+)?(?:work(?:ing)?\s+)?days",
    r"(?:לפחות|מינימום)\s*" + NUM + r"\s*ימי",
    NUM + r"\s*ימים?\s*(?:עבודה\s*)?ב?שבוע",
    NUM + r"\s*ימי עבודה",
]]
SEMESTERS_LEFT = [re.compile(p, re.I) for p in [
    r"(?:at least|minimum(?: of)?)\s+" + NUM + r"\s+semesters?\s+(?:left|remaining|until|before)",
    r"(?:at least|minimum(?: of)?)\s+" + NUM + r"\s+(?:academic\s+)?years?\s+(?:of studies\s+)?(?:left|remaining|until|before)",
    r"(?:לפחות|מינימום)\s*" + NUM + r"\s*סמסטרים?\s*(?:ל|עד)",
    r"(?:לפחות|מינימום)\s*" + NUM + r"\s*שנ(?:ים|ות|ה)\s*(?:לימוד\s*)?(?:ל|עד)",
    r"(?:לפחות|מינימום)\s*(שנתיים|שנה)\s*(?:לימוד\s*)?(?:ל|עד)",
]]
SEMESTERS_MAX = [re.compile(p, re.I) for p in [
    r"(?:up to|no more than|maximum(?: of)?|max\.?)\s+" + NUM + r"\s+semesters?\s+(?:left|remaining)",
    r"(?:up to|no more than|maximum(?: of)?)\s+" + NUM + r"\s+years?\s+(?:left|remaining)",
    r"(?:last|final)\s+year\s+(?:of\s+)?(?:b\.?sc|studies|students?)",
    r"(?:עד|לכל היותר|מקסימום)\s*" + NUM + r"\s*סמסטרים",
    r"שנה אחרונה",
]]
YEAR_OF_STUDY = re.compile(r"(\d)(?:st|nd|rd|th)(?:\s*(?:or|/|-|–)\s*(\d)(?:st|nd|rd|th))?[- ]year|שנה\s*([א-ד])['׳](?!\w)", re.I)
YEAR_MIN_CTX = _rx(r"at least\s*$", r"minimum\s*$", r"^\S*\s*(?:year)?\s*(?:students?\s*)?(?:or|and)\s+(?:above|higher|up)", r"^\s*(?:\+|ומעלה|ואילך)", r"^\s*year\s*\+")
GPA = [re.compile(p, re.I) for p in [
    r"(?:gpa|average|grade average|ממוצע)[^\d\n]{0,25}(\d{2})\s*\+?",
    r"(\d{2})\s*\+?\s*(?:gpa|average)",
]]
TRANSCRIPT = _rx(r"grade ?sheet", r"\btranscripts?\b", r"grades? (sheet|report)", r"גיליון ציונים", r"גליון ציונים")


def _semesters_remaining(today: date | None = None) -> float:
    """Academic semesters left including the current one (winter Oct-Feb, spring Mar-Jul).
    Nov 2026 with graduation Aug 2028 -> winter26, spring27, winter27, spring28 = 4."""
    today = today or date.today()
    gy, gm = map(int, PROFILE["education"]["graduation"].split("-"))
    # semester index: year*2 + (0 winter starting Oct of `year`, 1 spring of year+1)
    def idx(y, m):
        return (y * 2) if m >= 10 else ((y - 1) * 2 + 1) if m >= 3 else ((y - 1) * 2)
    last = idx(gy, min(gm, 7)) if gm >= 3 else idx(gy, gm)
    return float(max(last - idx(today.year, today.month) + 1, 0))


def _years_remaining(today=None) -> float:
    return round(_semesters_remaining(today) / 2, 2)


def evaluate(job: Job, company_cfg: dict | None = None, today: date | None = None) -> dict:
    title, text = job.title or "", job.description or ""
    full = f"{title}\n{text}"
    reasons_fail, warnings, notes = [], [], []

    # 1. student role?
    is_student = bool(STUDENT.search(title)) or (bool(STUDENT.search(text[:1500])) and not NOT_STUDENT_TITLE.search(title))
    if not is_student:
        reasons_fail.append("לא משרת סטודנט")
    if NOT_STUDENT_TITLE.search(title) and not STUDENT.search(title):
        reasons_fail.append("משרה לבוגרים / בכירים")
    if PRACTICAL_ENG.search(title):
        reasons_fail.append("מיועדת להנדסאים / טכנאים, לא לתואר ראשון")

    # 2. degree level
    if ADV_DEGREE_TITLE.search(title) and not BSC_MENTION.search(title):
        reasons_fail.append("דורשת תואר שני / דוקטורט")
    elif ADV_REQUIRED.search(full) and not BSC_MENTION.search(full):
        reasons_fail.append("דורשת סטודנט לתואר שני")

    # 2a. a COMPLETED degree is required (he is still a B.Sc. student) -> hard fail
    if COMPLETED_DEGREE.search(full) and not STUDENT_REQ.search(full):
        reasons_fail.append("דורשת תואר ראשון גמור (אתה עדיין סטודנט)")
    if FULL_TIME.search(full):
        notes.append("משרה מלאה / התמחות")

    # 2b. software / embedded development roles are out of scope (Maor's choice), unless the title is hardware-side
    if SW_DEV_TITLE.search(title) and not HW_TITLE.search(title):
        reasons_fail.append("תוכנה / Embedded / תמיכה / IT — לא התחום שאתה מחפש")

    # 3. field of study
    ee = bool(EE.search(full))
    for field, rx in OTHER_FIELD_ONLY.items():
        if rx.search(title) and not EE.search(title):
            if field == "computer science" and ee:
                warnings.append("מיועדת בעיקר למדעי המחשב")
            else:
                reasons_fail.append(f"תחום לא מתאים ({field})")
            break
    if not ee and not HW_TITLE.search(title) and not HW_TEXT.search(text):
        reasons_fail.append("לא מוזכרת הנדסת חשמל / אלקטרוניקה / חומרה")

    # 4. time until graduation
    sem_left, yrs_left = _semesters_remaining(today), _years_remaining(today)
    for rx in SEMESTERS_LEFT:
        m = rx.search(full)
        if m:
            raw = m.group(1)
            n = 2 if raw in ("שנתיים",) else 1 if raw == "שנה" else _n(raw)
            if n is None:
                continue
            needed_sem = n if "semester" in m.group(0).lower() or "סמסטר" in m.group(0) else n * 2
            if sem_left < needed_sem:
                reasons_fail.append(f"דורשת לפחות {needed_sem:g} סמסטרים לסיום (נשארו לך כ-{sem_left:g})")
            elif sem_left - needed_sem < 1:
                warnings.append(f"גבולי: דורשת {needed_sem:g} סמסטרים לסיום, נשארו לך כ-{sem_left:g}")
            notes.append(m.group(0).strip())
            break
    for rx in SEMESTERS_MAX:
        m = rx.search(full)
        if m:
            g = m.group(1) if m.groups() else None
            n = _n(g) if g else None
            if n is None:  # "last year" / "שנה אחרונה"
                if yrs_left > 1.0:
                    warnings.append("מיועדת לסטודנטים בשנה האחרונה (אתה בשנה ג׳) — אפשר לנסות")
            else:
                max_sem = n if "semester" in m.group(0).lower() or "סמסטר" in m.group(0) else n * 2
                if sem_left > max_sem + 0.5:
                    warnings.append(f"מעדיפים מקסימום {max_sem:g} סמסטרים לסיום (נשארו לך כ-{sem_left:g}) — אפשר לנסות")
                elif sem_left > max_sem:
                    warnings.append(f"גבולי: מקסימום {max_sem:g} סמסטרים לסיום, נשארו לך כ-{sem_left:g}")
            notes.append(m.group(0).strip())
            break
    m = YEAR_OF_STUDY.search(full)
    if m:
        heb = {"א": 1, "ב": 2, "ג": 3, "ד": 4}
        years = {int(x) for x in (m.group(1), m.group(2)) if x} or ({heb[m.group(3)]} if m.group(3) else set())
        before, after = full[max(0, m.start() - 15):m.start()], full[m.end():m.end() + 25]
        is_min = bool(YEAR_MIN_CTX.search(before) or YEAR_MIN_CTX.search(after))
        mine = PROFILE["education"]["year_of_study"]
        if years:
            if is_min and min(years) > mine:
                warnings.append(f"מיועדת לשנה {min(years)} ומעלה (אתה בשנה ג') — אפשר לנסות")
            elif not is_min and mine not in years and max(years) < 5:
                warnings.append(f"מיועדת לשנה {'/'.join(map(str, sorted(years)))} (אתה בשנה ג')")
            notes.append(m.group(0).strip())

    # 5. days per week
    max_days = PROFILE["availability"]["max_days_per_week_during_studies"]
    for rx in DAYS:
        m = rx.search(full)
        if m:
            n = _n(m.group(1))
            if n and 1 <= n <= 6:
                notes.append(m.group(0).strip())
                if n >= 4:
                    warnings.append(f"דורשת {n:g} ימים בשבוע — היקף גדול (אמרת שאתה גמיש)")
                break

    # 6. GPA
    for rx in GPA:
        m = rx.search(full)
        if m:
            req = int(m.group(1))
            if 70 <= req <= 100:
                notes.append(m.group(0).strip())
                my = PROFILE["education"]["gpa"]
                if req > my + 3:
                    reasons_fail.append(f"דורשת ממוצע {req}+ (שלך {my})")
                elif req > my:
                    warnings.append(f"דורשת ממוצע {req}+ (שלך {my}) — סיכוי נמוך")
                break

    # 7. location
    loc = classify(f"{job.location} {title}" if job.location else f"{title} {text[:800]}")
    if loc["tier"] == "far":
        reasons_fail.append(f"מיקום רחוק ({loc['city']})")
    elif loc["tier"] == "bus_long":
        warnings.append("אין רכבת, באוטובוס מעל שעה וחצי")

    # 8. fit score
    domains = []
    score = 0
    title_core = False
    for key, label, w, rx in DOMAINS:
        if rx.search(title):
            domains.append(label); score += w * 2
            title_core = title_core or w >= 7
        elif rx.search(text):
            score += w * 0.5
            if w >= 7:
                domains.append(label)
    domains = list(dict.fromkeys(domains))
    if not domains:
        warnings.append("לא זוהה תחום חומרה")

    # 9. language + transcript
    company = job.company
    he_chars = len(re.findall(r"[֐-׿]", full))
    lang = "he" if company in PROFILE["hebrew_cv_companies"] or he_chars > len(full) * 0.3 else "en"
    if company_cfg and company_cfg.get("lang") == "he":
        lang = "he"
    transcript = company in PROFILE["transcript_required_companies"] or bool(TRANSCRIPT.search(full))
    for r in PROFILE["transcript_required_title_rules"]:
        if company == r["company"] and any(t.lower() in title.lower() for t in r["title_contains"]):
            transcript = True

    # verdict
    if reasons_fail:
        verdict = "skip"
    elif ee and (title_core or score >= 24) and not any(w.startswith("דורשת ממוצע") for w in warnings):
        verdict = "go"
    else:
        verdict = "maybe"
    chance = "נמוך" if verdict == "skip" else ("גבוה" if score >= 30 and not warnings else
                                                 "בינוני" if score >= 14 else "נמוך")
    # notify only when worth a look: go, or maybe with an EE mention and some hardware signal
    notify = verdict in ("go", "maybe")    # Maor decides; only hard-requirement failures are held back
    return {
        "verdict": verdict,
        "notify": notify,
        "verdict_he": {"go": "כדאי להגיש", "maybe": "אפשר להגיש (עם הסתייגות)", "skip": "לא כדאי"}[verdict],
        "chance_he": chance,
        "fail_reasons": reasons_fail,
        "warnings": warnings,
        "requirement_snippets": notes,
        "domains": domains,
        "score": round(score, 1),
        "location": loc,
        "location_he": describe_he(loc),
        "cv_lang": lang,
        "transcript": transcript,
        "semesters_left": sem_left,
    }
