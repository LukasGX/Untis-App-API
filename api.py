from fastapi import FastAPI, HTTPException, Header, Depends, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from fastapi.responses import HTMLResponse
from fastapi import Form
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

class ContactItem(BaseModel):
    school: str
    username: str
    contact_infos: str  # json string


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

class BanItem(BaseModel):
    school: str
    username: str

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
    """),
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sent_at TEXT,
            school TEXT,
            username TEXT,
            message TEXT,
            deleted BOOLEAN
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

    def dict_row(row):
        """Konvertiert Row zu dict für Jinja2"""
        return dict(row) if row else None
    
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


@app.post("/send_message/", status_code=201)
async def send_message(
    item: MessageItem,
    token: str = Depends(verify_token)
):
    with get_db() as conn:
        # Prüfe ob User approved ist
        row = conn.execute(
            "SELECT * FROM requests WHERE school = ? AND username = ? AND status = 'approved'",
            (item.school, item.username)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=403, detail="User not approved")
        
        # Prüfe ob gebannt
        ban_row = conn.execute(
            "SELECT * FROM chat_bans WHERE username = ? AND active = 1",
            (item.username,)
        ).fetchone()
        if ban_row:
            raise HTTPException(status_code=403, detail="User banned from chat")
        
        conn.execute(
            "INSERT INTO messages (sent_at, school, username, message, deleted) VALUES (datetime('now'), ?, ?, ?, 0)",
            (item.school, item.username, item.message)
        )
        conn.commit()
    return {"message": "Message sent"}

@app.post("/get_messages/")
async def get_messages(
    school: str = Form(...),
    token: str = Depends(verify_token)
):
    with get_db() as conn:
        messages = conn.execute(
            """
            SELECT id, sent_at, username, message, deleted 
            FROM messages 
            WHERE school = ? 
            ORDER BY sent_at DESC 
            LIMIT 100
            """,
            (school,)
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
        return [{"id": b["id"], "username": b["username"], "active": b["active"]} for b in bans]

@app.post("/ban/{username}")
async def check_ban(
    username: str,
    token: str = Depends(verify_token)
):
    with get_db() as conn:
        row = conn.execute(
            "SELECT active FROM chat_bans WHERE username = ?",
            (username,)
        ).fetchone()
        if row:
            return {"banned": True, "active": row["active"]}
        return {"banned": False}

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
    
    # Cookie setzen und aufs Dashboard umleiten
    response = RedirectResponse(url="/admin/dashboard", status_code=303)
    response.set_cookie(
        key="admin_session",
        value="1",  # reicht als Flag, weil echter Token nur serverseitig geprüft wird
        httponly=True,
        secure=False,  # bei HTTPS -> True
        samesite="lax",
    )
    return response

def require_admin(request: Request):
    session = request.cookies.get("admin_session")
    if session != "1":
        raise HTTPException(status_code=401, detail="Not authenticated as admin")

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

@app.post("/admin/chats/{school}/{message_id}/delete")
async def admin_delete_message(school: str, message_id: int, request: Request):
    require_admin(request)
    with get_db() as conn:
        conn.execute(
            "UPDATE messages SET deleted = 1 WHERE id = ? AND school = ?",
            (message_id, school)
        )
        conn.commit()
    return RedirectResponse(url=f"/admin/chats/{school}", status_code=303)

@app.post("/admin/chats/{school}/{message_id}/restore")
async def admin_restore_message(school: str, message_id: int, request: Request):
    require_admin(request)
    with get_db() as conn:
        conn.execute(
            "UPDATE messages SET deleted = 0 WHERE id = ? AND school = ?",
            (message_id, school)
        )
        conn.commit()
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