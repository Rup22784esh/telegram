from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import os
from pathlib import Path
from telethon import TelegramClient

app = FastAPI()
BASE_DIR = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")

session_storage = {}

@app.get("/", response_class=HTMLResponse)
async def homepage(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/add_session")
async def add_session(request: Request, phone_number: str = Form(...)):
    session_file = f"sessions/{phone_number}.session"
    client = TelegramClient(session_file, API_ID, API_HASH)

    await client.connect()

    try:
        if await client.is_user_authorized():
            session_storage[phone_number] = client
            return RedirectResponse(url="/", status_code=303)
        else:
            await client.send_code_request(phone_number)
            session_storage[phone_number] = client
            return templates.TemplateResponse("verify_otp.html", {"request": request, "phone_number": phone_number})
    except Exception as e:
        print(f"Error: {e}")
        return templates.TemplateResponse("index.html", {"request": request, "error": str(e)})

@app.post("/verify_otp")
async def verify_otp(request: Request, phone_number: str = Form(...), otp: str = Form(...)):
    client = session_storage.get(phone_number)
    if client is None:
        return templates.TemplateResponse("index.html", {"request": request, "error": "Session not found."})

    try:
        await client.sign_in(phone_number, otp)
        return RedirectResponse(url="/", status_code=303)
    except Exception as e:
        return templates.TemplateResponse("verify_otp.html", {
            "request": request,
            "phone_number": phone_number,
            "error": str(e)
        })

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=10000, reload=True, log_level="debug")
