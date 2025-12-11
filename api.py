from fastapi import FastAPI, HTTPException, Header, Depends, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional
import sqlite3
from contextlib import contextmanager
import os
from datetime import datetime
from dotenv import load_dotenv

app = FastAPI()
templates = Jinja2Templates(directory="templates")
load_dotenv()
DB_PATH = "./data.db"

API_TOKEN = os.getenv("API_TOKEN")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")

class RequestItem(BaseModel):
    school: str
    username: str
    status: str

class RequestResponse(BaseModel):
    id: int
    created_at: str
    school: str
    username: str
    status: str

class StatusUpdate(BaseModel):
    status: str  # "pending", "denied", "approved"

async def verify_token(x_api_key: str = Header(None)):
    if not x_api_key or x_api_key != API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid API Token")
    return x_api_key

async def verify_admin_token(x_admin_key: str = Header(None)):
    if not x_admin_key or x_admin_key != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid Admin Token")
    return x_admin_key

@contextmanager
def get_db():
    os.makedirs(os.path.dirname(DB_PATH) or '.', exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            school TEXT,
            username TEXT UNIQUE,
            status TEXT
        )
    """)
    try:
        yield conn
    finally:
        conn.close()

@app.get("/")
async def root():
    raise HTTPException(status_code=404, detail="Not Found")

@app.get("/requests/{username}", response_model=Optional[RequestResponse])
async def get_requests(username: str):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM requests WHERE username = ?",
            (username,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="None")
        return RequestResponse(**dict(row))

@app.post("/new_request/", status_code=201)
async def create_request(
    item: RequestItem,
    token: str = Depends(verify_token)
):
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO requests (created_at, school, username, status) VALUES (datetime('now'), ?, ?, ?)",
                (item.school, item.username, item.status)
            )
            conn.commit()
        return {"message": "Request created", "username": item.username}
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="Username already exists")

@app.patch("/update_request/{request_id}", response_model=dict)
async def update_status(
    request_id: int,
    item: StatusUpdate,
    admin_token: str = Depends(verify_admin_token)
):
    if item.status not in ["pending", "denied", "approved"]:
        raise HTTPException(status_code=400, detail="Status must be 'pending', 'denied' or 'approved'")
    
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM requests WHERE id = ?",
            (request_id,)
        ).fetchone()
        
        if not row:
            raise HTTPException(status_code=404, detail="Request not found")
        
        conn.execute(
            "UPDATE requests SET status = ? WHERE id = ?",
            (item.status, request_id)
        )
        conn.commit()
    
    return {"message": f"Request {request_id} updated to '{item.status}'"}

@app.get("/admin", response_class=HTMLResponse)
async def admin_landing(request: Request):
    return templates.TemplateResponse("admin_landing.html", {"request": request})

@app.get("/admin/{admin_token}", response_class=HTMLResponse)
async def admin_view(admin_token: str, request: Request):
    if admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid Admin Token")
    
    with get_db() as conn:
        pending_requests = conn.execute(
            "SELECT * FROM requests WHERE status = 'pending' ORDER BY created_at DESC"
        ).fetchall()
        pending_count = len(pending_requests)
    
    return templates.TemplateResponse("admin.html", {
        "request": request,
        "pending_requests": pending_requests,
        "pending_count": pending_count,
        "admin_token": admin_token  # Für die Form-Actions
    })

@app.post("/admin/{admin_token}/{request_id}/allow")
async def admin_allow(admin_token: str, request_id: int):
    if admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401)
    
    with get_db() as conn:
        conn.execute("UPDATE requests SET status = 'approved' WHERE id = ?", (request_id,))
        conn.commit()
    
    return RedirectResponse(url=f"/admin/{admin_token}", status_code=303)

@app.post("/admin/{admin_token}/{request_id}/deny")
async def admin_deny(admin_token: str, request_id: int):
    if admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401)
    
    with get_db() as conn:
        conn.execute("UPDATE requests SET status = 'denied' WHERE id = ?", (request_id,))
        conn.commit()
    
    return RedirectResponse(url=f"/admin/{admin_token}", status_code=303)