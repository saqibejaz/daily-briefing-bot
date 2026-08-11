# Daily Briefing Bot

Sends a short, spoken-style summary of my day — calendar events, tasks, and notes — to Telegram every morning at 07:00 (Australia/Perth). MacroDroid on my phone then reads it aloud automatically. Entirely built on free-tier services.

## Architecture

```
GitHub Actions (cron, 23:00 UTC = 07:00 AWST)
        │
        ▼
daily_briefing.py
        │
        ├── Google Calendar API   — today's events
        ├── Google Tasks API      — "My Tasks" list + a "Notes" list
        │
        ▼
   Gemini API — condenses the raw data into a short natural-language briefing
   (falls back to a plain templated summary if Gemini is unavailable)
        │
        ▼
   Telegram Bot API — sendMessage delivers the briefing to my chat
        │
        ▼
   MacroDroid (on my phone, not in this repo) — polls Telegram's getUpdates
   endpoint on its own 07:05 time trigger and reads the result aloud via
   Android's built-in text-to-speech
```

## Files

- `daily_briefing.py` — the script GitHub Actions runs each morning. Fetches Calendar/Tasks data, summarizes it with Gemini, sends it to Telegram.
- `requirements.txt` — Python dependencies.
- `.github/workflows/daily-briefing.yml` — the schedule that runs the script daily, and lets it be triggered manually for testing.
- `scripts/get_refresh_token.py` — a one-time script run locally (not in CI) to obtain the Google OAuth refresh token used by `daily_briefing.py`.

## Configuration (GitHub Actions secrets)

| Secret | Where it comes from |
|---|---|
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | The OAuth "Desktop app" client created in Google Cloud Console |
| `GOOGLE_REFRESH_TOKEN` | Output of `scripts/get_refresh_token.py`, run once locally |
| `TELEGRAM_BOT_TOKEN` | Created via `@BotFather` on Telegram |
| `TELEGRAM_CHAT_ID` | Looked up via `https://api.telegram.org/bot<TOKEN>/getUpdates` |
| `GEMINI_API_KEY` | Created at [aistudio.google.com](https://aistudio.google.com) |

No secret is ever stored in code — only as encrypted repository secrets, injected as environment variables at run time.

## Why these components

- **Google Calendar/Tasks APIs** — official, free, read-only access to my schedule and to-dos. A "Notes" Tasks list stands in for Google Keep, which has no public API.
- **GitHub Actions** — free scheduled compute; no server of my own to maintain.
- **Gemini API (free tier)** — turns raw lists into a briefing that reads naturally aloud, with a plain-text fallback if it's ever unavailable.
- **Telegram Bot API** — free, reliable relay between the cloud job and my phone.
- **MacroDroid** (phone-side, not in this repo) — free automation that pulls the Telegram message on a schedule and speaks it, avoiding the flakiness of notification-listener triggers.

## Local testing

```
pip install -r requirements.txt
export GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... GOOGLE_REFRESH_TOKEN=...
export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... GEMINI_API_KEY=...
python daily_briefing.py
```
