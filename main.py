from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from telethon.sync import TelegramClient
from telethon.errors import SessionPasswordNeededError
import os

app = FastAPI()
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

# IMPORTANT: Set these in your Render environment variables
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
SESSION_DIR = "sessions"

if not os.path.exists(SESSION_DIR):
    os.makedirs(SESSION_DIR)

session_clients = {}

@app.get("/", response_class=HTMLResponse)
async def homepage(request: Request):
    sessions = []
    for filename in os.listdir(SESSION_DIR):
        if filename.endswith(".session"):
            phone = filename.replace(".session", "")
            status = "Running" if phone in session_clients else "Inactive"
            sessions.append({"phone": phone, "status": status})
    return templates.TemplateResponse("index.html", {"request": request, "sessions": sessions})

@app.post("/add_session")
async def add_session(request: Request, phone: str = Form(...)):
    client = TelegramClient(f"{SESSION_DIR}/{phone}", API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        try:
            await client.send_code_request(phone)
            session_clients[phone] = client
            return templates.TemplateResponse("otp.html", {"request": request, "phone": phone})
        except Exception as e:
            return templates.TemplateResponse("index.html", {"request": request, "error": str(e), "sessions": []})
    else:
        session_clients[phone] = client
        return RedirectResponse(url="/", status_code=303)

@app.post("/verify_otp")
async def verify_otp(request: Request, phone: str = Form(...), code: str = Form(...)):
    client = session_clients.get(phone)
    if not client:
        return RedirectResponse(url="/", status_code=303)
    try:
        await client.sign_in(phone, code)
    except SessionPasswordNeededError:
        return templates.TemplateResponse("password.html", {"request": request, "phone": phone})
    session_clients.pop(phone, None) # Remove from temp storage
    return RedirectResponse(url="/", status_code=303)

@app.post("/verify_password")
async def verify_password(request: Request, phone: str = Form(...), password: str = Form(...)):
    client = session_clients.get(phone)
    if not client:
        return RedirectResponse(url="/", status_code=303)
    await client.sign_in(password=password)
    session_clients.pop(phone, None) # Remove from temp storage
    return RedirectResponse(url="/", status_code=303)