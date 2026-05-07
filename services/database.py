import sqlite3
import json
from datetime import date
from config import DATABASE_PATH, GEMINI_API_KEY, ENTITIES


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS invoices (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            entity          TEXT    NOT NULL,
            invoice_type    TEXT    NOT NULL,
            invoice_number  TEXT,
            invoice_date    TEXT,
            party_name      TEXT,
            party_gstin     TEXT,
            entity_gstin    TEXT,
            total_amount    REAL,
            taxable_amount  REAL,
            gst_amount      REAL,
            cgst            REAL,
            sgst            REAL,
            igst            REAL,
            description     TEXT,
            filename        TEXT,
            drive_party_url TEXT,
            drive_month_url TEXT,
            local_path      TEXT,
            telegram_user_id INTEGER,
            created_at      TEXT    DEFAULT CURRENT_TIMESTAMP,
            status          TEXT    DEFAULT 'confirmed'
        )
    """)
    conn.commit()
    conn.close()


def save_invoice(data: dict, filename: str, drive_party_url: str,
                 drive_month_url: str, local_path: str, telegram_user_id: int) -> int:
    conn = _conn()
    cur = conn.execute("""
        INSERT INTO invoices (
            entity, invoice_type, invoice_number, invoice_date,
            party_name, party_gstin, entity_gstin,
            total_amount, taxable_amount, gst_amount, cgst, sgst, igst,
            description, filename, drive_party_url, drive_month_url,
            local_path, telegram_user_id
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        data.get("entity"), data.get("invoice_type"),
        data.get("invoice_number"), data.get("invoice_date"),
        data.get("party_name"), data.get("party_gstin"),
        data.get("entity_gstin"), data.get("total_amount"),
        data.get("taxable_amount"), data.get("gst_amount"),
        data.get("cgst"), data.get("sgst"), data.get("igst"),
        data.get("description"), filename,
        drive_party_url, drive_month_url, local_path, telegram_user_id,
    ))
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def get_stats() -> dict:
    conn = _conn()
    total = conn.execute("SELECT COUNT(*) FROM invoices WHERE status='confirmed'").fetchone()[0]
    purchase = conn.execute(
        "SELECT COUNT(*) FROM invoices WHERE status='confirmed' AND invoice_type='purchase'"
    ).fetchone()[0]
    sale = conn.execute(
        "SELECT COUNT(*) FROM invoices WHERE status='confirmed' AND invoice_type='sale'"
    ).fetchone()[0]
    first_of_month = date.today().replace(day=1).isoformat()
    this_month = conn.execute(
        "SELECT COUNT(*) FROM invoices WHERE status='confirmed' AND invoice_date >= ?",
        (first_of_month,)
    ).fetchone()[0]
    tp = conn.execute(
        "SELECT COALESCE(SUM(total_amount),0) FROM invoices WHERE status='confirmed' AND invoice_type='purchase'"
    ).fetchone()[0]
    ts = conn.execute(
        "SELECT COALESCE(SUM(total_amount),0) FROM invoices WHERE status='confirmed' AND invoice_type='sale'"
    ).fetchone()[0]
    conn.close()
    return {"total": total, "purchase": purchase, "sale": sale,
            "this_month": this_month, "total_purchase": tp, "total_sale": ts}


# NL → SQL via Gemini
_NL_PROMPT = """
You are a SQL generator for an SQLite invoice database.

Table: invoices
Columns:
  id, entity (TEXT), invoice_type (TEXT: 'sale'|'purchase'),
  invoice_number (TEXT), invoice_date (TEXT: YYYY-MM-DD),
  party_name (TEXT), party_gstin (TEXT),
  total_amount (REAL), taxable_amount (REAL), gst_amount (REAL),
  cgst (REAL), sgst (REAL), igst (REAL),
  description (TEXT), filename (TEXT),
  drive_party_url (TEXT), drive_month_url (TEXT),
  created_at (TEXT), status (TEXT: 'confirmed'|'cancelled')

Entity values:
  "Chaurasia Enterprises India Private Limited"
  "Elarware Infra Private Limited"
  "Leelaraj Infratech Private Limited"

Today: {today}

User query: "{query}"

Rules:
- Always filter WHERE status = 'confirmed' unless user asks for cancelled.
- For listing results use LIMIT 15.
- For totals/counts use SUM/COUNT with clear column aliases.
- Match entity names partially with LIKE if needed.
- Return ONLY the raw SQL query. No markdown, no explanation.
"""


def query_natural_language(user_query: str) -> tuple:
    from services.gemini_service import generate_sql
    prompt = _NL_PROMPT.format(today=date.today().isoformat(), query=user_query)
    sql = generate_sql(prompt)

    conn = _conn()
    try:
        cur = conn.execute(sql)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        return rows, sql
    except Exception as e:
        raise ValueError(f"SQL error: {e}\n\nGenerated SQL:\n{sql}")
    finally:
        conn.close()
