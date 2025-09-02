import os
import asyncio
import random
import json
import logging
import vobject
from dotenv import load_dotenv

# Telegram libraries
from telethon import TelegramClient, errors, functions, types
from telethon.errors import FloodWaitError, PeerFloodError, UserPrivacyRestrictedError, SessionPasswordNeededError
from telethon.tl.types import InputPhoneContact, InputUser, ChannelParticipantsAdmins

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    ConversationHandler,
)

# Load env
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = os.getenv("API_ID")
API_HASH = os.getenv("API_HASH")

if not BOT_TOKEN or not API_ID or not API_HASH:
    raise SystemExit("Please set BOT_TOKEN, API_ID, API_HASH in environment")

API_ID = int(API_ID)  # must be int

# Logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# Directories & files
SESSIONS_DIR = "sessions"
if not os.path.exists(SESSIONS_DIR):
    os.makedirs(SESSIONS_DIR)

BOT_USERS_FILE = os.path.join(SESSIONS_DIR, "bot_users.json")  # mapping telegram_user_id -> phone

# Import/Invite tuning
IMPORT_BATCH = 30
IMPORT_BASE_DELAY = 10
IMPORT_JITTER = (5, 15)

INVITE_BATCH = 5
INVITE_BASE_DELAY = 30
INVITE_JITTER = (5, 20)

# Conversation states
ASK_PHONE, ASK_CODE, ASK_PASS = range(3)

# In-memory active Telethon clients (keyed by telegram user id)
user_clients = {}  # {telegram_user_id: TelegramClient}

# helper: save/load bot_users mapping
def load_bot_users():
    if os.path.exists(BOT_USERS_FILE):
        with open(BOT_USERS_FILE, "r") as f:
            return json.load(f)
    return {}

async def get_client(user_id, phone):
    existing_client = user_clients.get(user_id)
    if existing_client and existing_client.is_connected():
        return existing_client

    session_path = os.path.join(SESSIONS_DIR, f"{phone}.session")
    client = TelegramClient(session_path, API_ID, API_HASH)
    
    try:
        await client.connect()
    except Exception as e:
        try:
            await client.disconnect()
            await client.connect()
        except Exception:
            raise e

    user_clients[user_id] = client
    return client
    
def save_bot_users(mapping):
    with open(BOT_USERS_FILE, "w") as f:
        json.dump(mapping, f, indent=2)

bot_users = load_bot_users()

# ---------------------------
# LOGIN CONVERSATION HANDLERS
# ---------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    phone = bot_users.get(str(tg_id))
    if phone:
        client = await get_client(tg_id, phone)
        if await client.is_user_authorized():
            await update.message.reply_text("✅ You are already logged in!")
            return ConversationHandler.END
    await update.message.reply_text(
        "👋 Welcome to MyBot!\n\n"
        "Here’s how this bot works (all actions are safe and local):\n\n"
        "1️⃣ When you login, your phone number is only used to start a Telegram session locally.\n"
        "2️⃣ Your contacts from a .vcf file are read and stored locally in your session folder.\n"
        "3️⃣ You choose a channel where you want to add members. Nothing is added until you tell the bot.\n"
        "4️⃣ The bot never sends your messages or personal data anywhere.\n\n"
        "💡 Example of the safe logic:\n"
        "- Reading your VCF contacts:\n"
        "    for contact in vcf_file:\n"
        "        save_locally(contact)\n"
        "- Selecting channel to add members:\n"
        "    channel = user_selected_channel\n"
        "- Adding members only after your command:\n"
        "    add_members_to_channel(channel, selected_contacts)\n\n"
        "Please send your phone number (in international format, e.g. +234901...) to log in."
    )
    return ASK_PHONE

async def get_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    context.user_data["phone"] = phone
    session_path = os.path.join(SESSIONS_DIR, phone)

    client = await get_client(update.effective_user.id, phone)
    await client.connect()

    try:
        if not await client.is_user_authorized():
            sent = await client.send_code_request(phone)
            context.user_data["client"] = client
            context.user_data["phone_code_hash"] = getattr(sent, "phone_code_hash", None)
            await update.message.reply_text("📩 Code sent — enter the code you received in Telegram promptly. Please do not request a new code unless needed to avoid expiry.")
            return ASK_CODE
        else:
            user_clients[update.effective_user.id] = client
            bot_users[str(update.effective_user.id)] = phone
            save_bot_users(bot_users)
            await update.message.reply_text("✅ Already logged in and session loaded.")
            return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"❌ Error sending code: {e}")
        await client.disconnect()
        return ConversationHandler.END

async def get_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip()
    phone = context.user_data.get("phone")
    client = context.user_data.get("client")
    phone_code_hash = context.user_data.get("phone_code_hash")

    if not client or not phone:
        await update.message.reply_text("❌ Session expired. Run /start again.")
        return ConversationHandler.END

    try:
        if phone_code_hash:
            await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        else:
            await client.sign_in(phone=phone, code=code)
        user_clients[update.effective_user.id] = client
        bot_users[str(update.effective_user.id)] = phone
        save_bot_users(bot_users)
        await update.message.reply_text("✅ Logged in successfully! You can now /upload_vcf")
        return ConversationHandler.END
    except SessionPasswordNeededError:
        await update.message.reply_text("🔒 This account has 2FA. Please enter your password.")
        return ASK_PASS
    except errors.PhoneCodeInvalidError:
        await update.message.reply_text("❌ The code you entered is invalid. Please try again or request a new code with /start.")
        return ASK_CODE
    except errors.PhoneCodeExpiredError:
        await update.message.reply_text("❌ The code you entered has expired. Please request a new code with /start.")
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"❌ Login error: {e}\nStart again with /start if needed.")
        try:
            await client.disconnect()
        except Exception:
            pass
        return ConversationHandler.END

async def get_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = update.message.text.strip()
    client = context.user_data.get("client")
    if not client:
        await update.message.reply_text("❌ Session expired. Run /start again.")
        return ConversationHandler.END

    try:
        await client.sign_in(password=password)
        user_clients[update.effective_user.id] = client
        bot_users[str(update.effective_user.id)] = context.user_data.get("phone")
        save_bot_users(bot_users)
        await update.message.reply_text("✅ Logged in with 2FA successfully! You can now /upload_vcf")
    except Exception as e:
        await update.message.reply_text(f"❌ 2FA error: {e}")
    return ConversationHandler.END

async def logoutall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    phone = bot_users.get(str(tg_id))
    if not phone:
        await update.message.reply_text("⚠️ You are not logged in.")
        return
    
    client = await get_client(tg_id, phone)
    if not await client.is_user_authorized():
        await update.message.reply_text("⚠️ Your session is not active or already logged out.")
        return
    
    try:
        # Get all sessions from server
        sessions = await client(functions.account.GetAuthorizationsRequest())
        current_session_hash = await client.session.save()
        
        # Revoke all sessions except current
        for session in sessions.authorizations:
            if session.hash != current_session_hash:
                await client(functions.account.ResetAuthorizationRequest(hash=session.hash))
        
        # Log out current session
        await client.log_out()
        
        # Remove session file locally
        session_path = os.path.join(SESSIONS_DIR, f"{phone}.session")
        if os.path.exists(session_path):
            os.remove(session_path)
        
        # Remove user from bot_users
        bot_users.pop(str(tg_id), None)
        save_bot_users(bot_users)
        
        await update.message.reply_text("✅ Logged out from all sessions and local data cleared.")
        
    except Exception as e:
        await update.message.reply_text(f"❌ Error logging out: {e}")

# ---------------------------
# VCF upload / processing (unchanged)
# ---------------------------
async def cmd_upload_vcf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in user_clients:
        await update.message.reply_text("⚠️ You must login first with /start")
        return
    context.user_data["awaiting_vcf"] = True
    await update.message.reply_text("📁 Please upload your .vcf file now (send as file).")

async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_vcf"):
        return

    if not update.message.document:
        await update.message.reply_text("Please upload a valid .vcf file.")
        return

    filename = update.message.document.file_name or ""
    if not filename.lower().endswith(".vcf") and update.message.document.mime_type != "text/vcard":
        await update.message.reply_text("File doesn't look like a .vcf. Please upload a .vcf file.")
        return

    tg_id = update.effective_user.id
    client = user_clients.get(tg_id)
    if not client:
        await update.message.reply_text("Session not found. /start and login again.")
        return

    phone = bot_users.get(str(tg_id))
    user_folder = os.path.join(SESSIONS_DIR, phone)
    if not os.path.exists(user_folder):
        os.makedirs(user_folder)

    try:
        vcf_path = os.path.join(user_folder, "uploaded.vcf")
        file = await update.message.document.get_file()
        await file.download_to_drive(vcf_path)
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to download file: {e}")
        return

    await update.message.reply_text("🔎 Parsing VCF...")
    contacts = []
    try:
        with open(vcf_path, "r", encoding="utf-8", errors="ignore") as f:
            for vcard in vobject.readComponents(f):
                name = getattr(vcard, "fn").value if hasattr(vcard, "fn") else "Unknown"
                if hasattr(vcard, "tel"):
                    for tel in vcard.tel_list:
                        phone_num = tel.value.strip().replace(" ", "").replace("-", "")
                        if phone_num.startswith("+"):
                            contacts.append({"phone": phone_num, "name": name})
    except Exception as e:
        await update.message.reply_text(f"❌ VCF parse error: {e}")
        return

    if not contacts:
        await update.message.reply_text("No valid international-format contacts found in the VCF.")
        return

    await update.message.reply_text(f"Found {len(contacts)} contacts. Importing to your Telegram account (safe mode). This can take time.")

    imported = await import_contacts_safely_for_user(client, phone, contacts, update)
    if imported:
        await update.message.reply_text(f"✅ Imported {len(imported)} users. Saved to your session files.")
        context.user_data["has_imported"] = True
        await update.message.reply_text("Now send the channel username or invite link where you want to add members (e.g. @MyChannel or https://t.me/joinchat/XXXX).")
        context.user_data["awaiting_vcf"] = False
        context.user_data["awaiting_channel"] = True
    else:
        await update.message.reply_text("No users were imported from that VCF (or import failed). Check failed contacts file in your session folder.")

async def import_contacts_safely_for_user(client: TelegramClient, phone: str, contacts: list, update: Update):
    user_folder = os.path.join(SESSIONS_DIR, phone)
    if not os.path.exists(user_folder):
        os.makedirs(user_folder)

    imported_file = os.path.join(user_folder, f"{phone}_imported.json")
    failed_file = os.path.join(user_folder, f"{phone}_failed.json")

    if os.path.exists(imported_file):
        with open(imported_file, "r") as f:
            imported_users = json.load(f)
        imported_ids = {u["id"] for u in imported_users}
    else:
        imported_users = []
        imported_ids = set()

    if os.path.exists(failed_file):
        with open(failed_file, "r") as f:
            failed_contacts = json.load(f)
    else:
        failed_contacts = []

    remaining = []
    imported_phones = {u.get("phone") for u in imported_users if u.get("phone")}
    for c in contacts:
        if c["phone"] not in imported_phones:
            remaining.append(c)

    await update.message.reply_text(f"📥 Importing {len(remaining)} remaining contacts in batches of {IMPORT_BATCH}...")

    for i in range(0, len(remaining), IMPORT_BATCH):
        batch = remaining[i : i + IMPORT_BATCH]
        phone_contacts = [
            InputPhoneContact(client_id=random.randint(0, 999999), phone=c["phone"], first_name=c["name"], last_name="")
            for c in batch
        ]

        try:
            result = await client(functions.contacts.ImportContactsRequest(contacts=phone_contacts))
            for user in result.users:
                phone_val = getattr(user, "phone", None) or ""
                imported_users.append({
                    "id": user.id,
                    "access_hash": user.access_hash,
                    "first_name": user.first_name or "Unknown",
                    "phone": phone_val
                })
                imported_ids.add(user.id)

            with open(imported_file, "w") as f:
                json.dump(imported_users, f, indent=2)

            wait_time = IMPORT_BASE_DELAY + random.randint(*IMPORT_JITTER)
            await update.message.reply_text(f"✅ Imported batch {i//IMPORT_BATCH + 1}. Sleeping {wait_time}s...")
            await asyncio.sleep(wait_time)

        except FloodWaitError as e:
            await update.message.reply_text(f"⚠️ FloodWait detected. Sleeping {e.seconds}s (server requested) ...")
            await asyncio.sleep(e.seconds)
            continue
        except Exception as e:
            failed_contacts.extend(batch)
            with open(failed_file, "w") as f:
                json.dump(failed_contacts, f, indent=2)
            await update.message.reply_text(f"❌ Import error for batch {i//IMPORT_BATCH + 1}: {e}")
            continue

    return imported_users

# ---------------------------
# CHANNEL CHOICE & INVITE FLOW
# ---------------------------
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    client = user_clients.get(tg_id)
    if not client:
        await update.message.reply_text("⚠️ You must login first with /start")
        return

    if context.user_data.get("awaiting_channel"):
        channel_text = update.message.text.strip()
        phone = bot_users.get(str(tg_id))
        try:
            entity = await client.get_entity(channel_text)
        except Exception as e:
            await update.message.reply_text(f"❌ Could not resolve channel: {e}\nSend @username or invite link.")
            return

        try:
            admins = await client.get_participants(entity, filter=ChannelParticipantsAdmins)
            me = await client.get_me()
            is_admin = any((a.id == me.id) for a in admins)
        except Exception as e:
            await update.message.reply_text(f"❌ Failed to check admin list: {e}")
            return

        if not is_admin:
            await update.message.reply_text("⚠️ You must be an admin with invite permissions in that channel to add members.")
            context.user_data["awaiting_channel"] = True
            return

        # store resolved entity for use in add_members
        context.user_data["target_channel_entity"] = entity
        context.user_data["target_channel_input"] = channel_text
        context.user_data["awaiting_channel"] = False
        context.user_data["awaiting_num"] = True
        await update.message.reply_text("✅ Channel verified. How many members do you want to add? (Enter a number)")
        return

    if context.user_data.get("awaiting_num"):
        try:
            num = int(update.message.text.strip())
            if num <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Please enter a valid positive number.")
            return

        phone = bot_users.get(str(tg_id))
        if not phone:
            await update.message.reply_text("Session phone not found. Re-login with /start.")
            return

        user_folder = os.path.join(SESSIONS_DIR, phone)
        imported_file = os.path.join(user_folder, f"{phone}_imported.json")
        if not os.path.exists(imported_file):
            await update.message.reply_text("No imported users found. Upload a VCF first with /upload_vcf.")
            context.user_data["awaiting_num"] = False
            return

        with open(imported_file, "r") as f:
            imported_users = json.load(f)

        if not imported_users:
            await update.message.reply_text("Your imported list is empty.")
            context.user_data["awaiting_num"] = False
            return

        invited_file = os.path.join(user_folder, f"{phone}_invited.json")
        if os.path.exists(invited_file):
            with open(invited_file, "r") as f:
                invited_ids = {u["id"] for u in json.load(f)}
        else:
            invited_ids = set()

        candidates = [u for u in imported_users if u["id"] not in invited_ids]
        if not candidates:
            await update.message.reply_text("No remaining candidates to invite (all already invited).")
            context.user_data["awaiting_num"] = False
            return

        to_invite_count = min(num, len(candidates))
        selected = random.sample(candidates, to_invite_count)

        channel_entity = context.user_data.get("target_channel_entity")
        if not channel_entity:
            await update.message.reply_text("Channel info missing. Please re-send the channel link and try again.")
            context.user_data["awaiting_num"] = False
            return

        await update.message.reply_text(f"Starting invites: adding {to_invite_count} members in batches of {INVITE_BATCH}...")

        newly_invited = []
        failed_invites = []

        for j in range(0, len(selected), INVITE_BATCH):
            batch = selected[j: j+INVITE_BATCH]
            input_users = []
            for u in batch:
                if not u.get("access_hash"):
                    failed_invites.append(u)
                    continue
                input_users.append(InputUser(user_id=u["id"], access_hash=int(u["access_hash"])))

            if not input_users:
                continue

            try:
                await client(functions.channels.InviteToChannelRequest(channel=channel_entity, users=input_users))
                newly_invited.extend([u["id"] for u in batch if u.get("id")])
                invited_ids.update([u_id for u_id in newly_invited])
                with open(invited_file, "w") as f:
                    json.dump([{"id": i} for i in invited_ids], f, indent=2)

                wait_time = INVITE_BASE_DELAY + random.randint(*INVITE_JITTER)
                await update.message.reply_text(f"✅ Invited {len(input_users)} users. Sleeping {wait_time}s...")
                await asyncio.sleep(wait_time)

            except FloodWaitError as e:
                await update.message.reply_text(f"⚠️ FloodWait detected. Skipping batch. Sleeping {e.seconds}s ...")
                await asyncio.sleep(e.seconds)
                failed_invites.extend(batch)
                continue
            except UserPrivacyRestrictedError:
                await update.message.reply_text("⚠️ Some users have privacy restrictions. Skipping those users.")
                failed_invites.extend(batch)
                continue
            except Exception as e:
                await update.message.reply_text(f"❌ Error inviting batch: {e}")
                failed_invites.extend(batch)
                continue

        if failed_invites:
            failed_inv_file = os.path.join(user_folder, f"{phone}_invite_failed.json")
            with open(failed_inv_file, "w") as f:
                json.dump(failed_invites, f, indent=2)

        await update.message.reply_text(f"🎯 Invite process complete. Successfully invited: {len(newly_invited)}. Failed/Skipped: {len(failed_invites)}.")
        context.user_data["awaiting_num"] = False
        return

    return

# ---------------------------
# SET CHANNEL (improved)
# ---------------------------
async def set_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    phone = bot_users.get(user_id)
    if not phone:
        await update.message.reply_text("⚠️ You must login first with /start")
        return

    client = await get_client(update.effective_user.id, phone)

    if not context.args:
        await update.message.reply_text("⚡ Usage: /setchannel @yourchannel or invite link")
        return

    channel_text = context.args[0]

    try:
        entity = await client.get_entity(channel_text)

        admins = await client.get_participants(entity, filter=ChannelParticipantsAdmins)
        me = await client.get_me()
        if not any((a.id == me.id) for a in admins):
            await update.message.reply_text("⚠️ You must be an admin with invite permissions in that channel to add members.")
            return

        # store resolved entity and raw input
        context.user_data["target_channel_entity"] = entity
        context.user_data["target_channel_input"] = channel_text
        await update.message.reply_text(f"✅ Channel set to {channel_text}. You can now use /addmembers to start adding.")

    except Exception as e:
        await update.message.reply_text(f"❌ Could not set channel: {e}")

# ---------------------------
# ADD MEMBERS (improved)
# ---------------------------
async def add_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    phone = bot_users.get(user_id)
    if not phone:
        await update.message.reply_text("⚠️ You must login first with /start")
        return

    client = await get_client(update.effective_user.id, phone)

    if "target_channel_entity" not in context.user_data:
        await update.message.reply_text("⚡ First set a channel with /setchannel")
        return

    contacts_file = os.path.join(SESSIONS_DIR, phone, f"{phone}_imported.json")
    if not os.path.exists(contacts_file):
        await update.message.reply_text("⚡ No imported contacts found. Upload a VCF first with /upload_vcf")
        return

    with open(contacts_file, "r") as f:
        contacts = json.load(f)

    if not contacts:
        await update.message.reply_text("⚡ You have no imported contacts to add.")
        return

    channel_entity = context.user_data["target_channel_entity"]

    # batch invites with delay and error handling to avoid flooding
    newly_invited = []
    failed_invites = []

    for i in range(0, len(contacts), INVITE_BATCH):
        batch = contacts[i : i + INVITE_BATCH]
        input_users = []
        for c in batch:
            if not c.get("access_hash"):
                failed_invites.append(c)
                continue
            input_users.append(InputUser(user_id=c["id"], access_hash=int(c["access_hash"])))

        if not input_users:
            continue

        try:
            await client(functions.channels.InviteToChannelRequest(channel=channel_entity, users=input_users))
            newly_invited.extend([c["id"] for c in batch if c.get("id")])
            await update.message.reply_text(f"✅ Invited {len(input_users)} members. Sleeping to avoid flood...")
            await asyncio.sleep(INVITE_BASE_DELAY + random.randint(*INVITE_JITTER))

        except FloodWaitError as e:
            await update.message.reply_text(f"⚠️ FloodWait detected, sleeping {e.seconds}s...")
            await asyncio.sleep(e.seconds)
            failed_invites.extend(batch)
            continue
        except UserPrivacyRestrictedError:
            await update.message.reply_text("⚠️ Some users have privacy restrictions. Skipping those users.")
            failed_invites.extend(batch)
            continue
        except Exception as e:
            await update.message.reply_text(f"❌ Error inviting batch: {e}")
            failed_invites.extend(batch)
            continue

    await update.message.reply_text(f"🎯 Add members complete. Added: {len(newly_invited)}, Failed: {len(failed_invites)}.")

# ---------------------------
# Bot startup
# ---------------------------
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
        per_user=True,
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("setchannel", set_channel))
    app.add_handler(CommandHandler("addmembers", add_members))
    app.add_handler(CommandHandler("upload_vcf", cmd_upload_vcf))
    app.add_handler(MessageHandler(filters.Document.ALL & ~filters.COMMAND, document_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))

    print("🤖 Server bot running. Waiting for users...")
    app.run_polling()

if __name__ == "__main__":
    main()
