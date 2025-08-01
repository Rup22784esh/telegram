
import os
import json
import time
import asyncio
import logging
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
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
from telethon.errors.rpcbaseerrors import RPCError
from telethon.tl.functions.channels import InviteToChannelRequest, JoinChannelRequest

# --- Configuration ---
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
SESSION_DIR = "sessions"
LOGS_DIR = "logs"

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

async def add_members_task(phone, source, target):
    log(phone, f"Starting session: {source} -> {target}")
    client = TelegramClient(f"{SESSION_DIR}/{phone}", int(API_ID), API_HASH)
    
    try:
        await client.connect()
        if not await client.is_user_authorized():
            update_status(phone, "Error: Session is not authorized. Please re-add.")
            log(phone, "Session is not authorized.")
            return

@app.get("/", response_class=HTMLResponse)
async def homepage(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/add_session")
async def add_session_route(request: Request, phone: str = Form(...), source: str = Form(...), target: str = Form(...)):
    phone = phone.strip()
    
    # Check if session file exists, which implies it's authorized
    if os.path.exists(f"{SESSION_DIR}/{phone}.session"):
        SESSIONS[phone] = {
            "phone": phone, "source": source, "target": target,
            "status": "Ready", "added": 0, "skipped": 0
        }
        log(phone, "Authorized session found. Starting worker.")
        asyncio.create_task(add_members_task(phone, source, target))
        return RedirectResponse(url="/", status_code=303)

    # If no session file, begin authorization flow
    client = TelegramClient(f"{SESSION_DIR}/{phone}", int(API_ID), API_HASH)
    await client.connect()

    try:
        phone_code_hash = await client.send_code_request(phone)
        SESSIONS[phone] = {
            "phone": phone, "source": source, "target": target,
            "client": client, "phone_code_hash": phone_code_hash.phone_code_hash,
            "status": "Awaiting OTP", "added": 0, "skipped": 0
        }
        log(phone, "New session. Sent OTP code.")
        return templates.TemplateResponse("otp.html", {"request": request, "phone": phone})
    except Exception as e:
        log(phone, f"Failed to send OTP: {e}")
        await client.disconnect()
        return HTMLResponse(f"Error initializing session: {e}", status_code=500)

@app.post("/verify_otp")
async def verify_otp_route(request: Request, phone: str = Form(...), code: str = Form(...), password: str = Form(None)):
    session_data = SESSIONS.get(phone)
    if not session_data or "client" not in session_data:
        return HTMLResponse("Session not found or already active.", status_code=400)
    
    client = session_data["client"]
    try:
        if password:
            await client.sign_in(password=password)
        else:
            await client.sign_in(phone=phone, code=code, phone_code_hash=session_data["phone_code_hash"])
        
        await client.disconnect()
        session_data.pop("client", None)
        session_data.pop("phone_code_hash", None)
        session_data["status"] = "Ready"
        asyncio.create_task(add_members_task(phone, session_data["source"], session_data["target"]))
        return RedirectResponse(url="/", status_code=303)

    except SessionPasswordNeededError:
        return templates.TemplateResponse("password.html", {"request": request, "phone": phone})
    except Exception as e:
        return HTMLResponse(f"Error: {e}", status_code=400)

@app.get("/api/sessions")
async def api_sessions():
    # Exclude client object from the response
    return JSONResponse({"sessions": {p: {k: v for k, v in d.items() if k != 'client'} for p, d in SESSIONS.items()}})

@app.get("/api/logs/{phone}")
async def get_logs(phone: str):
    logs = SESSION_LOGS.get(phone, [])
    return JSONResponse({"logs": logs})

@app.post("/restart_session")
async def restart_session_route(request: Request, phone: str = Form(...)):
    if phone not in SESSIONS:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    
    s = SESSIONS[phone]
    log(phone, "Restarting session manually.")
    asyncio.create_task(add_members_task(phone, s["source"], s["target"]))
    return JSONResponse({"success": True})
