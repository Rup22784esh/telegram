import os
import json
import time
import asyncio
import logging
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError,
    UserPrivacyRestrictedError,
    UserAlreadyParticipantError,
    UsersTooMuchError,
    UserChannelsTooMuchError,
    SessionPasswordNeededError
)
from telethon.errors.rpcerrorlist import RpcError
from telethon.tl.functions.channels import InviteToChannelRequest, JoinChannelRequest

# --- Configuration ---
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
SESSION_DIR = "sessions"
LOGS_DIR = "logs"
STATE_FILE = "sessions_state.json"

if not API_ID or not API_HASH:
    raise ValueError("API_ID and API_HASH must be set in environment variables.")

os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(SESSION_DIR, exist_ok=True)

# --- FastAPI Setup ---
app = FastAPI()
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

# --- State & Logging ---
SESSIONS = {}
SESSION_LOGS = {}

def log(session_phone, message):
    timestamp = time.strftime("%H:%M:%S")
    log_line = f"[{timestamp}] {message}"
    print(f"[{session_phone}] {log_line}")
    SESSION_LOGS.setdefault(session_phone, []).append(log_line)

def update_status(phone, message, flood_wait_until=None, added=None, skipped=None):
    data = SESSIONS.get(phone)
    if data:
        data['status'] = message
        if flood_wait_until is not None:
            data['flood_wait_until'] = flood_wait_until
        if added is not None:
            data['added'] = added
        if skipped is not None:
            data['skipped'] = skipped

async def add_members_task(phone, source, target, last_seen_filter):
    log(phone, f"Starting session: {source} -> {target}")
    client = TelegramClient(f"{SESSION_DIR}/{phone}", int(API_ID), API_HASH)
    
    try:
        await client.start(phone=phone)
    except SessionPasswordNeededError:
        update_status(phone, "Error: 2FA Password Needed. Please re-add session.")
        log(phone, "SessionPasswordNeededError - manual intervention needed")
        return
    except Exception as e:
        update_status(phone, f"Error: {e}")
        log(phone, f"Fatal connection error: {e}")
        return

    SESSIONS[phone].update({"client": client})
    
    try:
        log(phone, "Joining source and target channels...")
        await client(JoinChannelRequest(source))
        await client(JoinChannelRequest(target))

        log(phone, "Fetching source group members...")
        all_members = await client.get_participants(source, limit=None)
        target_members = await client.get_participants(target, limit=None)
        target_ids = {u.id for u in target_members}

        log(phone, f"Found {len(all_members)} total members. Filtering...")
        
        valid_members = [
            m for m in all_members 
            if m.id not in target_ids and not m.bot
        ]
        
        log(phone, f"Total valid members found: {len(valid_members)}")
        added_count = SESSIONS[phone].get('added', 0)
        skipped_count = SESSIONS[phone].get('skipped', 0)

        for idx, user in enumerate(valid_members):
            username = getattr(user, 'username', user.id)
            update_status(phone, f"Adding {idx+1}/{len(valid_members)}: {username}", added=added_count, skipped=skipped_count)
            
            try:
                await asyncio.sleep(1) # Gentle delay
                await client(InviteToChannelRequest(channel=target, users=[user]))
                added_count += 1
                log(phone, f"Successfully added {username}")

            except FloodWaitError as e:
                wait_time = e.seconds + 15
                flood_until = time.time() + wait_time
                update_status(phone, f"FloodWait: waiting {wait_time}s", flood_wait_until=flood_until)
                log(phone, f"FloodWaitError for {wait_time} seconds. Pausing session.")
                await asyncio.sleep(wait_time)
                log(phone, "Resuming after flood wait.")
                continue

            except (UserPrivacyRestrictedError, UserAlreadyParticipantError) as e:
                skipped_count += 1
                log(phone, f"{type(e).__name__} - skipping user {username}")
                await asyncio.sleep(1)
                continue

            except (UsersTooMuchError, UserChannelsTooMuchError):
                update_status(phone, "Error: Account group/channel limit reached.")
                log(phone, "UsersTooMuchError or UserChannelsTooMuchError - stopping session.")
                break

            except RpcError as e:
                err_str = str(e).lower()
                if "maximum number of users" in err_str:
                    update_status(phone, "Error: Target group member limit reached.")
                    log(phone, "InviteToChannelRequestError - group member limit reached")
                    break
                else:
                    log(phone, f"RPC error: {e}")
                    await asyncio.sleep(10)
            
            except Exception as e:
                skipped_count += 1
                log(phone, f"Unexpected error for user {username}: {str(e)}")
                await asyncio.sleep(5)

        update_status(phone, "Finished", added=added_count, skipped=skipped_count)
        log(phone, f"Session finished. Added: {added_count}, Skipped: {skipped_count}")

    except Exception as e:
        error_message = f"Fatal error: {e}"
        update_status(phone, error_message)
        log(phone, error_message)
    finally:
        if client.is_connected():
            await client.disconnect()
        SESSIONS[phone].pop("client", None)

@app.get("/", response_class=HTMLResponse)
async def homepage(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/add_session")
async def add_session_route(request: Request, phone: str = Form(...), source: str = Form(...), target: str = Form(...), last_seen_filter: int = Form(7)):
    phone = phone.strip()
    if phone in SESSIONS and "client" in SESSIONS[phone]:
        return JSONResponse({"error": "Session already running"}, status_code=400)
    
    SESSION_LOGS[phone] = []
    SESSIONS[phone] = {
        "phone": phone, "source": source, "target": target,
        "last_seen_filter": last_seen_filter, "status": "Initializing...",
        "added": 0, "skipped": 0, "flood_wait_until": None
    }
    
    asyncio.create_task(add_members_task(phone, source, target, last_seen_filter))
    return JSONResponse({"success": True})

@app.get("/api/sessions")
async def api_sessions():
    return JSONResponse({"sessions": SESSIONS})

@app.get("/api/logs/{phone}")
async def get_logs(phone: str):
    logs = SESSION_LOGS.get(phone, [])
    return JSONResponse({"logs": logs})

@app.post("/restart_session")
async def restart_session_route(request: Request, phone: str = Form(...)):
    if phone not in SESSIONS:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    
    s = SESSIONS[phone]
    if "client" in s and s["client"].is_connected():
        await s["client"].disconnect()

    log(phone, "Restarting session manually.")
    asyncio.create_task(add_members_task(phone, s["source"], s["target"], s["last_seen_filter"]))
    return JSONResponse({"success": True})