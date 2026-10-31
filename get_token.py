"""Run once on your laptop to authorize Google Calendar access.
Needs credentials.json (OAuth client, type "Desktop app") in this folder.
Writes token.json."""
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
creds = flow.run_local_server(port=0)
with open("token.json", "w") as f:
    f.write(creds.to_json())
print("Saved token.json")
