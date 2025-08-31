import os
from telethon import TelegramClient
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters, ConversationHandler

# ========================
# TELEGRAM BOT CREDENTIALS
# ========================
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")      # from .env
API_ID = int(os.getenv("API_ID"))       # must be int
API_HASH = os.getenv("API_HASH")        # string
# ========================
# FOLDER FOR USER SESSIONS
# ========================
SESSIONS_DIR = "sessions"
if not os.path.exists(SESSIONS_DIR):
    os.makedirs(SESSIONS_DIR)

# Conversation states
ASK_PHONE, ASK_CODE, ASK_PASS = range(3)

# Store active clients in memory
user_clients = {}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Welcome!\nSend me your phone number to log in."
    )
    return ASK_PHONE

async def get_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    context.user_data["phone"] = phone
    session_path = os.path.join(SESSIONS_DIR, f"{phone}.session")

    client = TelegramClient(session_path, API_ID, API_HASH)
    await client.connect()

    if not await client.is_user_authorized():
        try:
            await client.send_code_request(phone)
            context.user_data["client"] = client
            await update.message.reply_text("📩 I sent you a code on Telegram.\nPlease enter it here:")
            return ASK_CODE
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")
            return ConversationHandler.END
    else:
        user_clients[update.effective_user.id] = client
        await update.message.reply_text("✅ Already logged in!")
        return ConversationHandler.END

async def get_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip()
    phone = context.user_data["phone"]
    client: TelegramClient = context.user_data["client"]

    try:
        await client.sign_in(phone, code)
        user_clients[update.effective_user.id] = client
        await update.message.reply_text("✅ Login successful!")
        return ConversationHandler.END
    except Exception as e:
        if "password" in str(e).lower():
            await update.message.reply_text("🔒 Enter your 2FA password:")
            return ASK_PASS
        else:
            await update.message.reply_text(f"❌ Error: {e}")
            return ConversationHandler.END

async def get_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = update.message.text.strip()
    phone = context.user_data["phone"]
    client: TelegramClient = context.user_data["client"]

    try:
        await client.sign_in(password=password)
        user_clients[update.effective_user.id] = client
        await update.message.reply_text("✅ Logged in with 2FA successfully!")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")
    return ConversationHandler.END

# Example command: upload contacts
async def upload_contacts(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in user_clients:
        await update.message.reply_text("⚠️ You must login first using /start")
        return

    client = user_clients[user_id]

    # Example: add dummy contact
    try:
        await client.connect()
        result = await client(functions.contacts.ImportContactsRequest(
            contacts=[types.InputPhoneContact(client_id=0, phone="+1234567890", first_name="Test", last_name="User")]
        ))
        await update.message.reply_text("📲 Contact uploaded successfully!")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            ASK_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_phone)],
            ASK_CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_code)],
            ASK_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_pass)],
        },
        fallbacks=[],
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("upload", upload_contacts))

    print("🤖 Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
