"""One-page CV builder (EN / HE) from approved content only.

- Text comes ONLY from profile/cv_content.json (by id). Nothing is generated.
- Rendered HTML -> PDF with headless Chromium, font shrinks until it fits one A4 page.
- Safety checks after rendering: exactly 1 page, no forbidden words (SystemVerilog, UVM, Linux, VHDL, C++).
- Optional: append the transcript PDF unchanged (never edited).
"""
from __future__ import annotations

import html
import json
import os
import re
from pathlib import Path

from pypdf import PdfReader, PdfWriter

ROOT = Path(__file__).resolve().parents[2]
CONTENT = json.loads((ROOT / "profile" / "cv_content.json").read_text(encoding="utf-8"))
PROFILE = json.loads((ROOT / "profile" / "profile.json").read_text(encoding="utf-8"))
FILENAME = "Maor Shitrit CV.pdf"


class CVSafetyError(Exception):
    pass


def _contact() -> dict:
    """Personal contact details: env (secrets), or contact.json in the PRIVATE data repo (profile/) / local private/."""
    f = next((p for p in (ROOT / "profile" / "contact.json", ROOT / "private" / "contact.json") if p.exists()),
             ROOT / "private" / "contact.json")
    d = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    return {k: os.environ.get(f"CV_{k.upper()}", d.get(k, "")) for k in ("phone", "email", "linkedin")}


def _rank(item, domains):
    tags = item.get("tags", ["*"])
    hits = [domains.index(t) for t in tags if t in domains]
    return min(hits) if hits else (50 if "*" in tags else 100)


def _pick(items, domains, order=None, limit=None):
    if order:
        by = {i["id"]: i for i in items}
        out = [by[i] for i in order if i in by]
    else:
        out = sorted(items, key=lambda i: _rank(i, domains))
    return out[:limit] if limit else out


def choose_profile(lang, domains, profile_id=None):
    allow_cpp = PROFILE.get("allow_cpp", False)
    cands = [p for p in CONTENT["profiles"] if p["lang"] == lang and (allow_cpp or not p["cpp"])]
    if not cands:  # no Hebrew variant fits -> general Hebrew
        cands = [p for p in CONTENT["profiles"] if p["lang"] == lang]
    if profile_id:
        for p in cands:
            if p["id"] == profile_id:
                return p
    return min(cands, key=lambda p: (_rank(p, domains), 0 if "general" in p["id"] else 1))


def _esc(s):
    return html.escape(s or "")


def render_html(lang: str, domains: list[str], selection: dict | None = None, font_pt: float = 9.6) -> str:
    sel = selection or {}
    he = lang == "he"
    allow_cpp = PROFILE.get("allow_cpp", False)
    T = (lambda en, hb: hb if he else en)
    c = _contact()
    link = f'<a href="{_esc(c["linkedin"])}">LinkedIn</a>' if c["linkedin"] else ""
    contact = " | ".join(x for x in [_esc(c["phone"]), _esc(c["email"]), link] if x)
    name = PROFILE["name_he"] if he else PROFILE["name_en"].upper()
    prof = choose_profile(lang, domains, sel.get("profile"))
    ed = CONTENT["education"]
    courses = _pick(CONTENT["coursework"], domains, sel.get("coursework"), limit=sel.get("course_limit", 5))
    skills = _pick(CONTENT["skills"], domains, sel.get("skills"))

    def skill_text(sk):
        if not allow_cpp and f"{lang}_no_cpp" in sk:
            return sk[f"{lang}_no_cpp"]
        return sk[lang]

    L = []
    L.append(f'<div class="name">{_esc(name)}</div>')
    L.append(f'<div class="headline">{_esc(CONTENT["headline"][lang])}</div>')
    L.append(f'<div class="contact">{contact}</div>')
    L.append(f'<h2>{T("Profile", "פרופיל")}</h2><p>{_esc(prof["text"])}</p>')
    L.append(f'<h2>{T("Education", "השכלה")}</h2>')
    L.append(f'<div class="b">{_esc(ed["degree_" + lang])}</div>'
             f'<div><b>{_esc(ed["school_" + lang])}</b> <span class="muted">{_esc(ed["years_" + lang])}</span></div><ul>')
    L.append(f'<li>{_esc(ed["gpa_" + lang])}</li>')
    L.append(f'<li>{T("Relevant Coursework", "קורסים רלוונטיים")}: {_esc(", ".join(x[lang] for x in courses))}.</li>')
    if not CONTENT["registered"].get("hide"):
        L.append(f'<li>{_esc(CONTENT["registered"][lang])}</li>')
    lab = CONTENT["lab"]
    L.append(f'<li>{_esc(lab["title_" + lang])}<ul class="sub">' +
             "".join(f"<li>{_esc(i[lang])}</li>" for i in lab["items"]) + "</ul></li>")
    L.append(f'<li>{_esc(CONTENT["independent_study"][lang])}</li></ul>')
    L.append(f'<h2>{T("Technical Skills", "מיומנויות טכניות")}</h2><ul>')
    L += [f'<li><b>{_esc(sk["label_" + lang])}:</b> {_esc(skill_text(sk))}</li>' for sk in skills]
    L.append("</ul>")
    proj_items = []
    fp = CONTENT.get("fpga_project") if PROFILE.get("show_fpga_project", True) else None
    if fp:
        week = PROFILE.get("project_week_done", 0)
        stage = [st for st in fp["stages"] if week >= st["from_week"]][-1]
        if stage.get("final"):
            proj_items.append(f'<li><b>{_esc(fp["title_" + lang])}:</b> {_esc(stage[lang])}</li>')
        else:
            tag = "(in progress)" if lang == "en" else "(בתהליך)"
            proj_items.append(f'<li><b>{_esc(fp["title_" + lang])}</b> {tag}: {_esc(fp["goal_" + lang])} {_esc(stage[lang])}</li>')
    for pr in CONTENT["projects"]:
        only = pr.get("tags_only")
        if only and not (set(only) & set(domains)):
            continue          # e.g. the web app / job-search tool only for validation & test roles
        proj_items.append(f'<li><b>{_esc(pr["title_" + lang])}</b> {_esc(pr["when_" + lang])}: {_esc(pr[lang])}</li>')
    if proj_items:            # no relevant project for this role -> no Projects section at all
        L.append(f'<h2>{T("Projects", "פרויקטים")}</h2><ul>' + "".join(proj_items) + "</ul>")
    L.append(f'<h2>{T("Military Service &amp; Volunteering", "שירות צבאי והתנדבות")}</h2><ul>')
    L += [f'<li><b>{_esc(m["b_" + lang])}</b> {_esc(m[lang])}</li>' for m in CONTENT["military"]]
    L.append("</ul>")
    L.append(f'<h2>{T("Languages", "שפות")}</h2><ul>' + "".join(f"<li>{_esc(x)}</li>" for x in CONTENT["languages"][lang]) + "</ul>")
    font = "'Noto Sans Hebrew','Arial','Liberation Sans','DejaVu Sans',sans-serif" if he else "'Helvetica','Arial','Liberation Sans',sans-serif"
    navy = "#1f2f6b"
    return f"""<!doctype html><html lang="{lang}" dir="{'rtl' if he else 'ltr'}"><head><meta charset="utf-8">
<style>
@page {{ size: A4; margin: 11mm 15mm 10mm; }}
body {{ font-family: {font}; font-size: {font_pt}pt; line-height: 1.3; color: #222; margin: 0; }}
.name {{ text-align: center; color: {navy}; font-weight: 700; font-size: {font_pt * 2.35:.1f}pt; letter-spacing: {'2pt' if he else '4pt'}; }}
.headline {{ text-align: center; font-weight: 700; font-size: {font_pt * 1.02:.1f}pt; letter-spacing: 1.2pt; margin-top: 2pt; }}
.contact {{ text-align: center; font-size: {font_pt * .92:.1f}pt; margin: 3pt 0 2pt; padding-bottom: 4pt; border-bottom: .8pt solid #999; }}
.contact a {{ color: #222; }}
h2 {{ color: {navy}; font-size: {font_pt * 1.38:.1f}pt; margin: {font_pt * .55:.1f}pt 0 2pt; }}
h2 + * {{ margin-top: 0; }}
p {{ margin: 0 0 2pt; padding-bottom: 4pt; border-bottom: .8pt solid #999; }}
ul {{ margin: 1pt 0 0; padding-{'right' if he else 'left'}: 14pt; padding-bottom: 4pt; border-bottom: .8pt solid #999; }}
ul.sub {{ border: 0; padding-bottom: 0; list-style: circle; }}
li {{ margin: 0 0 1.5pt; }}
.b {{ font-weight: 700; }}
.muted {{ color: #888; }}
</style></head><body>{''.join(L)}</body></html>"""


def _pdf_from_html(page, html_str: str, out: Path):
    page.set_content(html_str, wait_until="load")
    page.pdf(path=str(out), format="A4", print_background=True, prefer_css_page_size=True)


def _check(pdf: Path):
    r = PdfReader(str(pdf))
    if len(r.pages) != 1:
        raise CVSafetyError(f"CV has {len(r.pages)} pages")
    text = " ".join(p.extract_text() or "" for p in r.pages)
    sv_ok = PROFILE.get("project_week_done", 0) >= PROFILE.get("systemverilog_from_week", 99)
    forbidden = [w for w in PROFILE["forbidden_in_cv"] if not (w == "C++" and PROFILE.get("allow_cpp"))
                 and not (w == "SystemVerilog" and sv_ok)]
    for w in forbidden:
        if re.search(r"(?<![A-Za-z])" + re.escape(w) + r"(?![A-Za-z+])", text, re.I):
            raise CVSafetyError(f"forbidden word in CV: {w}")
    return text


def build_cv(out_dir: Path, lang: str, domains: list[str], selection: dict | None = None,
             transcript_pdf: Path | None = None) -> dict:
    """Returns {"cv": path, "cv_with_transcript": path|None}."""
    from playwright.sync_api import sync_playwright
    out_dir.mkdir(parents=True, exist_ok=True)
    cv = out_dir / FILENAME
    with sync_playwright() as p:
        exe = os.environ.get("CHROMIUM_PATH")
        b = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        page = b.new_page()
        for pt in (9.8, 9.6, 9.3, 9.0, 8.7, 8.4, 8.1):
            _pdf_from_html(page, render_html(lang, domains, selection, pt), cv)
            if len(PdfReader(str(cv)).pages) == 1:
                break
        b.close()
    _check(cv)
    res = {"cv": cv, "cv_with_transcript": None}
    if transcript_pdf and Path(transcript_pdf).exists():
        d = out_dir / "with_transcript"
        d.mkdir(exist_ok=True)
        w = PdfWriter()
        w.append(str(cv))
        w.append(str(transcript_pdf))   # appended as-is, never modified
        merged = d / FILENAME
        with open(merged, "wb") as f:
            w.write(f)
        res["cv_with_transcript"] = merged
    return res


if __name__ == "__main__":
    import sys
    lang = sys.argv[1] if len(sys.argv) > 1 else "en"
    doms = sys.argv[3].split(",") if len(sys.argv) > 3 else ["verification", "digital_design"]
    r = build_cv(Path(sys.argv[2] if len(sys.argv) > 2 else "out"), lang, doms)
    print(r)
