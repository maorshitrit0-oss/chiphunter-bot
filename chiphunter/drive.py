"""Google Drive sync of the CV library (shows up in iPhone Files via the Google Drive app).

Env: GDRIVE_CLIENT_ID, GDRIVE_CLIENT_SECRET, GDRIVE_REFRESH_TOKEN (OAuth, scope drive.file),
     GDRIVE_ROOT_FOLDER_ID (optional; default: creates "קורות חיים" in My Drive).

Layout (short paths -> fast in the iPhone Files app; Maor marks "1_להגשה עכשיו" available offline once):
  CV_Library/1_להגשה עכשיו/<Company - Role>/Maor Shitrit CV.pdf    jobs sent, not applied / skipped yet
  CV_Library/2_הוגשו/<Company - Role>/...                           applied / interview / offer (+ interview prep)
  CV_Library/3_ארכיון/...                                          skipped, rejected, the old library
  CV_Library/_system/                                               transcripts (never edited)
A job's folder moves between them when Maor presses "הגשתי" / "דלג" or reports an answer.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .http import post, request

ROOT_NAME = "CV_Library"
MAP = Path(__file__).resolve().parent.parent / "data" / "library_map.json"
FOLDER = "application/vnd.google-apps.folder"
_token = None


def enabled() -> bool:
    return all(os.environ.get(k) for k in ("GDRIVE_CLIENT_ID", "GDRIVE_CLIENT_SECRET", "GDRIVE_REFRESH_TOKEN"))


def auth_ok() -> bool:
    try:
        _auth()
        return True
    except Exception:
        return False


def _auth():
    global _token
    if not _token:
        _token = post("https://oauth2.googleapis.com/token", data={
            "client_id": os.environ["GDRIVE_CLIENT_ID"], "client_secret": os.environ["GDRIVE_CLIENT_SECRET"],
            "refresh_token": os.environ["GDRIVE_REFRESH_TOKEN"], "grant_type": "refresh_token"}).json()["access_token"]
    return {"Authorization": f"Bearer {_token}"}


def _q(s):
    return s.replace("\\", "\\\\").replace("'", "\\'")


def _find(name, parent, mime=None):
    q = f"name = '{_q(name)}' and trashed = false and '{parent}' in parents"
    if mime:
        q += f" and mimeType = '{mime}'"
    r = request("GET", "https://www.googleapis.com/drive/v3/files", headers=_auth(),
                params={"q": q, "fields": "files(id,name)", "spaces": "drive"}).json()
    f = r.get("files", [])
    return f[0]["id"] if f else None


def folder(path: list[str]) -> str:
    parent = os.environ.get("GDRIVE_ROOT_FOLDER_ID") or "root"
    if parent == "root":
        path = [ROOT_NAME] + path
    for name in path:
        fid = _find(name, parent, FOLDER)
        if not fid:
            fid = post("https://www.googleapis.com/drive/v3/files", headers=_auth(),
                       json={"name": name, "mimeType": FOLDER, "parents": [parent]}).json()["id"]
        parent = fid
    return parent


def upload(local: Path, path: list[str], name: str | None = None, mime="application/pdf") -> str:
    """Create or replace the file (same name in same folder is overwritten, keeping one copy)."""
    parent = folder(path)
    name = name or local.name
    existing = _find(name, parent)
    meta = {"name": name} if existing else {"name": name, "parents": [parent]}
    boundary = "chiphunter-boundary"
    body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(meta)}\r\n"
            f"--{boundary}\r\nContent-Type: {mime}\r\n\r\n").encode() + local.read_bytes() + f"\r\n--{boundary}--".encode()
    url = ("https://www.googleapis.com/upload/drive/v3/files" + (f"/{existing}" if existing else "")
           + "?uploadType=multipart&fields=id,webViewLink")
    r = request("PATCH" if existing else "POST", url, data=body,
                headers={**_auth(), "Content-Type": f"multipart/related; boundary={boundary}"}).json()
    return r.get("webViewLink", "")


def safe(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "-", s).strip()[:80]


def company_folder(company: str) -> str:
    """'Marvell' -> '01_Marvell' (existing numbering kept, new companies get the next number)."""
    company = (company or "").strip() or "Other"
    m = json.loads(MAP.read_text(encoding="utf-8")) if MAP.exists() else {}
    for name, folder_name in m.items():
        if name.lower() == company.lower():
            return folder_name
    nxt = max([int(v.split("_", 1)[0]) for v in m.values() if v[:2].isdigit()] + [0]) + 1
    m[company] = f"{nxt:02d}_{safe(company)}"
    MAP.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
    return m[company]


BUCKETS = {"todo": "1_להגשה עכשיו", "applied": "2_הוגשו", "archive": "3_ארכיון"}
CV_NAME = "Maor Shitrit CV.pdf"


def bucket_for(status: str | None) -> str:
    if status in ("cv_built", "held", None):
        return "todo"
    if status in ("applied", "interview", "offer"):
        return "applied"
    return "archive"


def job_folder(company: str, title: str) -> str:
    return safe(f"{(company or '').strip() or 'Other'} - {(title or '').strip() or 'Job'}")[:100]


def library_path(company: str, title: str, transcript: bool = False, bucket: str = "todo") -> list[str]:
    return [BUCKETS[bucket], job_folder(company, title)]


def display_path(company: str, title: str, transcript: bool = False, lang: str = "en", bucket: str = "todo") -> str:
    return "/".join([ROOT_NAME] + library_path(company, title, bucket=bucket) + [CV_NAME])


def _api(method, path, **kw):
    return request(method, "https://www.googleapis.com/drive/v3/files" + path, headers=_auth(), **kw).json()


def children(parent: str) -> list[dict]:
    out, token = [], None
    while True:
        r = _api("GET", "", params={"q": f"'{parent}' in parents and trashed = false", "pageSize": 1000,
                                    "fields": "nextPageToken,files(id,name,mimeType,parents)",
                                    **({"pageToken": token} if token else {})})
        out += r.get("files", [])
        token = r.get("nextPageToken")
        if not token:
            return out


def move(fid: str, new_parent: str, old_parent: str | None = None):
    if not old_parent:
        old_parent = ",".join(_api("GET", f"/{fid}", params={"fields": "parents"}).get("parents", []))
    return _api("PATCH", f"/{fid}", params={"addParents": new_parent, "removeParents": old_parent, "fields": "id"}, json={})


def find_path(path: list[str]) -> str | None:
    parent = os.environ.get("GDRIVE_ROOT_FOLDER_ID") or "root"
    for part in ([ROOT_NAME] if parent == "root" else []) + path:
        parent = _find(part, parent)
        if not parent:
            return None
    return parent


def move_job(company: str, title: str, bucket: str) -> bool:
    """Move a job's folder to the bucket that matches its status (no-op if it's already there / doesn't exist)."""
    name = job_folder(company, title)
    target = folder([BUCKETS[bucket]])
    for b, bname in BUCKETS.items():
        if b == bucket:
            continue
        src = find_path([bname])
        fid = src and _find(name, src, FOLDER)
        if fid:
            move(fid, target, src)
            return True
    return False


def download(path: list[str], name: str, dest: Path) -> Path | None:
    parent = os.environ.get("GDRIVE_ROOT_FOLDER_ID") or "root"
    for part in ([ROOT_NAME] if parent == "root" else []) + path:
        parent = _find(part, parent, FOLDER)
        if not parent:
            return None
    fid = _find(name, parent)
    if not fid:
        return None
    r = request("GET", f"https://www.googleapis.com/drive/v3/files/{fid}?alt=media", headers=_auth())
    dest.write_bytes(r.content)
    return dest
