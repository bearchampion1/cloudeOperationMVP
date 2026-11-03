"""履歷篩選系統伺服器（只用 Python 標準函式庫，方便之後用 PyInstaller 打包成 exe）。

啟動：python server.py [--port 8000] [--host 127.0.0.1] [--no-browser]
求職者頁：http://127.0.0.1:8000/        （註冊 / 登入後投遞履歷、查看投遞狀態）
HR 後台： http://127.0.0.1:8000/admin   （預設帳號 admin、密碼 admin，請登入後在「設定」修改）
"""
import argparse
import base64
import hashlib
import html
import http.cookies
import json
import mimetypes
import os
import queue
import re
import secrets
import sqlite3
import sys
import threading
import uuid
import webbrowser
import zipfile
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

import llm

# 打包成 exe 時，靜態檔在暫存解壓目錄，資料庫與附件則放在 exe 旁邊
if getattr(sys, "frozen", False):
    STATIC_DIR = os.path.join(sys._MEIPASS, "static")
    DATA_DIR = os.path.dirname(sys.executable)
else:
    STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    DATA_DIR = os.path.dirname(os.path.abspath(__file__))

DB_PATH = os.environ.get("RESUME_DB", os.path.join(DATA_DIR, "resumes.db"))
UPLOAD_DIR = os.environ.get("RESUME_UPLOADS", os.path.join(DATA_DIR, "uploads"))

FIELD_TYPES = {"text", "textarea", "email", "tel", "number", "date", "select", "radio", "checkbox", "file"}
FILE_EXTS = {".pdf", ".doc", ".docx", ".txt", ".png", ".jpg", ".jpeg"}
MAX_FILE_BYTES = 5 * 1024 * 1024
STATUSES = ["新投遞", "審核中", "面試", "錄取", "不合適"]

FILE_FIELD = {"key": "resume_file", "label": "履歷附件", "type": "file",
              "help": "可上傳 PDF、Word（.docx）、TXT 或圖片，5 MB 以內"}
DEFAULT_FIELDS = [
    {"key": "name", "label": "姓名", "type": "text", "required": True, "show_in_list": True},
    {"key": "phone", "label": "電話號碼", "type": "tel", "required": True, "show_in_list": True,
     "placeholder": "0912-345-678"},
    {"key": "email", "label": "Email", "type": "email", "required": True, "show_in_list": True},
    {"key": "address", "label": "地址", "type": "text"},
    {"key": "school", "label": "學校", "type": "text", "show_in_list": True},
    {"key": "degree", "label": "最高學歷", "type": "select", "show_in_list": True,
     "options": ["高中職", "專科", "大學", "碩士", "博士"]},
    {"key": "position", "label": "應徵職位", "type": "text", "required": True, "show_in_list": True},
    {"key": "experience_years", "label": "工作年資（年）", "type": "number"},
    {"key": "skills", "label": "專長技能", "type": "checkbox",
     "options": ["Python", "JavaScript", "SQL", "設計", "行銷", "業務"]},
    {"key": "autobiography", "label": "自傳", "type": "textarea", "required": True},
    FILE_FIELD,
]

DEFAULT_LLM = {
    "enabled": False,
    "auto": True,
    "base_url": "http://127.0.0.1:1234/v1",
    "model": "qwen/qwen3-4b-2507",
    "api_key": "lm-studio",
    "temperature": llm.DEFAULT_TEMPERATURE,
    "requirements": "職缺：後端工程師，負責 API 與資料庫開發，需要和前端、PM 密切合作。",
    "criteria": [
        {"name": "Python", "weight": 25, "required": True, "desc": "能用 Python 開發後端或自動化"},
        {"name": "SQL／資料庫", "weight": 20, "required": True, "desc": ""},
        {"name": "大學以上學歷", "weight": 10, "required": False, "desc": "資訊相關科系加分"},
        {"name": "相關工作經驗", "weight": 15, "required": False, "desc": "2 年以上為佳，實習與專案也可以算"},
        {"name": "溝通與團隊合作", "weight": 15, "required": False, "desc": "軟技能，可從自傳與經歷描述推論"},
        {"name": "學習能力與主動性", "weight": 15, "required": False, "desc": "軟技能，例如自學新技術、主動做專案"},
    ],
}

AI_COLUMNS = {
    "ai_status": "TEXT NOT NULL DEFAULT ''",  # ''=未評分, queued, running, done, error
    "ai_score": "INTEGER",
    "ai_verdict": "TEXT NOT NULL DEFAULT ''",
    "ai_summary": "TEXT NOT NULL DEFAULT ''",
    "ai_highlights": "TEXT NOT NULL DEFAULT '[]'",  # JSON list
    "ai_breakdown": "TEXT NOT NULL DEFAULT '[]'",   # JSON list：每個評估項目的分數
    "ai_error": "TEXT NOT NULL DEFAULT ''",
    "ai_at": "TEXT NOT NULL DEFAULT ''",
}

# HR 與求職者分開登入：各用一個 cookie，同一個瀏覽器可以同時登入兩邊
COOKIES = {"hr": "rs_hr", "applicant": "rs_user"}
SESSIONS = {}  # token -> (role, user_id)
db_lock = threading.Lock()


def now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------- 資料庫 ----------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()
    return f"{salt}${digest}"


def check_password(password, stored):
    salt, _ = stored.split("$", 1)
    return secrets.compare_digest(hash_password(password, salt), stored)


def init_db():
    with db_lock, db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS resumes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT '新投遞',
                note TEXT NOT NULL DEFAULT '',
                data TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,       -- hr 或 applicant
                username TEXT NOT NULL,   -- HR 帳號；求職者用 Email
                name TEXT NOT NULL DEFAULT '',
                password TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(role, username)
            );
        """)
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(resumes)")}
        for col, ddl in {**AI_COLUMNS, "user_id": "INTEGER"}.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE resumes ADD COLUMN {col} {ddl}")
        defaults = {
            "llm": json.dumps(DEFAULT_LLM, ensure_ascii=False),
            "fields": json.dumps([normalize_field(f) for f in DEFAULT_FIELDS], ensure_ascii=False),
            "form_title": "線上履歷表",
            "form_intro": "請填寫以下資料，我們會盡快與您聯繫。",
        }
        for k, v in defaults.items():
            conn.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))
        # 舊版只有一組後台密碼：轉成 HR 帳號 admin
        if not conn.execute("SELECT 1 FROM users WHERE role = 'hr'").fetchone():
            old = conn.execute("SELECT value FROM settings WHERE key = 'admin_password'").fetchone()
            conn.execute("INSERT INTO users(role, username, name, password, created_at) VALUES ('hr', 'admin', '管理員', ?, ?)",
                         (old["value"] if old else hash_password("admin"), now()))
        # 舊版沒有附件欄位：補一次（之後 HR 刪掉就不會再加回來）
        if not conn.execute("SELECT 1 FROM settings WHERE key = 'file_field_added'").fetchone():
            fields = json.loads(conn.execute("SELECT value FROM settings WHERE key = 'fields'").fetchone()["value"])
            if not any(f["type"] == "file" for f in fields) and not any(f["key"] == FILE_FIELD["key"] for f in fields):
                fields.append(normalize_field(FILE_FIELD))
                conn.execute("UPDATE settings SET value = ? WHERE key = 'fields'", (json.dumps(fields, ensure_ascii=False),))
            conn.execute("INSERT INTO settings(key, value) VALUES ('file_field_added', '1')")


def get_setting(key):
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_setting(key, value):
    with db_lock, db() as conn:
        conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, value))


def get_fields():
    return json.loads(get_setting("fields"))


def get_llm_config():
    return {**DEFAULT_LLM, **json.loads(get_setting("llm") or "{}")}


def get_user(user_id):
    with db() as conn:
        return conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()


# ---------- 附件 ----------

def save_uploads(data):
    """把驗證過的附件（name + bytes）寫進 uploads 資料夾，換成 {name, file, size}。"""
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    for key, v in data.items():
        if isinstance(v, dict) and "bytes" in v:
            stored = uuid.uuid4().hex + os.path.splitext(v["name"])[1].lower()
            with open(os.path.join(UPLOAD_DIR, stored), "wb") as fh:
                fh.write(v["bytes"])
            data[key] = {"name": v["name"], "file": stored, "size": len(v["bytes"])}
    return data


def delete_uploads(data):
    for v in data.values():
        if isinstance(v, dict) and re.fullmatch(r"[0-9a-f]{32}\.[a-z]+", v.get("file", "")):
            try:
                os.remove(os.path.join(UPLOAD_DIR, v["file"]))
            except OSError:
                pass


def attachment_text(path, limit=6000):
    """盡量把附件轉成文字給 AI 看；讀不到就回傳空字串。PDF 需要另外安裝 pypdf。"""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".txt":
            with open(path, "rb") as fh:
                raw = fh.read()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                text = raw.decode("cp950", errors="ignore")  # Windows 記事本常見的 Big5
        elif ext == ".docx":
            with zipfile.ZipFile(path) as z:
                xml = z.read("word/document.xml").decode("utf-8", errors="ignore")
            text = html.unescape(re.sub(r"<[^>]+>", "", xml.replace("</w:p>", "\n")))
        elif ext == ".pdf":
            try:
                from pypdf import PdfReader
            except ImportError:
                return ""
            text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages[:10])
        else:
            return ""
    except Exception:  # 壞檔、加密檔等，當作讀不到
        return ""
    return re.sub(r"\n\s*\n+", "\n", text).strip()[:limit]


def data_for_ai(fields, data):
    """附件欄位換成「檔名 + 附件文字」，讓 AI 也能看到附件內容。"""
    out = dict(data)
    for f in fields:
        v = data.get(f["key"])
        if f["type"] == "file" and isinstance(v, dict):
            text = attachment_text(os.path.join(UPLOAD_DIR, v["file"]))
            out[f["key"]] = f"{v['name']}\n{text}" if text else f"{v['name']}（附件內容無法自動讀取，請勿據此評分）"
    return out


# ---------- AI 評分（單一背景工作者，避免同時塞爆本機 LLM） ----------

screen_queue = queue.Queue()


def enqueue_screening(resume_id):
    with db_lock, db() as conn:
        conn.execute("UPDATE resumes SET ai_status = 'queued', ai_error = '' WHERE id = ?", (resume_id,))
    screen_queue.put(resume_id)


def screening_worker():
    while True:
        resume_id = screen_queue.get()
        try:
            with db() as conn:
                row = conn.execute("SELECT * FROM resumes WHERE id = ?", (resume_id,)).fetchone()
            if not row or row["ai_status"] != "queued":
                continue
            with db_lock, db() as conn:
                conn.execute("UPDATE resumes SET ai_status = 'running' WHERE id = ?", (resume_id,))
            cfg, fields = get_llm_config(), get_fields()
            result = llm.screen_resume(cfg, fields, data_for_ai(fields, json.loads(row["data"])))
            with db_lock, db() as conn:
                conn.execute(
                    "UPDATE resumes SET ai_status = 'done', ai_score = ?, ai_verdict = ?, ai_summary = ?, "
                    "ai_highlights = ?, ai_breakdown = ?, ai_error = '', ai_at = ? WHERE id = ?",
                    (result["score"], result["verdict"], result["summary"],
                     json.dumps(result["highlights"], ensure_ascii=False),
                     json.dumps(result["breakdown"], ensure_ascii=False), now(), resume_id),
                )
        except Exception as e:  # 任何錯誤都記錄在該筆履歷上，不讓工作者停掉
            with db_lock, db() as conn:
                conn.execute("UPDATE resumes SET ai_status = 'error', ai_error = ? WHERE id = ?",
                             (str(e)[:500], resume_id))
        finally:
            screen_queue.task_done()


def start_screening_worker():
    threading.Thread(target=screening_worker, daemon=True).start()
    # 上次關閉前還沒評完的，重新排入
    with db() as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM resumes WHERE ai_status IN ('queued', 'running') ORDER BY id")]
    for i in ids:
        enqueue_screening(i)


# ---------- 驗證 ----------

def normalize_field(f):
    field = {
        "key": str(f.get("key", "")).strip(),
        "label": str(f.get("label", "")).strip(),
        "type": f.get("type", "text"),
        "required": bool(f.get("required")),
        "show_in_list": bool(f.get("show_in_list")),
        "placeholder": str(f.get("placeholder", "")),
        "help": str(f.get("help", "")),
        "options": [str(o).strip() for o in f.get("options", []) if str(o).strip()],
    }
    if field["type"] not in FIELD_TYPES:
        raise ValueError(f"不支援的欄位類型：{field['type']}")
    if not re.fullmatch(r"[A-Za-z0-9_]+", field["key"]):
        raise ValueError(f"欄位代碼只能用英數字與底線：{field['key'] or '(空白)'}")
    if not field["label"]:
        raise ValueError(f"欄位 {field['key']} 缺少名稱")
    if field["type"] in ("select", "radio", "checkbox") and not field["options"]:
        raise ValueError(f"欄位「{field['label']}」需要至少一個選項")
    return field


def normalize_criteria(items):
    """HR 設定的評估項目：[{name, weight 1～100, required, desc}]，空白名稱的列直接略過。"""
    out = []
    for c in items or []:
        name = str(c.get("name", "")).strip()[:40]
        if not name:
            continue
        try:
            weight = int(c.get("weight", 0))
        except (TypeError, ValueError):
            raise ValueError(f"「{name}」的權重要是整數")
        if not 1 <= weight <= 100:
            raise ValueError(f"「{name}」的權重要在 1～100 之間")
        out.append({"name": name, "weight": weight, "required": bool(c.get("required")),
                    "desc": str(c.get("desc", "")).strip()[:200]})
    if len(out) > 20:
        raise ValueError("評估項目最多 20 個")
    return out


def validate_file(f, value):
    """回傳 (值, 錯誤訊息)。值是 '' 或 {name, bytes}。"""
    value = value if isinstance(value, dict) else {}
    name = os.path.basename(str(value.get("name", "")).replace("\\", "/")).strip()[:120]
    content = value.get("content") or ""
    if not name or not content:
        return "", ("請上傳檔案" if f["required"] else None)
    if os.path.splitext(name)[1].lower() not in FILE_EXTS:
        return "", "只接受 PDF、Word、TXT、PNG、JPG 檔案"
    try:
        raw = base64.b64decode(content, validate=True)
    except ValueError:
        return "", "檔案內容錯誤，請重新選擇檔案"
    if len(raw) > MAX_FILE_BYTES:
        return "", "檔案不可超過 5 MB"
    return {"name": name, "bytes": raw}, None


def validate_submission(fields, data):
    """回傳 (清理後的資料, 錯誤 dict)。"""
    clean, errors = {}, {}
    for f in fields:
        value = data.get(f["key"])
        if f["type"] == "file":
            clean[f["key"]], err = validate_file(f, value)
            if err:
                errors[f["key"]] = err
            continue
        if f["type"] == "checkbox":
            value = [v for v in (value or []) if v in f["options"]]
            empty = not value
        else:
            value = "" if value is None else str(value).strip()
            empty = value == ""
        if empty:
            if f["required"]:
                errors[f["key"]] = "此欄位為必填"
            clean[f["key"]] = value
            continue
        t = f["type"]
        if t == "email" and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            errors[f["key"]] = "Email 格式不正確"
        elif t == "tel" and not re.fullmatch(r"[0-9+\-()# ]{6,20}", value):
            errors[f["key"]] = "電話號碼格式不正確"
        elif t == "number":
            try:
                float(value)
            except ValueError:
                errors[f["key"]] = "請輸入數字"
        elif t == "date" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            errors[f["key"]] = "日期格式不正確"
        elif t in ("select", "radio") and value not in f["options"]:
            errors[f["key"]] = "請從選項中選擇"
        elif len(value) > 20000:
            errors[f["key"]] = "內容過長"
        clean[f["key"]] = value
    return clean, errors


def resume_row(row, include_data=True):
    ai = {k[3:]: row[k] for k in AI_COLUMNS}
    ai["highlights"] = json.loads(ai["highlights"] or "[]")
    ai["breakdown"] = json.loads(ai["breakdown"] or "[]")
    item = {"id": row["id"], "created_at": row["created_at"], "status": row["status"], "note": row["note"], "ai": ai}
    if include_data:
        item["data"] = json.loads(row["data"])
    return item


def file_label(v):
    return v.get("name", "") if isinstance(v, dict) else v


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "ResumeScreener/0.2"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (datetime.now().strftime("%H:%M:%S"), fmt % args))

    # --- 工具 ---
    def send_json(self, obj, status=200, headers=None):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, status, message, **extra):
        self.send_json({"error": message, **extra}, status)

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 10_000_000:  # 附件以 base64 放在 JSON 裡，5 MB 檔案約 6.7 MB
            raise ValueError("資料過大")
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode() or "{}")

    def send_static(self, name, content_type):
        path = os.path.join(STATIC_DIR, name)
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except FileNotFoundError:
            return self.send_error_json(404, "找不到頁面")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def session_token(self, role):
        cookie = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
        return cookie[COOKIES[role]].value if COOKIES[role] in cookie else None

    def current_user(self, role):
        """回傳目前登入的 user_id（角色不符就是 None）。"""
        s = SESSIONS.get(self.session_token(role))
        return s[1] if s and s[0] == role else None

    def require(self, role):
        uid = self.current_user(role)
        if uid is None:
            self.send_error_json(401, "請先登入")
        return uid

    def login(self, role, username, password):
        with db() as conn:
            user = conn.execute("SELECT * FROM users WHERE role = ? AND username = ?", (role, username)).fetchone()
        if not user or not check_password(password, user["password"]):
            return self.send_error_json(401, "帳號或密碼錯誤")
        self.start_session(role, user["id"])

    def start_session(self, role, user_id, status=200):
        token = secrets.token_urlsafe(32)
        SESSIONS[token] = (role, user_id)
        self.send_json({"ok": True}, status, headers={
            "Set-Cookie": f"{COOKIES[role]}={token}; HttpOnly; SameSite=Strict; Path=/"})

    def logout(self, role):
        SESSIONS.pop(self.session_token(role), None)
        self.send_json({"ok": True}, headers={"Set-Cookie": f"{COOKIES[role]}=; Max-Age=0; Path=/"})

    def send_attachment(self, resume_id, key):
        with db() as conn:
            row = conn.execute("SELECT data FROM resumes WHERE id = ?", (resume_id,)).fetchone()
        v = json.loads(row["data"]).get(key) if row else None
        if not isinstance(v, dict) or not re.fullmatch(r"[0-9a-f]{32}\.[a-z]+", v.get("file", "")):
            return self.send_error_json(404, "找不到附件")
        try:
            with open(os.path.join(UPLOAD_DIR, v["file"]), "rb") as fh:
                body = fh.read()
        except FileNotFoundError:
            return self.send_error_json(404, "附件檔案不見了")
        ctype = mimetypes.guess_type(v["file"])[0] or "application/octet-stream"
        inline = ctype in ("application/pdf", "image/png", "image/jpeg", "text/plain")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition", f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(v['name'])}")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    # --- 路由 ---
    def do_GET(self):
        url = urlparse(self.path)
        p = url.path
        static = {
            "/": ("apply.html", "text/html; charset=utf-8"),
            "/admin": ("admin.html", "text/html; charset=utf-8"),
            "/static/style.css": ("style.css", "text/css; charset=utf-8"),
            "/static/apply.js": ("apply.js", "text/javascript; charset=utf-8"),
            "/static/admin.js": ("admin.js", "text/javascript; charset=utf-8"),
        }
        if p in static:
            return self.send_static(*static[p])
        if p == "/api/form":
            return self.send_json({
                "title": get_setting("form_title"),
                "intro": get_setting("form_intro"),
                "fields": get_fields(),
            })

        # 求職者
        if p == "/api/user/me":
            uid = self.current_user("applicant")
            user = get_user(uid) if uid else None
            return self.send_json({"logged_in": bool(user), "email": user["username"] if user else "",
                                   "name": user["name"] if user else ""})
        if p == "/api/user/resumes":
            uid = self.require("applicant")
            if uid is None:
                return
            with db() as conn:
                rows = conn.execute("SELECT id, created_at, status, data FROM resumes WHERE user_id = ? ORDER BY id DESC",
                                    (uid,)).fetchall()
            return self.send_json({"items": [{"id": r["id"], "created_at": r["created_at"], "status": r["status"],
                                              "position": json.loads(r["data"]).get("position", "")} for r in rows]})

        # HR
        if p == "/api/admin/me":
            uid = self.current_user("hr")
            user = get_user(uid) if uid else None
            return self.send_json({"logged_in": bool(user), "username": user["username"] if user else ""})
        if not p.startswith("/api/admin/"):
            return self.send_error_json(404, "找不到頁面")
        if self.require("hr") is None:
            return
        if p == "/api/admin/llm":
            return self.send_json({"config": get_llm_config(), "queue": screen_queue.qsize()})
        if p == "/api/admin/accounts":
            with db() as conn:
                rows = conn.execute("SELECT id, username, name, created_at FROM users WHERE role = 'hr' ORDER BY id").fetchall()
            return self.send_json({"items": [dict(r) for r in rows], "me": self.current_user("hr")})
        if p == "/api/admin/resumes":
            return self.list_resumes(parse_qs(url.query))
        if p == "/api/admin/resumes.csv":
            return self.export_csv()
        m = re.fullmatch(r"/api/admin/resumes/(\d+)/files/([A-Za-z0-9_]+)", p)
        if m:
            return self.send_attachment(int(m.group(1)), m.group(2))
        m = re.fullmatch(r"/api/admin/resumes/(\d+)", p)
        if m:
            with db() as conn:
                row = conn.execute("SELECT * FROM resumes WHERE id = ?", (int(m.group(1)),)).fetchone()
            if not row:
                return self.send_error_json(404, "找不到這份履歷")
            applicant = get_user(row["user_id"]) if row["user_id"] else None
            return self.send_json({"resume": resume_row(row), "fields": get_fields(), "statuses": STATUSES,
                                   "applicant": applicant["username"] if applicant else "",
                                   "llm_enabled": get_llm_config()["enabled"]})
        self.send_error_json(404, "找不到 API")

    def do_POST(self):
        p = urlparse(self.path).path
        try:
            body = self.read_json()
        except (ValueError, json.JSONDecodeError):
            return self.send_error_json(400, "資料格式錯誤")

        # 求職者
        if p == "/api/user/register":
            email = str(body.get("email", "")).strip().lower()
            name = str(body.get("name", "")).strip()[:50]
            password = str(body.get("password", ""))
            if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                return self.send_error_json(400, "Email 格式不正確")
            if not name:
                return self.send_error_json(400, "請填寫姓名")
            if len(password) < 6:
                return self.send_error_json(400, "密碼至少 6 個字元")
            try:
                with db_lock, db() as conn:
                    cur = conn.execute(
                        "INSERT INTO users(role, username, name, password, created_at) VALUES ('applicant', ?, ?, ?, ?)",
                        (email, name, hash_password(password), now()))
            except sqlite3.IntegrityError:
                return self.send_error_json(400, "這個 Email 已經註冊過，請直接登入")
            return self.start_session("applicant", cur.lastrowid, 201)
        if p == "/api/user/login":
            return self.login("applicant", str(body.get("email", "")).strip().lower(), str(body.get("password", "")))
        if p == "/api/user/logout":
            return self.logout("applicant")
        if p == "/api/resumes":
            uid = self.require("applicant")
            if uid is None:
                return
            fields = get_fields()
            data, errors = validate_submission(fields, body.get("data") or {})
            if errors:
                return self.send_error_json(400, "請修正標示的欄位", fields=errors)
            save_uploads(data)
            with db_lock, db() as conn:
                cur = conn.execute(
                    "INSERT INTO resumes(created_at, data, user_id) VALUES (?, ?, ?)",
                    (now(), json.dumps(data, ensure_ascii=False), uid),
                )
            cfg = get_llm_config()
            if cfg["enabled"] and cfg["auto"]:
                enqueue_screening(cur.lastrowid)
            return self.send_json({"ok": True, "id": cur.lastrowid}, 201)

        # HR
        if p == "/api/admin/login":
            return self.login("hr", str(body.get("username", "")).strip(), str(body.get("password", "")))
        if p == "/api/admin/logout":
            return self.logout("hr")
        if p.startswith("/api/admin/") and self.require("hr") is None:
            return

        if p == "/api/admin/accounts":
            username = str(body.get("username", "")).strip()
            password = str(body.get("password", ""))
            if not re.fullmatch(r"[A-Za-z0-9_.@-]{3,40}", username):
                return self.send_error_json(400, "帳號需為 3～40 個英數字（可含 _ . @ -）")
            if len(password) < 6:
                return self.send_error_json(400, "密碼至少 6 個字元")
            try:
                with db_lock, db() as conn:
                    conn.execute("INSERT INTO users(role, username, name, password, created_at) VALUES ('hr', ?, ?, ?, ?)",
                                 (username, str(body.get("name", "")).strip()[:50], hash_password(password), now()))
            except sqlite3.IntegrityError:
                return self.send_error_json(400, "這個帳號已經存在")
            return self.send_json({"ok": True}, 201)

        if p == "/api/admin/llm/test":
            cfg = {**get_llm_config(), **{k: v for k, v in body.items() if k in ("base_url", "model", "api_key")}}
            try:
                models = llm.list_models(cfg)
                reply = llm.chat(cfg, [{"role": "user", "content": "請用繁體中文回答：你好，請用一句話自我介紹。"}],
                                 timeout=60)
            except llm.LLMError as e:
                return self.send_error_json(502, str(e))
            return self.send_json({"ok": True, "models": models, "reply": reply})

        m = re.fullmatch(r"/api/admin/resumes/(\d+)/screen", p)
        if m:
            if not get_llm_config()["enabled"]:
                return self.send_error_json(400, "請先在「AI 篩選」頁啟用 AI 評分")
            enqueue_screening(int(m.group(1)))
            return self.send_json({"ok": True})

        if p == "/api/admin/screen-all":
            if not get_llm_config()["enabled"]:
                return self.send_error_json(400, "請先在「AI 篩選」頁啟用 AI 評分")
            only_new = body.get("only_new", True)
            with db() as conn:
                sql = "SELECT id FROM resumes" + (" WHERE ai_status IN ('', 'error')" if only_new else "")
                ids = [r["id"] for r in conn.execute(sql + " ORDER BY id")]
            for i in ids:
                enqueue_screening(i)
            return self.send_json({"ok": True, "count": len(ids)})

        self.send_error_json(404, "找不到 API")

    def do_PUT(self):
        p = urlparse(self.path).path
        uid = self.require("hr")
        if uid is None:
            return
        try:
            body = self.read_json()
        except (ValueError, json.JSONDecodeError):
            return self.send_error_json(400, "資料格式錯誤")

        if p == "/api/admin/form":
            try:
                fields = [normalize_field(f) for f in body.get("fields", [])]
            except ValueError as e:
                return self.send_error_json(400, str(e))
            keys = [f["key"] for f in fields]
            if len(keys) != len(set(keys)):
                return self.send_error_json(400, "欄位代碼不可重複")
            if not fields:
                return self.send_error_json(400, "至少需要一個欄位")
            set_setting("fields", json.dumps(fields, ensure_ascii=False))
            set_setting("form_title", str(body.get("title") or "線上履歷表"))
            set_setting("form_intro", str(body.get("intro") or ""))
            return self.send_json({"ok": True})

        if p == "/api/admin/llm":
            cfg = get_llm_config()
            for k in ("base_url", "model", "api_key", "requirements"):
                if k in body:
                    cfg[k] = str(body[k]).strip()
            for k in ("enabled", "auto"):
                if k in body:
                    cfg[k] = bool(body[k])
            if "criteria" in body:
                try:
                    cfg["criteria"] = normalize_criteria(body["criteria"])
                except ValueError as e:
                    return self.send_error_json(400, str(e))
            if "temperature" in body:
                try:
                    cfg["temperature"] = float(body["temperature"])
                except (TypeError, ValueError):
                    return self.send_error_json(400, "Temperature 要是數字")
            if not 0 <= cfg["temperature"] <= 1.5:
                return self.send_error_json(400, "Temperature 要在 0～1.5 之間")
            if not cfg["base_url"].startswith(("http://", "https://")):
                return self.send_error_json(400, "API 網址需以 http:// 或 https:// 開頭")
            if cfg["enabled"] and not cfg["criteria"]:
                return self.send_error_json(400, "請至少新增一個評估項目")
            set_setting("llm", json.dumps(cfg, ensure_ascii=False))
            return self.send_json({"ok": True})

        if p == "/api/admin/password":
            user = get_user(uid)
            if not check_password(str(body.get("current", "")), user["password"]):
                return self.send_error_json(400, "目前密碼錯誤")
            new = str(body.get("new", ""))
            if len(new) < 6:
                return self.send_error_json(400, "新密碼至少 6 個字元")
            with db_lock, db() as conn:
                conn.execute("UPDATE users SET password = ? WHERE id = ?", (hash_password(new), uid))
            return self.send_json({"ok": True})

        m = re.fullmatch(r"/api/admin/resumes/(\d+)", p)
        if m:
            status = body.get("status")
            if status is not None and status not in STATUSES:
                return self.send_error_json(400, "狀態不正確")
            with db_lock, db() as conn:
                row = conn.execute("SELECT * FROM resumes WHERE id = ?", (int(m.group(1)),)).fetchone()
                if not row:
                    return self.send_error_json(404, "找不到這份履歷")
                conn.execute(
                    "UPDATE resumes SET status = ?, note = ? WHERE id = ?",
                    (status or row["status"], str(body.get("note", row["note"])), row["id"]),
                )
            return self.send_json({"ok": True})
        self.send_error_json(404, "找不到 API")

    def do_DELETE(self):
        uid = self.require("hr")
        if uid is None:
            return
        p = urlparse(self.path).path
        m = re.fullmatch(r"/api/admin/accounts/(\d+)", p)
        if m:
            if int(m.group(1)) == uid:
                return self.send_error_json(400, "不能刪除自己的帳號")
            with db_lock, db() as conn:
                conn.execute("DELETE FROM users WHERE id = ? AND role = 'hr'", (int(m.group(1)),))
            for token, s in list(SESSIONS.items()):
                if s == ("hr", int(m.group(1))):
                    SESSIONS.pop(token, None)
            return self.send_json({"ok": True})
        m = re.fullmatch(r"/api/admin/resumes/(\d+)", p)
        if not m:
            return self.send_error_json(404, "找不到 API")
        with db_lock, db() as conn:
            row = conn.execute("SELECT data FROM resumes WHERE id = ?", (int(m.group(1)),)).fetchone()
            conn.execute("DELETE FROM resumes WHERE id = ?", (int(m.group(1)),))
        if row:
            delete_uploads(json.loads(row["data"]))
        self.send_json({"ok": True})

    # --- 後台列表 ---
    def list_resumes(self, query):
        q = (query.get("q") or [""])[0].strip().lower()
        status = (query.get("status") or [""])[0]
        verdict = (query.get("verdict") or [""])[0]
        sort = (query.get("sort") or [""])[0]
        sql, where, args = "SELECT * FROM resumes", [], []
        if status:
            where.append("status = ?")
            args.append(status)
        if verdict:
            where.append("ai_verdict = ?")
            args.append(verdict)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY ai_score IS NULL, ai_score DESC, id DESC" if sort == "score" else " ORDER BY id DESC"
        with db() as conn:
            rows = conn.execute(sql, args).fetchall()
        items = [resume_row(r) for r in rows]
        if q:
            items = [i for i in items if q in json.dumps(i["data"], ensure_ascii=False).lower()
                     or q in i["note"].lower() or any(q in h.lower() for h in i["ai"]["highlights"])]
        fields = get_fields()
        list_keys = [f["key"] for f in fields if f["show_in_list"]]
        for i in items:
            i["data"] = {k: i["data"].get(k, "") for k in list_keys}
            i["ai"].pop("summary")  # 列表用不到，詳細頁才看
        self.send_json({
            "columns": [f for f in fields if f["show_in_list"]],
            "items": items,
            "statuses": STATUSES,
            "verdicts": llm.VERDICTS,
            "llm_enabled": get_llm_config()["enabled"],
        })

    def export_csv(self):
        import csv
        import io
        fields = get_fields()
        with db() as conn:
            rows = conn.execute("SELECT * FROM resumes ORDER BY id").fetchall()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["編號", "投遞時間", "狀態", "備註", "AI 分數", "AI 建議", "AI 各項分數", "AI 亮點", "AI 摘要"]
                        + [f["label"] for f in fields])
        for r in rows:
            data = json.loads(r["data"])
            values = [", ".join(v) if isinstance(v, list) else file_label(v) for v in (data.get(f["key"], "") for f in fields)]
            breakdown = "；".join(f"{b['name']} {b['score']}" for b in json.loads(r["ai_breakdown"] or "[]"))
            writer.writerow([r["id"], r["created_at"], r["status"], r["note"],
                             "" if r["ai_score"] is None else r["ai_score"], r["ai_verdict"], breakdown,
                             "、".join(json.loads(r["ai_highlights"] or "[]")), r["ai_summary"]] + values)
        body = ("﻿" + buf.getvalue()).encode("utf-8")  # BOM 讓 Excel 正確顯示中文
        self.send_response(200)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", "attachment; filename=resumes.csv")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description="履歷篩選系統")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    parser.add_argument("--no-browser", action="store_true", help="啟動時不要自動開瀏覽器")
    args = parser.parse_args()

    init_db()
    start_screening_worker()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"履歷篩選系統已啟動\n  求職者頁：{url}/\n  HR 後台： {url}/admin\n  資料庫：  {DB_PATH}\n按 Ctrl+C 結束")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url + "/admin")).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
