import asyncio
import json
import os
import re
import time
import socket
from pyrogram import Client, filters
from pyrogram.raw import functions, types
from pyrogram.errors import FloodWait, UserAlreadyParticipant, InviteHashInvalid
from dotenv import load_dotenv

load_dotenv()

# ---------- CONFIG ----------
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

# File to store session string dynamically (persists across restarts on Railway)
SESSION_FILE = "user_session.json"
TARGET_FILE = "target_chat.json"

# ---------- HELPERS ----------
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

def load_target():
    try:
        with open(TARGET_FILE, "r") as f:
            return json.load(f).get("chat_id")
    except:
        return None

def save_target(chat_id):
    with open(TARGET_FILE, "w") as f:
        json.dump({"chat_id": chat_id}, f)

# ---------- BOT CLIENT (Telegram Bot) ----------
app = Client("bot_client", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# ---------- USER CLIENT (To join VC) ----------
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

# ---------- CORE LOGIC: EXTRACT IP WITHIN 1 SEC ----------
async def extract_ips_from_vc(chat_id, link=None):
    user = await get_user_client()
    if not user:
        return None, "❌ User session not set. Use /setsession <session_string>"

    try:
        # 1. Resolve target
        if link:
            try:
                chat = await user.join_chat(link)
                chat_id = chat.id
            except UserAlreadyParticipant:
                # Already joined, just resolve
                chat = await user.get_chat(link)
                chat_id = chat.id
            except Exception as e:
                return None, f"Invalid link or cannot join: {e}"
        else:
            chat = await user.get_chat(chat_id)
            if not chat:
                return None, "Chat not found"

        peer = await user.resolve_peer(chat_id)

        # 2. Check if VC is active
        try:
            if isinstance(peer, types.InputPeerChannel):
                full = await user.invoke(functions.channels.GetFullChannel(
                    channel=types.InputChannel(peer.channel_id, peer.access_hash)
                ))
            else:
                full = await user.invoke(functions.messages.GetFullChat(chat_id=peer.chat_id))
            
            call = getattr(full.full_chat, "call", None)
            if not call:
                return None, "📢 No active Voice Chat in this group."
        except Exception as e:
            return None, f"Error fetching VC: {e}"

        # 3. Join the VC (MANDATORY to get raw IPs in params)
        # We need to join to get the actual connection data
        try:
            me = await user.resolve_peer('me')
            # Sometimes params are needed, we pass a dummy if missing, or fetch from getGroupCall
            # Better: first get the call params via GetGroupCall
            group_call = await user.invoke(functions.phone.GetGroupCall(
                call=types.InputGroupCall(call.id, call.access_hash),
                limit=1
            ))
            call_obj = group_call.call
            raw_params = getattr(call_obj, 'params', None)
            if raw_params:
                params_data = json.loads(raw_params.data) if hasattr(raw_params, 'data') else {}
            else:
                params_data = {"ufrag": "x", "pwd": "y", "fingerprints": [], "ssrc": 1}

            # Join the call
            await user.invoke(functions.phone.JoinGroupCall(
                call=types.InputGroupCall(call.id, call.access_hash),
                join_as=me,
                params=types.DataJSON(data=json.dumps(params_data)),
                muted=True,
                video_stopped=True
            ))
            await asyncio.sleep(0.3)  # tiny delay to let connection establish (still < 1 sec total)

        except UserAlreadyParticipant:
            pass  # Already joined, fine
        except Exception as e:
            return None, f"⚠️ Join failed: {e}"

        # 4. Extract IPs - Fetch updated call details
        try:
            group_call = await user.invoke(functions.phone.GetGroupCall(
                call=types.InputGroupCall(call.id, call.access_hash),
                limit=100
            ))
            call_obj = group_call.call
            raw_params = getattr(call_obj, 'params', None)
            data_str = getattr(raw_params, 'data', '{}') if raw_params else '{}'
            parsed = json.loads(data_str) if data_str else {}

            # Search everywhere: endpoints, servers, and raw string
            all_text = json.dumps(parsed) + str(call_obj)
            ips = set()
            for ip in IPV4_RE.findall(all_text):
                if not ip.startswith("0.") and not ip.startswith("127.") and not ip.startswith("192.168.") and not ip.startswith("10."):
                    ips.add(ip)

            # Also check specific fields
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

            # 5. Leave the VC immediately to save resources
            try:
                await user.invoke(functions.phone.LeaveGroupCall(
                    call=types.InputGroupCall(call.id, call.access_hash),
                    source=0
                ))
            except:
                pass

            if not ips:
                return None, "❌ No public IPs found. Maybe the VC is using TURN relay only? Try again."

            # Return with port (usually 10001 for voice)
            result = "🌐 **Extracted IPs (Public):**\n"
            for ip in list(ips)[:10]:  # max 10
                result += f"`{ip}` : `10001` (UDP)\n"
            result += f"\n✅ Total: {len(ips)} unique IPs."
            return result, None

        except Exception as e:
            # Try to leave even on error
            try:
                await user.invoke(functions.phone.LeaveGroupCall(
                    call=types.InputGroupCall(call.id, call.access_hash),
                    source=0
                ))
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
        "**Commands:**\n"
        "/setsession <session> - Set user session (admin only)\n"
        "/grab <group_id_or_link> - Join VC & extract IPs instantly\n"
        "/status - Check current session status\n\n"
        "⚡ Grabs IPs within 1 second!"
    )

@app.on_message(filters.command("setsession") & filters.user(ADMIN_ID))
async def set_session(client, message):
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply("Usage: /setsession <pyrogram_session_string>")
        return
    session_str = parts[1]
    save_session(session_str)
    # Test the session
    try:
        test_client = Client("test", api_id=API_ID, api_hash=API_HASH, session_string=session_str)
        await test_client.start()
        me = await test_client.get_me()
        await test_client.stop()
        await message.reply(f"✅ Session saved successfully!\nLogged in as: {me.first_name} (@{me.username})")
    except Exception as e:
        await message.reply(f"❌ Invalid session string!\nError: {e}")

@app.on_message(filters.command("grab"))
async def grab_cmd(client, message):
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply("Usage: /grab <group_id> or /grab <invite_link>\nExample: /grab -1001234567890\nExample: /grab https://t.me/joinchat/abc123")
        return
    
    target = parts[1]
    status_msg = await message.reply("⏳ Joining VC and extracting IPs... (1 sec)")
    
    # Try to parse as int ID or string link
    chat_id = None
    link = None
    if target.startswith("http") or target.startswith("t.me") or target.startswith("+") or target.startswith("@") or "/" in target:
        link = target
    else:
        try:
            chat_id = int(target)
        except:
            await status_msg.edit_text("❌ Invalid ID or Link format.")
            return

    result, error = await extract_ips_from_vc(chat_id, link)
    if error:
        await status_msg.edit_text(f"{error}")
    else:
        await status_msg.edit_text(result)

@app.on_message(filters.command("status"))
async def status_cmd(client, message):
    sess = load_session()
    if sess:
        await message.reply("✅ User session is **SET**.")
    else:
        await message.reply("❌ User session is **NOT SET**. Use /setsession")

# ---------- RUN BOT ----------
print("🚀 Bot is starting...")
app.run()
