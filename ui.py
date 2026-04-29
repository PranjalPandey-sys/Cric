"""Screen rendering system — banner photo + structured card + keyboard."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from telegram import InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes
from telegram.helpers import escape_markdown

logger = logging.getLogger(__name__)

ASSETS_DIR = Path("assets")
SEPARATOR = "━━━━━━━━━━━━━━━━━━━━━━"
CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096


def md(text: str) -> str:
    """Escape user-supplied text for Telegram Markdown (legacy)."""
    return escape_markdown(text or "", version=1)


def card(
    title: str,
    subtitle: Optional[str] = None,
    body: Optional[str] = None,
    actions: Optional[str] = None,
    footer: Optional[str] = None,
) -> str:
    """Render a standardized card with consistent visual hierarchy."""
    lines = [SEPARATOR, f"🏏 *{title}*"]
    if subtitle:
        lines.append(f"_{subtitle}_")
    lines.append(SEPARATOR)
    if body:
        lines.append(body)
    if actions:
        lines.append(SEPARATOR)
        lines.append(f"⚡ {actions}")
    if footer:
        lines.append(SEPARATOR)
        lines.append(footer)
    lines.append(SEPARATOR)
    return "\n".join(lines)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


async def show_screen(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    image: Optional[str],
    text: str,
    keyboard: Optional[InlineKeyboardMarkup] = None,
) -> None:
    """Render a full app-style screen.

    Always presents a fresh message so transitions feel like navigating between
    pages in a mobile app.
    """
    image_path: Optional[Path] = None
    if image:
        candidate = ASSETS_DIR / image
        if candidate.exists():
            image_path = candidate

    chat_id = update.effective_chat.id

    # If this came from a callback button, drop the previous screen first
    if update.callback_query is not None:
        try:
            await update.callback_query.message.delete()
        except TelegramError:
            pass

    try:
        if image_path is not None:
            with image_path.open("rb") as fh:
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=fh,
                    caption=_truncate(text, CAPTION_LIMIT),
                    reply_markup=keyboard,
                    parse_mode=ParseMode.MARKDOWN,
                )
        else:
            await context.bot.send_message(
                chat_id=chat_id,
                text=_truncate(text, TEXT_LIMIT),
                reply_markup=keyboard,
                parse_mode=ParseMode.MARKDOWN,
                disable_web_page_preview=True,
            )
    except BadRequest as exc:
        # Fall back to plain text if Markdown fails for some reason
        logger.warning("Markdown render failed (%s) — sending plain text.", exc)
        await context.bot.send_message(
            chat_id=chat_id,
            text=_truncate(text, TEXT_LIMIT),
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
