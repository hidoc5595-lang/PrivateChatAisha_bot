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
QR_IMAGE_PATH = os.getenv("QR_IMAGE_PATH", "qr.png").strip()

PLANS = {
    "plan_1": {
        "title": "Plan 1",
        "price": 99,
        "duration": 5,
        "duration_label": "5min",
        "offer": "🔥 Starter Offer",
    },
    "plan_2": {
        "title": "Plan 2",
        "price": 199,
        "duration": 10,
        "duration_label": "10 min",
        "offer": "⭐ Popular Offer",
    },
    "plan_3": {
        "title": "Plan 3",
        "price": 499,
        "duration": 60,
        "duration_label": "1 hours min",
        "offer": "💎 Best Value Offer",
    },
}

DEFAULT_PRICE = PLANS["plan_1"]["price"]
DEFAULT_DURATION = PLANS["plan_1"]["duration"]

mongo_client = MongoClient(MONGO_URL)
db = mongo_client["private_chat_aisha_db"]
users_col = db["users"]
payments_col = db["payments"]

AWAITING_PROOF_KEY = "awaiting_payment_proof"
SELECTED_PLAN_KEY = "selected_plan"


def plans_keyboard() -> InlineKeyboardMarkup:
    rows = []
    for plan_id, plan in PLANS.items():
        btn_text = (
            f"{plan['title']} • {plan['duration_label']} — ₹{plan['price']} ({plan['offer']})"
        )
        rows.append(
            [InlineKeyboardButton(btn_text, callback_data=f"select_{plan_id}")]
        )
    rows.append(
        [InlineKeyboardButton("Contact Support", url=f"https://t.me/{CONTACT_USERNAME}")]
    )
    return InlineKeyboardMarkup(rows)


def payment_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("I Have Paid", callback_data="paid_start")],
            [InlineKeyboardButton("⬅️ Change Plan", callback_data="back_to_plans")],
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

    name = user.first_name or "there"
    plans_text = "\n".join(
        [
            f"• *{p['title']}* ({p['offer']}): *{p['duration_label']} — ₹{p['price']}*"
            for p in PLANS.values()
        ]
    )
    text = (
        f"Namaste {name}!\n\n"
        f"Private Chat access ke liye apna offer plan chunein:\n"
        f"{plans_text}\n\n"
        f"Neeche kisi bhi plan par tap karke QR code aur payment details dekhein."
    )
    await update.message.reply_text(
        text, reply_markup=plans_keyboard(), parse_mode="Markdown"
    )


async def send_plan_qr(
    update: Update, context: ContextTypes.DEFAULT_TYPE, plan_id: str
) -> None:
    query = update.callback_query
    if not query or not query.message:
        return

    plan = PLANS.get(plan_id)
    if not plan:
        await query.answer("Invalid plan.", show_alert=True)
        return

    context.user_data[SELECTED_PLAN_KEY] = plan_id
    context.user_data[AWAITING_PROOF_KEY] = False

    caption = (
        f"*{plan['title']}* — _{plan['offer']}_\n"
        f"• *Access Duration:* {plan['duration_label']}\n"
        f"• *Pass Price:* ₹{plan['price']}\n"
        f"• *UPI ID:* `{UPI_ID}`\n\n"
        f"Upar diye gaye *QR code* ya *UPI ID* par ₹{plan['price']} pay karein, "
        f"aur phir *I Have Paid* par tap karein."
    )

    if os.path.exists(QR_IMAGE_PATH):
        with open(QR_IMAGE_PATH, "rb") as qr_file:
            await query.message.reply_photo(
                photo=qr_file,
                caption=caption,
                reply_markup=payment_keyboard(),
                parse_mode="Markdown",
            )
    else:
        await query.message.reply_text(
            caption + "\n\n_(Note: QR image file abhi server par nahi mili.)_",
            reply_markup=payment_keyboard(),
            parse_mode="Markdown",
        )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    data = query.data or ""
    user = query.from_user

    if data == "back_to_plans":
        context.user_data[AWAITING_PROOF_KEY] = False
        if query.message:
            await start_command(update, context)
        return

    if data.startswith("select_"):
        plan_id = data.replace("select_", "", 1)
        await send_plan_qr(update, context, plan_id)
        return

    if data == "paid_start":
        plan_id = context.user_data.get(SELECTED_PLAN_KEY, "plan_1")
        plan = PLANS.get(plan_id, PLANS["plan_1"])
        context.user_data[AWAITING_PROOF_KEY] = True
        if query.message:
            await query.message.reply_text(
                f"Kripya *₹{plan['price']}* ({plan['title']} • {plan['duration_label']}) ke payment ka screenshot "
                f"ya UTR/transaction reference is chat mein bhejein.\n"
                f"Admin verify karte hi aapko *{plan['duration_label']}* ka invite link mil jayega.",
                parse_mode="Markdown",
            )
        return

    if data.startswith("approve_") or data.startswith("reject_"):
        if not user or user.id != ADMIN_ID:
            await query.answer("Sirf admin hi yeh action le sakta hai.", show_alert=True)
            return

        action, payment_id = data.split("_", 1)
        record = payments_col.find_one({"_id": payment_id})
        if not record:
            await query.answer("Payment record nahi mila.", show_alert=True)
            return

        target_user_id = int(record["user_id"])
        duration = int(record.get("duration_minutes", DEFAULT_DURATION))
        duration_label = record.get("duration_label", f"{duration} minutes")
        price = int(record.get("price", DEFAULT_PRICE))
        plan_title = record.get("plan_title", "Selected Plan")

        if action == "approve":
            expire_dt = datetime.now(timezone.utc) + timedelta(minutes=duration)
            invite = await context.bot.create_chat_invite_link(
                chat_id=CHANNEL_ID,
                expire_date=expire_dt,
                member_limit=1,
            )
            payments_col.update_one(
                {"_id": payment_id},
                {
                    "$set": {
                        "status": "approved",
                        "approved_at": datetime.now(timezone.utc),
                        "invite_link": invite.invite_link,
                    }
                },
            )
            await context.bot.send_message(
                chat_id=target_user_id,
                text=(
                    f"Payment approve ho gaya! ({plan_title} • {duration_label} — ₹{price})\n"
                    f"Yeh raha aapka *{duration_label}* ka one-time private invite link:\n"
                    f"{invite.invite_link}"
                ),
                parse_mode="Markdown",
            )
            if query.message:
                await query.message.reply_text(
                    f"✅ Approved for user `{target_user_id}` ({plan_title}, {duration_label} — ₹{price}).",
                    parse_mode="Markdown",
                )
        else:
            payments_col.update_one(
                {"_id": payment_id},
                {
                    "$set": {
                        "status": "rejected",
                        "rejected_at": datetime.now(timezone.utc),
                    }
                },
            )
            await context.bot.send_message(
                chat_id=target_user_id,
                text=(
                    "Aapka payment proof verify nahi ho paaya. "
                    "Kripya sahi screenshot/UTR bhejein ya support se sampark karein."
                ),
            )
            if query.message:
                await query.message.reply_text(
                    f"❌ Rejected for user `{target_user_id}`.",
                    parse_mode="Markdown",
                )


async def handle_proof_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    user = update.effective_user
    msg = update.message
    if not user or not msg:
        return

    if not context.user_data.get(AWAITING_PROOF_KEY):
        return

    plan_id = context.user_data.get(SELECTED_PLAN_KEY, "plan_1")
    plan = PLANS.get(plan_id, PLANS["plan_1"])

    context.user_data[AWAITING_PROOF_KEY] = False
    payment_id = f"{user.id}_{int(datetime.now(timezone.utc).timestamp())}"

    proof_type = "photo" if msg.photo else "text"
    proof_text = msg.caption or msg.text or ""

    payments_col.insert_one(
        {
            "_id": payment_id,
            "user_id": user.id,
            "username": user.username or "",
            "first_name": user.first_name or "",
            "plan_id": plan_id,
            "plan_title": plan["title"],
            "price": plan["price"],
            "duration_minutes": plan["duration"],
            "duration_label": plan["duration_label"],
            "proof_type": proof_type,
            "proof_text": proof_text,
            "status": "pending",
            "created_at": datetime.now(timezone.utc),
        }
    )

    admin_markup = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Approve", callback_data=f"approve_{payment_id}"),
                InlineKeyboardButton("❌ Reject", callback_data=f"reject_{payment_id}"),
            ]
        ]
    )

    summary = (
        f"New Payment Proof:\n"
        f"• User: {user.first_name or ''} (@{user.username or 'no_username'})\n"
        f"• User ID: `{user.id}`\n"
        f"• Plan: *{plan['title']}* ({plan['offer']})\n"
        f"• Amount: *{plan['duration_label']} — ₹{plan['price']}*\n"
        f"• Note/UTR: {proof_text or 'N/A'}"
    )

    if msg.photo:
        photo_file_id = msg.photo[-1].file_id
        await context.bot.send_photo(
            chat_id=ADMIN_ID,
            photo=photo_file_id,
            caption=summary,
            reply_markup=admin_markup,
            parse_mode="Markdown",
        )
    else:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=summary,
            reply_markup=admin_markup,
            parse_mode="Markdown",
        )

    await msg.reply_text(
        "Aapka payment proof admin ko bhej diya gaya hai. Verification ke baad invite link mil jayega."
    )


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(
        MessageHandler(
            (filters.PHOTO | filters.TEXT) & ~filters.COMMAND, handle_proof_message
        )
    )

    logger.info("Starting @PrivateChatAisha_bot polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
    
