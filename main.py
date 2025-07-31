
import os
import asyncio
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError, FloodWaitError, UserPrivacyRestrictedError, UserNotMutualContactError
from telethon.tl.functions.channels import JoinChannelRequest, InviteToChannelRequest

# --- Configuration ---
# IMPORTANT: Set API_ID and API_HASH in your Render Environment Variables
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
SESSION_DIR = "sessions"

if not API_ID or not API_HASH:
    raise ValueError("API_ID and API_HASH must be set in environment variables.")

if not os.path.exists(SESSION_DIR):
    os.makedirs(SESSION_DIR)

# --- FastAPI Setup ---
app = FastAPI()
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

# --- In-Memory Session & Task Storage ---
# This dictionary will hold the state of all running sessions
SESSIONS = {}

# --- Helper Functions ---
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
            update_status(phone, "Error: Session expired or invalid. Please re-add.")
            return

        # 1. Join channels
        update_status(phone, "Joining groups...")
        await client(JoinChannelRequest(source_group))
        await client(JoinChannelRequest(target_group))

        # 2. Scrape members from source
        update_status(phone, "Fetching members from source...")
        source_members = await client.get_participants(source_group)

        # 3. Filter out existing members from target
        update_status(phone, "Filtering existing members...")
        target_members = await client.get_participants(target_group)
        target_member_ids = {user.id for user in target_members}

        valid_members_to_add = [
            user for user in source_members
            if user.id not in target_member_ids and not user.bot and user.username
        ]

        if not valid_members_to_add:
            update_status(phone, "Completed: No new members to add.")
            return

        # 4. Start adding members
        total = len(valid_members_to_add)
        for i, user in enumerate(valid_members_to_add):
            update_status(phone, f"Adding: {i+1}/{total} ({user.username})")
            try:
                await client(InviteToChannelRequest(target_group, [user]))
                await asyncio.sleep(10) # Add a small delay to be safe
            except FloodWaitError as e:
                update_status(phone, f"Flood Wait: Paused for {e.seconds}s")
                await asyncio.sleep(e.seconds)
            except (UserPrivacyRestrictedError, UserNotMutualContactError):
                update_status(phone, f"Skipped: {user.username} (Privacy)")
                await asyncio.sleep(5)
            except Exception as e:
                update_status(phone, f"Error adding {user.username}: {e}")
                await asyncio.sleep(10)

        update_status(phone, f"Completed: Successfully processed {total} members.")

    except Exception as e:
        update_status(phone, f"Critical Error: {e}")
    finally:
        if client.is_connected():
            await client.disconnect()

# --- FastAPI Routes ---
@app.get("/", response_class=HTMLResponse)
async def get_dashboard(request: Request):
    """Renders the main dashboard UI."""
    return templates.TemplateResponse("index.html", {"request": request, "sessions": SESSIONS})

@app.post("/add_session")
async def add_session(request: Request):
    """Handles the form submission to add a new account."""
    form = await request.form()
    phone = form.get("phone")
    source = form.get("source")
    target = form.get("target")

    if not all([phone, source, target]):
        raise HTTPException(400, "Phone, source, and target are required.")

    session_file = f"{SESSION_DIR}/{phone}.session"
    client = TelegramClient(session_file, int(API_ID), API_HASH)
    await client.connect()

    SESSIONS[phone] = {
        "phone": phone,
        "source": source,
        "target": target,
        "status": "waiting_for_otp",
        "client": client, # Store client temporarily
        "task": None
    }

    if not await client.is_user_authorized():
        update_status(phone, "Sending OTP...")
        await client.send_code_request(phone)
        return templates.TemplateResponse("otp.html", {"request": request, "phone": phone})
    else:
        # Already authorized, start worker immediately
        task = asyncio.create_task(member_adder_worker(phone, source, target))
        SESSIONS[phone]["task"] = task
        SESSIONS[phone].pop("client") # Don't store client object long-term
        return RedirectResponse(url="/", status_code=303)


@app.post("/verify_otp")
async def verify_otp(request: Request):
    """Handles OTP and 2FA verification."""
    form = await request.form()
    phone = form.get("phone")
    code = form.get("code")
    password = form.get("password")

    if phone not in SESSIONS:
        raise HTTPException(404, "Session not found. Please start over.")

    session_data = SESSIONS[phone]
    client = session_data["client"]
    
    try:
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

    # Login successful, start the background worker
    task = asyncio.create_task(member_adder_worker(phone, session_data["source"], session_data["target"]))
    SESSIONS[phone]["task"] = task
    SESSIONS[phone].pop("client") # Clean up temporary client object
    
    return RedirectResponse(url="/", status_code=303)

@app.post("/restart_session")
async def restart_session(request: Request):
    """Stops the existing task and restarts the worker for a session."""
    form = await request.form()
    phone = form.get("phone")

    if phone not in SESSIONS:
        raise HTTPException(404, "Session not found.")

    # Cancel the old task if it exists and is running
    if SESSIONS[phone].get("task") and not SESSIONS[phone]["task"].done():
        SESSIONS[phone]["task"].cancel()

    # Start a new worker task
    session_data = SESSIONS[phone]
    new_task = asyncio.create_task(member_adder_worker(phone, session_data["source"], session_data["target"]))
    SESSIONS[phone]["task"] = new_task
    
    update_status(phone, "Restarted manually.")
    return RedirectResponse(url="/", status_code=303)
