import json
import asyncio
import logging
from pathlib import Path

from google import genai
from google.genai import types
from config import GEMINI_API_KEY

logger = logging.getLogger(__name__)

_client = genai.Client(api_key=GEMINI_API_KEY)
_MODEL = "gemini-2.0-flash"

_EXTRACTION_PROMPT = """
You are an expert invoice analysis AI for these three Indian companies:
1. Chaurasia Enterprises India Private Limited
2. Elarware Infra Private Limited
3. Leelaraj Infratech Private Limited

Carefully examine the invoice and extract the following.

KEY RULES:
- "invoice_type" is from the PERSPECTIVE OF OUR COMPANY (entity field):
    * Entity is the SELLER/SUPPLIER on the invoice → "sale"
    * Entity is the BUYER/RECIPIENT on the invoice → "purchase"
    * The same physical document can be a sale invoice for one entity and
      a purchase invoice for another entity — pick the one named on the invoice.
- "party_name" is the OTHER party (not our entity):
    * Purchase invoice → supplier/seller name
    * Sale invoice → client/buyer name
- "party_first_name": first meaningful word of party_name (used for filename).
- DATE PARSING — CRITICAL:
  * These are Indian business invoices. All are recent documents (2020 onwards).
  * Indian date order is ALWAYS Day → Month → Year. NEVER Month → Day → Year.
  * 2-digit years ALWAYS mean 20XX. "26" = 2026. "25" = 2025. "24" = 2024.
  * Examples of correct interpretation:
      "29-April-26"  = 29th April 2026      → "2026-04-29"
      "29/04/26"     = 29th April 2026      → "2026-04-29"
      "01.05.2026"   = 1st May 2026         → "2026-05-01"
      "5.1.26"       = 5th January 2026     → "2026-01-05"
      "31-12-25"     = 31st December 2025   → "2025-12-31"
      "15/03/2025"   = 15th March 2025      → "2025-03-15"
  * ALWAYS extract the INVOICE DATE / BILL DATE — the date the invoice was issued.
    Do NOT use: due date, supply date, delivery date, payment date, or any other date.
  * Output MUST be YYYY-MM-DD with 4-digit year, zero-padded month and day.
- All amounts must be numeric (no ₹ symbol, no commas).
- If a field cannot be determined, use null.

Return ONLY this JSON (no markdown, no extra text):
{
  "entity": "exact entity name from the list above",
  "invoice_type": "sale" or "purchase",
  "invoice_number": "invoice/bill number string",
  "invoice_date": "YYYY-MM-DD",
  "party_name": "full name of the other party",
  "party_first_name": "first word of party name",
  "party_gstin": "GSTIN of other party or null",
  "entity_gstin": "GSTIN of our entity or null",
  "total_amount": numeric,
  "taxable_amount": numeric,
  "gst_amount": numeric,
  "cgst": numeric or null,
  "sgst": numeric or null,
  "igst": numeric or null,
  "description": "brief 1-2 sentence description of goods/services",
  "confidence": "high" or "medium" or "low"
}
"""


def _parse_json(text: str) -> dict:
    text = text.strip()
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()
    start, end = text.find("{"), text.rfind("}") + 1
    if start >= 0 and end > start:
        text = text[start:end]
    return json.loads(text)


def _do_extract(file_path: str) -> dict:
    path = Path(file_path)
    suffix = path.suffix.lower()

    mime_map = {
        ".pdf": "application/pdf",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".tiff": "image/tiff",
        ".gif": "image/gif",
    }
    mime_type = mime_map.get(suffix, "application/octet-stream")

    # Upload file to Gemini Files API (works for PDFs and images)
    uploaded = _client.files.upload(
        file=str(path),
        config=types.UploadFileConfig(mime_type=mime_type, display_name=path.name),
    )
    try:
        response = _client.models.generate_content(
            model=_MODEL,
            contents=[uploaded, _EXTRACTION_PROMPT],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.1,
            ),
        )
        return _parse_json(response.text)
    finally:
        try:
            _client.files.delete(name=uploaded.name)
        except Exception:
            pass


async def extract_invoice(file_path: str) -> dict:
    """Run Gemini extraction in a thread (non-blocking)."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _do_extract, file_path)


# ── NL → SQL (used by database.py) ───────────────────────────────────────────

def generate_sql(prompt: str) -> str:
    response = _client.models.generate_content(
        model=_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(temperature=0.1),
    )
    sql = response.text.strip()
    if "```" in sql:
        parts = sql.split("```")
        sql = parts[1].strip()
        if sql.lower().startswith("sql"):
            sql = sql[3:].strip()
    return sql
