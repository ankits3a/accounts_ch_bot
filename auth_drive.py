"""
One-time Google Drive authorization via browser (OAuth2 Desktop App flow).

Run this ONCE:
    python auth_drive.py

It opens your browser, you log in with your Google account, and it saves
token.json which the bot uses for all future Drive operations.

Prerequisites:
  1. client_secret.json must be in this directory
     (downloaded from Google Cloud Console → APIs & Services → Credentials → OAuth 2.0 Client)
"""

import os
import sys
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/drive"]
CLIENT_SECRET_FILE = "client_secret.json"
TOKEN_FILE = "token.json"


def main():
    if not os.path.isfile(CLIENT_SECRET_FILE):
        print(f"❌ '{CLIENT_SECRET_FILE}' not found in current directory.")
        print()
        print("Steps to get it:")
        print("  1. Go to https://console.cloud.google.com")
        print("  2. APIs & Services → Credentials → Create Credentials → OAuth 2.0 Client IDs")
        print("  3. Application type: Desktop app  (name it anything)")
        print("  4. Click 'Download JSON' → save as client_secret.json here")
        print("  5. Re-run this script")
        sys.exit(1)

    print("Opening browser for Google authorization…")
    print("Log in with the Google account that owns the Drive folder.\n")

    flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_FILE, SCOPES)
    creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")

    with open(TOKEN_FILE, "w") as f:
        f.write(creds.to_json())

    print(f"\nAuthorization complete! '{TOKEN_FILE}' saved.")
    print("You can now run the bot:  python bot.py")
    print()
    print("Next: update DRIVE_ROOT_FOLDER_ID in .env with your shared folder ID.")


if __name__ == "__main__":
    main()
