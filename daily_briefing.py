"""
Daily Briefing Bot — main script.

Runs once a day (triggered by GitHub Actions). It:
  1. Reads today's Google Calendar events.
  2. Reads open Google Tasks from your main list and a "Notes" list.
  3. Asks Gemini to turn that into a short, natural morning briefing.
  4. Sends the result to Telegram, where MacroDroid later picks it up
     and reads it aloud on your phone.

All configuration comes from environment variables (set as GitHub Actions
secrets) — nothing sensitive is hardcoded here.

See README.md for the full architecture description.
"""

import logging
import os
import sys
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("daily_briefing")

TIMEZONE = ZoneInfo("Australia/Perth")
NOTES_LIST_NAME = os.environ.get("NOTES_LIST_NAME", "Notes")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/tasks.readonly",
]


def require_env(name):
    """Fetch a required environment variable or exit with a clear error.

    Failing fast with a readable message here is much easier to debug from
    the GitHub Actions log than a generic KeyError deep inside a library call.
    """
    value = os.environ.get(name)
    if not value:
        log.error("Missing required environment variable: %s", name)
        sys.exit(1)
    return value


def build_google_credentials():
    """Build OAuth credentials from the stored refresh token and refresh them.

    We only ever hold a refresh token (never a password), and Google's
    client library exchanges it for a short-lived access token on demand.
    """
    credentials = Credentials(
        token=None,
        refresh_token=require_env("GOOGLE_REFRESH_TOKEN"),
        client_id=require_env("GOOGLE_CLIENT_ID"),
        client_secret=require_env("GOOGLE_CLIENT_SECRET"),
        token_uri="https://oauth2.googleapis.com/token",
        scopes=GOOGLE_SCOPES,
    )
    try:
        credentials.refresh(GoogleAuthRequest())
    except Exception as exc:  # noqa: BLE001 - any refresh failure is fatal here
        log.error(
            "Could not refresh Google credentials (%s). The refresh token may "
            "have been revoked — you'll need to re-run get_refresh_token.py.",
            exc,
        )
        raise
    return credentials


def fetch_today_events(credentials):
    """Return a list of today's Calendar events as short human-readable strings."""
    now = datetime.now(TIMEZONE)
    start_of_day = datetime.combine(now.date(), time.min, tzinfo=TIMEZONE)
    end_of_day = start_of_day + timedelta(days=1)

    service = build("calendar", "v3", credentials=credentials)
    try:
        response = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=start_of_day.isoformat(),
                timeMax=end_of_day.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
    except HttpError as exc:
        log.warning("Calendar API call failed (%s) — continuing without events.", exc)
        return []

    events = []
    for item in response.get("items", []):
        title = item.get("summary", "(untitled event)")
        start = item["start"].get("dateTime", item["start"].get("date"))
        events.append(f"{title} at {start}")
    return events


def _find_task_list_id(service, list_title):
    """Look up a Tasks list's ID by its display name (case-insensitive)."""
    try:
        response = service.tasklists().list().execute()
    except HttpError as exc:
        log.warning("Could not list task lists (%s).", exc)
        return None

    for task_list in response.get("items", []):
        if task_list.get("title", "").strip().lower() == list_title.strip().lower():
            return task_list["id"]
    return None


def _fetch_list_tasks(service, list_id):
    """Return incomplete task titles from a single Tasks list."""
    try:
        response = (
            service.tasks()
            .list(tasklist=list_id, showCompleted=False, showHidden=False)
            .execute()
        )
    except HttpError as exc:
        log.warning("Could not read tasks from list %s (%s).", list_id, exc)
        return []
    return [task["title"] for task in response.get("items", []) if task.get("title")]


def fetch_tasks_and_notes(credentials):
    """Return (tasks, notes) as lists of strings from the default list and
    the "Notes" list respectively. Missing lists are treated as empty, not
    fatal — you may not have created a Notes list yet.
    """
    service = build("tasks", "v1", credentials=credentials)

    tasks = _fetch_list_tasks(service, "@default")

    notes_list_id = _find_task_list_id(service, NOTES_LIST_NAME)
    if notes_list_id is None:
        log.info("No '%s' task list found — skipping notes.", NOTES_LIST_NAME)
        notes = []
    else:
        notes = _fetch_list_tasks(service, notes_list_id)

    return tasks, notes


def build_prompt(events, tasks, notes):
    """Turn raw calendar/tasks/notes data into a prompt for Gemini."""
    today_str = datetime.now(TIMEZONE).strftime("%A, %-d %B %Y")

    def format_section(title, items):
        if not items:
            return f"{title}: none"
        bullet_lines = "\n".join(f"- {item}" for item in items)
        return f"{title}:\n{bullet_lines}"

    sections = "\n\n".join(
        [
            format_section("Calendar events today", events),
            format_section("Open tasks", tasks),
            format_section("Notes", notes),
        ]
    )

    return (
        f"Today is {today_str}. Write a short, warm, spoken-style morning "
        "briefing for me based on the information below. Keep it under 100 "
        "words, use plain conversational sentences (this will be read aloud "
        "by text-to-speech), and skip any section that has no items rather "
        "than mentioning it's empty.\n\n"
        f"{sections}"
    )


def summarize_with_gemini(prompt):
    """Call Gemini to turn the prompt into a spoken-style briefing.

    Returns None on any failure so the caller can fall back to a templated
    summary instead of sending nothing at all.
    """
    try:
        from google import genai
    except ImportError:
        log.warning("google-genai package not installed — skipping Gemini step.")
        return None

    try:
        client = genai.Client(api_key=require_env("GEMINI_API_KEY"))
        response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        text = (response.text or "").strip()
        return text or None
    except Exception as exc:  # noqa: BLE001 - any Gemini failure should degrade gracefully
        log.warning("Gemini summarization failed (%s) — falling back to a plain summary.", exc)
        return None


def build_fallback_summary(events, tasks, notes):
    """A simple, guaranteed-to-work summary used if Gemini is unavailable."""
    lines = ["Good morning. Here's your briefing:"]
    if events:
        lines.append(f"You have {len(events)} calendar event(s) today: " + "; ".join(events) + ".")
    if tasks:
        lines.append("Open tasks: " + "; ".join(tasks) + ".")
    if notes:
        lines.append("Notes: " + "; ".join(notes) + ".")
    if len(lines) == 1:
        lines.append("Nothing on your plate today. Enjoy the quiet morning.")
    return " ".join(lines)


def send_telegram_message(text):
    """Send the briefing to Telegram, with one retry on transient failures."""
    bot_token = require_env("TELEGRAM_BOT_TOKEN")
    chat_id = require_env("TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}

    for attempt in (1, 2):
        try:
            response = requests.post(url, json=payload, timeout=15)
            response.raise_for_status()
            return
        except requests.RequestException as exc:
            log.warning("Telegram send attempt %d failed: %s", attempt, exc)

    log.error("Telegram send failed after retry — the briefing was not delivered.")
    sys.exit(1)


def main():
    log.info("Starting daily briefing run.")

    credentials = build_google_credentials()
    events = fetch_today_events(credentials)
    tasks, notes = fetch_tasks_and_notes(credentials)

    log.info(
        "Fetched %d event(s), %d task(s), %d note(s).",
        len(events),
        len(tasks),
        len(notes),
    )

    prompt = build_prompt(events, tasks, notes)
    summary = summarize_with_gemini(prompt) or build_fallback_summary(events, tasks, notes)

    send_telegram_message(summary)
    log.info("Briefing sent successfully.")


if __name__ == "__main__":
    main()
