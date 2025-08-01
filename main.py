import os
import glob
import json
import time
import asyncio
import logging
from datetime import datetime, timedelta
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from telethon import TelegramClient
from telethon.tl.types import UserStatusRecently, UserStatusLastWeek, UserStatusLastMonth
from telethon.errors import SessionPasswordNeededError, FloodWaitError, UserPrivacyRestrictedError, UserNotMutualContactError, UserChannelsTooMuchError, UsersTooMuchError, UserAlreadyParticipantError
from telethon.tl.functions.channels import JoinChannelRequest, InviteToChannelRequest

# --- Configuration ---
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
SESSION_DIR = "sessions"
LOGS_DIR = "logs"
STATE_FILE = "sessions_state.json"
AUTO_RESTART_DELAY = 60  # seconds

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
RUNNING_TASKS = {}

def get_logger(phone: str):
    logger = logging.getLogger(phone)
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        handler = logging.FileHandler(f"{LOGS_DIR}/{phone}.log")
        formatter = logging.Formatter('%(asctime)s - %(message)s')
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger

def save_state():
    with open(STATE_FILE, 'w') as f:
        json.dump({p: {k: v for k, v in d.items() if k != 'client'} for p, d in SESSIONS.items()}, f, indent=4)

def load_state():
    global SESSIONS
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            SESSIONS = json.load(f)

def update_status(phone, message, flood_wait_until=None):
    logger = get_logger(phone)
    logger.info(message)
    if phone in SESSIONS:
        SESSIONS[phone]['status'] = message
        SESSIONS[phone]['flood_wait_until'] = flood_wait_until
        save_state()
    print(f"[{phone}] {message}")

# --- Core Worker Logic ---
async def member_adder_worker(phone: str):
    session_data = SESSIONS.get(phone, {})
    source_group = session_data.get('source')
    target_group = session_data.get('target')
    last_seen_filter = session_data.get('last_seen_filter', 7) # Default to 7 days

    if not all([source_group, target_group]):
        update_status(phone, "Error: Source/Target not configured.")
        return

    client = TelegramClient(f"{SESSION_DIR}/{phone}.session", int(API_ID), API_HASH)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            update_status(phone, "Error: Session invalid.")
            return

        update_status(phone, "Starting process...")
        await client(JoinChannelRequest(source_group))
        await client(JoinChannelRequest(target_group))

        source_members = await client.get_participants(source_group, limit=None)
        target_members = await client.get_participants(target_group, limit=None)
        target_ids = {u.id for u in target_members}

        now = datetime.utcnow()
        recent_cutoff = now - timedelta(days=last_seen_filter)
        
        valid_members = []
        for u in source_members:
            if u.id in target_ids or u.bot:
                continue
            
            last_seen = u.status.was_online if hasattr(u.status, 'was_online') else None
            if last_seen and last_seen.replace(tzinfo=None) > recent_cutoff:
                 valid_members.append(u)

        update_status(phone, f"Found {len(valid_members)} recent & valid members to add.")
        
        added_count, skipped_count = 0, 0
        for i, user in enumerate(valid_members):
            username = user.username or f"id:{user.id}"
            try:
                update_status(phone, f"[{i+1}/{len(valid_members)}] Adding: {username}")
                await client(InviteToChannelRequest(target_group, [user]))
                added_count += 1
                SESSIONS[phone]['added'] = added_count
                await asyncio.sleep(1)
            except FloodWaitError as e:
                wait_time = e.seconds + 10
                flood_until = time.time() + wait_time
                update_status(phone, f"Flood Wait: sleeping for {wait_time}s", flood_wait_until=flood_until)
                return # Exit and let the cron job restart it
            except UserAlreadyParticipantError:
                skipped_count += 1
                SESSIONS[phone]['skipped'] = skipped_count
                update_status(phone, f"Skipped (already in group): {username}")
                continue
            except (UserPrivacyRestrictedError, UserNotMutualContactError, UserChannelsTooMuchError) as known:
                update_status(phone, f"Skipped: {type(known).__name__} for {username}")
                await asyncio.sleep(1)
                continue
            except UsersTooMuchError:
                update_status(phone, "Error: Target group is full or the account has joined too many channels.")
                return
            except Exception as e:
                update_status(phone, f"Unexpected Error on {username}: {e}")
                await asyncio.sleep(5)

        update_status(phone, f"Completed: {added_count} added, {skipped_count} skipped.")

    except Exception as e:
        update_status(phone, f"Critical Error: {e}")
    finally:
        if client.is_connected():
            await client.disconnect()
        RUNNING_TASKS.pop(phone, None)
        # Schedule auto-restart
        await asyncio.sleep(AUTO_RESTART_DELAY)
        if phone in SESSIONS: # Check if session still exists
             task = asyncio.create_task(member_adder_worker(phone))
             RUNNING_TASKS[phone] = task


# --- FastAPI Routes ---
@app.on_event("startup")
async def on_startup():
    load_state()
    print("✅ Application started. Loaded previous state.")
    # Start a background task to wake up sessions periodically
    asyncio.create_task(periodic_wake_up())

@app.get("/", response_class=HTMLResponse)
async def get_dashboard(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "sessions": SESSIONS, "time": time})

@app.post("/add_session")
async def add_session(request: Request):
    form = await request.form()
    phone, source, target = form.get("phone"), form.get("source"), form.get("target")
    last_seen_filter = int(form.get("last_seen_filter", 7))

    client = TelegramClient(f"{SESSION_DIR}/{phone}.session", int(API_ID), API_HASH)
    await client.connect()

    SESSIONS[phone] = {
        "phone": phone, 
        "source": source, 
        "target": target, 
        "status": "Authenticating", 
        "client": client,
        "last_seen_filter": last_seen_filter,
        "added": 0,
        "skipped": 0
    }
    
    if await client.is_user_authorized():
        await client.disconnect()
        SESSIONS[phone].pop("client", None)
        update_status(phone, "Ready to start.")
        task = asyncio.create_task(member_adder_worker(phone))
        RUNNING_TASKS[phone] = task
        return RedirectResponse(url="/", status_code=303)
    else:
        await client.send_code_request(phone)
        return templates.TemplateResponse("otp.html", {"request": request, "phone": phone})

@app.post("/verify_otp")
async def verify_otp(request: Request):
    form = await request.form()
    phone, code, password = form.get("phone"), form.get("code"), form.get("password")
    client = SESSIONS[phone].get("client")

    try:
        if password:
            await client.sign_in(password=password)
        else:
            await client.sign_in(phone, code)
    except SessionPasswordNeededError:
        return templates.TemplateResponse("password.html", {"request": request, "phone": phone})
    finally:
        await client.disconnect()
        SESSIONS[phone].pop("client", None)

    update_status(phone, "Ready to start.")
    task = asyncio.create_task(member_adder_worker(phone))
    RUNNING_TASKS[phone] = task
    return RedirectResponse(url="/", status_code=303)

@app.post("/restart_session")
async def restart_session(request: Request):
    form = await request.form()
    phone = form.get('phone')
    if phone in SESSIONS:
        if phone in RUNNING_TASKS and not RUNNING_TASKS[phone].done():
            RUNNING_TASKS[phone].cancel()
        
        SESSIONS[phone]['status'] = 'Restarting...'
        SESSIONS[phone]['added'] = 0
        SESSIONS[phone]['skipped'] = 0
        task = asyncio.create_task(member_adder_worker(phone))
        RUNNING_TASKS[phone] = task
    return RedirectResponse(url="/", status_code=303)

async def periodic_wake_up():
    while True:
        await asyncio.sleep(30) # Check every 30 seconds
        resumed_count = 0
        for phone, data in SESSIONS.items():
            if phone in RUNNING_TASKS and not RUNNING_TASKS[phone].done():
                continue

            should_run = False
            if 'flood_wait_until' in data and data['flood_wait_until']:
                if time.time() > data['flood_wait_until']:
                    should_run = True
            elif data.get('status') not in ["Starting process...", "Authenticating"]:
                 # Avoid restarting sessions that are actively running or waiting for OTP
                 should_run = True

            if should_run:
                task = asyncio.create_task(member_adder_worker(phone))
                RUNNING_TASKS[phone] = task
                resumed_count += 1
        if resumed_count > 0:
            print(f"Resumed {resumed_count} sessions.")
