# -*- coding: utf-8 -*-
"""Database layer — SQLite via thread-pool executor"""

import sqlite3
import os
import threading
import bcrypt
from typing import Optional, List, Dict

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "parcer.db")

_local = threading.local()


def get_conn() -> sqlite3.Connection:
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA foreign_keys=ON")
    return _local.conn


_lock = threading.Lock()


def execute(sql: str, params=()) -> sqlite3.Cursor:
    with _lock:
        conn = get_conn()
        cur = conn.execute(sql, params)
        conn.commit()
        return cur


def fetchall(sql: str, params=()) -> List[Dict]:
    with _lock:
        conn = get_conn()
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def fetchone(sql: str, params=()) -> Optional[Dict]:
    with _lock:
        conn = get_conn()
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with _lock:
        conn = get_conn()
        conn.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;

            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                gigachat_token TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                created_by INTEGER REFERENCES users(id)
            );

            CREATE TABLE IF NOT EXISTS tenders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                original_name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'uploaded',
                progress INTEGER DEFAULT 0,
                total_items INTEGER DEFAULT 0,
                processed_items INTEGER DEFAULT 0,
                results_per_item INTEGER DEFAULT 5,
                created_at TEXT DEFAULT (datetime('now')),
                completed_at TEXT,
                error_message TEXT
            );

            CREATE TABLE IF NOT EXISTS tender_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tender_id INTEGER NOT NULL REFERENCES tenders(id) ON DELETE CASCADE,
                num INTEGER,
                name TEXT NOT NULL,
                qty REAL DEFAULT 0,
                unit TEXT DEFAULT 'шт.',
                max_price REAL DEFAULT 0,
                description TEXT DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS search_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL REFERENCES tender_items(id) ON DELETE CASCADE,
                tender_id INTEGER NOT NULL,
                supplier TEXT DEFAULT '',
                price REAL DEFAULT 0,
                url TEXT DEFAULT '',
                title TEXT DEFAULT '',
                quantity_available TEXT DEFAULT '',
                sort_order INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tender_id INTEGER NOT NULL REFERENCES tenders(id) ON DELETE CASCADE,
                user_id INTEGER NOT NULL REFERENCES users(id),
                format TEXT NOT NULL,
                filename TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS system_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        conn.commit()

    # Create root user if not exists
    root = fetchone("SELECT id FROM users WHERE username = 'root'")
    if not root:
        default_password = os.environ.get("ROOT_PASSWORD", "admin").encode()
        h = bcrypt.hashpw(default_password, bcrypt.gensalt()).decode()
        execute(
            "INSERT INTO users (username, password_hash, role, gigachat_token) VALUES (?,?,?,?)",
            ("root", h, "root", None)
        )


# ─────────────────────── USERS ───────────────────────

def get_user_by_username(username: str) -> Optional[Dict]:
    return fetchone("SELECT * FROM users WHERE username = ?", (username,))


def get_user_by_id(user_id: int) -> Optional[Dict]:
    return fetchone("SELECT * FROM users WHERE id = ?", (user_id,))


def get_all_users() -> List[Dict]:
    return fetchall("""
        SELECT id, username, role, gigachat_token IS NOT NULL as has_token, created_at,
               (SELECT username FROM users u2 WHERE u2.id = users.created_by) as created_by_name
        FROM users ORDER BY id
    """)


def create_user(username: str, password: str, role: str, created_by: int) -> Dict:
    h = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    execute(
        "INSERT INTO users (username, password_hash, role, created_by) VALUES (?,?,?,?)",
        (username, h, role, created_by)
    )
    return fetchone("SELECT * FROM users WHERE username = ?", (username,))


def update_user_password(user_id: int, new_password: str):
    h = bcrypt.hashpw(new_password.encode(), bcrypt.gensalt()).decode()
    execute("UPDATE users SET password_hash = ? WHERE id = ?", (h, user_id))


def update_user_token(user_id: int, token: Optional[str]):
    execute("UPDATE users SET gigachat_token = ? WHERE id = ?", (token, user_id))


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def get_effective_gigachat_token(user_id: int) -> Optional[str]:
    """Returns user's token, or root's token if user has none."""
    user = fetchone("SELECT gigachat_token FROM users WHERE id = ?", (user_id,))
    if user and user.get("gigachat_token"):
        return user["gigachat_token"]
    root = fetchone("SELECT gigachat_token FROM users WHERE role = 'root' LIMIT 1")
    return root.get("gigachat_token") if root else None


# ─────────────────────── TENDERS ───────────────────────

def get_user_tenders(user_id: int) -> List[Dict]:
    return fetchall("""
        SELECT t.*,
               (SELECT COUNT(*) FROM tender_items ti WHERE ti.tender_id = t.id) as item_count
        FROM tenders t WHERE t.user_id = ? ORDER BY t.created_at DESC
    """, (user_id,))


def get_tender(tender_id: int) -> Optional[Dict]:
    return fetchone("SELECT * FROM tenders WHERE id = ?", (tender_id,))


def create_tender(user_id: int, original_name: str, results_per_item: int = 5) -> int:
    cur = execute(
        "INSERT INTO tenders (user_id, original_name, results_per_item, status) VALUES (?,?,?,?)",
        (user_id, original_name, results_per_item, "uploaded")
    )
    return cur.lastrowid


def update_tender_status(tender_id: int, status: str, progress: int = None,
                         total_items: int = None, processed_items: int = None,
                         error_message: str = None, completed: bool = False):
    fields = ["status = ?"]
    vals = [status]
    if progress is not None:
        fields.append("progress = ?"); vals.append(progress)
    if total_items is not None:
        fields.append("total_items = ?"); vals.append(total_items)
    if processed_items is not None:
        fields.append("processed_items = ?"); vals.append(processed_items)
    if error_message is not None:
        fields.append("error_message = ?"); vals.append(error_message)
    if completed:
        fields.append("completed_at = datetime('now')")
    vals.append(tender_id)
    execute(f"UPDATE tenders SET {', '.join(fields)} WHERE id = ?", tuple(vals))


def save_tender_items(tender_id: int, items: List[Dict]) -> List[int]:
    ids = []
    for it in items:
        cur = execute(
            "INSERT INTO tender_items (tender_id,num,name,qty,unit,max_price,description) VALUES (?,?,?,?,?,?,?)",
            (tender_id, it.get("num", 0), it["name"], it.get("qty", 0),
             it.get("unit", "шт."), it.get("max_price", 0), it.get("description", ""))
        )
        ids.append(cur.lastrowid)
    return ids


def get_tender_items(tender_id: int) -> List[Dict]:
    return fetchall("SELECT * FROM tender_items WHERE tender_id = ? ORDER BY num, id", (tender_id,))


def save_search_results(item_id: int, tender_id: int, offers: List[Dict]):
    execute("DELETE FROM search_results WHERE item_id = ?", (item_id,))
    for i, o in enumerate(offers):
        execute(
            "INSERT INTO search_results (item_id,tender_id,supplier,price,url,title,quantity_available,sort_order) VALUES (?,?,?,?,?,?,?,?)",
            (item_id, tender_id, o.get("supplier", ""), float(o.get("price", 0) or 0),
             o.get("url", ""), o.get("title", ""), o.get("quantity_available", ""), i)
        )


def get_search_results_for_tender(tender_id: int) -> List[Dict]:
    return fetchall("""
        SELECT sr.*, ti.num, ti.name as item_name, ti.qty, ti.unit, ti.max_price, ti.description
        FROM search_results sr
        JOIN tender_items ti ON ti.id = sr.item_id
        WHERE sr.tender_id = ?
        ORDER BY ti.num, sr.sort_order
    """, (tender_id,))


def save_report(tender_id: int, user_id: int, fmt: str, filename: str) -> int:
    cur = execute(
        "INSERT INTO reports (tender_id, user_id, format, filename) VALUES (?,?,?,?)",
        (tender_id, user_id, fmt, filename)
    )
    return cur.lastrowid


def get_tender_reports(tender_id: int) -> List[Dict]:
    return fetchall("SELECT * FROM reports WHERE tender_id = ? ORDER BY created_at DESC", (tender_id,))


def delete_tender(tender_id: int):
    execute("DELETE FROM tenders WHERE id = ?", (tender_id,))


# ─────────────────────── SYSTEM SETTINGS ───────────────────────

DEFAULT_YANDEX_KEY = os.environ.get("YANDEX_API_KEY", "")


def get_setting(key: str) -> Optional[str]:
    row = fetchone("SELECT value FROM system_settings WHERE key = ?", (key,))
    if row:
        return row["value"]
    if key == "yandex_api_key":
        return DEFAULT_YANDEX_KEY
    return None


def set_setting(key: str, value: str):
    execute(
        "INSERT INTO system_settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value)
    )


def get_all_settings() -> Dict[str, str]:
    rows = fetchall("SELECT key, value FROM system_settings")
    result = {r["key"]: r["value"] for r in rows}
    if "yandex_api_key" not in result:
        result["yandex_api_key"] = DEFAULT_YANDEX_KEY
    if "yandex_folder_id" not in result:
        result["yandex_folder_id"] = ""
    return result
