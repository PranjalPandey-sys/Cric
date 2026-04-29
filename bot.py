"""Cricway Enterprise Support Bot — entry point."""
from __future__ import annotations

import logging
import os
from datetime import datetime, time, timezone
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import admin
import tickets
from ai import AI_FALLBACK_TEXT, faq_match, faq_suggest, get_ai_response
from database import connect, format_ticket_id, init_db, log_event, now_iso
from ui import card, md, show_screen

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

print("🚀 BOT STARTING...")
print("TOKEN FOUND:", bool(os.getenv("TOKEN")))
print("GEMINI FOUND:", bool(os.getenv("GEMINI_API_KEY")))

TOKEN = os.getenv("TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

if not TOKEN:
    raise ValueError("❌ TOKEN is missing! Set it in environment variables.")

SPAM_REPEAT_LIMIT = 3
PENDING_TICKET_KEY = "pending_ticket_id"

# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------


def home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🆘 Support Center", callback_data="usr_support"),
            InlineKeyboardButton("🤖 AI Assistant", callback_data="usr_ai"),
        ],
        [
            InlineKeyboardButton("🎫 My Tickets", callback_data="usr_tickets"),
            InlineKeyboardButton("📊 Live Status", callback_data="usr_status"),
        ],
        [
            InlineKeyboardButton("📚 Help Center", callback_data="usr_faq"),
            InlineKeyboardButton("⚠️ Safety Info", callback_data="usr_safety"),
        ],
    ])


def back_home_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🏠 Home", callback_data="usr_home")]]
    )


def status_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="usr_status_refresh")],
        [InlineKeyboardButton("🏠 Home", callback_data="usr_home")],
    ])


def support_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎫 My Tickets", callback_data="usr_tickets")],
        [InlineKeyboardButton("📚 Browse FAQ", callback_data="usr_faq")],
        [InlineKeyboardButton("🏠 Home", callback_data="usr_home")],
    ])


def ai_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🆘 Talk to Human", callback_data="usr_support")],
        [InlineKeyboardButton("🏠 Home", callback_data="usr_home")],
    ])


def tickets_list_keyboard(tlist: list[dict]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for t in tlist[:6]:
        rows.append([
            InlineKeyboardButton(
                f"{tickets.STATUS_EMOJI.get(t['status'], '•')} {format_ticket_id(t['ticket_id'])}",
                callback_data=f"tkt_view_{t['ticket_id']}",
            )
        ])
    rows.append([InlineKeyboardButton("🏠 Home", callback_data="usr_home")])
    return InlineKeyboardMarkup(rows)


def ticket_view_keyboard(ticket_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data=f"tkt_view_{ticket_id}")],
        [InlineKeyboardButton("🎫 All Tickets", callback_data="usr_tickets")],
        [InlineKeyboardButton("🏠 Home", callback_data="usr_home")],
    ])


def ai_followup_keyboard(ticket_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💬 Not satisfied? Talk to a human", callback_data=f"tkt_escalate_{ticket_id}")],
        [InlineKeyboardButton(f"🎫 View ticket {format_ticket_id(ticket_id)}", callback_data=f"tkt_view_{ticket_id}")],
        [InlineKeyboardButton("🏠 Home", callback_data="usr_home")],
    ])


def escalated_keyboard(ticket_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🎫 View ticket {format_ticket_id(ticket_id)}", callback_data=f"tkt_view_{ticket_id}")],
        [InlineKeyboardButton("🏠 Home", callback_data="usr_home")],
    ])


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def upsert_user(user_id: int, username: Optional[str], first_name: Optional[str]) -> None:
    ts = now_iso()
    with connect() as con:
        con.execute(
            "INSERT INTO users (user_id, username, first_name, first_seen, last_active, total_requests) "
            "VALUES (?, ?, ?, ?, ?, 0) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "username = excluded.username, first_name = excluded.first_name, "
            "last_active = excluded.last_active",
            (user_id, username, first_name, ts, ts),
        )


def increment_request_count(user_id: int) -> None:
    with connect() as con:
        con.execute(
            "UPDATE users SET total_requests = total_requests + 1, last_active = ? WHERE user_id = ?",
            (now_iso(), user_id),
        )


def fetch_user(user_id: int) -> Optional[dict]:
    with connect() as con:
        row = con.execute(
            "SELECT user_id, username, first_name, first_seen, total_requests "
            "FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


def display_name(user) -> str:
    if not user:
        return "there"
    return user.first_name or user.username or "there"


def short_subject(text: str, n: int = 60) -> str:
    text = (text or "").replace("\n", " ").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def fmt_ts(ts: str) -> str:
    if not ts:
        return "—"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%d %b %Y, %H:%M UTC")
    except ValueError:
        return ts[:19].replace("T", " ")


# ---------------------------------------------------------------------------
# Screen builders
# ---------------------------------------------------------------------------


def build_home_screen(user, user_row: Optional[dict]) -> str:
    name = display_name(user)
    is_returning = bool(user_row and user_row.get("total_requests", 0) > 0)
    greeting = (
        f"Welcome back, *{md(name)}* 👋"
        if is_returning
        else f"Welcome aboard, *{md(name)}* 👋"
    )
    subtitle = "Enterprise Support · 24/7"
    body = (
        f"{greeting}\n\n"
        "We're here to make every match smooth — pick an option below to get started."
    )
    actions = (
        "🆘 Support · 🤖 AI · 🎫 Tickets\n"
        "📊 Status · 📚 Help · ⚠️ Safety"
    )
    footer = "💡 _One team. One goal. Your satisfaction._"
    return card("Cricway Support", subtitle, body, actions, footer)


def build_support_screen(user) -> str:
    name = display_name(user)
    body = (
        f"Hi *{md(name)}* — describe your issue in a single message and we'll open a tracked ticket instantly.\n\n"
        "*How it works*\n"
        "1️⃣  *Describe your issue* — share details in your own words.\n"
        "2️⃣  *Get an instant ticket* — we generate a `CRIC-XXXX` id.\n"
        "3️⃣  *Expert team review* — AI first, then a human if needed.\n"
        "4️⃣  *Real-time updates* — replies arrive right here."
    )
    actions = "🟢 General · 🟡 Urgent · 🔴 Critical"
    footer = "💡 *Tip:* be clear and include any IDs / details — it speeds things up."
    return card("Support Center", "We're here to help, 24/7", body, actions, footer)


def build_ai_screen(user) -> str:
    name = display_name(user)
    body = (
        f"Hello *{md(name)}* — I'm Cricway AI, your always-on assistant.\n\n"
        "Type your question below and I'll reply in seconds. If I can't fully resolve it, "
        "I'll loop in a human teammate automatically.\n\n"
        "*I can help with:*\n"
        "• Account & login\n"
        "• Payments & withdrawals\n"
        "• Features & usage\n"
        "• Technical issues\n"
        "• General questions"
    )
    actions = "🧠 Smart · ⚡ Fast · 🔒 Private"
    footer = "💡 _Not satisfied with the answer? Tap Talk to a human anytime._"
    return card("Cricway AI Assistant", "Smart. Fast. Always here.", body, actions, footer)


def build_safety_screen() -> str:
    body = (
        "🔒 *Protect your account*\n"
        "Never share your login, OTP, or personal info.\n\n"
        "🔗 *Avoid unofficial links*\n"
        "Don't click suspicious links or DMs from unknown sources.\n\n"
        "🎧 *Only trust official support*\n"
        "We will *never* ask for your password or payment details.\n\n"
        "🚩 *Report suspicious activity*\n"
        "Help us keep the community safe — flag anything that seems off."
    )
    actions = "🛡 Encrypted · 👁 24/7 monitoring"
    footer = "🔐 _Your security. Our commitment._"
    return card("Your Safety, Our Priority", "Play safe. Stay safe. Always.", body, actions, footer)


def build_faq_screen() -> str:
    body = (
        "*Q1 · Getting started*\n"
        "Sign up, verify, and explore. New users may receive an onboarding bonus.\n\n"
        "*Q2 · Getting support*\n"
        "Just describe your issue — every message becomes a tracked ticket.\n\n"
        "*Q3 · Talking to a human*\n"
        "If our AI can't resolve it, your case is auto-escalated to a human agent.\n\n"
        "*Q4 · Deposits & withdrawals*\n"
        "Deposits 2–5 min · Withdrawals 15–30 min on average.\n\n"
        "*Q5 · Response times*\n"
        "AI: instant · Human: typically 5–30 min."
    )
    actions = "📚 Search · 🆘 Open ticket · 🤖 Ask AI"
    footer = "💡 _Not finding an answer? Just type your question._"
    return card("Help Center", "Answers at a glance", body, actions, footer)


def build_status_screen() -> str:
    stats = tickets.ticket_stats()
    open_total = stats["open"] + stats["in_progress"]
    if open_total >= 25:
        support_state, support_label = "🔴", "Issue Detected"
    elif open_total >= 10:
        support_state, support_label = "🟡", "High Load"
    else:
        support_state, support_label = "🟢", "Operational"

    body = (
        f"🟢 *Platform* — Operational\n"
        f"🟢 *Payments* — Operational\n"
        f"{support_state} *Support Desk* — {support_label}\n"
        f"🟢 *API Services* — Operational\n"
        f"🟢 *Security* — Operational\n\n"
        f"*Snapshot*\n"
        f"• Open tickets: `{stats['open']}`\n"
        f"• In progress: `{stats['in_progress']}`\n"
        f"• Resolved (all-time): `{stats['resolved']}`"
    )
    actions = f"🔄 Last updated: {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
    footer = "💡 _Pull to refresh — get the latest status instantly._"
    return card("Live System Status", "Real-time · Reliable · Always on", body, actions, footer)


def build_my_tickets_screen(user_id: int, tlist: list[dict]) -> str:
    if not tlist:
        body = (
            "📭 *No active tickets — you're all set!*\n\n"
            "When you send us a message, it'll appear here as a tracked case "
            "you can follow end-to-end."
        )
        actions = "🆘 Open Support · 🤖 Ask AI"
        footer = "💡 _Pro tip: include details for the fastest reply._"
        return card("My Tickets", "Your support history", body, actions, footer)

    lines = []
    for t in tlist[:6]:
        lines.append(
            f"{tickets.STATUS_EMOJI.get(t['status'], '•')} "
            f"{tickets.PRIORITY_EMOJI.get(t['priority'], '')} "
            f"`{format_ticket_id(t['ticket_id'])}` — {md(short_subject(t['subject']))}"
        )
    body = "\n".join(lines)
    actions = f"Showing {min(len(tlist), 6)} of {len(tlist)} ticket(s)"
    footer = "💡 _Tap any ticket to view its timeline._"
    return card("My Tickets", "Your support history", body, actions, footer)


def build_ticket_detail_screen(ticket: dict, replies: list[dict]) -> str:
    pretty = format_ticket_id(ticket["ticket_id"])
    status = ticket["status"]
    priority = ticket["priority"]
    handled = ticket.get("handled_by") or "PENDING"

    timeline_lines = []
    for r in replies[-6:]:
        ts = fmt_ts(r["created_at"])
        role = r["sender_role"]
        icon = {"USER": "🙋", "ADMIN": "👨‍💼", "AI": "🤖", "SYSTEM": "⚙️"}.get(role, "•")
        msg = md(short_subject(r["message"], 90))
        timeline_lines.append(f"{icon} *{role}* · _{ts}_\n   {msg}")
    timeline = "\n\n".join(timeline_lines) if timeline_lines else "_No activity yet._"

    body = (
        f"*Status:* {tickets.STATUS_EMOJI.get(status, '•')} `{status}`\n"
        f"*Priority:* {tickets.PRIORITY_EMOJI.get(priority, '•')} `{priority}`\n"
        f"*Handled by:* `{handled}`\n"
        f"*Created:* _{fmt_ts(ticket['created_at'])}_\n"
        f"*Updated:* _{fmt_ts(ticket['updated_at'])}_\n\n"
        f"*Timeline*\n{timeline}"
    )
    actions = "🔄 Refresh · 🎫 All tickets"
    footer = "💡 _A reply from our team will land here in real time._"
    return card(f"Ticket {pretty}", "Case timeline & history", body, actions, footer)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user:
        upsert_user(user.id, user.username, user.first_name)
        admin.maybe_bootstrap_admin(user.id, user.username)
        log_event("INFO", "USER", "/start", actor_id=user.id)
    user_row = fetch_user(user.id) if user else None
    context.user_data.clear()
    await show_screen(
        update,
        context,
        image="home.png",
        text=build_home_screen(user, user_row),
        keyboard=home_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_screen(
        update, context, image=None, text=build_faq_screen(), keyboard=back_home_keyboard()
    )


async def safety_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_screen(
        update, context, image="safety.png", text=build_safety_screen(), keyboard=back_home_keyboard()
    )


# ---------------------------------------------------------------------------
# User callbacks
# ---------------------------------------------------------------------------


async def user_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if user:
        upsert_user(user.id, user.username, user.first_name)
    data = query.data or ""
    user_row = fetch_user(user.id) if user else None

    if data == "usr_status_refresh":
        await query.answer("Refreshed", show_alert=False)
    else:
        await query.answer()

    if data == "usr_home":
        await show_screen(
            update, context, image="home.png",
            text=build_home_screen(user, user_row), keyboard=home_keyboard(),
        )
    elif data == "usr_support":
        await show_screen(
            update, context, image="support_hub.png",
            text=build_support_screen(user), keyboard=support_keyboard(),
        )
    elif data == "usr_ai":
        await show_screen(
            update, context, image="ai.png",
            text=build_ai_screen(user), keyboard=ai_keyboard(),
        )
    elif data == "usr_safety":
        await show_screen(
            update, context, image="safety.png",
            text=build_safety_screen(), keyboard=back_home_keyboard(),
        )
    elif data == "usr_faq":
        await show_screen(
            update, context, image=None,
            text=build_faq_screen(), keyboard=back_home_keyboard(),
        )
    elif data in ("usr_status", "usr_status_refresh"):
        await show_screen(
            update, context, image="status.png",
            text=build_status_screen(), keyboard=status_keyboard(),
        )
    elif data == "usr_tickets":
        my = tickets.list_tickets(user_id=user.id, limit=10) if user else []
        await show_screen(
            update, context, image="support_hub.png",
            text=build_my_tickets_screen(user.id if user else 0, my),
            keyboard=tickets_list_keyboard(my),
        )


async def ticket_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    data = query.data or ""

    if data.startswith("tkt_view_"):
        await query.answer()
        ticket_id = int(data.split("_")[-1])
        ticket = tickets.get_ticket(ticket_id)
        if not ticket or (user and ticket["user_id"] != user.id and not admin.is_admin_user_id(user.id)):
            await query.answer("Ticket not found.", show_alert=True)
            return
        replies = tickets.list_replies(ticket_id, limit=20)
        await show_screen(
            update, context, image="support_ticket.png",
            text=build_ticket_detail_screen(ticket, replies),
            keyboard=ticket_view_keyboard(ticket_id),
        )

    elif data.startswith("tkt_escalate_"):
        await query.answer("Escalated to a human agent ✅", show_alert=False)
        ticket_id = int(data.split("_")[-1])
        ticket = tickets.get_ticket(ticket_id)
        if not ticket or (user and ticket["user_id"] != user.id):
            return
        tickets.set_handled_by(ticket_id, "PENDING")
        tickets.update_status(ticket_id, "OPEN", actor_id=user.id if user else None)
        tickets.update_priority(ticket_id, "HIGH", actor_id=user.id if user else None)
        tickets.add_reply(ticket_id, user.id if user else None, "SYSTEM",
                          "User requested human follow-up (Not satisfied with AI).")
        await _notify_admins_new_ticket(context, ticket_id, user, ticket["subject"], escalated=True)

        body = (
            f"✅ *Escalated to a human agent.*\n\n"
            f"Ticket `{format_ticket_id(ticket_id)}` is now *HIGH priority* and our team has been notified. "
            f"You'll get a reply right here as soon as an agent picks it up."
        )
        text = card("Escalation Confirmed", "We're on it", body,
                    actions="🟡 Your ticket is now under review",
                    footer="💡 _You can keep adding details — just send another message._")
        await show_screen(
            update, context, image="support_ticket.png",
            text=text, keyboard=escalated_keyboard(ticket_id),
        )


# ---------------------------------------------------------------------------
# Free-form message → ticket → FAQ → AI → escalate
# ---------------------------------------------------------------------------


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    text = (update.message.text or "").strip()
    if not user or not text:
        return

    upsert_user(user.id, user.username, user.first_name)
    admin.maybe_bootstrap_admin(user.id, user.username)

    # Spam / repeat detection
    last = context.user_data.get("last_msg")
    repeat = context.user_data.get("repeat_count", 0)
    repeat = repeat + 1 if last == text else 1
    context.user_data["last_msg"] = text
    context.user_data["repeat_count"] = repeat
    if repeat >= SPAM_REPEAT_LIMIT:
        await show_screen(
            update, context, image=None,
            text=card(
                "Slow down a moment",
                "We've already received your message",
                body="⚠️ It looks like you're sending the same message repeatedly.\n"
                     "We've logged it — our team will respond shortly.",
                footer="💡 _Adding new details will help us help you faster._",
            ),
            keyboard=back_home_keyboard(),
        )
        log_event("WARN", "USER", f"Spam-repeat ({repeat}x): {text[:80]}", actor_id=user.id)
        return

    increment_request_count(user.id)
    ticket_id = tickets.create_ticket(user.id, text)
    pretty_id = format_ticket_id(ticket_id)

    # 1) FAQ keyword match
    rule = faq_match(text)
    if rule:
        body = (
            f"🎫 *Ticket {pretty_id}* opened — instant match found.\n\n"
            f"{rule['response']}"
        )
        screen = card("Instant Answer", "Matched from our knowledge base", body,
                      actions=f"Status: {tickets.STATUS_EMOJI['RESOLVED']} RESOLVED",
                      footer="💡 _Need a human? Tap Not satisfied below._")
        tickets.add_reply(ticket_id, None, "AI", rule["response"])
        tickets.set_handled_by(ticket_id, "AI")
        tickets.update_status(ticket_id, "RESOLVED")
        await show_screen(
            update, context, image="support_ticket.png",
            text=screen, keyboard=ai_followup_keyboard(ticket_id),
        )
        return

    # 2) AI assistant
    await update.message.chat.send_action(ChatAction.TYPING)
    processing = await update.message.reply_text(
        f"⏳ *Analyzing your request…* `{pretty_id}`",
        parse_mode="Markdown",
    )

    ai_reply, escalate = get_ai_response(text)
    tickets.add_reply(ticket_id, None, "AI", ai_reply)

    try:
        await processing.delete()
    except Exception:  # noqa: BLE001
        pass

    if escalate or ai_reply == AI_FALLBACK_TEXT:
        tickets.set_handled_by(ticket_id, "PENDING")
        tickets.update_priority(ticket_id, "HIGH")
        await _notify_admins_new_ticket(context, ticket_id, user, text)

        suggestion = faq_suggest(text)
        suggestion_block = ""
        if suggestion:
            suggestion_block = (
                "\n\n💡 *You might be looking for:*\n"
                f"{suggestion['response']}"
            )

        body = (
            f"📩 We received your request — *Ticket {pretty_id}* is open.\n\n"
            f"Our AI couldn't resolve this with full confidence, so we've escalated it to a human agent. "
            f"You'll hear back here shortly."
            f"{suggestion_block}"
        )
        screen = card("Escalated to Human Support", "A teammate will reply soon", body,
                      actions=f"Status: {tickets.STATUS_EMOJI['OPEN']} OPEN · 🔴 HIGH priority",
                      footer="💡 _Add more details anytime — just send another message._")
        await show_screen(
            update, context, image="support_ticket.png",
            text=screen, keyboard=escalated_keyboard(ticket_id),
        )
    else:
        tickets.set_handled_by(ticket_id, "AI")
        tickets.update_status(ticket_id, "RESOLVED")
        body = (
            f"🎫 *Ticket {pretty_id}* — answered by AI.\n\n"
            f"{ai_reply}"
        )
        screen = card("Cricway AI · Reply", "Smart. Fast. Always here.", body,
                      actions=f"Status: {tickets.STATUS_EMOJI['RESOLVED']} RESOLVED",
                      footer="💡 _Not satisfied? Tap below to talk to a human._")
        await show_screen(
            update, context, image="ai.png",
            text=screen, keyboard=ai_followup_keyboard(ticket_id),
        )


async def _notify_admins_new_ticket(
    context: ContextTypes.DEFAULT_TYPE,
    ticket_id: int,
    user,
    text: str,
    escalated: bool = False,
) -> None:
    pretty_id = format_ticket_id(ticket_id)
    snippet = text if len(text) <= 400 else text[:400] + "…"
    header = "🚨 *Escalated by user*" if escalated else "🚨 *New escalated ticket*"
    msg = (
        f"{header} `{pretty_id}`\n"
        f"From: @{user.username or '—'} (`{user.id}`)\n\n"
        f"{snippet}\n\n"
        f"Reply: `/reply {pretty_id} your message`"
    )
    for admin_id in admin.get_admin_ids():
        try:
            await context.bot.send_message(chat_id=admin_id, text=msg, parse_mode="Markdown")
        except Exception as exc:  # noqa: BLE001
            logger.info("Could not notify admin %s: %s", admin_id, exc)


# ---------------------------------------------------------------------------
# Background jobs & error handling
# ---------------------------------------------------------------------------


async def daily_auto_close(context: ContextTypes.DEFAULT_TYPE) -> None:
    closed = tickets.auto_close_stale(days=7)
    if closed:
        logger.info("Auto-closed %d stale ticket(s)", closed)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled error", exc_info=context.error)
    log_event("ERROR", "SYSTEM", f"Unhandled: {context.error}")


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def build_application() -> Application:
    application = Application.builder().token(TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("safety", safety_command))

    application.add_handler(CommandHandler("admin", admin.admin_command))
    application.add_handler(CommandHandler("reply", admin.reply_command))
    application.add_handler(CommandHandler("status", admin.status_command))
    application.add_handler(CommandHandler("priority", admin.priority_command))
    application.add_handler(CommandHandler("broadcast", admin.broadcast_command))

    application.add_handler(CallbackQueryHandler(admin.admin_callback, pattern=r"^adm_"))
    application.add_handler(CallbackQueryHandler(admin.broadcast_callback, pattern=r"^bcast_"))
    application.add_handler(CallbackQueryHandler(ticket_callback, pattern=r"^tkt_"))
    application.add_handler(CallbackQueryHandler(user_callback, pattern=r"^usr_"))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))
    application.add_error_handler(error_handler)

    if application.job_queue:
        application.job_queue.run_daily(daily_auto_close, time=time(hour=3, minute=0))

    return application


def main() -> None:
    init_db()
    logger.info("Starting Cricway Enterprise Support Bot…")
    application = build_application()
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    try:
        print("Starting Cricway Bot...")
        main()
    except Exception as e:
        print("❌ CRASH ERROR:", e)
        import traceback
        traceback.print_exc()
