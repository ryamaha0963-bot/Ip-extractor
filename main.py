import asyncio
import json
import os
import re
import logging
from pyrogram import Client, filters
from pyrogram.raw import functions, types
from pyrogram.errors import PeerIdInvalid, UserAlreadyParticipant
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO)
LOGGER = logging.getLogger(__name__)

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
    me = await user_client.get_me()
    LOGGER.info(f"✅ User session active: @{me.username}")
    return user_client

# ---------- RAW DIALOGS FETCH (NO CACHE) ----------
async def get_all_groups_raw(user):
    """Fetch dialogs directly from server using raw API."""
    groups = []
    try:
        dialogs = await user.invoke(functions.messages.GetDialogs(
            offset_date=0,
            offset_id=0,
            offset_peer=types.InputPeerEmpty(),
            limit=200,
            hash=0
        ))
        for dialog in dialogs.dialogs:
            peer = dialog.peer
            # Find chat info
            chat = None
            if isinstance(peer, types.PeerChannel):
                # Search in channels
                for channel in dialogs.channels:
                    if channel.id == peer.channel_id:
                        chat = channel
                        break
            elif isinstance(peer, types.PeerChat):
                for group in dialogs.chats:
                    if group.id == peer.chat_id:
                        chat = group
                        break
            if chat:
                groups.append({
                    "id": chat.id,
                    "title": getattr(chat, "title", "No Title"),
                    "type": "channel" if isinstance(peer, types.PeerChannel) else "group"
                })
    except Exception as e:
        LOGGER.error(f"Raw dialogs error: {e}")
    return groups

# ---------- EXTRACT IP (WITH PEER RESOLVE) ----------
async def extract_ips_from_vc(chat_id, link=None):
    user = await get_user_client()
    if not user:
        return None, "❌ User session not set."

    try:
        if link:
            try:
                chat = await user.join_chat(link)
                chat_id = chat.id
            except UserAlreadyParticipant:
                chat = await user.get_chat(link)
                chat_id = chat.id
            except Exception as e:
                return None, f"Link join failed: {e}"

        # Try to get peer using resolve_peer (will work if member)
        try:
            peer = await user.resolve_peer(chat_id)
        except PeerIdInvalid:
            return None, "❌ Account is not a member of this group. Use invite link or add account."

        # Get full chat details
        if isinstance(peer, types.InputPeerChannel):
            full = await user.invoke(
                functions.channels.GetFullChannel(
                    channel=types.InputChannel(
                        channel_id=peer.channel_id,
                        access_hash=peer.access_hash
                    )
                )
            )
        elif isinstance(peer, types.InputPeerChat):
            full = await user.invoke(
                functions.messages.GetFullChat(chat_id=peer.chat_id)
            )
        else:
            return None, "Unsupported chat type."

        call = getattr(full.full_chat, "call", None)
        if not call:
            return None, "📢 No active Voice Chat."

        # Join VC
        try:
            me = await user.resolve_peer('me')
            group_call = await user.invoke(
                functions.phone.GetGroupCall(
                    call=types.InputGroupCall(id=call.id, access_hash=call.access_hash),
                    limit=1
                )
            )
            call_obj = group_call.call
            raw_params = getattr(call_obj, 'params', None)
            params_data = json.loads(raw_params.data) if raw_params and hasattr(raw_params, 'data') else {}
            await user.invoke(
                functions.phone.JoinGroupCall(
                    call=types.InputGroupCall(id=call.id, access_hash=call.access_hash),
                    join_as=me,
                    params=types.DataJSON(data=json.dumps(params_data)),
                    muted=True,
                    video_stopped=True
                )
            )
            await asyncio.sleep(0.5)
        except Exception as e:
            LOGGER.warning(f"Join error: {e}")

        # Extract IPs
        try:
            group_call = await user.invoke(
                functions.phone.GetGroupCall(
                    call=types.InputGroupCall(id=call.id, access_hash=call.access_hash),
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

            # Leave
            try:
                await user.invoke(
                    functions.phone.LeaveGroupCall(
                        call=types.InputGroupCall(id=call.id, access_hash=call.access_hash),
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
                        call=types.InputGroupCall(id=call.id, access_hash=call.access_hash),
                        source=0
                    )
                )
            except:
                pass
            return None, f"Extraction error: {e}"

    except Exception as e:
        LOGGER.error(f"Fatal: {e}")
        return None, f"Fatal error: {e}"

# ---------- BOT COMMANDS ----------
@app.on_message(filters.command("start"))
async def start_cmd(client, message):
    await message.reply(
        "👋 **VC IP Grabber Bot**\n\n"
        "/setsession <session> - Set user session\n"
        "/grab <group_id or link> - Extract IPs\n"
        "/groups - List groups (raw, no cache)\n"
        "/whoami - Show logged in account\n"
        "/ping - Check if bot is alive"
    )

@app.on_message(filters.command("ping"))
async def ping_cmd(client, message):
    await message.reply("🏓 Pong! Bot is alive.")

@app.on_message(filters.command("whoami"))
async def whoami_cmd(client, message):
    user = await get_user_client()
    if not user:
        await message.reply("❌ User session not set.")
        return
    me = await user.get_me()
    await message.reply(f"✅ Logged in as: @{me.username} (ID: {me.id})")

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

@app.on_message(filters.command("groups"))
async def groups_cmd(client, message):
    user = await get_user_client()
    if not user:
        await message.reply("❌ User session not set. Use /setsession")
        return
    status = await message.reply("⏳ Fetching groups (raw API)...")
    groups = await get_all_groups_raw(user)
    if not groups:
        await status.edit_text("❌ Account is not a member of any group.\n\nPossible reasons:\n- Session is for a different account.\n- Account has no groups.\n- Use invite link directly: /grab https://t.me/joinchat/...")
        return
    text = "📋 **Groups (raw, no cache):**\n\n"
    for g in groups[:20]:
        text += f"🔹 `{g['id']}` – {g['title']}\n"
    if len(groups) > 20:
        text += f"\n... and {len(groups) - 20} more"
    await status.edit_text(text)

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

if __name__ == "__main__":
    LOGGER.info("🚀 Bot is starting...")
    app.run()
