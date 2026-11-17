"""SQLite 持久层：用户/会话/素材/知识片段/卡片/作业/对话，单连接 + 锁，WAL 模式。"""

import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  phone TEXT PRIMARY KEY, name TEXT, role TEXT, email TEXT,
  auth_kind TEXT, created_at REAL, last_login REAL);

CREATE TABLE IF NOT EXISTS sessions(
  token TEXT PRIMARY KEY, phone TEXT NOT NULL, created_at REAL, expires_at REAL);

CREATE TABLE IF NOT EXISTS materials(
  id TEXT PRIMARY KEY, phone TEXT NOT NULL, filename TEXT, path TEXT, size INTEGER,
  sha256 TEXT, mime TEXT, status TEXT, reason TEXT, created_at REAL);
CREATE INDEX IF NOT EXISTS idx_materials_phone ON materials(phone, created_at DESC);

CREATE TABLE IF NOT EXISTS chunks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  material_id TEXT NOT NULL, phone TEXT NOT NULL, seq INTEGER, body TEXT);
CREATE INDEX IF NOT EXISTS idx_chunks_phone ON chunks(phone, material_id);

CREATE TABLE IF NOT EXISTS cards(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  material_id TEXT NOT NULL, phone TEXT NOT NULL,
  title TEXT, summary TEXT, bullets TEXT, tags TEXT, created_at REAL);

CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, phone TEXT, kind TEXT, status TEXT,
  payload TEXT, result TEXT, error TEXT, owner TEXT,
  created_at REAL, updated_at REAL);
CREATE INDEX IF NOT EXISTS idx_jobs_phone ON jobs(phone, created_at DESC);

CREATE TABLE IF NOT EXISTS uploads(
  id TEXT PRIMARY KEY, phone TEXT NOT NULL, filename TEXT, size INTEGER,
  offset INTEGER, sha256 TEXT, path TEXT, status TEXT, material_id TEXT,
  created_at REAL, updated_at REAL);
CREATE INDEX IF NOT EXISTS idx_uploads_phone ON uploads(phone, created_at DESC);

CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  phone TEXT NOT NULL, role TEXT NOT NULL, content TEXT, created_at REAL);
CREATE INDEX IF NOT EXISTS idx_messages_phone ON messages(phone, id DESC);
"""


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        with self._lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=NORMAL")
            self.db.executescript(SCHEMA)
            self._migrate()
            self.db.commit()

    def _migrate(self):
        """老库补充列，避免升级时丢历史数据。"""
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(jobs)").fetchall()}
        if "owner" not in cols:
            self.db.execute("ALTER TABLE jobs ADD COLUMN owner TEXT")

    def exec(self, sql, params=()):
        with self._lock:
            cur = self.db.execute(sql, params)
            self.db.commit()
            return cur

    def one(self, sql, params=()):
        with self._lock:
            row = self.db.execute(sql, params).fetchone()
        return dict(row) if row else None

    def all(self, sql, params=()):
        with self._lock:
            rows = self.db.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- 会话
    def create_session(self, token, phone, ttl_sec):
        now = time.time()
        self.exec("INSERT OR REPLACE INTO sessions(token, phone, created_at, expires_at) VALUES(?,?,?,?)",
                  (token, phone, now, now + ttl_sec))
        return now

    def session_user(self, token):
        row = self.one("SELECT * FROM sessions WHERE token=?", (token,))
        if not row or row["expires_at"] < time.time():
            return None
        return self.one("SELECT * FROM users WHERE phone=?", (row["phone"],))

    def drop_session(self, token):
        self.exec("DELETE FROM sessions WHERE token=?", (token,))

    # ---------------------------------------------------------------- 素材
    def add_material(self, phone, filename, path, size, sha256, mime):
        mid = "mat_" + sha256[:12] + "_" + str(int(time.time() * 1000))[-5:]
        self.exec("INSERT INTO materials(id, phone, filename, path, size, sha256, mime, status, created_at)"
                  " VALUES(?,?,?,?,?,?,?, 'pending', ?)",
                  (mid, phone, filename, str(path), size, sha256, mime, time.time()))
        return mid

    def set_material_status(self, material_id, status, reason=None):
        self.exec("UPDATE materials SET status=?, reason=? WHERE id=?", (status, reason, material_id))

    def material(self, material_id):
        return self.one("SELECT * FROM materials WHERE id=?", (material_id,))

    def materials(self, phone, limit=50):
        return self.all("SELECT * FROM materials WHERE phone=? ORDER BY created_at DESC LIMIT ?",
                        (phone, int(limit)))

    def pending_materials(self, phone):
        return self.all("SELECT * FROM materials WHERE phone=? AND status IN ('pending','failed')"
                        " ORDER BY created_at", (phone,))

    # ---------------------------------------------------------------- 知识
    def replace_chunks(self, material_id, phone, chunks):
        self.exec("DELETE FROM chunks WHERE material_id=?", (material_id,))
        rows = [(material_id, phone, i, c) for i, c in enumerate(chunks)]
        with self._lock:
            self.db.executemany("INSERT INTO chunks(material_id, phone, seq, body) VALUES(?,?,?,?)", rows)
            self.db.commit()
        return len(rows)

    def chunks(self, phone, limit=2000):
        return self.all("SELECT * FROM chunks WHERE phone=? ORDER BY id DESC LIMIT ?", (phone, int(limit)))

    def add_card(self, material_id, phone, title, summary, bullets, tags):
        self.exec("INSERT INTO cards(material_id, phone, title, summary, bullets, tags, created_at)"
                  " VALUES(?,?,?,?,?,?,?)",
                  (material_id, phone, title, summary, bullets, tags, time.time()))

    def card_for(self, material_id):
        return self.one("SELECT * FROM cards WHERE material_id=? ORDER BY id DESC", (material_id,))

    def cards(self, phone, limit=20):
        return self.all("SELECT c.*, m.filename FROM cards c LEFT JOIN materials m ON m.id=c.material_id"
                        " WHERE c.phone=? ORDER BY c.id DESC LIMIT ?", (phone, int(limit)))

    # ---------------------------------------------------------------- 作业
    def add_job(self, job_id, phone, kind, payload_json, owner=None):
        now = time.time()
        self.exec("INSERT INTO jobs(id, phone, kind, status, payload, owner, created_at, updated_at)"
                  " VALUES(?,?,?, 'queued', ?, ?, ?, ?)",
                  (job_id, phone, kind, payload_json, owner, now, now))

    def update_job(self, job_id, status, result_json=None, error=None):
        self.exec("UPDATE jobs SET status=?, result=COALESCE(?, result), error=?, updated_at=? WHERE id=?",
                  (status, result_json, error, time.time(), job_id))

    def job(self, job_id):
        return self.one("SELECT * FROM jobs WHERE id=?", (job_id,))

    def jobs(self, phone, limit=20):
        return self.all("SELECT id, kind, status, created_at, updated_at, error FROM jobs"
                        " WHERE phone=? ORDER BY created_at DESC LIMIT ?", (phone, int(limit)))

    def recover_stale_jobs(self, owner=None, include_legacy=False):
        """只恢复本进程遗留的作业，避免两个进程共用库时互相误杀。

        owner=None 且 include_legacy=True 时兼容老数据（owner 为空的全部视为本进程）。
        """
        now = time.time()
        if owner is None:
            if not include_legacy:
                return 0
            cur = self.exec("UPDATE jobs SET status='interrupted', error='服务重启中断', updated_at=? "
                            "WHERE status IN ('queued','running')", (now,))
            return cur.rowcount
        if include_legacy:
            cur = self.exec("UPDATE jobs SET status='interrupted', error='服务重启中断', updated_at=? "
                            "WHERE status IN ('queued','running') AND (owner=? OR owner IS NULL)",
                            (now, owner))
        else:
            cur = self.exec("UPDATE jobs SET status='interrupted', error='服务重启中断', updated_at=? "
                            "WHERE status IN ('queued','running') AND owner=?", (now, owner))
        return cur.rowcount

    # ---------------------------------------------------------------- 上传单
    def add_upload(self, upload_id, phone, filename, size, path):
        now = time.time()
        self.exec("INSERT INTO uploads(id, phone, filename, size, offset, path, status, created_at, updated_at)"
                  " VALUES(?,?,?,?,0,?, 'uploading', ?, ?)",
                  (upload_id, phone, filename, size, str(path), now, now))

    def upload(self, upload_id):
        return self.one("SELECT * FROM uploads WHERE id=?", (upload_id,))

    def update_upload(self, upload_id, **fields):
        if not fields:
            return
        cols = ", ".join("%s=?" % k for k in fields)
        params = list(fields.values()) + [time.time(), upload_id]
        self.exec("UPDATE uploads SET %s, updated_at=? WHERE id=?" % cols, tuple(params))

    def uploads(self, phone, limit=50):
        return self.all("SELECT * FROM uploads WHERE phone=? ORDER BY created_at DESC LIMIT ?",
                        (phone, int(limit)))

    def stale_uploads(self, before_ts, limit=200):
        return self.all("SELECT * FROM uploads WHERE status='uploading' AND updated_at<? LIMIT ?",
                        (before_ts, int(limit)))

    # ---------------------------------------------------------------- 对话
    def add_message(self, phone, role, content):
        self.exec("INSERT INTO messages(phone, role, content, created_at) VALUES(?,?,?,?)",
                  (phone, role, content, time.time()))

    def messages(self, phone, limit=12):
        rows = self.all("SELECT role, content FROM messages WHERE phone=? ORDER BY id DESC LIMIT ?",
                        (phone, int(limit)))
        return list(reversed(rows))
