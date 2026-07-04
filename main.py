import asyncio
import json
import os
import re
from pyrogram import Client, filters
from pyrogram.raw import functions, types
from pyrogram.enums import ChatType
from pyrogram.errors import PeerIdInvalid, InviteHashInvalid, UserAlreadyParticipant
from dotenv import load_dotenv

load_dotenv()

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

SESSION_FILE = "user_session.json"
IPV4_RE = re.compile(r'\b(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\b')

def load_session():
    try:
        with open(SESSION_FILE, "r") as f:
            return json.load(f).get("session")
    except:
        return None

def save_session(session_str):
    with open(SESSION_FILE, "w") as f:
        json.dump({"session": session_str}, f)

app = Client("bot_client", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)
user_client = None

async def get_user_client():
    global user_client
    if user_client and user_client.is_connected:
        return user_client
    session_str = load_session()
    if not session_str:
        return None
    user_client = Client("user_session", api_id=API_ID, api_hash=API_HASH, session_string=session_str)
    await user_client.start()
    return user_client

# ---------- 🔥 NEW: SAFE CHAT RESOLVER (Even if cache missing) ----------
async def resolve_chat_safe(user, chat_id):
    # Try 1: Normal get_chat
    try:
        chat = await user.get_chat(chat_id)
        return chat
    except PeerIdInvalid:
        pass
    except Exception:
        pass

    # Try 2: Brute-force search in all dialogs (guaranteed if member)
    try:
        async for dialog in user.get_dialogs():
            if dialog.chat.id == chat_id:
                return dialog.chat
    except Exception:
        pass

    # Try 3: If it's a supergroup, try resolving with peer (last hope)
    try:
        # For supergroups, peer id is negative, but we can try to get it via invite link if available? No.
        # Let's just raise error if nothing works.
        raise ValueError(f"Chat {chat_id} not found in dialogs. Make sure account is member.")
    except Exception as e:
        raise e

# ---------- CORE EXTRACT FUNCTION ----------
async def extract_ips_from_vc(chat_id, link=None):
    user = await get_user_client()
    if not user:
        return None, "❌ User session not set. Use /setsession"

    try:
        chat = None
        if link:
            try:
                chat = await user.join_chat(link)
                chat_id = chat.id
            except UserAlreadyParticipant:
                chat = await user.get_chat(link)
                chat_id = chat.id
            except Exception as e:
                return None, f"Link join failed: {e}"
        else:
            # 🔥 USE THE SAFE RESOLVER HERE
            chat = await resolve_chat_safe(user, chat_id)
            if not chat:
                return None, "❌ Account is NOT a member of this group, or group ID is wrong."

        # ---- GET ACCESS_HASH SAFELY ----
        if chat.type in [ChatType.SUPERGROUP, ChatType.CHANNEL]:
            if not hasattr(chat, 'access_hash') or not chat.access_hash:
                return None, "Cannot fetch access_hash for this chat."
            input_channel = types.InputChannel(
                channel_id=chat.id,
                access_hash=chat.access_hash
            )
            full = await user.invoke(
                functions.channels.GetFullChannel(channel=input_channel)
            )
        else:
            peer = await user.resolve_peer(chat_id)
            full = await user.invoke(
                functions.messages.GetFullChat(chat_id=peer.chat_id)
            )

        call = getattr(full.full_chat, "call", None)
        if not call:
            return None, "📢 No active Voice Chat."

        # ---- JOIN VC ----
        try:
            me = await user.resolve_peer('me')
            group_call = await user.invoke(
                functions.phone.GetGroupCall(
                    call=types.InputGroupCall(call.id, call.access_hash),
                    limit=1
                )
            )
            call_obj = group_call.call
            raw_params = getattr(call_obj, 'params', None)
            params_data = json.loads(raw_params.data) if raw_params and hasattr(raw_params, 'data') else {}
            await user.invoke(
                functions.phone.JoinGroupCall(
                    call=types.InputGroupCall(call.id, call.access_hash),
                    join_as=me,
                    params=types.DataJSON(data=json.dumps(params_data)),
                    muted=True,
                    video_stopped=True
                )
            )
            await asyncio.sleep(0.3)
        except Exception as e:
            pass  # already joined or error, proceed

        # ---- EXTRACT IPs ----
        try:
            group_call = await user.invoke(
                functions.phone.GetGroupCall(
                    call=types.InputGroupCall(call.id, call.access_hash),
                    limit=100
                )
            )
            call_obj = group_call.call
            raw_params = getattr(call_obj, 'params', None)
            data_str = getattr(raw_params, 'data', '{}') if raw_params else '{}'
            parsed = json.loads(data_str) if data_str else {}

            all_text = json.dumps(parsed) + str(call_obj)
            ips = set()
            for ip in IPV4_RE.findall(all_text):
                if not ip.startswith("0.") and not ip.startswith("127.") and not ip.startswith("192.168.") and not ip.startswith("10."):
                    ips.add(ip)
            for ep in parsed.get("endpoints", []):
                if isinstance(ep, str) and ":" in ep:
                    parts = ep.rsplit(":", 1)
                    if len(parts) == 2 and parts[0].replace('.', '').isdigit():
                        ips.add(parts[0])
            for srv in parsed.get("servers", []):
                if isinstance(srv, dict):
                    ip = srv.get("ip") or srv.get("host")
                    if ip and isinstance(ip, str):
                        ips.add(ip)

            # Leave VC
            try:
                await user.invoke(
                    functions.phone.LeaveGroupCall(
                        call=types.InputGroupCall(call.id, call.access_hash),
                        source=0
                    )
                )
            except:
                pass

            if not ips:
                return None, "❌ No public IPs found."

            result = "🌐 **Extracted IPs:**\n"
            for ip in list(ips)[:10]:
                result += f"`{ip}` : `10001`\n"
            return result, None

        except Exception as e:
            try:
                await user.invoke(
                    functions.phone.LeaveGroupCall(
                        call=types.InputGroupCall(call.id, call.access_hash),
                        source=0
                    )
                )
            except:
                pass
            return None, f"Extraction error: {e}"

    except Exception as e:
        return None, f"Fatal error: {e}"

# ---------- BOT COMMANDS ----------
@app.on_message(filters.command("start"))
async def start_cmd(client, message):
    await message.reply(
        "👋 **VC IP Grabber Bot**\n\n"
        "/setsession <session> - Set user session\n"
        "/grab <group_id or link> - Extract IPs\n"
        "/status - Check session"
    )

@app.on_message(filters.command("setsession") & filters.user(ADMIN_ID))
async def set_session(client, message):
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply("Usage: /setsession <session_string>")
        return
    session_str = parts[1].strip()
    if len(session_str) < 50:
        await message.reply("❌ Too short.")
        return
    save_session(session_str)
    try:
        test = Client("test", api_id=API_ID, api_hash=API_HASH, session_string=session_str)
        await test.start()
        me = await test.get_me()
        await test.stop()
        await message.reply(f"✅ Session saved! Logged in as @{me.username}")
    except Exception as e:
        await message.reply(f"❌ Invalid session!\nError: {str(e)[:200]}")

@app.on_message(filters.command("grab"))
async def grab_cmd(client, message):
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply("Usage: /grab <group_id> or /grab <invite_link>")
        return
    target = parts[1]
    status_msg = await message.reply("⏳ Joining VC & extracting IPs... (1 sec)")

    chat_id = None
    link = None
    if "t.me" in target or "joinchat" in target or "/" in target:
        link = target
    else:
        try:
            chat_id = int(target)
        except:
            await status_msg.edit_text("❌ Invalid ID/Link.")
            return

    result, error = await extract_ips_from_vc(chat_id, link)
    if error:
        await status_msg.edit_text(error)
    else:
        await status_msg.edit_text(result)

@app.on_message(filters.command("status"))
async def status_cmd(client, message):
    sess = load_session()
    await message.reply("✅ Session SET" if sess else "❌ Session NOT SET")

print("🚀 Bot is starting...")
app.run()
