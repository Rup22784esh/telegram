from telethon.sync import TelegramClient
from telethon.errors import SessionPasswordNeededError, FloodWaitError
from telethon.tl.functions.channels import GetParticipantsRequest, InviteToChannelRequest, JoinChannelRequest
from telethon.tl.types import ChannelParticipantsSearch, InputUser
import os, asyncio, datetime, json

api_id = 22519301
api_hash = '1a503c6dce6195a37e082a88f7e20dd5'

SESSION_STATUS = {}  # Keeps phone => status

async def start_session(phone):
    log_file = f"/data/data/com.termux/files/home/telegram/one/project/logs/{phone}.log"
    open(log_file, 'w').close()  # Clear old log
    session_file = f"/data/data/com.termux/files/home/telegram/one/project/sessions/{phone}"
    client = TelegramClient(session_file, api_id, api_hash)
    await client.connect()

    if not await client.is_user_authorized():
        await client.send_code_request(phone)
        SESSION_STATUS[phone] = {'status': 'OTP_SENT'}

        while 'code' not in SESSION_STATUS[phone]:
            await asyncio.sleep(1)

        try:
            await client.sign_in(phone, SESSION_STATUS[phone]['code'])
        except SessionPasswordNeededError:
            SESSION_STATUS[phone]['status'] = '2FA_NEEDED'
            return

    source = 'aashmanup'
    target = 'nepalsenoob'

    try:
        source_group = await client.get_entity(source)
    except:
        await client(JoinChannelRequest(source))
        source_group = await client.get_entity(source)

    try:
        target_group = await client.get_entity(target)
    except:
        await client(JoinChannelRequest(target))
        target_group = await client.get_entity(target)

    source_members = []
    offset = 0
    limit = 100
    while True:
        participants = await client(GetParticipantsRequest(
            source_group, ChannelParticipantsSearch(''), offset, limit, hash=0))
        if not participants.users:
            break
        source_members.extend(participants.users)
        offset += len(participants.users)

    target_ids = [u.id for u in (await client(GetParticipantsRequest(
        target_group, ChannelParticipantsSearch(''), 0, 10000, hash=0))).users]

    added = 0
    for i, member in enumerate(source_members):
        try:
            if member.bot or member.id in target_ids:
                continue
            user = InputUser(member.id, member.access_hash)
            await client(InviteToChannelRequest(target_group, [user]))
            SESSION_STATUS[phone] = {'status': f"[{i+1}/{len(source_members)}] Added {member.first_name}"}
            added += 1
        except FloodWaitError as e:
            SESSION_STATUS[phone] = {'status': f"Flood wait {e.seconds}s. Sleeping..."}
            await asyncio.sleep(e.seconds + 5)
        except Exception as e:
            SESSION_STATUS[phone] = {'status': f"Error: {str(e)}"}
            await asyncio.sleep(1)

    await client.disconnect()
    SESSION_STATUS[phone] = {'status': f"Completed. Total added: {added}"}
