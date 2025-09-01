# bot.py
import os
import asyncio
import random
import json
import logging
import vobject
from dotenv import load_dotenv

# Telegram libraries
from telethon import TelegramClient, errors
from telethon.errors import FloodWaitError, PeerFloodError, UserPrivacyRestrictedError, SessionPasswordNeededError
from telethon.tl.types import InputPhoneContact, InputUser, ChannelParticipantsAdmins
from telethon.tl.functions.contacts import ImportContactsRequest
from telethon.tl.functions.channels import InviteToChannelRequest

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

# Import/Invite tuning (change if you understand the risk)
IMPORT_BATCH = 30             # how many numbers to import per ImportContactsRequest
IMPORT_BASE_DELAY = 10        # seconds base delay between import batches
IMPORT_JITTER = (5, 15)       # extra random seconds to add to base

INVITE_BATCH = 5              # how many users to invite per InviteToChannelRequest
INVITE_BASE_DELAY = 30        # base seconds between invite batches
INVITE_JITTER = (5, 20)       # extra random seconds to add to base

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
    # check existing client
    existing_client = user_clients.get(user_id)
    if existing_client and await existing_client.is_connected():
        return existing_client

    # path for session
    session_path = os.path.join(SESSIONS_DIR, f"{phone}.session")
    client = TelegramClient(session_path, API_ID, API_HASH)
    
    try:
        await client.connect()
    except Exception as e:
        # maybe the session is locked, try to disconnect first
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

bot_users = load_bot_users()  # telegram_user_id -> phone

# ---------------------------
# LOGIN CONVERSATION HANDLERS
# ---------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Send your phone number (in international format, e.g. +234901...) to log in.")
    return ASK_PHONE

async def get_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    context.user_data["phone"] = phone
    session_path = os.path.join(SESSIONS_DIR, phone)  # Telethon uses folder name; .session suffix will be applied

    client = await get_client(update.effective_user.id, phone)
    await client.connect()

    try:
        if not await client.is_user_authorized():
            sent = await client.send_code_request(phone)
            context.user_data["client"] = client
            # store phone_code_hash for sign_in
            context.user_data["phone_code_hash"] = getattr(sent, "phone_code_hash", None)
            await update.message.reply_text("📩 Code sent — enter the code you received in Telegram.")
            return ASK_CODE
        else:
            # already authorized (session file existed)
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
        # sign in with code (include phone_code_hash if available)
        if phone_code_hash:
            await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        else:
            await client.sign_in(phone=phone, code=code)
        # success
        user_clients[update.effective_user.id] = client
        bot_users[str(update.effective_user.id)] = phone
        save_bot_users(bot_users)
        await update.message.reply_text("✅ Logged in successfully! You can now /upload_vcf")
        return ConversationHandler.END
    except SessionPasswordNeededError:
        await update.message.reply_text("🔒 This account has 2FA. Please enter your password.")
        return ASK_PASS
    except Exception as e:
        # Some errors may contain hints like "PHONE_NUMBER_INVALID", "PHONE_CODE_EXPIRED", etc.
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

# ---------------------------
# VCF upload / processing
# ---------------------------
async def cmd_upload_vcf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # require login
    if update.effective_user.id not in user_clients:
        await update.message.reply_text("⚠️ You must login first with /start")
        return
    context.user_data["awaiting_vcf"] = True
    await update.message.reply_text("📁 Please upload your .vcf file now (send as file).")

async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # handle uploaded vcf if user is awaiting
    if not context.user_data.get("awaiting_vcf"):
        return  # ignore if not expecting vcf

    if not update.message.document:
        await update.message.reply_text("Please upload a valid .vcf file.")
        return

    filename = update.message.document.file_name or ""
    if not filename.lower().endswith(".vcf") and update.message.document.mime_type != "text/vcard":
        await update.message.reply_text("File doesn't look like a .vcf. Please upload a .vcf file.")
        return

    # get user's telethon client
    tg_id = update.effective_user.id
    client = user_clients.get(tg_id)
    if not client:
        await update.message.reply_text("Session not found. /start and login again.")
        return

    phone = bot_users.get(str(tg_id))
    user_folder = os.path.join(SESSIONS_DIR, phone)
    if not os.path.exists(user_folder):
        os.makedirs(user_folder)

    # download
    try:
        # download file to sessions/{phone}/uploaded.vcf
        vcf_path = os.path.join(user_folder, "uploaded.vcf")
        file = await update.message.document.get_file()
        await file.download_to_drive(vcf_path)
    except Exception as e:
        await update.message.reply_text(f"❌ Failed to download file: {e}")
        return

    await update.message.reply_text("🔎 Parsing VCF...")
    # parse VCF
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

    # import contacts into user's account (safe)
    imported = await import_contacts_safely_for_user(client, phone, contacts, update)
    if imported:
        await update.message.reply_text(f"✅ Imported {len(imported)} users. Saved to your session files.")
        # store a flag that user has imported contacts and can now choose channel
        context.user_data["has_imported"] = True
        await update.message.reply_text("Now send the channel username or invite link where you want to add members (e.g. @MyChannel or https://t.me/joinchat/XXXX).")
        context.user_data["awaiting_vcf"] = False
        context.user_data["awaiting_channel"] = True
    else:
        await update.message.reply_text("No users were imported from that VCF (or import failed). Check failed contacts file in your session folder.")

# ---------------------------
# IMPORT CONTACTS (per-user)
# ---------------------------
async def import_contacts_safely_for_user(client: TelegramClient, phone: str, contacts: list, update: Update):
    """
    Imports contacts into the given Telethon client safely with batching, resume and failed logs.
    Saves results in sessions/{phone}/{phone}_imported.json
    """
    user_folder = os.path.join(SESSIONS_DIR, phone)
    if not os.path.exists(user_folder):
        os.makedirs(user_folder)

    imported_file = os.path.join(user_folder, f"{phone}_imported.json")
    failed_file = os.path.join(user_folder, f"{phone}_failed.json")

    # load existing imported users
    if os.path.exists(imported_file):
        with open(imported_file, "r") as f:
            imported_users = json.load(f)
        imported_ids = {u["id"] for u in imported_users}
    else:
        imported_users = []
        imported_ids = set()

    # load failed contacts so far
    if os.path.exists(failed_file):
        with open(failed_file, "r") as f:
            failed_contacts = json.load(f)
    else:
        failed_contacts = []

    # determine remaining contacts (avoid re-importing same phone)
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
            result = await client(ImportContactsRequest(contacts=phone_contacts))
            # result.users often contains User objects for imported contacts
            for user in result.users:
                # attempt to get phone from the batch by matching username/email? best-effort:
                phone_val = getattr(user, "phone", None) or ""
                imported_users.append({
                    "id": user.id,
                    "access_hash": user.access_hash,
                    "first_name": user.first_name or "Unknown",
                    "phone": phone_val
                })
                imported_ids.add(user.id)

            # save progress
            with open(imported_file, "w") as f:
                json.dump(imported_users, f, indent=2)

            # pause w/ jitter
            wait_time = IMPORT_BASE_DELAY + random.randint(*IMPORT_JITTER)
            await update.message.reply_text(f"✅ Imported batch {i//IMPORT_BATCH + 1}. Sleeping {wait_time}s...")
            await asyncio.sleep(wait_time)

        except FloodWaitError as e:
            await update.message.reply_text(f"⚠️ FloodWait detected. Sleeping {e.seconds}s (server requested) ...")
            await asyncio.sleep(e.seconds)
            # retry same batch after waiting
            continue
        except Exception as e:
            # mark these contacts as failed for retry later
            failed_contacts.extend(batch)
            with open(failed_file, "w") as f:
                json.dump(failed_contacts, f, indent=2)
            await update.message.reply_text(f"❌ Import error for batch {i//IMPORT_BATCH + 1}: {e}")
            # continue with next batches
            continue

    return imported_users

# ---------------------------
# CHANNEL CHOICE & INVITE FLOW
# ---------------------------
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Generic text handler to pick up:
    - channel link after vcf import (context.user_data['awaiting_channel'])
    - number of members to invite (context.user_data['awaiting_num'])
    """
    tg_id = update.effective_user.id
    client = user_clients.get(tg_id)
    if not client:
        # ignore or tell to login
        await update.message.reply_text("⚠️ You must login first with /start")
        return

    # 1) If awaiting a channel after import
    if context.user_data.get("awaiting_channel"):
        channel_text = update.message.text.strip()
        phone = bot_users.get(str(tg_id))
        try:
            # resolve channel entity with user's client
            entity = await client.get_entity(channel_text)
        except Exception as e:
            await update.message.reply_text(f"❌ Could not resolve channel: {e}\nSend @username or invite link.")
            return

        # Check if user is admin in that channel
        try:
            admins = await client.get_participants(entity, filter=ChannelParticipantsAdmins)
            me = await client.get_me()
            is_admin = any((a.id == me.id) for a in admins)
        except Exception as e:
            await update.message.reply_text(f"❌ Failed to check admin list: {e}")
            return

        if not is_admin:
            await update.message.reply_text("⚠️ You must be an admin with invite permissions in that channel to add members.")
            # cancel awaiting channel so user can re-send different channel if they want
            context.user_data["awaiting_channel"] = True
            return

        # store channel and ask how many members to add
        context.user_data["target_channel_entity"] = entity.to_dict() if hasattr(entity, "to_dict") else entity  # store raw
        context.user_data["target_channel_input"] = channel_text
        context.user_data["awaiting_channel"] = False
        context.user_data["awaiting_num"] = True
        await update.message.reply_text("✅ Channel verified. How many members do you want to add? (Enter a number)")

        return

    # 2) If awaiting number to invite
    if context.user_data.get("awaiting_num"):
        try:
            num = int(update.message.text.strip())
            if num <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text("Please enter a valid positive number.")
            return

        # proceed to invite
        phone = bot_users.get(str(tg_id))
        if not phone:
            await update.message.reply_text("Session phone not found. Re-login with /start.")
            return

        # load imported users for this phone
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

        # load invited progress file to avoid double-invites
        invited_file = os.path.join(user_folder, f"{phone}_invited.json")
        if os.path.exists(invited_file):
            with open(invited_file, "r") as f:
                invited_ids = {u["id"] for u in json.load(f)}
        else:
            invited_ids = set()

        # prepare candidates (skip already invited)
        candidates = [u for u in imported_users if u["id"] not in invited_ids]
        if not candidates:
            await update.message.reply_text("No remaining candidates to invite (all already invited).")
            context.user_data["awaiting_num"] = False
            return

        to_invite_count = min(num, len(candidates))
        # choose random subset to add (so it's not always the same)
        selected = random.sample(candidates, to_invite_count)

        # get channel entity stored earlier
        channel_text = context.user_data.get("target_channel_input")
        if not channel_text:
            await update.message.reply_text("Channel info missing. Please re-send the channel link and try again.")
            context.user_data["awaiting_num"] = False
            return

        try:
            channel_entity = await client.get_entity(channel_text)
        except Exception as e:
            await update.message.reply_text(f"❌ Could not resolve channel: {e}")
            context.user_data["awaiting_num"] = False
            return

        await update.message.reply_text(f"Starting invites: adding {to_invite_count} members in batches of {INVITE_BATCH}...")

        # invite in batches with safety
        newly_invited = []
        failed_invites = []
        for j in range(0, len(selected), INVITE_BATCH):
            batch = selected[j : j + INVITE_BATCH]
            input_users = []
            for u in batch:
                # ensure access_hash exists
                if not u.get("access_hash"):
                    # we cannot invite without access_hash; skip and log
                    failed_invites.append(u)
                    continue
                input_users.append(InputUser(user_id=u["id"], access_hash=int(u["access_hash"])))

            if not input_users:
                continue

            try:
                await client(InviteToChannelRequest(channel=channel_entity, users=input_users))
                newly_invited.extend([u["id"] for u in batch if u.get("id")])
                # persist invited
                invited_ids.update([u_id for u_id in newly_invited])
                # save invited file
                invited_save = [{"id": i} for i in invited_ids]
                with open(invited_file, "w") as f:
                    json.dump(invited_save, f, indent=2)

                # user feedback and wait
                wait_time = INVITE_BASE_DELAY + random.randint(*INVITE_JITTER)
                await update.message.reply_text(f"✅ Invited {len(input_users)} users. Sleeping {wait_time}s...")
                await asyncio.sleep(wait_time)

            except FloodWaitError as e:
                await update.message.reply_text(f"⚠️ Some users refused invites (privacy). Skipping batch. Error: {e}")
                # mark batch members as failed
                failed_invites.extend(batch)
                continue
            except Exception as e:
                # other errors - log & continue
                await update.message.reply_text(f"❌ Error inviting batch: {e}")
                failed_invites.extend(batch)
                continue

        # write failed invites if any
        if failed_invites:
            failed_inv_file = os.path.join(user_folder, f"{phone}_invite_failed.json")
            with open(failed_inv_file, "w") as f:
                json.dump(failed_invites, f, indent=2)

        await update.message.reply_text(f"🎯 Invite process complete. Successfully invited: {len(newly_invited)}. Failed/Skipped: {len(failed_invites)}.")
        context.user_data["awaiting_num"] = False
        return

    # default fallback for other texts
    return

# ---------------------------
# SET CHANNEL
# ---------------------------
async def set_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    phone = bot_users.get(user_id)
    if not phone:
        await update.message.reply_text("⚠️ You must login first with /start")
        return

    client = await get_client(update.effective_user.id, phone)

    if not context.args:
        await update.message.reply_text("⚡ Usage: /setchannel @yourchannel")
        return

    channel = context.args[0]
    # store channel in user_data
    context.user_data["channel"] = channel
    await update.message.reply_text(f"✅ Channel set to {channel}. You can now use /addmembers to start adding.")


# ---------------------------
# ADD MEMBERS
# ---------------------------
async def add_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    phone = bot_users.get(user_id)
    if not phone:
        await update.message.reply_text("⚠️ You must login first with /start")
        return

    client = await get_client(update.effective_user.id, phone)

    if "channel" not in context.user_data:
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

    channel = context.user_data["channel"]
    added = 0

    for contact in contacts:
        try:
            result = await client(functions.contacts.ImportContactsRequest(
                contacts=[InputPhoneContact(client_id=random.randint(0, 999999), phone=contact["phone"], first_name=contact["name"], last_name="")]
            ))
            if result.users:
                user = result.users[0]
                await client(functions.channels.InviteToChannelRequest(
                    channel=channel,
                    users=[user.id]
                ))
                added += 1
            await asyncio.sleep(2)  # small delay to avoid flood
        except Exception:
            continue

    await update.message.reply_text(f"✅ Added {added} members to {channel}!")
# ---------------------------
# Bot startup
# ---------------------------
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # login conversation
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
