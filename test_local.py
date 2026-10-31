"""Local check: Gemini parsing + Google Calendar, without WhatsApp.
Usage:  python test_local.py "Dentist tomorrow 4pm"
This creates a REAL event on your calendar. Delete it afterwards."""
import json
import sys

import main

text = " ".join(sys.argv[1:]) or "Test event from my bot tomorrow 4pm"
print("Message:", text)

ev = main.parse_event(text)
print("Parsed by Gemini:", json.dumps(ev, indent=2))

if ev.get("is_event") and ev.get("title") and ev.get("start"):
    link, summary = main.create_event(ev)
    print("Created on calendar:", summary)
    print(link)
else:
    print("Gemini did not treat that as an event.")
