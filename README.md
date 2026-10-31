# Text -> Google Calendar (iPhone Shortcut)

Share or dictate text like "Dentist Friday 4pm" from an iPhone Shortcut. The Shortcut posts it to this
server, Gemini turns it into an event, and it lands on your Google Calendar.

## Local setup (Windows)
```
python -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
copy .env.example .env      # then fill in .env
python -m uvicorn main:app --port 8000
```
Put `service_account.json` in this folder and share your calendar with its email (permission: Make changes to events).

## Test locally
```
python test_local.py "Dentist tomorrow 4pm"
```
Test the HTTP endpoint (PowerShell, with the server running):
```
Invoke-RestMethod -Uri http://127.0.0.1:8000/add -Method Post -ContentType "application/json" -Headers @{"X-API-Secret"="YOUR_API_SECRET"} -Body '{"text":"Lunch with Priya tomorrow 1pm"}'
```

## Deploy (Render free tier)
Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`. Build command: `pip install -r requirements.txt`.
Environment variables: `GEMINI_API_KEY`, `GOOGLE_CALENDAR_ID`, `API_SECRET`, `GOOGLE_SERVICE_ACCOUNT_JSON`
(full contents of service_account.json). Free instances sleep when idle, so the first request after a quiet period can take 30-60 seconds.

## Notes
- `/add` rejects requests without the right `X-API-Secret` header. Keep that secret private.
- The optional WhatsApp Cloud API webhook code stays dormant unless the WHATSAPP_/META_ variables are set.
- Free Gemini tier: Google may use prompts to improve its products; avoid sensitive details.
