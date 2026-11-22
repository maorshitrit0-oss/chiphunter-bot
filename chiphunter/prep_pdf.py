"""Render an interview prep pack (prep.json) to a Hebrew RTL PDF with the 4-box format."""
from __future__ import annotations

import html
import os
from pathlib import Path

BOX = {"analogy": ("#fff6d6", "#b58900", "🟡 אנלוגיה"), "intent": ("#e3efff", "#1f5fbf", "🔵 מה בודקים"),
       "answer": ("#e3f7e8", "#1e7d3a", "🟢 תסריט תשובה"), "trap": ("#fde4e4", "#b3261e", "🔴 מלכודת")}


def _e(s):
    return html.escape(str(s or ""))


def render(prep: dict) -> str:
    q_html = []
    for i, q in enumerate(prep.get("questions", []), 1):
        tag = "📌 דווח בראיונות" if q.get("reported") else "שאלה נפוצה"
        src = f' · <a href="{_e(q["source"])}">מקור</a>' if q.get("source") else ""
        boxes = "".join(
            f'<div class="box" style="background:{bg};border-color:{fg}"><b style="color:{fg}">{label}</b><br>{_e(q.get(k))}</div>'
            for k, (bg, fg, label) in BOX.items() if q.get(k))
        q_html.append(f'<div class="q"><h3>{i}. {_e(q.get("q"))}</h3>'
                      f'<div class="meta">{_e(q.get("category"))} · {tag}{src}</div>{boxes}</div>')
    plan = "".join(f"<li><b>{_e(d.get('day'))}:</b> {_e(' · '.join(d.get('items', [])))}</li>" for d in prep.get("study_plan", []))
    return f"""<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8"><style>
@page {{ size: A4; margin: 14mm; }}
body {{ font-family: 'Noto Sans Hebrew', Arial, sans-serif; font-size: 10.5pt; color: #222; line-height: 1.45; }}
h1 {{ color: #1f2f6b; margin: 0; }} h2 {{ color: #1f2f6b; border-bottom: 1px solid #999; margin-top: 16pt; }}
.q {{ page-break-inside: avoid; margin: 10pt 0; }} h3 {{ margin: 0 0 2pt; font-size: 11.5pt; }}
.meta {{ color: #777; font-size: 9pt; margin-bottom: 4pt; }} a {{ color: #1f5fbf; }}
.box {{ border-right: 4px solid; padding: 5pt 8pt; margin: 3pt 0; border-radius: 3pt; }}
</style></head><body>
<h1>הכנה לראיון — {_e(prep.get('company'))}</h1><div class="meta">{_e(prep.get('role'))}</div>
<h2>איך נראה התהליך</h2><p>{_e(prep.get('process_he'))}</p>
<h2>על מה להתמקד</h2><ul>{''.join(f'<li>{_e(x)}</li>' for x in prep.get('focus_he', []))}</ul>
<h2>תוכנית לימוד</h2><ul>{plan}</ul>
<h2>"ספר על עצמך" (45 שניות)</h2><div class="box" style="background:#e3f7e8;border-color:#1e7d3a">{_e(prep.get('about_me_he'))}</div>
<h2>שאלות ותשובות</h2>{''.join(q_html)}
<h2>שאלות לשאול אותם</h2><ul>{''.join(f'<li>{_e(x)}</li>' for x in prep.get('questions_to_ask', []))}</ul>
<h2>מקורות</h2><ul>{''.join(f'<li><a href="{_e(u)}">{_e(u)}</a></li>' for u in prep.get('sources', []))}</ul>
</body></html>"""


def to_pdf(prep: dict, out: Path) -> Path:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        exe = os.environ.get("CHROMIUM_PATH")
        b = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        pg = b.new_page()
        pg.set_content(render(prep), wait_until="load")
        pg.pdf(path=str(out), format="A4", print_background=True)
        b.close()
    return out
