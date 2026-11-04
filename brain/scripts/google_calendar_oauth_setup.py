"""One-time browser OAuth flow for `modules/productivity/calendar_sync.py`.

Prerequisite: `config/google_credentials.json` (OAuth client ID JSON,
type "Desktop app", downloaded from Google Cloud Console with the
Calendar API enabled - see calendar_sync.py docstring for the full
setup steps).

Run: ./.venv/bin/python -m scripts.google_calendar_oauth_setup

Opens a browser for you to log in and grant read-only Calendar access,
then writes config/google_token.json (gitignored). After this,
`calendar_sync.upcoming_events()` works without further browser
interaction - the token auto-refreshes.
"""

import os

from google_auth_oauthlib.flow import InstalledAppFlow

from modules.productivity.calendar_sync import CREDENTIALS_PATH, SCOPES, TOKEN_PATH


def main():
    if not os.path.exists(CREDENTIALS_PATH):
        print(
            f"⚠️ 找不到 {CREDENTIALS_PATH}\n"
            "請先到 Google Cloud Console 建立 OAuth 用戶端（啟用 Calendar "
            "API，類型選 Desktop app），下載 JSON 並存成這個檔名。"
        )
        return

    flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
    creds = flow.run_local_server(port=0)

    with open(TOKEN_PATH, "w", encoding="utf-8") as f:
        f.write(creds.to_json())

    print(f"✅ 授權完成，已寫入 {TOKEN_PATH}")


if __name__ == "__main__":
    main()
