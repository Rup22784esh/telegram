
import os
import glob
import json
import time
import asyncio
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, FloodWaitError, UserPrivacyRestrictedError, UserNotMutualContactError, UserChannelsTooMuchError
from telethon.tl.functions.channels import JoinChannelRequest, InviteToChannelRequest

# --- Configuration ---
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
SESSION_DIR = "sessions"
STATE_FILE = "sessions_state.json"

if not API_ID or not API_HASH:
    raise ValueError("API_ID and API_HASH must be set in environment variables.")

# --- FastAPI Setup ---
app = FastAPI()
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

# --- State Management ---
SESSIONS = {}
RUNNING_TASKS = {}

def save_state():
    with open(STATE_FILE, 'w') as f:
        json.dump({p: {k: v for k, v in d.items() if k != 'client'} for p, d in SESSIONS.items()}, f, indent=4)

def load_state():
    global SESSIONS
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            SESSIONS = json.load(f)

def update_status(phone, message, flood_wait_until=None):
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
        valid_members = [u for u in source_members if u.id not in target_ids and not u.bot and u.username]

        for i, user in enumerate(valid_members):
            try:
                update_status(phone, f"Adding {i+1}/{len(valid_members)}: {user.username}")
                await client(InviteToChannelRequest(target_group, [user]))
                await asyncio.sleep(15)
            except FloodWaitError as e:
                wait_time = e.seconds + 60
                flood_until = time.time() + wait_time
                update_status(phone, f"Flood Wait", flood_wait_until=flood_until)
                return
            except (UserPrivacyRestrictedError, UserNotMutualContactError, UserChannelsTooMuchError):
                await asyncio.sleep(5)
                continue
            except Exception as e:
                update_status(phone, f"Error: {e}")
                await asyncio.sleep(30)

        update_status(phone, "Completed: All members processed.")

    except Exception as e:
        update_status(phone, f"Critical Error: {e}")
    finally:
        if client.is_connected():
            await client.disconnect()
        RUNNING_TASKS.pop(phone, None)

# --- FastAPI Routes ---
@app.on_event("startup")
async def on_startup():
    load_state()
    print("✅ Application started. Loaded previous state.")
    await wake_up_sessions()

@app.get("/", response_class=HTMLResponse)
async def get_dashboard(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "sessions": SESSIONS, "time": time})

@app.post("/add_session")
async def add_session(request: Request):
    form = await request.form()
    phone, source, target = form.get("phone"), form.get("source"), form.get("target")

    client = TelegramClient(f"{SESSION_DIR}/{phone}.session", int(API_ID), API_HASH)
    await client.connect()

    SESSIONS[phone] = {"phone": phone, "source": source, "target": target, "status": "Authenticating", "client": client}
    
    if await client.is_user_authorized():
        await client.disconnect()
        SESSIONS[phone].pop("client", None)
        update_status(phone, "Ready to start.")
        task = asyncio.create_task(member_adder_worker(phone))
        RUNNING_TASKS[phone] = task
        return RedirectResponse(url="/", status_code=303)
    else:
        await client.send_code_request(phone)
        # This is the critical fix: return the OTP template here
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

@app.get("/wake")
async def wake_up_sessions():
    resumed_count = 0
    for phone, data in SESSIONS.items():
        if phone in RUNNING_TASKS and not RUNNING_TASKS[phone].done():
            continue

        should_run = False
        if 'flood_wait_until' in data and data['flood_wait_until']:
            if time.time() > data['flood_wait_until']:
                should_run = True
        elif data.get('status') == 'Ready to start':
            should_run = True

        if should_run:
            task = asyncio.create_task(member_adder_worker(phone))
            RUNNING_TASKS[phone] = task
            resumed_count += 1
            
    return {"status": "Woken up", "resumed_sessions": resumed_count}
