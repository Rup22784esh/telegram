import os
import glob
import asyncio
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, FloodWaitError, UserPrivacyRestrictedError, UserNotMutualContactError
from telethon.tl.functions.channels import JoinChannelRequest, InviteToChannelRequest

# --- Configuration ---
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
SESSION_DIR = "sessions"

if not API_ID or not API_HASH:
    raise ValueError("API_ID and API_HASH must be set in environment variables.")

# --- FastAPI Setup ---
app = FastAPI()
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

# --- In-Memory Session & Task Storage ---
SESSIONS = {}

# --- Core Logic & Helpers ---

def clear_session_files():
    """Deletes all .session files to prevent db lock errors on startup."""
    if not os.path.exists(SESSION_DIR):
        os.makedirs(SESSION_DIR)
    pattern = os.path.join(SESSION_DIR, "*.session*")
    for session_file in glob.glob(pattern):
        try:
            os.remove(session_file)
            print(f"Deleted stale session file: {session_file}")
        except Exception as e:
            print(f"Failed to delete {session_file}: {e}")

def update_status(phone, message):
    """Updates the status message for a given session."""
    if phone in SESSIONS:
        SESSIONS[phone]["status"] = message
    print(f"[{phone}] {message}")

async def member_adder_worker(phone: str, source_group: str, target_group: str):
    """The core background task for scraping and adding members."""
    session_file = f"{SESSION_DIR}/{phone}.session"
    client = TelegramClient(session_file, int(API_ID), API_HASH)

    try:
        update_status(phone, "Connecting...")
        await client.connect()

        if not await client.is_user_authorized():
            update_status(phone, "Error: Session is invalid. Please re-add.")
            return

        update_status(phone, "Joining groups...")
        await client(JoinChannelRequest(source_group))
        await client(JoinChannelRequest(target_group))

        update_status(phone, "Fetching members...")
        source_members = await client.get_participants(source_group, limit=None)
        target_members = await client.get_participants(target_group, limit=None)
        target_member_ids = {user.id for user in target_members}

        valid_members_to_add = [
            user for user in source_members
            if user.id not in target_member_ids and not user.bot and user.username
        ]

        if not valid_members_to_add:
            update_status(phone, "Completed: No new members to add.")
            return

        total = len(valid_members_to_add)
        for i, user in enumerate(valid_members_to_add):
            update_status(phone, f"Adding: {i+1}/{total} ({user.username})")
            try:
                await client(InviteToChannelRequest(target_group, [user]))
                await asyncio.sleep(10)
            except FloodWaitError as e:
                update_status(phone, f"Flood Wait: Paused for {e.seconds}s")
                await asyncio.sleep(e.seconds)
            except (UserPrivacyRestrictedError, UserNotMutualContactError):
                update_status(phone, f"Skipped: {user.username} (Privacy)")
                await asyncio.sleep(5)
            except Exception as e:
                update_status(phone, f"Error on {user.username}: {e}")
                await asyncio.sleep(10)

        update_status(phone, f"Completed: Processed {total} members.")

    except Exception as e:
        update_status(phone, f"Critical Error: {e}")
    finally:
        if client.is_connected():
            await client.disconnect()
            update_status(phone, "Disconnected.")

# --- FastAPI Routes ---
@app.on_event("startup")
async def on_startup():
    """Clear old session files when the application starts."""
    clear_session_files()
    print("✅ Application startup complete. All stale sessions cleared.")

@app.get("/", response_class=HTMLResponse)
async def get_dashboard(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "sessions": SESSIONS})

@app.post("/add_session")
async def add_session(request: Request):
    form = await request.form()
    phone, source, target = form.get("phone"), form.get("source"), form.get("target")

    if not all([phone, source, target]):
        raise HTTPException(400, "All fields are required.")

    SESSIONS[phone] = {"phone": phone, "source": source, "target": target, "status": "Initializing...", "task": None}
    
    client = TelegramClient(f"{SESSION_DIR}/{phone}.session", int(API_ID), API_HASH)
    try:
        await client.connect()
        if await client.is_user_authorized():
            update_status(phone, "Session already verified. Starting worker...")
            task = asyncio.create_task(member_adder_worker(phone, source, target))
            SESSIONS[phone]["task"] = task
            return RedirectResponse(url="/", status_code=303)
        else:
            update_status(phone, "Sending OTP...")
            await client.send_code_request(phone)
            return templates.TemplateResponse("otp.html", {"request": request, "phone": phone})
    finally:
        if client.is_connected():
            await client.disconnect()

@app.post("/verify_otp")
async def verify_otp(request: Request):
    form = await request.form()
    phone, code, password = form.get("phone"), form.get("code"), form.get("password")

    if phone not in SESSIONS:
        raise HTTPException(404, "Session not found. Please start over.")

    client = TelegramClient(f"{SESSION_DIR}/{phone}.session", int(API_ID), API_HASH)
    try:
        await client.connect()
        if password:
            await client.sign_in(password=password)
        else:
            await client.sign_in(phone, code)
    except SessionPasswordNeededError:
        update_status(phone, "2FA Password Needed")
        return templates.TemplateResponse("password.html", {"request": request, "phone": phone})
    except Exception as e:
        update_status(phone, f"Verification Error: {e}")
        SESSIONS.pop(phone, None)
        return RedirectResponse(url="/", status_code=303)
    finally:
        if client.is_connected():
            await client.disconnect()

    # Login successful, start the background worker
    session_data = SESSIONS[phone]
    task = asyncio.create_task(member_adder_worker(phone, session_data["source"], session_data["target"]))
    SESSIONS[phone]["task"] = task
    return RedirectResponse(url="/", status_code=303)

@app.post("/restart_session")
async def restart_session(request: Request):
    form = await request.form()
    phone = form.get("phone")

    if phone not in SESSIONS:
        raise HTTPException(404, "Session not found.")

    if SESSIONS[phone].get("task") and not SESSIONS[phone]["task"].done():
        SESSIONS[phone]["task"].cancel()

    session_data = SESSIONS[phone]
    new_task = asyncio.create_task(member_adder_worker(phone, session_data["source"], session_data["target"]))
    SESSIONS[phone]["task"] = new_task
    update_status(phone, "Restarted manually.")
    return RedirectResponse(url="/", status_code=303)