import json
from pathlib import Path
import re
from fastapi import FastAPI, HTTPException, Header, Depends, Request, WebSocket, WebSocketDisconnect, Form
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse, HTMLResponse
from pydantic import BaseModel
from typing import Optional
import sqlite3
from contextlib import contextmanager
import os
from datetime import datetime
from dotenv import load_dotenv
from collections import defaultdict
import asyncio

app = FastAPI()
templates = Jinja2Templates(directory="templates")
load_dotenv()
DB_PATH = "./data.db"

API_TOKEN = os.getenv("API_TOKEN")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN")

# WebSocket Manager
class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, list[WebSocket]] = defaultdict(list)
    
    async def connect(self, websocket: WebSocket, school: str, token: str):
        await websocket.accept()
        if token != API_TOKEN:
            await websocket.close(code=1008)
            return
        self.active_connections[school].append(websocket)
    
    def disconnect(self, websocket: WebSocket, school: str):
        if school in self.active_connections:
            self.active_connections[school] = [
                conn for conn in self.active_connections[school] 
                if conn != websocket
            ]
    
    async def broadcast(self, school: str, event: dict):
        if school not in self.active_connections:
            return
        disconnected = []
        for connection in self.active_connections[school][:]:
            try:
                await connection.send_json(event)
            except:
                disconnected.append(connection)
        for conn in disconnected:
            self.disconnect(conn, school)

manager = ConnectionManager()

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
    status: str

class ContactItem(BaseModel):
    school: str
    username: str
    contact_infos: str

class ContactResponse(BaseModel):
    id: int
    created_at: str
    school: str
    username: str
    contact_infos: str

class ContactLookup(BaseModel):
    school: str
    username: str

class MessageItem(BaseModel):
    school: str
    username: str
    message: str

class GetMessagesRequest(BaseModel):
    school: str

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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS contacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            school TEXT,
            username TEXT UNIQUE,
            contact_infos TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sent_at TEXT,
            school TEXT,
            username TEXT,
            message TEXT,
            deleted BOOLEAN DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_bans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            school TEXT,
            username TEXT,
            active BOOLEAN,
            UNIQUE(school, username)
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

@app.post("/new_contact/", status_code=201)
async def create_contact(
    item: ContactItem,
    token: str = Depends(verify_token)
):
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO contacts (created_at, school, username, contact_infos) VALUES (datetime('now'), ?, ?, ?)",
                (item.school, item.username, item.contact_infos)
            )
            conn.commit()
        return {"message": "Contact created", "username": item.username}
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=409, detail="Username already exists")

@app.post("/get_contact/", response_model=Optional[ContactResponse])
async def get_contact(
    item: ContactLookup,
    token: str = Depends(verify_token)
):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM contacts WHERE username = ? AND school = ?",
            (item.username, item.school)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Contact not found")
        return ContactResponse(**dict(row))
    
# 🔥 WORTFILTER aus JSON laden
BADWORDS_PATH = Path("badwords.json")
if BADWORDS_PATH.exists():
    with open(BADWORDS_PATH, 'r', encoding='utf-8') as f:
        badwords_config = json.load(f)
        BAD_WORDS = badwords_config["words"]
else:
    # Fallback
    BAD_WORDS = [r'\b(fick|scheiße|arsch)\b']

WORD_FILTER_PATTERN = re.compile('|'.join(BAD_WORDS), re.IGNORECASE)

def check_word_filter(message: str) -> bool:
    """Prüft ob Nachricht verbotene Wörter enthält"""
    return bool(WORD_FILTER_PATTERN.search(message))

@app.post("/send_message/", status_code=201)
async def send_message(
    item: MessageItem,
    token: str = Depends(verify_token)
):
    # WORTFILTER CHECK (NEU!)
    if check_word_filter(item.message):
        raise HTTPException(
            status_code=403, 
            detail="Nachricht enthält unangemessene Inhalte. Bitte drücke dich anders aus."
        )
    
    with get_db() as conn:
        # Prüfe approved
        row = conn.execute(
            "SELECT * FROM requests WHERE school = ? AND username = ? AND status = 'approved'",
            (item.school, item.username)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=403, detail="User not approved")
        
        # Prüfe ban
        ban_row = conn.execute(
            "SELECT * FROM chat_bans WHERE school = ? AND username = ? AND active = 1",
            (item.school, item.username)
        ).fetchone()
        if ban_row:
            raise HTTPException(status_code=403, detail="Du wurdest von der Chat-Funktion ausgeschlossen")
        
        # Längenprüfung
        if len(item.message.strip()) < 2 or len(item.message.strip()) > 500:
            raise HTTPException(status_code=400, detail="Nachricht muss 2-500 Zeichen haben")
        
        # FIX: Cursor verwenden!
        cursor = conn.execute(
            "INSERT INTO messages (sent_at, school, username, message, deleted) VALUES (datetime('now'), ?, ?, ?, 0)",
            (item.school, item.username, item.message.strip())
        )
        new_id = cursor.lastrowid
        conn.commit()
    
    # Broadcast
    new_msg = {
        "type": "message_new",
        "id": new_id,
        "sent_at": datetime.now().isoformat(),
        "username": item.username,
        "message": item.message.strip(),
        "deleted": False
    }
    asyncio.create_task(manager.broadcast(item.school, new_msg))
    
    return {"message": "Message sent"}

@app.post("/get_messages/")
async def get_messages(
    item: GetMessagesRequest,
    x_api_key: str = Header(alias="X-API-Key")
):
    if not x_api_key or x_api_key != API_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid API Token")
    
    with get_db() as conn:
        messages = conn.execute(
            "SELECT id, sent_at, username, message, deleted FROM messages WHERE school = ? ORDER BY sent_at DESC LIMIT 100",
            (item.school,)
        ).fetchall()
        
        return [
            {
                "id": dict(m)["id"], 
                "sent_at": dict(m)["sent_at"], 
                "username": dict(m)["username"], 
                "message": "" if dict(m)["deleted"] else dict(m)["message"],
                "deleted": dict(m)["deleted"]
            } 
            for m in messages
        ]

@app.post("/get_bans/")
async def get_bans(
    token: str = Depends(verify_token)
):
    with get_db() as conn:
        bans = conn.execute(
            "SELECT * FROM chat_bans ORDER BY created_at DESC"
        ).fetchall()
        return [{"id": b["id"], "school": b["school"], "username": b["username"], "active": b["active"]} for b in bans]

@app.post("/ban/{username}")
async def check_ban(
    username: str,
    item: ContactLookup,
    token: str = Depends(verify_token)
):
    with get_db() as conn:
        row = conn.execute(
            "SELECT active FROM chat_bans WHERE school = ? AND username = ?",
            (item.school, username)
        ).fetchone()
        if row:
            return {"banned": True, "active": row["active"]}
        return {"banned": False}

@app.websocket("/ws/{school}")
async def websocket_endpoint(websocket: WebSocket, school: str):
    token = websocket.query_params.get("token")
    await manager.connect(websocket, school, token)
    
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket, school)

def require_admin(request: Request):
    session = request.cookies.get("admin_session")
    if session != "1":
        raise HTTPException(status_code=401, detail="Not authenticated as admin")

@app.get("/admin", response_class=HTMLResponse)
async def admin_landing(request: Request):
    return templates.TemplateResponse(
        "admin_login.html",
        {"request": request, "error": None}
    )

@app.post("/admin/login", response_class=HTMLResponse)
async def admin_login(request: Request, token: str = Form(...)):
    if token != ADMIN_TOKEN:
        return templates.TemplateResponse(
            "admin_login.html",
            {"request": request, "error": "Invalid Admin Token"}
        )
    
    response = RedirectResponse(url="/admin/dashboard", status_code=303)
    response.set_cookie(
        key="admin_session",
        value="1",
        httponly=True,
        secure=False,
        samesite="lax",
    )
    return response

@app.get("/admin/dashboard", response_class=HTMLResponse)
async def admin_overview(request: Request):
    require_admin(request)
    with get_db() as conn:
        schools = conn.execute(
            "SELECT DISTINCT school FROM requests ORDER BY school"
        ).fetchall()
        pending_count = conn.execute(
            "SELECT COUNT(*) as count FROM requests WHERE status = 'pending'"
        ).fetchone()["count"]
        
    return templates.TemplateResponse(
        "admin_overview.html",
        {
            "request": request,
            "schools": [s["school"] for s in schools],
            "pending_count": pending_count
        }
    )

@app.get("/admin/requests", response_class=HTMLResponse)
async def admin_requests(request: Request):
    require_admin(request)
    with get_db() as conn:
        requests = conn.execute(
            "SELECT * FROM requests ORDER BY created_at DESC"
        ).fetchall()
    
    return templates.TemplateResponse(
        "admin_requests.html",
        {"request": request, "requests": requests}
    )

@app.post("/admin/requests/{request_id}/update")
async def admin_update_request(
    request: Request,
    request_id: int, 
    status: str = Form(...)
):
    require_admin(request)
    
    if status not in ["pending", "approved", "denied"]:
        raise HTTPException(status_code=400, detail="Invalid status")
    
    with get_db() as conn:
        row = conn.execute(
            "SELECT id FROM requests WHERE id = ?",
            (request_id,)
        ).fetchone()
        
        if not row:
            raise HTTPException(status_code=404, detail="Request not found")
        
        conn.execute(
            "UPDATE requests SET status = ? WHERE id = ?",
            (status, request_id)
        )
        conn.commit()
    
    return RedirectResponse(url="/admin/requests", status_code=303)

@app.get("/admin/chats/{school}", response_class=HTMLResponse)
async def admin_chat_view(school: str, request: Request):
    require_admin(request)
    with get_db() as conn:
        messages = [dict(msg) for msg in conn.execute("SELECT * FROM messages WHERE school = ? ORDER BY sent_at DESC LIMIT 500", (school,)).fetchall()]
    
    return templates.TemplateResponse(
        "admin_chat.html",
        {"request": request, "school": school, "messages": messages}
    )

@app.post("/admin/chats/{school}/system")
async def admin_system_message(school: str, request: Request, message: str = Form(...)):
    require_admin(request)
    
    if len(message.strip()) < 1 or len(message.strip()) > 200:
        raise HTTPException(status_code=400, detail="Nachricht muss 1-200 Zeichen haben")
    
    with get_db() as conn:
        cursor = conn.execute(
            "INSERT INTO messages (sent_at, school, username, message, deleted) VALUES (datetime('now'), ?, '[SYSTEM]', ?, 0)",
            (school, message.strip())
        )
        new_id = cursor.lastrowid
        conn.commit()
    
    # SYSTEM Broadcast an alle Clients!
    system_msg = {
        "type": "message_new",
        "id": new_id,
        "sent_at": datetime.now().isoformat(),
        "username": "[SYSTEM]",
        "message": message.strip(),
        "deleted": False
    }
    asyncio.create_task(manager.broadcast(school, system_msg))
    
    return RedirectResponse(url=f"/admin/chats/{school}", status_code=303)

@app.post("/admin/chats/{school}/{message_id}/delete")
async def admin_delete_message(school: str, message_id: int, request: Request):
    require_admin(request)
    with get_db() as conn:
        conn.execute(
            "UPDATE messages SET deleted = 1 WHERE id = ? AND school = ?",
            (message_id, school)
        )
        conn.commit()
    
    delete_event = {
        "type": "message_deleted",
        "id": message_id,
        "school": school
    }
    asyncio.create_task(manager.broadcast(school, delete_event))
    
    return RedirectResponse(url=f"/admin/chats/{school}", status_code=303)

@app.post("/admin/chats/{school}/{message_id}/restore")
async def admin_restore_message(school: str, message_id: int, request: Request):
    require_admin(request)
    with get_db() as conn:
        row = conn.execute(
            "SELECT username, message FROM messages WHERE id = ? AND school = ?",
            (message_id, school)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE messages SET deleted = 0 WHERE id = ? AND school = ?",
                (message_id, school)
            )
            conn.commit()
        
        restore_event = {
            "type": "message_restored",
            "id": message_id,
            "username": row["username"] if row else None,
            "message": row["message"] if row else None,
            "school": school
        }
        asyncio.create_task(manager.broadcast(school, restore_event))
    
    return RedirectResponse(url=f"/admin/chats/{school}", status_code=303)

@app.get("/admin/bans", response_class=HTMLResponse)
async def admin_bans(request: Request):
    require_admin(request)
    with get_db() as conn:
        bans = [dict(ban) for ban in conn.execute(
            "SELECT * FROM chat_bans ORDER BY created_at DESC"
        ).fetchall()]
        
        schools_raw = conn.execute(
            "SELECT DISTINCT school FROM requests ORDER BY school"
        ).fetchall()
        schools = [dict(school)['school'] for school in schools_raw]
    
    return templates.TemplateResponse(
        "admin_bans.html",
        {"request": request, "bans": bans, "schools": schools}
    )

@app.post("/admin/bans/{ban_id}/toggle")
async def admin_toggle_ban(ban_id: int, request: Request):
    require_admin(request)
    with get_db() as conn:
        row = conn.execute("SELECT active FROM chat_bans WHERE id = ?", (ban_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Ban not found")
        
        new_status = 0 if row["active"] == 1 else 1
        conn.execute("UPDATE chat_bans SET active = ? WHERE id = ?", (new_status, ban_id))
        conn.commit()
    
    return RedirectResponse(url="/admin/bans", status_code=303)

@app.post("/admin/bans/new")
async def admin_new_ban(
    request: Request,
    username: str = Form(...),
    school: str = Form(...),
):
    require_admin(request)
    with get_db() as conn:
        try:
            conn.execute(
                "INSERT INTO chat_bans (created_at, school, username, active) VALUES (datetime('now'), ?, ?, 1)",
                (school, username)
            )
            conn.commit()
        except sqlite3.IntegrityError:
            pass
    return RedirectResponse(url="/admin/bans", status_code=303)
