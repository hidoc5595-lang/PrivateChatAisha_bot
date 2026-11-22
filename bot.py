import asyncio
from datetime import datetime, timedelta, timezone
import logging
import os

from pymongo import MongoClient
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def get_env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None or value.strip() == "":
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value.strip()


BOT_TOKEN = get_env("BOT_TOKEN")
ADMIN_ID = int(get_env("ADMIN_ID"))
CHANNEL_ID = int(get_env("CHANNEL_ID"))
MONGO_URL = get_env("MONGO_URL")
UPI_ID = get_env("UPI_ID")
CONTACT_USERNAME = get_env("CONTACT_USERNAME").lstrip("@")
PRICE = int(os.getenv("PRICE", "500").strip())
DURATION_MINUTES = int(os.getenv("DURATION_MINUTES", "10").strip())

mongo_client = MongoClient(MONGO_URL)
db = mongo_client["private_chat_aisha_db"]
users_col = db["users"]
payments_col = db["payments"]

AWAITING_PROOF_KEY = "awaiting_payment_proof"


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("I Have Paid", callback_data="paid_start")],
            [
                InlineKeyboardButton(
                    "Contact Support", url=f"https://t.me/{CONTACT_USERNAME}"
                )
            ],
        ]
    )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not update.message:
        return

    context.user_data[AWAITING_PROOF_KEY] = False

    users_col.update_one(
        {"user_id": user.id},
        {
            "$set": {
                "user_id": user.id,
                "username": user.username or "",
                "first_name": user.first_name or "",
                "last_seen": datetime.now(timezone.utc),
            },
            "$setOnInsert": {"created_at": datetime.now(timezone.utc)},
        },
        upsert=True,
    )

    text = (
        f"Namaste {user.first_name or ' dost'}!\n\n"
        f"Private Chat access ke liye details:\n"
        f"• Pass Price: ₹{PRICE}\n"
        f"• Access Duration: {DURATION_MINUTES} minutes\n"
        f"• UPI ID: `{UPI_ID}`\n\n"
        f"Payment karne ke baad **I Have Paid** par tap karein aur payment screenshot ya UTR/transaction ID bhejein."
    )
    await update.message.reply_text(
        text, reply_markup=main_menu_keyboard(), parse_mode="Markdown"
    )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return

    await query.answer()
    data = query.data
    actor = query.from_user

    if data == "paid_start":
        if not actor:
            return
        context.user_data[AWAITING_PROOF_KEY] = True
        await query.message.reply_text(
            f"Kripya ₹{PRICE} ke payment ka screenshot ya UTR/transaction reference is chat mein bhejein.\n"
            f"Admin verify karte hi aapko {DURATION_MINUTES} minutes ka invite link mil jayega."
        )
        return

    if data.startswith("approve_") or data.startswith("reject_"):
        if not actor or actor.id != ADMIN_ID:
            await query.message.reply_text("Sirf admin hi yeh action le sakta hai.")
            return

        action, target_user_str = data.split("_", 1)
        target_user_id = int(target_user_str)

        payment_doc = payments_col.find_one(
            {"user_id": target_user_id, "status": "pending"},
            sort=[("_id", -1)],
        )

        if action == "approve":
            now = datetime.now(timezone.utc)
            expires_at = now + timedelta(minutes=DURATION_MINUTES)

            invite = await context.bot.create_chat_invite_link(
                chat_id=CHANNEL_ID,
                expire_date=expires_at,
                member_limit=1,
                name=f"pass_{target_user_id}_{int(now.timestamp())}",
            )

            payments_col.update_one(
                {"_id": payment_doc["_id"]} if payment_doc else {"user_id": target_user_id},
                {
                    "$set": {
                        "status": "approved",
                        "approved_by": actor.id,
                        "approved_at": now,
                        "expires_at": expires_at,
                        "invite_link": invite.invite_link,
                    }
                },
            )

            users_col.update_one(
                {"user_id": target_user_id},
                {
                    "$set": {
                        "last_approved_at": now,
                        "access_expires_at": expires_at,
                    }
                },
            )

            await context.bot.send_message(
                chat_id=target_user_id,
                text=(
                    f"Aapka payment approve ho gaya hai!\n\n"
                    f"Yeh raha aapka {DURATION_MINUTES} minutes ka private invite link (1-time use):\n"
                    f"{invite.invite_link}"
                ),
            )

            if query.message.photo:
                await query.edit_message_caption(
                    caption=(query.message.caption or "") + "\n\nStatus: APPROVED"
                )
            else:
                await query.edit_message_text(
                    text=(query.message.text or "") + "\n\nStatus: APPROVED"
                )

        elif action == "reject":
            now = datetime.now(timezone.utc)
            payments_col.update_one(
                {"_id": payment_doc["_id"]} if payment_doc else {"user_id": target_user_id},
                {
                    "$set": {
                        "status": "rejected",
                        "rejected_by": actor.id,
                        "rejected_at": now,
                    }
                },
            )

            await context.bot.send_message(
                chat_id=target_user_id,
                text=(
                    f"Maaf kijiye, aapka payment verify nahi ho paya.\n"
                    f"Madad ke liye @{CONTACT_USERNAME} se sampark karein."
                ),
            )

            if query.message.photo:
                await query.edit_message_caption(
                    caption=(query.message.caption or "") + "\n\nStatus: REJECTED"
                )
            else:
                await query.edit_message_text(
                    text=(query.message.text or "") + "\n\nStatus: REJECTED"
                )


async def handle_payment_proof(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    message = update.message
    if not user or not message:
        return

    if not context.user_data.get(AWAITING_PROOF_KEY):
        await message.reply_text(
            "Shuru karne ke liye /start bhejein ya payment bhejne ke baad 'I Have Paid' par tap karein.",
            reply_markup=main_menu_keyboard(),
        )
        return

    context.user_data[AWAITING_PROOF_KEY] = False
    now = datetime.now(timezone.utc)

    proof_text = message.text or message.caption or ""
    has_photo = bool(message.photo)

    payments_col.insert_one(
        {
            "user_id": user.id,
            "username": user.username or "",
            "first_name": user.first_name or "",
            "price": PRICE,
            "duration_minutes": DURATION_MINUTES,
            "proof_text": proof_text,
            "has_photo": has_photo,
            "status": "pending",
            "created_at": now,
        }
    )

    admin_keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Approve", callback_data=f"approve_{user.id}"),
                InlineKeyboardButton("Reject", callback_data=f"reject_{user.id}"),
            ]
        ]
    )

    user_label = f"@{user.username}" if user.username else f"ID: {user.id}"
    admin_caption = (
        f"Naya Payment Proof Aaya Hai!\n"
        f"• Name: {user.first_name or 'User'}\n"
        f"• User: {user_label} (`{user.id}`)\n"
        f"• Amount: ₹{PRICE} ({DURATION_MINUTES} mins)\n"
        f"• Note/UTR: {proof_text or 'N/A'}"
    )

    if has_photo:
        photo_file_id = message.photo[-1].file_id
        await context.bot.send_photo(
            chat_id=ADMIN_ID,
            photo=photo_file_id,
            caption=admin_caption,
            reply_markup=admin_keyboard,
            parse_mode="Markdown",
        )
    else:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=admin_caption,
            reply_markup=admin_keyboard,
            parse_mode="Markdown",
        )

    await message.reply_text(
        "Aapka payment proof admin ko bhej diya gaya hai. Approve hote hi aapko invite link mil jayega."
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling update:", exc_info=context.error)


def main() -> None:
    # Render free/web environments sometimes retain an older event loop in main thread
    asyncio.set_event_loop(asyncio.new_event_loop())

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(
        MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND, handle_payment_proof)
    )
    app.add_error_handler(error_handler)

    logger.info("Starting @PrivateChatAisha_bot polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
  
