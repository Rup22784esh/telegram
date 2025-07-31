from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import os, asyncio
from pathlib import Path
from telethon_worker import start_session, SESSION_STATUS

app = FastAPI()
BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    return templates.TemplateResponse("index.html", {"request": request, "sessions": SESSION_STATUS})

@app.post("/start")
async def start(request: Request, phone: str = Form(...)):
    asyncio.create_task(start_session(phone))
    return RedirectResponse("/", status_code=303)

@app.post("/verify")
async def verify(request: Request, phone: str = Form(...), code: str = Form(...)):
    SESSION_STATUS[phone] = {'status': 'VERIFYING', 'code': code}
    return RedirectResponse("/", status_code=303)

@app.get("/login/{phone}", response_class=HTMLResponse)
async def login(request: Request, phone: str):
    return templates.TemplateResponse("login.html", {"request": request, "phone": phone})
