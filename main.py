# -*- coding: utf-8 -*-
"""
PARCER — Tender Search Service
FastAPI backend with SSE progress streaming
"""

import os
import uuid
import threading
import queue
from datetime import datetime
from typing import Optional

from fastapi import (FastAPI, HTTPException, Request, UploadFile, File,
                     Form, Response, status)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import database as db
import auth
from file_parser import parse_file
from yandexgpt_client import generate_search_queries, extract_price_from_snippets
from yandex_client import search_item
from report_generator import generate_xlsx, generate_pdf

# ─────────────────────── APP SETUP ───────────────────────

app = FastAPI(title="PARCER", docs_url=None, redoc_url=None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR     = os.path.dirname(os.path.abspath(__file__))
UPLOADS_DIR  = os.path.join(BASE_DIR, "data", "uploads")
REPORTS_DIR  = os.path.join(BASE_DIR, "data", "reports")
STATIC_DIR   = os.path.join(BASE_DIR, "static")

os.makedirs(UPLOADS_DIR, exist_ok=True)
os.makedirs(REPORTS_DIR, exist_ok=True)

# SSE progress queues: {tender_id: queue.Queue}
_progress_queues: dict = {}
_progress_lock = threading.Lock()


@app.on_event("startup")
def startup():
    db.init_db()


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


# ─────────────────────── AUTH ───────────────────────

class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login")
def login(req: LoginRequest, response: Response):
    user = db.get_user_by_username(req.username)
    if not user or not db.verify_password(req.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")
    token = auth.create_token(user["id"], user["username"], user["role"])
    response.set_cookie("token", token, httponly=True, max_age=86400, samesite="lax")
    return {"id": user["id"], "username": user["username"], "role": user["role"],
            "has_token": bool(user.get("gigachat_token"))}


@app.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie("token")
    return {"ok": True}


@app.get("/api/auth/me")
def me(request: Request):
    user = auth.get_current_user(request)
    row = db.get_user_by_id(user["id"])
    return {"id": row["id"], "username": row["username"], "role": row["role"],
            "has_token": bool(row.get("gigachat_token"))}


# ─────────────────────── USERS (admin/root) ───────────────────────

@app.get("/api/users")
def list_users(request: Request):
    auth.require_admin(request)
    return db.get_all_users()


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "user"


@app.post("/api/users")
def create_user(req: CreateUserRequest, request: Request):
    admin = auth.require_admin(request)
    if req.role == "root":
        raise HTTPException(400, "Нельзя создать root пользователя")
    if req.role == "admin" and admin["role"] != "root":
        raise HTTPException(403, "Только root может создавать администраторов")
    existing = db.get_user_by_username(req.username)
    if existing:
        raise HTTPException(400, "Пользователь уже существует")
    user = db.create_user(req.username, req.password, req.role, admin["id"])
    return {"id": user["id"], "username": user["username"], "role": user["role"]}


class ChangePasswordRequest(BaseModel):
    password: str


@app.put("/api/users/{user_id}/password")
def change_password(user_id: int, req: ChangePasswordRequest, request: Request):
    current = auth.get_current_user(request)
    if current["id"] != user_id and current["role"] not in ("admin", "root"):
        raise HTTPException(403, "Недостаточно прав")
    target = db.get_user_by_id(user_id)
    if not target:
        raise HTTPException(404, "Пользователь не найден")
    # Only root can change admin/root passwords
    if target["role"] in ("root", "admin") and current["role"] != "root":
        raise HTTPException(403, "Только root может менять пароль администратора")
    db.update_user_password(user_id, req.password)
    return {"ok": True}


class ChangeRoleRequest(BaseModel):
    role: str


@app.put("/api/users/{user_id}/role")
def change_role(user_id: int, req: ChangeRoleRequest, request: Request):
    current = auth.require_root(request)
    target = db.get_user_by_id(user_id)
    if not target:
        raise HTTPException(404, "Пользователь не найден")
    if target["role"] == "root":
        raise HTTPException(403, "Нельзя изменить роль root")
    if req.role not in ("admin", "user"):
        raise HTTPException(400, "Недопустимая роль")
    db.execute("UPDATE users SET role = ? WHERE id = ?", (req.role, user_id))
    return {"ok": True}


@app.delete("/api/users/{user_id}")
def delete_user(user_id: int, request: Request):
    current = auth.require_root(request)
    target = db.get_user_by_id(user_id)
    if not target:
        raise HTTPException(404, "Пользователь не найден")
    if target["role"] == "root":
        raise HTTPException(403, "Нельзя удалить root")
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return {"ok": True}


class SetTokenRequest(BaseModel):
    token: Optional[str] = None


@app.put("/api/users/{user_id}/token")
def set_user_token(user_id: int, req: SetTokenRequest, request: Request):
    current = auth.get_current_user(request)
    if current["id"] != user_id and current["role"] not in ("admin", "root"):
        raise HTTPException(403, "Недостаточно прав")
    db.update_user_token(user_id, req.token or None)
    return {"ok": True}


# ─────────────────────── SYSTEM SETTINGS (root only) ───────────────────────

@app.get("/api/settings")
def get_settings(request: Request):
    auth.require_root(request)
    return db.get_all_settings()


class UpdateSettingsRequest(BaseModel):
    yandex_api_key: Optional[str] = None
    yandex_folder_id: Optional[str] = None


@app.put("/api/settings")
def update_settings(req: UpdateSettingsRequest, request: Request):
    auth.require_root(request)
    if req.yandex_api_key is not None:
        db.set_setting("yandex_api_key", req.yandex_api_key.strip())
    if req.yandex_folder_id is not None:
        db.set_setting("yandex_folder_id", req.yandex_folder_id.strip())
    return db.get_all_settings()


# ─────────────────────── TENDERS ───────────────────────

ALLOWED_EXTENSIONS = {".html", ".htm", ".xlsx", ".docx", ".rtf", ".xml"}


@app.get("/api/tenders")
def list_tenders(request: Request):
    user = auth.get_current_user(request)
    return db.get_user_tenders(user["id"])


@app.post("/api/tenders/upload")
async def upload_tender(
    request: Request,
    file: UploadFile = File(...),
    results_per_item: int = Form(5),
):
    user = auth.get_current_user(request)
    ext = os.path.splitext(file.filename.lower())[1]
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, f"Неподдерживаемый формат: {ext}. Поддерживаются: {', '.join(ALLOWED_EXTENSIONS)}")

    # Save uploaded file
    file_id = str(uuid.uuid4())
    save_path = os.path.join(UPLOADS_DIR, f"{file_id}{ext}")
    content = await file.read()
    with open(save_path, "wb") as f:
        f.write(content)

    # Create tender record
    tender_id = db.create_tender(user["id"], file.filename, results_per_item)

    # Parse file in background thread
    yandex_key = db.get_setting("yandex_api_key")
    yandex_folder = db.get_setting("yandex_folder_id")
    llm_creds = (yandex_key, yandex_folder) if yandex_key and yandex_folder else None
    _push_progress(tender_id, "parsing", 0, "Парсинг файла...")

    def do_parse():
        try:
            db.update_tender_status(tender_id, "parsing")
            items = parse_file(save_path, file.filename, llm_creds)
            if not items:
                db.update_tender_status(tender_id, "error",
                                        error_message="Не удалось извлечь позиции из файла")
                _push_progress(tender_id, "error", 0, "Ошибка парсинга файла")
                return
            db.save_tender_items(tender_id, items)
            db.update_tender_status(tender_id, "parsed",
                                    total_items=len(items), processed_items=0, progress=0)
            _push_progress(tender_id, "parsed", 0, f"Извлечено {len(items)} позиций")
        except Exception as e:
            db.update_tender_status(tender_id, "error", error_message=str(e))
            _push_progress(tender_id, "error", 0, f"Ошибка: {e}")

    threading.Thread(target=do_parse, daemon=True).start()
    return {"tender_id": tender_id, "filename": file.filename}


@app.get("/api/tenders/{tender_id}")
def get_tender(tender_id: int, request: Request):
    user = auth.get_current_user(request)
    tender = db.get_tender(tender_id)
    if not tender or tender["user_id"] != user["id"]:
        raise HTTPException(404, "Заявка не найдена")
    items = db.get_tender_items(tender_id)
    results = db.get_search_results_for_tender(tender_id)
    # Group results by item_id and attach directly to each item
    results_by_item: dict = {}
    for r in results:
        results_by_item.setdefault(r["item_id"], []).append(r)
    for item in items:
        item["offers"] = results_by_item.get(item["id"], [])
    return {**tender, "items": items}


@app.delete("/api/tenders/{tender_id}")
def delete_tender(tender_id: int, request: Request):
    user = auth.get_current_user(request)
    tender = db.get_tender(tender_id)
    if not tender or tender["user_id"] != user["id"]:
        raise HTTPException(404, "Заявка не найдена")
    db.delete_tender(tender_id)
    return {"ok": True}


# ─────────────────────── SEARCH ───────────────────────

class StartSearchRequest(BaseModel):
    results_per_item: int = 5


@app.post("/api/tenders/{tender_id}/search")
def start_search(tender_id: int, req: StartSearchRequest, request: Request):
    user = auth.get_current_user(request)
    tender = db.get_tender(tender_id)
    if not tender or tender["user_id"] != user["id"]:
        raise HTTPException(404, "Заявка не найдена")
    if tender["status"] == "searching":
        raise HTTPException(400, "Поиск уже запущен")

    items = db.get_tender_items(tender_id)
    if not items:
        raise HTTPException(400, "Нет позиций для поиска")

    yandex_key = db.get_setting("yandex_api_key")
    if not yandex_key:
        raise HTTPException(400, "Yandex API ключ не настроен")
    yandex_folder = db.get_setting("yandex_folder_id")
    if not yandex_folder:
        raise HTTPException(400, "Yandex Folder ID не настроен. Укажите в разделе Система.")

    results_per = req.results_per_item or tender["results_per_item"] or 5
    db.update_tender_status(tender_id, "searching",
                            total_items=len(items), processed_items=0, progress=0)

    def do_search():
        total = len(items)
        for i, item in enumerate(items):
            try:
                _push_progress(tender_id, "searching",
                                int(i * 100 / total),
                                f"[{i+1}/{total}] {item['name'][:50]}...")

                # Generate search queries via YandexGPT
                queries = generate_search_queries(yandex_key, yandex_folder, item["name"], item.get("description", ""))

                # Search via Yandex async API
                raw_results = search_item(yandex_key, queries, results_per_item=results_per + 5)

                # Build offers list
                offers = _build_offers(yandex_key, yandex_folder, item, raw_results, results_per)

                db.save_search_results(item["id"], tender_id, offers)
                db.update_tender_status(tender_id, "searching",
                                        processed_items=i + 1,
                                        progress=int((i + 1) * 100 / total))
            except Exception as e:
                print(f"[Search] Error for item {item['id']}: {e}")

        db.update_tender_status(tender_id, "done",
                                processed_items=total,
                                progress=100, completed=True)
        _push_progress(tender_id, "done", 100, "Поиск завершён")

    threading.Thread(target=do_search, daemon=True).start()
    return {"ok": True, "total_items": len(items)}


def _build_offers(yandex_key: str, yandex_folder: str, item: dict, raw_results: list, limit: int) -> list:
    """Convert raw Yandex results to offer dicts, extract prices via GigaChat if needed."""
    with_price = [r for r in raw_results if r.get("price", 0) > 0]
    without_price = [r for r in raw_results if not r.get("price", 0)]

    offers = []
    seen_urls = set()

    for r in with_price:
        if r["url"] in seen_urls:
            continue
        seen_urls.add(r["url"])
        offers.append({
            "supplier":           r.get("domain", ""),
            "price":              r["price"],
            "url":                r["url"],
            "title":              r["title"],
            "quantity_available": r.get("quantity_available", ""),
        })

    # If we have fewer priced results than desired, try YandexGPT extraction
    if len(offers) < limit and without_price:
        extra = extract_price_from_snippets(
            yandex_key, yandex_folder, item["name"], item.get("max_price", 0), without_price
        )
        for e in extra:
            url = e.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                offers.append({
                    "supplier":           e.get("supplier", ""),
                    "price":              float(e.get("price", 0) or 0),
                    "url":                url,
                    "title":              e.get("title", ""),
                    "quantity_available": e.get("quantity_available", ""),
                })

    # Sort: priced first (ascending), then unpriced
    offers.sort(key=lambda x: (0 if x["price"] > 0 else 1, x["price"]))
    return offers[:limit]


# ─────────────────────── SSE PROGRESS ───────────────────────

def _push_progress(tender_id: int, status: str, progress: int, message: str):
    with _progress_lock:
        q = _progress_queues.get(tender_id)
    if q:
        try:
            q.put_nowait({"status": status, "progress": progress, "message": message})
        except Exception:
            pass


@app.get("/api/tenders/{tender_id}/stream")
def stream_progress(tender_id: int, request: Request):
    user = auth.get_current_user(request)
    tender = db.get_tender(tender_id)
    if not tender or tender["user_id"] != user["id"]:
        raise HTTPException(404)

    q: queue.Queue = queue.Queue(maxsize=100)
    with _progress_lock:
        _progress_queues[tender_id] = q

    def event_generator():
        import json, time
        try:
            # Send current state immediately
            t = db.get_tender(tender_id)
            yield f"data: {json.dumps({'status': t['status'], 'progress': t['progress'], 'processed': t['processed_items'], 'total': t['total_items']})}\n\n"

            while True:
                try:
                    msg = q.get(timeout=20)
                    yield f"data: {json.dumps(msg)}\n\n"
                    if msg.get("status") in ("done", "error"):
                        break
                except queue.Empty:
                    # Send heartbeat
                    t = db.get_tender(tender_id)
                    yield f"data: {json.dumps({'status': t['status'], 'progress': t['progress'], 'processed': t['processed_items'], 'total': t['total_items'], 'heartbeat': True})}\n\n"
                    if t["status"] in ("done", "error", "parsed"):
                        break
        finally:
            with _progress_lock:
                _progress_queues.pop(tender_id, None)

    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─────────────────────── REPORTS ───────────────────────

@app.get("/api/tenders/{tender_id}/report/{fmt}")
def download_report(tender_id: int, fmt: str, request: Request):
    user = auth.get_current_user(request)
    tender = db.get_tender(tender_id)
    if not tender or tender["user_id"] != user["id"]:
        raise HTTPException(404, "Заявка не найдена")
    if tender["status"] != "done":
        raise HTTPException(400, "Поиск ещё не завершён")
    if fmt not in ("xlsx", "pdf"):
        raise HTTPException(400, "Формат должен быть xlsx или pdf")

    items = db.get_tender_items(tender_id)
    raw_results = db.get_search_results_for_tender(tender_id)

    # Group results by item_id
    results_map: dict = {}
    for r in raw_results:
        results_map.setdefault(r["item_id"], []).append(r)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = tender["original_name"].replace(" ", "_")[:30]
    filename = f"report_{safe_name}_{ts}.{fmt}"
    filepath = os.path.join(REPORTS_DIR, str(user["id"]), filename)
    os.makedirs(os.path.dirname(filepath), exist_ok=True)

    tender_name = tender["original_name"]
    if fmt == "xlsx":
        generate_xlsx(items, results_map, tender_name, filepath)
        media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        generate_pdf(items, results_map, tender_name, filepath)
        media = "application/pdf"

    db.save_report(tender_id, user["id"], fmt, filename)
    return FileResponse(filepath, media_type=media, filename=filename)
