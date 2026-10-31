"""WhatsApp -> Google Calendar.

Text like "Dentist Friday 4pm" is parsed by Gemini and added to Google Calendar.
Entry points: POST /add (used by an iPhone Shortcut) and, optionally, a WhatsApp
Cloud API webhook (only active if the WHATSAPP_/META_ variables are set).
"""
import hashlib
import hmac
import json
import logging
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, Response
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from pydantic import BaseModel

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("wa-cal")

# ---- Config (see .env.example) ----
API_SECRET = os.getenv("API_SECRET", "")                  # shared secret for POST /add
VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "")      # any string you choose
APP_SECRET = os.getenv("META_APP_SECRET", "")              # Meta app > Settings > Basic
WA_TOKEN = os.getenv("WHATSAPP_ACCESS_TOKEN", "")          # use a permanent system-user token
PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "")
ALLOWED_NUMBER = os.getenv("ALLOWED_NUMBER", "")           # your number, digits only, e.g. 91XXXXXXXXXX
TZ_NAME = os.getenv("TIMEZONE", "Asia/Kolkata")
GEMINI_KEY = os.environ["GEMINI_API_KEY"]               # free key from Google AI Studio
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")  # change here if Google retires it
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GRAPH_URL = f"https://graph.facebook.com/v21.0/{PHONE_NUMBER_ID}/messages"
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")  # with a service account, set this to your Gmail address

TZ = ZoneInfo(TZ_NAME)
app = FastAPI()
_seen: set[str] = set()         # Meta retries webhooks; dedupe by message id

EVENT_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "is_event": {"type": "BOOLEAN", "description": "False if the message is not a request to schedule something."},
        "title": {"type": "STRING"},
        "start": {"type": "STRING", "description": f"Local time in {TZ_NAME}, ISO 8601 without offset, e.g. 2026-10-30T16:00:00"},
        "end": {"type": "STRING", "description": "Same format. Omit if not stated."},
        "all_day": {"type": "BOOLEAN", "description": "True if a date is given but no time."},
        "location": {"type": "STRING"},
    },
    "required": ["is_event"],
}


class RateLimited(Exception):
    """Gemini free-tier quota hit (HTTP 429)."""


# ---------- WhatsApp ----------
def send_whatsapp(text: str) -> None:
    r = httpx.post(
        GRAPH_URL,
        headers={"Authorization": f"Bearer {WA_TOKEN}"},
        json={"messaging_product": "whatsapp", "to": ALLOWED_NUMBER, "type": "text", "text": {"body": text}},
        timeout=15,
    )
    if r.status_code >= 400:
        log.error("WhatsApp send failed: %s %s", r.status_code, r.text)


def verify_signature(raw: bytes, header: str | None) -> None:
    if not APP_SECRET:
        raise HTTPException(status_code=404)  # WhatsApp webhook not configured
    expected = "sha256=" + hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    if not header or not hmac.compare_digest(expected, header):
        raise HTTPException(status_code=403, detail="Bad signature")


# ---------- Parsing ----------
def parse_event(text: str) -> dict:
    now = datetime.now(TZ)
    system = (
        "You convert short WhatsApp messages into calendar events. "
        f"Current date/time: {now:%A, %d %B %Y, %H:%M} ({TZ_NAME}). "
        "Resolve relative dates (tomorrow, Friday, next week) from that. "
        "If only a date is given, set all_day true. Don't invent an end time. "
        "If the message isn't about scheduling something, set is_event false."
    )
    body = {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"role": "user", "parts": [{"text": text}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": EVENT_SCHEMA,
            "temperature": 0,
        },
    }
    url = GEMINI_URL.format(model=GEMINI_MODEL)
    for attempt in range(2):
        r = httpx.post(url, headers={"x-goog-api-key": GEMINI_KEY}, json=body, timeout=30)
        if r.status_code == 429:
            raise RateLimited()
        if r.status_code in (500, 503) and attempt == 0:
            time.sleep(2)  # transient Google-side error; retry once
            continue
        r.raise_for_status()
        break
    parts = r.json()["candidates"][0]["content"]["parts"]
    raw = "".join(p.get("text", "") for p in parts if not p.get("thought"))
    return json.loads(raw)


# ---------- Google Calendar ----------
def calendar_service():
    # 1) Service account (recommended for personal use: no consent screen, no expiring tokens)
    if os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"):  # hosted deployments
        creds = service_account.Credentials.from_service_account_info(
            json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]), scopes=SCOPES)
    elif os.path.exists("service_account.json"):
        creds = service_account.Credentials.from_service_account_file("service_account.json", scopes=SCOPES)
    # 2) OAuth user token (alternative; see get_token.py)
    elif os.getenv("GOOGLE_TOKEN_JSON"):
        creds = Credentials.from_authorized_user_info(json.loads(os.environ["GOOGLE_TOKEN_JSON"]), SCOPES)
    else:
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if getattr(creds, "refresh_token", None) and creds.expired:
        creds.refresh(GoogleRequest())
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def create_event(ev: dict) -> tuple[str, str]:
    body: dict = {"summary": ev["title"], "reminders": {"useDefault": True}}
    if ev.get("all_day"):
        day = datetime.fromisoformat(ev["start"]).date()
        body["start"] = {"date": day.isoformat()}
        body["end"] = {"date": (day + timedelta(days=1)).isoformat()}
        when = f"{day:%a, %d %b} (all day)"
    else:
        start = datetime.fromisoformat(ev["start"])
        if start.tzinfo is None:
            start = start.replace(tzinfo=TZ)
        end = datetime.fromisoformat(ev["end"]) if ev.get("end") else start + timedelta(hours=1)
        if end.tzinfo is None:
            end = end.replace(tzinfo=TZ)
        body["start"] = {"dateTime": start.isoformat(), "timeZone": TZ_NAME}
        body["end"] = {"dateTime": end.isoformat(), "timeZone": TZ_NAME}
        when = f"{start:%a, %d %b, %I:%M %p}"
    if ev.get("location"):
        body["location"] = ev["location"]
    created = calendar_service().events().insert(calendarId=CALENDAR_ID, body=body).execute()
    return created.get("htmlLink", ""), f"{ev['title']} - {when}"


# ---------- Shared pipeline ----------
def process_text(text: str) -> tuple[bool, str]:
    """Parse text and create the event. Returns (ok, message to show the user)."""
    try:
        ev = parse_event(text)
        if not ev.get("is_event") or not ev.get("title") or not ev.get("start"):
            return False, "I couldn't find an event in that. Try: 'Dentist Friday 4pm'."
        _, summary = create_event(ev)
        return True, f"✅ Added: {summary}"
    except RateLimited:
        log.warning("Gemini free-tier limit hit")
        return False, "⏳ Gemini's free limit is hit for now. Try again in a minute."
    except Exception:
        log.exception("Failed to process text")
        return False, "⚠️ Something went wrong adding that."


# ---------- WhatsApp pipeline ----------
def handle_message(text: str) -> None:
    try:
        ev = parse_event(text)
        if not ev.get("is_event") or not ev.get("title") or not ev.get("start"):
            send_whatsapp("I couldn't find an event in that. Try: 'Dentist Friday 4pm'.")
            return
        link, summary = create_event(ev)
        send_whatsapp(f"✅ Added: {summary}\n{link}")
    except RateLimited:
        log.warning("Gemini free-tier limit hit")
        try:
            send_whatsapp("⏳ Gemini's free limit is hit for now. Try again in a minute (or tomorrow if it was the daily cap).")
        except Exception:
            pass
    except Exception:
        log.exception("Failed to process message")
        try:
            send_whatsapp("⚠️ Something went wrong adding that. Check the server logs.")
        except Exception:
            pass


# ---------- Routes ----------
@app.get("/webhook")
def verify(request: Request):
    q = request.query_params
    if VERIFY_TOKEN and q.get("hub.mode") == "subscribe" and q.get("hub.verify_token") == VERIFY_TOKEN:
        return Response(content=q.get("hub.challenge", ""), media_type="text/plain")
    raise HTTPException(status_code=403)


@app.post("/webhook")
async def receive(request: Request, background: BackgroundTasks):
    raw = await request.body()
    verify_signature(raw, request.headers.get("X-Hub-Signature-256"))
    payload = json.loads(raw)
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            for msg in change.get("value", {}).get("messages", []):
                if msg.get("type") != "text" or msg["id"] in _seen:
                    continue
                if len(_seen) > 1000:
                    _seen.clear()
                _seen.add(msg["id"])
                if msg.get("from") != ALLOWED_NUMBER:
                    log.warning("Ignored message from unknown sender")
                    continue
                background.add_task(handle_message, msg["text"]["body"])
    return {"status": "ok"}  # respond fast; Meta retries on slow replies


class AddBody(BaseModel):
    text: str


@app.post("/add")
def add(body: AddBody, x_api_secret: str = Header(default="")):
    """Used by the iPhone Shortcut. Send JSON {"text": "..."} with header X-API-Secret."""
    if not API_SECRET or not hmac.compare_digest(x_api_secret.encode(), API_SECRET.encode()):
        raise HTTPException(status_code=403, detail="Forbidden")
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    ok, message = process_text(text)
    return {"ok": ok, "message": message}


@app.get("/health")
def health():
    return {"ok": True}
