#!/usr/bin/env python3
"""Example custom bot script for telegram-hoster.

This is the ONE script the owner uploads; every user's bot runs it, each with
its own BOT_TOKEN / OWNER_ID injected by the panel. Whatever you print to
stdout or stderr shows up live in that user's "Logs" pane.

Handy patterns demonstrated here:
  * reading the injected credentials
  * a plain command, an owner-only command, and an inline keyboard
  * logging that lands in the panel
  * a global error handler so one bad update can't kill the process
"""

import logging
import os
import sys
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# --- config injected by the hoster -----------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
OWNER_ID = int(os.environ.get("OWNER_ID") or 0)

logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
log = logging.getLogger("bot")

START_TIME = time.time()
SEEN = set()  # users who have talked to this bot


def owner_only(func):
    """Only the OWNER_ID may run this command."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user and update.effective_user.id == OWNER_ID:
            return await func(update, context)
        if update.effective_message:
            await update.effective_message.reply_text("This command is for the bot owner only.")
        return None

    return wrapper


# --- commands ---------------------------------------------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    SEEN.add(update.effective_user.id)
    name = update.effective_user.first_name or "there"
    log.info("start from %s", update.effective_user.id)
    await update.message.reply_text(
        f"Hi {name}! I'm alive.\nTry /help, /id, or /menu."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Commands:\n"
        "/start  - greet me\n"
        "/help   - this message\n"
        "/id     - show your Telegram user id\n"
        "/menu   - show an inline keyboard\n"
        "/stats  - (owner only) uptime and counts\n\n"
        "Or just send any text and I'll echo it."
    )


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(f"Your id is {update.effective_user.id}")


async def cmd_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Say hi", callback_data="hi"),
                InlineKeyboardButton("Ping", callback_data="ping"),
            ]
        ]
    )
    await update.message.reply_text("Pick one:", reply_markup=keyboard)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()  # stop the loading spinner
    replies = {"hi": "Hello!", "ping": "Pong."}
    await query.edit_message_text(replies.get(query.data, "?"))
    log.info("button %s by %s", query.data, query.from_user.id)


@owner_only
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    up = int(time.time() - START_TIME)
    await update.message.reply_text(
        f"Uptime: {up}s\nUsers seen: {len(SEEN)}\nOwner id: {OWNER_ID}"
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    SEEN.add(update.effective_user.id)
    text = update.message.text or ""
    log.info("msg from %s: %s", update.effective_user.id, text)
    await update.message.reply_text(text)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("update caused an error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("Something went wrong. Try again.")


# --- entry point ------------------------------------------------------------
def main() -> None:
    if not BOT_TOKEN:
        log.error("BOT_TOKEN is not set -- save your token in the panel first.")
        sys.exit(1)

    log.info("starting bot for owner_id=%s", OWNER_ID)
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
