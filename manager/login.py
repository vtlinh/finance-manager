import asyncio
import os
from pathlib import Path

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from monarchmoney import MonarchMoney, RequireMFAException

load_dotenv()


_DIR = Path(__file__).parent


class Monarch:
    SESSION_FILE = str(_DIR / ".monarch_session")

    @staticmethod
    async def get_client() -> MonarchMoney:
        email = os.environ.get("MONARCH_EMAIL")
        password = os.environ.get("MONARCH_PASSWORD")
        if not email or not password:
            raise ValueError("Add MONARCH_EMAIL and MONARCH_PASSWORD to your .env file.")

        mm = MonarchMoney()

        if Path(Monarch.SESSION_FILE).exists():
            print("Loading saved Monarch session...")
            mm.load_session(Monarch.SESSION_FILE)
            try:
                await mm.get_accounts()
            except Exception:
                print("Session expired, re-logging in...")
                Path(Monarch.SESSION_FILE).unlink()
                await Monarch._login(mm, email, password)
        else:
            print("Logging in to Monarch Money...")
            await Monarch._login(mm, email, password)

        return mm

    @staticmethod
    async def _login(mm: MonarchMoney, email: str, password: str) -> None:
        try:
            await mm.login(email, password)
        except RequireMFAException:
            code = input("Enter MFA / OTP code: ").strip()
            await mm.multi_factor_authenticate(email, password, code)
        mm.save_session(Monarch.SESSION_FILE)
        print("Session saved.")


class Google:
    SCOPES = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.file",
    ]
    CREDS_FILE = str(_DIR / "credentials.json")
    TOKEN_FILE = str(_DIR / ".gsheets_token.json")

    @staticmethod
    def get_credentials() -> Credentials:
        creds = None
        if Path(Google.TOKEN_FILE).exists():
            creds = Credentials.from_authorized_user_file(Google.TOKEN_FILE, Google.SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(Google.CREDS_FILE, Google.SCOPES)
                creds = flow.run_local_server(port=0)
            with open(Google.TOKEN_FILE, "w") as f:
                f.write(creds.to_json())
        return creds
