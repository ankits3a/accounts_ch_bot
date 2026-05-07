"""
accounts_ch_bot — Invoice capture bot for:
  • Chaurasia Enterprises India Private Limited
  • Elarware Infra Private Limited
  • Leelaraj Infratech Private Limited
"""

import asyncio
import html
import logging
import os
import re
from datetime import date
from pathlib import Path

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import (
    ALLOWED_USERS,
    DATABASE_PATH,
    DRIVE_ROOT_FOLDER_ID,
    ENTITIES,
    ENTITY_SHORT,
    PENDING_TIMEOUT,
    TELEGRAM_TOKEN,
)
from services import database, gemini_service
from services import drive_service as drive_module
from utils import fy_utils, pdf_utils

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── In-memory state ───────────────────────────────────────────────────────────
# pending_invoices[chat_id] = {
#   data, temp_path, pdf_path, original_filename, user_id,
#   auto_save_task, summary_msg_id
# }
pending_invoices: dict[int, dict] = {}

# user_states[chat_id] = {"state": "editing", "field": "invoice_number"}
user_states: dict[int, dict] = {}


# ── Auth ──────────────────────────────────────────────────────────────────────

def _authorized(user_id: int) -> bool:
    return not ALLOWED_USERS or user_id in ALLOWED_USERS


# ── Keyboards ─────────────────────────────────────────────────────────────────

def _action_kb(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm & Save", callback_data=f"confirm|{chat_id}"),
        InlineKeyboardButton("✏️ Edit",           callback_data=f"edit|{chat_id}"),
        InlineKeyboardButton("❌ Cancel",          callback_data=f"cancel|{chat_id}"),
    ]])


def _edit_kb(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🏢 Entity",      callback_data=f"ef|entity|{chat_id}"),
            InlineKeyboardButton("📋 Type",         callback_data=f"ef|invoice_type|{chat_id}"),
        ],
        [
            InlineKeyboardButton("🔢 Invoice No",  callback_data=f"ef|invoice_number|{chat_id}"),
            InlineKeyboardButton("📅 Date",         callback_data=f"ef|invoice_date|{chat_id}"),
        ],
        [
            InlineKeyboardButton("👤 Party Name",  callback_data=f"ef|party_name|{chat_id}"),
            InlineKeyboardButton("💰 Amount",       callback_data=f"ef|total_amount|{chat_id}"),
        ],
        [InlineKeyboardButton("◀️ Back to Summary", callback_data=f"back|{chat_id}")],
    ])


def _entity_kb(chat_id: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(ENTITY_SHORT[e], callback_data=f"sv|entity|{i}|{chat_id}")]
            for i, e in enumerate(ENTITIES)]
    rows.append([InlineKeyboardButton("◀️ Back", callback_data=f"edit|{chat_id}")])
    return InlineKeyboardMarkup(rows)


def _type_kb(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📤 Sale",     callback_data=f"sv|invoice_type|sale|{chat_id}"),
            InlineKeyboardButton("📥 Purchase", callback_data=f"sv|invoice_type|purchase|{chat_id}"),
        ],
        [InlineKeyboardButton("◀️ Back", callback_data=f"edit|{chat_id}")],
    ])


# ── Summary formatter ─────────────────────────────────────────────────────────

def _fmt_amount(v) -> str:
    return f"₹{v:,.2f}" if v is not None else "N/A"


def _fmt_date(iso_str: str) -> str:
    try:
        d = date.fromisoformat(iso_str)
        return d.strftime("%d %B %Y")
    except Exception:
        return iso_str or "N/A"


def format_summary(data: dict, timeout_sec: int | None = None) -> str:
    e = html.escape
    inv_type = data.get("invoice_type", "")
    party_label = "Supplier" if inv_type == "purchase" else "Client"

    cgst, sgst, igst = data.get("cgst"), data.get("sgst"), data.get("igst")
    if cgst is not None and sgst is not None:
        tax_detail = f" (CGST: {_fmt_amount(cgst)} + SGST: {_fmt_amount(sgst)})"
    elif igst is not None:
        tax_detail = f" (IGST: {_fmt_amount(igst)})"
    else:
        tax_detail = ""

    confidence = data.get("confidence", "")
    conf_icon = {"high": "🟢", "medium": "🟡", "low": "🔴"}.get(confidence, "")

    timeout_line = (
        f"\n\n⏱ <i>Auto-saving in {timeout_sec}s if no action is taken.</i>"
        if timeout_sec else ""
    )

    return (
        f"📄 <b>Invoice Processed</b> {conf_icon}\n\n"
        f"🏢 <b>Entity:</b> {e(data.get('entity') or 'Unknown')}\n"
        f"📋 <b>Type:</b> {e(inv_type.title())} Invoice\n"
        f"🔢 <b>Invoice No:</b> {e(str(data.get('invoice_number') or 'N/A'))}\n"
        f"📅 <b>Date:</b> {e(_fmt_date(data.get('invoice_date') or ''))}\n"
        f"👤 <b>{party_label}:</b> {e(data.get('party_name') or 'N/A')}\n"
        f"💰 <b>Total:</b> {_fmt_amount(data.get('total_amount'))}\n"
        f"   ↳ Taxable: {_fmt_amount(data.get('taxable_amount'))}\n"
        f"   ↳ GST: {_fmt_amount(data.get('gst_amount'))}{e(tax_detail)}\n"
        f"📝 <b>Description:</b> {e(data.get('description') or 'N/A')}"
        f"{timeout_line}"
    )


# ── File helpers ──────────────────────────────────────────────────────────────

def _sanitize(name: str) -> str:
    return re.sub(r"[^\w\-]", "_", name)


def _build_filename(data: dict) -> str:
    first = _sanitize(data.get("party_first_name") or (data.get("party_name") or "Unknown").split()[0])
    inv_no = _sanitize(data.get("invoice_number") or "Unknown")
    return f"{first}_{inv_no}.pdf"


def _invoice_date(data: dict) -> date:
    """Parse invoice_date from extracted data using robust multi-format parser."""
    return _parse_date(data.get("invoice_date") or "") or date.today()


def _local_save_path(data: dict, filename: str) -> str:
    """Mirror of the Party_Invoices folder structure on disk."""
    inv_type = data.get("invoice_type", "purchase")
    inv_date = _invoice_date(data)

    type_dir = "Purchase_Invoices" if inv_type == "purchase" else "Sale_Invoices"
    party_name = _sanitize(data.get("party_name") or "Unknown")
    fy = fy_utils.get_financial_year(inv_date)
    month = fy_utils.get_month_folder(inv_date)
    entity = _sanitize(data.get("entity") or "Unknown")

    path = Path("invoices") / entity / "Party_Invoices" / type_dir / fy / party_name / month
    path.mkdir(parents=True, exist_ok=True)
    return str(path / filename)


def _cleanup(*paths):
    for p in paths:
        if p and os.path.isfile(p):
            try:
                os.remove(p)
            except Exception:
                pass


# ── Save logic ────────────────────────────────────────────────────────────────

async def _do_save(chat_id: int, bot) -> str:
    """Upload to Drive (if configured) + local disk + DB. Returns a result message."""
    pending = pending_invoices.get(chat_id)
    if not pending:
        raise ValueError("No pending invoice.")

    data = pending["data"]
    pdf_path = pending["pdf_path"]
    user_id = pending["user_id"]

    filename = _build_filename(data)

    # Resolve date using robust multi-format parser
    inv_date = _invoice_date(data)

    fy = fy_utils.get_financial_year(inv_date)
    month_folder = fy_utils.get_month_folder(inv_date)
    entity = data.get("entity", "Unknown")
    party_name = data.get("party_name", "Unknown")
    inv_type = data.get("invoice_type", "purchase")

    # ── Local copy (always) ───────────────────────────────────────────────────
    local_path = _local_save_path(data, filename)
    import shutil
    shutil.copy2(pdf_path, local_path)

    # ── Mirror for Month_Invoices locally ─────────────────────────────────────
    type_dir = "Purchase" if inv_type == "purchase" else "Sale"
    month_local = Path("invoices") / entity / "Month_Invoices" / type_dir / fy / month_folder
    month_local.mkdir(parents=True, exist_ok=True)
    shutil.copy2(pdf_path, month_local / filename)

    # ── Google Drive (optional) ───────────────────────────────────────────────
    drive_party_url = ""
    drive_month_url = ""

    if drive_module.is_configured():
        try:
            drive = drive_module.DriveService()
            party_folder = await asyncio.to_thread(
                drive.get_party_invoices_folder,
                entity, inv_type, fy, party_name, month_folder, DRIVE_ROOT_FOLDER_ID,
            )
            drive_party_url = await asyncio.to_thread(
                drive.upload_file, pdf_path, filename, party_folder
            )
            month_folder_id = await asyncio.to_thread(
                drive.get_month_invoices_folder,
                entity, inv_type, fy, month_folder, DRIVE_ROOT_FOLDER_ID,
            )
            drive_month_url = await asyncio.to_thread(
                drive.upload_file, pdf_path, filename, month_folder_id
            )
        except Exception as exc:
            logger.error("Drive upload failed: %s", exc, exc_info=True)
            drive_party_url = "(Drive upload failed)"
            drive_month_url = "(Drive upload failed)"

    # ── Database ──────────────────────────────────────────────────────────────
    invoice_id = await asyncio.to_thread(
        database.save_invoice,
        data, filename, drive_party_url, drive_month_url, local_path, user_id,
    )

    # ── Build result message ──────────────────────────────────────────────────
    lines = [
        f"✅ <b>Invoice Saved</b>\n",
        f"📋 {html.escape(ENTITY_SHORT.get(entity, entity))} | {inv_type.title()} Invoice",
        f"📁 Filename: <code>{html.escape(filename)}</code>",
        f"📅 {fy} / {month_folder}",
        f"🆔 Record ID: #{invoice_id}",
    ]
    if drive_party_url and not drive_party_url.startswith("("):
        lines.append(f'\n🔗 <a href="{drive_party_url}">Party Folder</a>  '
                     f'<a href="{drive_month_url}">Month Folder</a>')
    else:
        lines.append(f"\n💾 Saved locally: <code>{html.escape(local_path)}</code>")

    return "\n".join(lines)


# ── Auto-save task ────────────────────────────────────────────────────────────

async def _auto_save_task(chat_id: int, app: Application):
    await asyncio.sleep(PENDING_TIMEOUT)
    if chat_id not in pending_invoices:
        return
    try:
        result_msg = await _do_save(chat_id, app.bot)
        await app.bot.send_message(
            chat_id,
            f"⏱ <i>Auto-saved (no action received).</i>\n\n{result_msg}",
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.error("Auto-save error: %s", exc, exc_info=True)
        await app.bot.send_message(chat_id, f"❌ Auto-save failed: {html.escape(str(exc))}", parse_mode="HTML")
    finally:
        _cleanup(
            pending_invoices.get(chat_id, {}).get("temp_path"),
            pending_invoices.get(chat_id, {}).get("pdf_path"),
        )
        pending_invoices.pop(chat_id, None)


# ── Command handlers ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update.effective_user.id):
        return
    await update.message.reply_html(
        "👋 <b>Welcome to Accounts Bot!</b>\n\n"
        "Send me an invoice (<b>PDF or image</b>) and I'll:\n"
        "• Extract all details using AI vision\n"
        "• Ask you to confirm, edit, or cancel\n"
        "• Save to Google Drive + local storage + database\n\n"
        "💬 <b>You can also ask questions:</b>\n"
        "• <i>List purchase invoices from ABC Hardware</i>\n"
        "• <i>Show total sales in April 2026</i>\n"
        "• <i>Invoices for Elarware this month</i>\n\n"
        "Type /help for more info."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html(
        "📋 <b>Commands</b>\n"
        "/start — Welcome\n"
        "/help  — This message\n"
        "/status — Database summary\n"
        "/pending — Show current pending invoice\n\n"
        "📤 <b>Upload Invoice</b>\n"
        "Send any PDF or image. The bot extracts:\n"
        "Entity · Type · Invoice No · Date · Party · Amounts\n\n"
        "💬 <b>Query Examples</b>\n"
        "• <i>List purchase invoices from ABC</i>\n"
        "• <i>Total sales in April 2026</i>\n"
        "• <i>Invoices above ₹50,000</i>\n"
        "• <i>Show Elarware purchase invoices this FY</i>"
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update.effective_user.id):
        return
    stats = await asyncio.to_thread(database.get_stats)
    await update.message.reply_html(
        f"📊 <b>Database Stats</b>\n\n"
        f"Total invoices : {stats['total']}\n"
        f"  Purchase : {stats['purchase']}\n"
        f"  Sale     : {stats['sale']}\n"
        f"This month   : {stats['this_month']}\n\n"
        f"Total Purchase Value : {_fmt_amount(stats['total_purchase'])}\n"
        f"Total Sale Value     : {_fmt_amount(stats['total_sale'])}"
    )


async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    pending = pending_invoices.get(chat_id)
    if not pending:
        await update.message.reply_text("No pending invoice.")
        return
    await update.message.reply_html(
        format_summary(pending["data"]),
        reply_markup=_action_kb(chat_id),
    )


# ── File handlers ─────────────────────────────────────────────────────────────

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    doc = update.message.document

    allowed_mimes = {
        "application/pdf", "image/jpeg", "image/png",
        "image/webp", "image/bmp", "image/tiff",
    }
    if doc.mime_type not in allowed_mimes:
        await update.message.reply_text("⚠️ Please send a PDF or image file (JPG, PNG, PDF, WebP).")
        return

    status_msg = await update.message.reply_text("📥 Downloading invoice…")
    os.makedirs("temp", exist_ok=True)
    tg_file = await doc.get_file()
    temp_path = f"temp/{chat_id}_{doc.file_name}"
    await tg_file.download_to_drive(temp_path)

    await status_msg.edit_text("🔍 Analysing with AI Vision…")
    await _process_file(update, context, temp_path, doc.file_name, status_msg)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update.effective_user.id):
        return
    chat_id = update.effective_chat.id
    photo = update.message.photo[-1]

    status_msg = await update.message.reply_text("📥 Downloading image…")
    os.makedirs("temp", exist_ok=True)
    tg_file = await photo.get_file()
    temp_path = f"temp/{chat_id}_{photo.file_id}.jpg"
    await tg_file.download_to_drive(temp_path)

    await status_msg.edit_text("🔍 Analysing with AI Vision…")
    await _process_file(update, context, temp_path, f"{photo.file_id}.jpg", status_msg)


async def _process_file(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    temp_path: str,
    original_filename: str,
    status_msg,
):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    # Cancel any previous pending for this chat
    old = pending_invoices.pop(chat_id, None)
    if old:
        task = old.get("auto_save_task")
        if task and not task.done():
            task.cancel()
        _cleanup(old.get("temp_path"), old.get("pdf_path"))

    try:
        # Convert image → PDF for storage
        if pdf_utils.is_image(original_filename):
            pdf_path = temp_path.rsplit(".", 1)[0] + "_converted.pdf"
            pdf_bytes = await asyncio.to_thread(pdf_utils.image_to_pdf_bytes, temp_path)
            Path(pdf_path).write_bytes(pdf_bytes)
        else:
            pdf_path = temp_path

        # Gemini extraction (uses original file for best quality)
        data = await gemini_service.extract_invoice(temp_path)

        pending_invoices[chat_id] = {
            "data": data,
            "temp_path": temp_path,
            "pdf_path": pdf_path,
            "original_filename": original_filename,
            "user_id": user_id,
        }

        summary_msg = await context.bot.send_message(
            chat_id,
            format_summary(data, PENDING_TIMEOUT),
            parse_mode="HTML",
            reply_markup=_action_kb(chat_id),
        )
        pending_invoices[chat_id]["summary_msg_id"] = summary_msg.message_id

        try:
            await status_msg.delete()
        except Exception:
            pass

        task = asyncio.create_task(_auto_save_task(chat_id, context.application))
        pending_invoices[chat_id]["auto_save_task"] = task

    except Exception as exc:
        logger.error("Processing error: %s", exc, exc_info=True)
        await status_msg.edit_text(
            f"❌ Could not process invoice.\n\n<code>{html.escape(str(exc))}</code>\n\n"
            "Please try again with a clearer image or PDF.",
            parse_mode="HTML",
        )
        _cleanup(temp_path)


# ── Callback handler ──────────────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split("|")
    action = parts[0]
    chat_id = int(parts[-1])

    if action == "confirm":
        await _cb_confirm(chat_id, query, context)
    elif action == "cancel":
        await _cb_cancel(chat_id, query)
    elif action == "edit":
        await _cb_edit_menu(chat_id, query)
    elif action == "back":
        await _cb_back(chat_id, query)
    elif action == "ef":
        field = parts[1]
        await _cb_edit_field(chat_id, field, query, context)
    elif action == "sv":
        field, value = parts[1], parts[2]
        await _cb_set_value(chat_id, field, value, query)


async def _cb_confirm(chat_id: int, query, context):
    pending = pending_invoices.get(chat_id)
    if not pending:
        await query.edit_message_text("⚠️ No pending invoice found.")
        return

    task = pending.get("auto_save_task")
    if task and not task.done():
        task.cancel()

    await query.edit_message_text("💾 Saving invoice…")
    try:
        result_msg = await _do_save(chat_id, context.bot)
        await context.bot.send_message(chat_id, result_msg, parse_mode="HTML")
    except Exception as exc:
        logger.error("Save error: %s", exc, exc_info=True)
        await context.bot.send_message(
            chat_id, f"❌ Save failed: {html.escape(str(exc))}", parse_mode="HTML"
        )
    finally:
        _cleanup(
            pending_invoices.get(chat_id, {}).get("temp_path"),
            pending_invoices.get(chat_id, {}).get("pdf_path"),
        )
        pending_invoices.pop(chat_id, None)


async def _cb_cancel(chat_id: int, query):
    pending = pending_invoices.pop(chat_id, None)
    if pending:
        task = pending.get("auto_save_task")
        if task and not task.done():
            task.cancel()
        _cleanup(pending.get("temp_path"), pending.get("pdf_path"))
    await query.edit_message_text("❌ Invoice discarded.")


async def _cb_edit_menu(chat_id: int, query):
    pending = pending_invoices.get(chat_id)
    if not pending:
        await query.edit_message_text("⚠️ No pending invoice.")
        return
    user_states.pop(chat_id, None)
    await query.edit_message_text(
        "✏️ <b>Edit Invoice</b>\n\nSelect the field to correct:",
        parse_mode="HTML",
        reply_markup=_edit_kb(chat_id),
    )


async def _cb_back(chat_id: int, query):
    pending = pending_invoices.get(chat_id)
    if not pending:
        await query.edit_message_text("⚠️ No pending invoice.")
        return
    user_states.pop(chat_id, None)
    await query.edit_message_text(
        format_summary(pending["data"]),
        parse_mode="HTML",
        reply_markup=_action_kb(chat_id),
    )


async def _cb_edit_field(chat_id: int, field: str, query, context):
    if field == "entity":
        await query.edit_message_text("🏢 Select the correct entity:", reply_markup=_entity_kb(chat_id))
        return
    if field == "invoice_type":
        await query.edit_message_text("📋 Select the correct invoice type:", reply_markup=_type_kb(chat_id))
        return

    prompts = {
        "invoice_number": "🔢 Type the correct <b>Invoice Number</b>:",
        "invoice_date":   "📅 Type the correct <b>Invoice Date</b> (DD/MM/YYYY or YYYY-MM-DD):",
        "party_name":     "👤 Type the correct <b>Party Name</b> (supplier / client):",
        "total_amount":   "💰 Type the correct <b>Total Amount</b> (numbers only, e.g. <code>45000</code>):",
    }
    prompt = prompts.get(field, f"Type the correct value for <b>{field}</b>:")

    user_states[chat_id] = {"state": "editing", "field": field}
    await query.edit_message_text(
        f"{prompt}\n\n<i>Just type your reply in the chat. Send /cancel_edit to go back.</i>",
        parse_mode="HTML",
    )


async def _cb_set_value(chat_id: int, field: str, value: str, query):
    pending = pending_invoices.get(chat_id)
    if not pending:
        await query.edit_message_text("⚠️ No pending invoice.")
        return

    if field == "entity":
        idx = int(value)
        pending["data"]["entity"] = ENTITIES[idx]
    elif field == "invoice_type":
        pending["data"]["invoice_type"] = value

    user_states.pop(chat_id, None)
    await query.edit_message_text(
        f"✅ Updated!\n\n{format_summary(pending['data'])}",
        parse_mode="HTML",
        reply_markup=_action_kb(chat_id),
    )


# ── Text handler ──────────────────────────────────────────────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update.effective_user.id):
        return

    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    if text == "/cancel_edit":
        user_states.pop(chat_id, None)
        pending = pending_invoices.get(chat_id)
        if pending:
            await update.message.reply_html(
                format_summary(pending["data"]),
                reply_markup=_action_kb(chat_id),
            )
        return

    # In editing mode?
    state = user_states.get(chat_id)
    if state and state.get("state") == "editing":
        await _handle_edit_value(update, context, text, state)
        return

    # Natural language query
    await _handle_nl_query(update, context, text)


async def _handle_edit_value(update: Update, context, text: str, state: dict):
    chat_id = update.effective_chat.id
    field = state["field"]
    pending = pending_invoices.get(chat_id)
    if not pending:
        await update.message.reply_text("⚠️ No pending invoice.")
        user_states.pop(chat_id, None)
        return

    # Parse & validate
    if field == "invoice_date":
        parsed_date = _parse_date(text)
        if not parsed_date:
            await update.message.reply_text(
                "❌ Invalid date. Use DD/MM/YYYY or YYYY-MM-DD (e.g. 15/04/2026 or 2026-04-15)."
            )
            return
        pending["data"]["invoice_date"] = parsed_date.isoformat()
    elif field == "total_amount":
        amt = _parse_amount(text)
        if amt is None:
            await update.message.reply_text("❌ Enter a valid number, e.g. <code>45000</code>.", parse_mode="HTML")
            return
        pending["data"]["total_amount"] = amt
    else:
        pending["data"][field] = text
        if field == "party_name":
            pending["data"]["party_first_name"] = text.split()[0]

    user_states.pop(chat_id, None)
    await update.message.reply_html(
        f"✅ Updated!\n\n{format_summary(pending['data'])}",
        reply_markup=_action_kb(chat_id),
    )


def _parse_date(text: str) -> date | None:
    """Parse Indian date formats: DD.MM.YYYY, D/M/YY, DD-MM-YY, etc.
    Always interprets as day-first (DD MM YYYY), never US month-first order.
    """
    from datetime import datetime
    text = text.strip()

    # Normalise: collapse multiple spaces, strip stray ₹ etc.
    text = re.sub(r"\s+", " ", text)

    # Try unambiguous ISO / year-first formats first
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass

    # All remaining formats are DD MM YYYY order (Indian standard)
    # %d and %m in strptime accept both padded (01) and unpadded (1) digits
    indian_formats = [
        "%d.%m.%Y", "%d.%m.%y",        # 01.05.2026  /  1.5.26
        "%d/%m/%Y", "%d/%m/%y",        # 01/05/2026  /  1/5/26
        "%d-%m-%Y", "%d-%m-%y",        # 01-05-2026  /  1-5-26
        "%d %m %Y", "%d %m %y",        # 01 05 2026  /  1 5 26
        "%d %b %Y", "%d %b %y",        # 01 May 2026 /  01 May 26
        "%d %B %Y", "%d %B %y",        # 01 May 2026 /  01 May 26 (full name)
        "%d-%b-%Y", "%d-%b-%y",        # 01-May-2026 /  01-May-26
        "%d/%b/%Y", "%d/%b/%y",        # 01/May/2026 /  01/May/26
        "%d-%B-%Y", "%d-%B-%y",        # 01-May-2026 /  29-April-26
        "%d/%B/%Y", "%d/%B/%y",        # 01/May/2026 /  29/April/26
    ]
    for fmt in indian_formats:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def _parse_amount(text: str) -> float | None:
    cleaned = re.sub(r"[₹,\s]", "", text)
    try:
        return float(cleaned)
    except ValueError:
        return None


async def _handle_nl_query(update: Update, context, text: str):
    msg = await update.message.reply_text("🔍 Querying database…")
    try:
        rows, _sql = await asyncio.to_thread(database.query_natural_language, text)
        if not rows:
            await msg.edit_text("📭 No matching invoices found.")
            return
        await msg.edit_text(_format_query_results(rows), parse_mode="HTML")
    except Exception as exc:
        logger.error("NL query error: %s", exc, exc_info=True)
        await msg.edit_text(f"❌ Query error:\n<code>{html.escape(str(exc))}</code>", parse_mode="HTML")


def _format_query_results(rows: list) -> str:
    if not rows:
        return "📭 No results."

    first = rows[0]
    keys = list(first.keys())

    # Aggregate result (single row with sum/count columns)
    is_agg = len(rows) == 1 and any(
        k.lower() in ("sum", "count", "total", "total_amount") or
        k.lower().startswith(("sum_", "count_", "total_"))
        for k in keys
    )
    if is_agg:
        lines = ["📊 <b>Query Result</b>\n"]
        for k, v in first.items():
            label = k.replace("_", " ").title()
            if isinstance(v, (int, float)) and "amount" in k.lower():
                lines.append(f"<b>{label}:</b> {_fmt_amount(v)}")
            else:
                lines.append(f"<b>{label}:</b> {html.escape(str(v))}")
        return "\n".join(lines)

    # Listing
    total = len(rows)
    lines = [f"📋 <b>Found {total} invoice(s)</b>\n"]
    for i, row in enumerate(rows[:15], 1):
        inv_no = row.get("invoice_number") or "N/A"
        inv_date = _fmt_date(row.get("invoice_date") or "")
        party = row.get("party_name") or "N/A"
        total_amt = row.get("total_amount")
        entity = ENTITY_SHORT.get(row.get("entity", ""), row.get("entity", ""))
        inv_type = (row.get("invoice_type") or "").title()

        amt_str = _fmt_amount(total_amt) if total_amt is not None else "N/A"
        lines.append(
            f"{i}. <b>{html.escape(inv_no)}</b> ({html.escape(inv_date)})\n"
            f"   {html.escape(entity)} · {html.escape(inv_type)} · {html.escape(party)}\n"
            f"   {amt_str}"
        )
    if total > 15:
        lines.append(f"\n<i>…and {total - 15} more</i>")
    return "\n\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    database.init_db()
    os.makedirs("temp", exist_ok=True)
    os.makedirs("invoices", exist_ok=True)

    drive_status = "✅ configured" if drive_module.is_configured() else "⚠️  credentials.json not found — will save locally only"
    logger.info("Google Drive: %s", drive_status)
    logger.info("Database: %s", DATABASE_PATH)
    logger.info("Pending timeout: %ds", PENDING_TIMEOUT)

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot started. Polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
