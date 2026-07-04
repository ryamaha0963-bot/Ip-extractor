import asyncio
import json
import os
import re
from pyrogram import Client, filters
from pyrogram.raw import functions, types
from pyrogram.enums import ChatType
from dotenv import load_dotenv

load_dotenv()

# ---------- CONFIG ----------
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

# ---------- BOT CLIENT ----------
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

# ---------- EXTRACT IP (FIXED - NO InputChannel ERROR) ----------
async def extract_ips_from_vc(chat_id, link=None):
    user = await get_user_client()
    if not user:
        return None, "❌ User session not set. Use /setsession"

    try:
        # 1. Resolve target (join if link)
        if link:
            try:
                chat = await user.join_chat(link)
                chat_id = chat.id
            except Exception as e:
                return None, f"Link join error: {e}"
        else:
            chat = await user.get_chat(chat_id)
            chat_id = chat.id

        # 2. CRITICAL FIX: Safely get access_hash using high-level client
        #    This avoids the "InputChannel.init() takes 1 argument" bug.
        if chat.type in [ChatType.SUPERGROUP, ChatType.CHANNEL]:
            # Supergroup/Channel - we need access_hash
            if not hasattr(chat, 'access_hash') or not chat.access_hash:
                return None, "Cannot fetch access_hash for this chat."
            
            # Construct InputChannel safely using chat object's attributes
            input_channel = types.InputChannel(
                channel_id=chat.id,
                access_hash=chat.access_hash
            )
            # Call raw API
            full = await user.invoke(
                functions.channels.GetFullChannel(channel=input_channel)
            )
        else:
            # Basic group
            peer = await user.resolve_peer(chat_id)
            full = await user.invoke(
                functions.messages.GetFullChat(chat_id=peer.chat_id)
            )

        # 3. Check if VC is active
        call = getattr(full.full_chat, "call", None)
        if not call:
            return None, "📢 No active Voice Chat in this group."

        # 4. Join VC (just to get raw connection data)
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
            await asyncio.sleep(0.3)  # Let connection establish
        except Exception as e:
            # Already joined or join failed - but we proceed anyway
            pass

        # 5. Extract IPs
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

            # Leave VC immediately
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
                return None, "❌ No public IPs found. Try again."

            result = "🌐 **Extracted IPs (UDP):**\n"
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
    await message.reply("👋 **VC IP Grabber Bot**\n\n/setsession <session>\n/grab <id/link>")

@app.on_message(filters.command("setsession") & filters.user(ADMIN_ID))
async def set_session(client, message):
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply("Usage: /setsession <session_string>")
        return
    session_str = parts[1].strip()
    if len(session_str) < 50:
        await message.reply("❌ Too short. Paste full string.")
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
