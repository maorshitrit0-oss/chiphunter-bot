# Chip Career — job-search bot (code)

A personal Telegram bot that scans job sites for student hardware-engineering roles, evaluates them, builds a tailored
CV (PDF) and tracks applications.

This repository holds **code only**. The owner's profile, CVs, application tracker and prompts live in a separate
private repository that the workflow checks out at run time; secrets are stored as GitHub Actions secrets.

- `chiphunter/` — scanners, evaluation, CV builder, Telegram, Google Drive, learning
- `worker/` — Cloudflare Worker: Telegram webhook, buttons, real-time watcher
- `.github/workflows/process.yml` — the job runner
