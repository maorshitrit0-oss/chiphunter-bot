"""One place for all AI calls. Provider order:
  1. Gemini API (free tier, GEMINI_API_KEY) — independent of Maor's Claude subscription, runs 24/7
  2. Claude Code CLI (CLAUDE_CODE_OAUTH_TOKEN) — fallback
  3. None -> callers fall back to rules

Prompts are written as "files in the working directory ... write X.json". For API providers the files are inlined
and the model returns the JSON directly.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import requests

GEMINI_PREFERRED = ["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash"]
_last_error = ""
_models_cache: list | None = None


def _gemini_models(key: str) -> list[str]:
    """Ask Google which models this key can use; prefer the newest stable 'flash' models (fast, free tier)."""
    global _models_cache
    if _models_cache is not None:
        return _models_cache
    names = []
    try:
        url = "https://generativelanguage.googleapis.com/v1beta/models"
        tok = None
        while True:
            r = requests.get(url, params={"key": key, "pageSize": 200, **({"pageToken": tok} if tok else {})}, timeout=30)
            r.raise_for_status()
            d = r.json()
            for m in d.get("models", []):
                if "generateContent" in m.get("supportedGenerationMethods", []):
                    names.append(m["name"].split("/", 1)[1])
            tok = d.get("nextPageToken")
            if not tok:
                break
    except Exception as ex:
        print(f"[llm] list models failed: {ex}", file=sys.stderr)
        return GEMINI_PREFERRED
    bad = ("image", "tts", "audio", "live", "embedding", "vision", "thinking", "exp", "learnlm", "gemma", "robotics", "computer")

    def ver(n):
        m = re.search(r"gemini-(\d+(?:\.\d+)?)", n)
        return float(m.group(1)) if m else 0.0

    usable = [n for n in names if n.startswith("gemini-") and "flash" in n and not any(b in n for b in bad)]
    # newest version first; within a version: plain flash, then lite; stable before preview; no dated/-latest dupes first
    usable.sort(key=lambda n: (-ver(n), "preview" in n, "latest" in n, len(n)))
    flash = [n for n in usable if "lite" not in n][:3]
    lite = [n for n in usable if "lite" in n][:2]
    # newest flash first, then a fast 'lite' model, then older flash versions (they are often less loaded)
    # measured: the newest 'flash' is often overloaded/slow (503 / >75s); 'lite' answers in seconds -> lite first
    order = lite[:2] + flash[1:2] + flash[:1]
    _models_cache = order or GEMINI_PREFERRED
    print(f"[llm] gemini models: {_models_cache}", file=sys.stderr)
    return _models_cache


def available() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY") or (shutil.which("claude") and os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")))


def last_error() -> str:
    return _last_error


def _extract_json(text: str):
    text = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        text = m.group(1)
    else:
        a, b = text.find("{"), text.rfind("}")
        if a >= 0 and b > a:
            text = text[a:b + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Hebrew acronyms (קו"ח, צה"ל, מל"מ) often come back with an unescaped ASCII quote -> use the Hebrew gershayim
        fixed = re.sub(r'(?<=[\u0590-\u05FF])"(?=[\u0590-\u05FF])', "״", text)
        fixed = re.sub(r",\s*([}\]])", r"\1", fixed)          # trailing commas
        return json.loads(fixed)


def _gemini(prompt: str, files: dict, out_name: str, image: Path | None, search: bool) -> dict | None:
    global _last_error
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    body_text = prompt + "\n\n==== FILES ====\n" + "\n".join(
        f"--- {name} ---\n{content}" for name, content in files.items()) + (
        f"\n\n==== OUTPUT ====\nDo not write files. Return ONLY the JSON object that the instructions say to write to "
        f"{out_name}. No markdown, no explanations.")
    parts = [{"text": body_text}]
    for img in (image if isinstance(image, (list, tuple)) else [image] if image else []):
        mime = mimetypes.guess_type(str(img))[0] or "image/jpeg"
        parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(Path(img).read_bytes()).decode()}})
    payload = {"contents": [{"role": "user", "parts": parts}], "generationConfig": {"temperature": 0.3}}
    if search:
        payload["tools"] = [{"google_search": {}}]          # JSON mode can't be combined with search -> parse text
    else:
        payload["generationConfig"]["responseMimeType"] = "application/json"
    per_call = 120 if search else 45
    for model in _gemini_models(key):
        try:
            # keep "thinking" low: answers come much faster and these tasks don't need deep reasoning
            p2 = json.loads(json.dumps(payload))
            p2["generationConfig"]["thinkingConfig"] = {"thinkingLevel": "low"}
            r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                              params={"key": key}, json=p2, timeout=per_call)
            if r.status_code == 400 and "thinking" in r.text.lower():
                r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                                  params={"key": key}, json=payload, timeout=per_call)
            if r.status_code in (404, 429, 500, 503):   # retired / quota / overloaded -> next model
                _last_error = f"gemini {model} {r.status_code}"
                print(f"[llm] {_last_error}: {r.text[:200]}", file=sys.stderr)
                continue
            if r.status_code in (400, 401, 403) and "API key" in r.text:
                _last_error = "gemini_auth"
                print(f"[llm] gemini auth error: {r.text[:200]}", file=sys.stderr)
                return None
            r.raise_for_status()
            cand = r.json()["candidates"][0]
            text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []))
            out = _extract_json(text)
            out["_provider"] = model
            return out
        except Exception as ex:
            _last_error = f"gemini {model}: {type(ex).__name__}"
            print(f"[llm] {_last_error}: {ex}", file=sys.stderr)
    return None


def _claude(prompt: str, files: dict, out_name: str, image: Path | None, search: bool, timeout: int) -> dict | None:
    global _last_error
    if not (shutil.which("claude") and os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")):
        return None
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        for name, content in files.items():
            (d / name).write_text(content, encoding="utf-8")
        for i, img in enumerate(image if isinstance(image, (list, tuple)) else [image] if image else []):
            shutil.copy(img, d / f"screenshot{i + 1}{Path(img).suffix}")
        tools = "Read,Write" + (",WebSearch,WebFetch" if search else "")
        p = subprocess.run(["claude", "-p", prompt, "--allowedTools", tools, "--max-turns", "40" if search else "15"],
                           cwd=d, timeout=timeout, capture_output=True, text=True)
        print(f"[llm] claude exit={p.returncode} {p.stdout[-300:]}", file=sys.stderr)
        try:
            out = json.loads((d / out_name).read_text(encoding="utf-8"))
            out["_provider"] = "claude"
            return out
        except Exception:
            _last_error = "claude: " + (p.stdout.strip().splitlines() or ["no output"])[-1][:120]
            return None


def run(prompt: str, files: dict, out_name: str, image: Path | None = None, search: bool = False,
        timeout: int = 300) -> dict | None:
    """files: {filename: text content}. Returns the parsed JSON or None."""
    return _gemini(prompt, files, out_name, image, search) or _claude(prompt, files, out_name, image, search, timeout)
