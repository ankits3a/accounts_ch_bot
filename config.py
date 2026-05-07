import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
DRIVE_ROOT_FOLDER_ID = os.getenv("DRIVE_ROOT_FOLDER_ID", "root")
DATABASE_PATH = os.getenv("DATABASE_PATH", "invoices.db")
PENDING_TIMEOUT = int(os.getenv("PENDING_TIMEOUT", "120"))

_allowed_str = os.getenv("ALLOWED_USERS", "")
ALLOWED_USERS: list[int] = [int(x.strip()) for x in _allowed_str.split(",") if x.strip()]

ENTITIES = [
    "Chaurasia Enterprises India Private Limited",
    "Elarware Infra Private Limited",
    "Leelaraj Infratech Private Limited",
]

ENTITY_SHORT = {
    "Chaurasia Enterprises India Private Limited": "Chaurasia Enterprises",
    "Elarware Infra Private Limited": "Elarware Infra",
    "Leelaraj Infratech Private Limited": "Leelaraj Infratech",
}
