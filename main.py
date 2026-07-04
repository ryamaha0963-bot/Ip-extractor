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

# ---------- RAW GROUPS LIST ----------
async def list_groups_raw(user):
    groups = []
    try:
        dialogs = await user.invoke(
            functions.messages.GetDialogs(
                offset_date=0,
                offset_id=0,
                offset_peer=types.InputPeerEmpty(),
                limit=100,
                hash=0
            )
        )
        for dialog in dialogs.dialogs:
            peer = dialog.peer
            chat_id = None
            title = "Unknown"
            for chat in dialogs.chats:
                if isinstance(peer, types.PeerChat) and chat.id == peer.chat_id:
                    chat_id = peer.chat_id
                    title = getattr(chat, 'title', 'Group')
                    break
                elif isinstance(peer, types.PeerChannel) and chat.id == peer.channel_id:
                    chat_id = -1000000000000 - chat.id
                    title = getattr(chat, 'title', 'Channel')
                    break
            if chat_id:
                groups.append({"id": chat_id, "title": title})
    except Exception as e:
        LOGGER.error(f"Raw dialogs error: {e}")
    return groups

# ---------- 🔥 ADVANCED IP EXTRACTOR ----------
async def extract_ips_from_vc(chat_id, link=None, debug=False):
    user = await get_user_client()
    if not user:
        return None, "❌ User session not set."

    try:
        # Join if link
        if link:
            try:
                chat = await user.join_chat(link)
                chat_id = chat.id
            except UserAlreadyParticipant:
                chat = await user.get_chat(link)
                chat_id = chat.id
            except Exception as e:
                return None, f"Link join failed: {e}"

        peer = await user.resolve_peer(chat_id)
        
        # Get full chat
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
                    call=types.InputGroupCall(
                        id=call.id,
                        access_hash=call.access_hash
                    ),
                    limit=100  # Get participants
                )
            )
            call_obj = group_call.call
            raw_params = getattr(call_obj, 'params', None)
            params_data = json.loads(raw_params.data) if raw_params and hasattr(raw_params, 'data') else {}
            
            await user.invoke(
                functions.phone.JoinGroupCall(
                    call=types.InputGroupCall(
                        id=call.id,
                        access_hash=call.access_hash
                    ),
                    join_as=me,
                    params=types.DataJSON(data=json.dumps(params_data)),
                    muted=True,
                    video_stopped=True
                )
            )
            await asyncio.sleep(0.8)  # Wait for connection
        except Exception as e:
            LOGGER.warning(f"Join error: {e}")

        # 🔥 FETCH AGAIN AFTER JOIN TO GET FULL DATA
        try:
            group_call = await user.invoke(
                functions.phone.GetGroupCall(
                    call=types.InputGroupCall(
                        id=call.id,
                        access_hash=call.access_hash
                    ),
                    limit=100
                )
            )
        except Exception as e:
            return None, f"Fetch call error: {e}"

        call_obj = group_call.call
        raw_params = getattr(call_obj, 'params', None)
        data_str = getattr(raw_params, 'data', '{}') if raw_params else '{}'
        parsed = json.loads(data_str) if data_str else {}

        # ---- EXTRACT IPs FROM MULTIPLE SOURCES ----
        ips = set()
        
        # Source 1: Call params (servers/endpoints)
        all_text = json.dumps(parsed) + str(call_obj)
        for ip in IPV4_RE.findall(all_text):
            if not ip.startswith("0.") and not ip.startswith("127.") and not ip.startswith("192.168.") and not ip.startswith("10."):
                ips.add(ip)

        # Source 2: Explicit fields in params
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
        for ice in parsed.get("ice_servers", []):
            if isinstance(ice, dict):
                ip = ice.get("ip") or ice.get("host") or ice.get("url")
                if ip and isinstance(ip, str):
                    # Clean IP from URL if needed
                    match = IPV4_RE.search(ip)
                    if match:
                        ips.add(match.group(0))

        # Source 3: 🔥 PARTICIPANTS LIST (MOST IMPORTANT)
        participants = getattr(group_call, 'participants', [])
        for p in participants:
            for attr_name in ['video', 'audio']:
                stream = getattr(p, attr_name, None)
                if stream:
                    # Check if stream has endpoint attribute
                    endpoint = getattr(stream, 'endpoint', None)
                    if endpoint:
                        # Endpoint is usually "ip:port"
                        if isinstance(endpoint, str):
                            match = IPV4_RE.search(endpoint)
                            if match:
                                ips.add(match.group(0))
                        elif isinstance(endpoint, dict):
                            ip = endpoint.get('ip') or endpoint.get('host')
                            if ip:
                                ips.add(ip)
                    # Also check source_groups for IPs
                    source_groups = getattr(stream, 'source_groups', [])
                    for sg in source_groups:
                        # Source groups often have ip and port
                        if isinstance(sg, dict):
                            ip = sg.get('ip')
                            if ip:
                                ips.add(ip)

        # Leave VC
        try:
            await user.invoke(
                functions.phone.LeaveGroupCall(
                    call=types.InputGroupCall(
                        id=call.id,
                        access_hash=call.access_hash
                    ),
                    source=0
                )
            )
        except:
            pass

        # DEBUG: Return raw data if debug mode
        if debug:
            debug_text = f"🔍 **DEBUG RAW DATA:**\n\n`{data_str[:1500]}`\n\nPlus participant endpoints extracted."
            return debug_text, None

        if not ips:
            return None, "❌ No public IPs found. Try `/debug <group_id>` to see raw data."

        result = "🌐 **Extracted IPs:**\n"
        for ip in list(ips)[:15]:
            result += f"`{ip}` : `10001`\n"
        return result, None

    except Exception as e:
        LOGGER.error(f"Fatal: {e}")
        return None, f"Fatal error: {e}"

# ---------- COMMANDS ----------
@app.on_message(filters.command("start"))
async def start_cmd(client, message):
    await message.reply(
        "👋 **VC IP Grabber Bot**\n\n"
        "/setsession <session> - Set user session\n"
        "/grab <group_id or link> - Extract IPs\n"
        "/debug <group_id or link> - Show raw VC data\n"
        "/groups - List groups\n"
        "/ping - Check if bot is alive"
    )

@app.on_message(filters.command("ping"))
async def ping_cmd(client, message):
    await message.reply("🏓 Pong!")

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
        await message.reply("❌ User session not set.")
        return
    status = await message.reply("⏳ Fetching groups...")
    groups = await list_groups_raw(user)
    if not groups:
        await status.edit_text("❌ No groups found.")
        return
    text = "📋 **Groups:**\n\n"
    for g in groups[:30]:
        text += f"🔹 `{g['id']}` – {g['title']}\n"
    await status.edit_text(text)

@app.on_message(filters.command("grab"))
async def grab_cmd(client, message):
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply("Usage: /grab <group_id> or /grab <invite_link>")
        return
    target = parts[1]
    status_msg = await message.reply("⏳ Joining VC & extracting IPs...")

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

    result, error = await extract_ips_from_vc(chat_id, link, debug=False)
    if error:
        await status_msg.edit_text(error)
    else:
        await status_msg.edit_text(result)

@app.on_message(filters.command("debug"))
async def debug_cmd(client, message):
    parts = message.text.split()
    if len(parts) < 2:
        await message.reply("Usage: /debug <group_id or link>")
        return
    target = parts[1]
    status_msg = await message.reply("⏳ Fetching raw data...")

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

    result, error = await extract_ips_from_vc(chat_id, link, debug=True)
    if error:
        await status_msg.edit_text(error)
    else:
        await status_msg.edit_text(result[:4000])  # Telegram limit

if __name__ == "__main__":
    LOGGER.info("🚀 Bot is starting...")
    app.run()
