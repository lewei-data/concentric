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

CREATE TABLE IF NOT EXISTS api_keys(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  phone TEXT NOT NULL, key_hash TEXT NOT NULL UNIQUE, label TEXT,
  source TEXT, created_at REAL, last_used REAL,
  expires_at REAL, enabled INTEGER DEFAULT 1, revoked INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS idx_api_keys_phone ON api_keys(phone);

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
  chunk_size INTEGER DEFAULT 0, supersedes TEXT, keep_history INTEGER DEFAULT 0,
  created_at REAL, updated_at REAL);
CREATE INDEX IF NOT EXISTS idx_uploads_phone ON uploads(phone, created_at DESC);

CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  phone TEXT NOT NULL, role TEXT NOT NULL, content TEXT, created_at REAL);
CREATE INDEX IF NOT EXISTS idx_messages_phone ON messages(phone, id DESC);

-- ask 幂等：同一 (phone, idempotency_key) 窗口内映射到同一 job
CREATE TABLE IF NOT EXISTS idempotency(
  map_key TEXT PRIMARY KEY, job_id TEXT NOT NULL, phone TEXT, created_at REAL);
CREATE INDEX IF NOT EXISTS idx_idem_phone ON idempotency(phone, created_at DESC);

-- 生成类工具（tts/image）的产物登记：不透明 file_key 对外，路径只留服务端
CREATE TABLE IF NOT EXISTS generated_files(
  file_key TEXT PRIMARY KEY, phone TEXT NOT NULL, path TEXT NOT NULL,
  kind TEXT NOT NULL, filename TEXT, mime TEXT, size INTEGER,
  created_at REAL NOT NULL, expires_at REAL);
CREATE INDEX IF NOT EXISTS idx_genfiles_phone ON generated_files(phone, created_at DESC);
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
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(uploads)").fetchall()}
        if "chunk_size" not in cols:
            self.db.execute("ALTER TABLE uploads ADD COLUMN chunk_size INTEGER DEFAULT 0")
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(materials)").fetchall()}
        if "superseded_by" not in cols:
            self.db.execute("ALTER TABLE materials ADD COLUMN superseded_by TEXT")
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(uploads)").fetchall()}
        if "supersedes" not in cols:
            self.db.execute("ALTER TABLE uploads ADD COLUMN supersedes TEXT")
        if "keep_history" not in cols:
            self.db.execute("ALTER TABLE uploads ADD COLUMN keep_history INTEGER DEFAULT 0")
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(api_keys)").fetchall()}
        for col, ddl in (("source", "ALTER TABLE api_keys ADD COLUMN source TEXT"),
                         ("expires_at", "ALTER TABLE api_keys ADD COLUMN expires_at REAL"),
                         ("enabled", "ALTER TABLE api_keys ADD COLUMN enabled INTEGER DEFAULT 1")):
            if col not in cols:
                self.db.execute(ddl)
        # 历史 key 默认启用；NULL 视为启用（新库 DEFAULT 已保证非 NULL）
        self.db.execute("UPDATE api_keys SET enabled=1 WHERE enabled IS NULL")

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

    # ---------------------------------------------------------------- 生成产物
    def add_generated_file(self, file_key, phone, path, kind, filename="",
                           mime="", size=0, ttl_sec=7 * 86400):
        self.exec("INSERT OR REPLACE INTO generated_files"
                  "(file_key, phone, path, kind, filename, mime, size, created_at, expires_at) "
                  "VALUES(?,?,?,?,?,?,?,?,?)",
                  (file_key, phone, path, kind, filename, mime, int(size or 0),
                   time.time(), time.time() + int(ttl_sec or 0)))
        return file_key

    def generated_file(self, file_key, phone=None):
        sql = "SELECT * FROM generated_files WHERE file_key=?"
        params = [file_key]
        if phone:
            sql += " AND phone=?"
            params.append(phone)
        return self.one(sql, params)

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

    # ---------------------------------------------------------------- 个人 API key
    def add_api_key(self, phone, key_hash, label=None, source=None, expires_at=None):
        self.exec("INSERT INTO api_keys(phone, key_hash, label, source, created_at, expires_at, enabled) "
                  "VALUES(?,?,?,?,?,?,1)",
                  (phone, key_hash, label, source, time.time(), expires_at))
        return self.one("SELECT * FROM api_keys WHERE key_hash=?", (key_hash,))

    def api_key_user(self, key_hash):
        row = self.one("SELECT * FROM api_keys WHERE key_hash=? AND revoked=0 AND enabled=1 "
                       "AND (expires_at IS NULL OR expires_at > ?)", (key_hash, time.time()))
        if not row:
            return None
        self.exec("UPDATE api_keys SET last_used=? WHERE id=?", (time.time(), row["id"]))
        return self.one("SELECT * FROM users WHERE phone=?", (row["phone"],))

    def api_keys(self, phone=None):
        cols = "id, phone, label, source, created_at, last_used, expires_at, enabled, revoked"
        if phone:
            return self.all("SELECT %s FROM api_keys WHERE phone=? ORDER BY id DESC" % cols, (phone,))
        return self.all("SELECT %s FROM api_keys ORDER BY id DESC" % cols)

    def api_key(self, key_id):
        return self.one("SELECT id, phone, label, source, created_at, last_used, expires_at, enabled, revoked "
                        "FROM api_keys WHERE id=?", (int(key_id),))

    def update_api_key(self, key_id, phone=None, label=None, source=None,
                       enabled=None, expires_at=None, clear_expires=False):
        """按 id 修改单个 key 的元数据；只更新显式传入的字段。返回最新行。"""
        sets, params = [], []
        if label is not None:
            sets.append("label=?"); params.append(str(label)[:80])
        if source is not None:
            sets.append("source=?"); params.append(str(source)[:40])
        if enabled is not None:
            sets.append("enabled=?"); params.append(1 if enabled else 0)
        if clear_expires:
            sets.append("expires_at=NULL")
        elif expires_at is not None:
            sets.append("expires_at=?"); params.append(float(expires_at))
        if not sets:
            return self.api_key(key_id)
        where = "id=?"; params.append(int(key_id))
        if phone:
            where += " AND phone=?"; params.append(phone)
        self.exec("UPDATE api_keys SET %s WHERE %s" % (", ".join(sets), where), tuple(params))
        return self.api_key(key_id)

    def revoke_api_key(self, key_id=None, key_hash=None, phone=None):
        clauses, params = [], []
        if key_id is not None:
            clauses.append("id=?")
            params.append(int(key_id))
        if key_hash is not None:
            clauses.append("key_hash=?")
            params.append(key_hash)
        if phone:
            clauses.append("phone=?")
            params.append(phone)
        if not clauses:
            return 0
        cur = self.exec("UPDATE api_keys SET revoked=1 WHERE %s" % " AND ".join(clauses),
                        tuple(params))
        return cur.rowcount

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

    def materials(self, phone, limit=50, offset=0, include_superseded=False):
        cond = "" if include_superseded else " AND status!='superseded'"
        return self.all("SELECT * FROM materials WHERE phone=?%s "
                        "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?" % cond,
                        (phone, int(limit), max(0, int(offset))))

    def materials_total(self, phone, include_superseded=False):
        cond = "" if include_superseded else " AND status!='superseded'"
        return self.one("SELECT COUNT(*) n FROM materials WHERE phone=?%s" % cond,
                        (phone,))["n"]

    def supersede_material(self, material_id, superseded_by):
        """标记素材被新版本取代：保留行与文件，回收切块/卡片使其不再参与召回。"""
        with self._lock:
            row = self.db.execute("SELECT * FROM materials WHERE id=?",
                                  (material_id,)).fetchone()
            if not row:
                return None
            n_chunks = self.db.execute("DELETE FROM chunks WHERE material_id=?",
                                       (material_id,)).rowcount
            n_cards = self.db.execute("DELETE FROM cards WHERE material_id=?",
                                      (material_id,)).rowcount
            self.db.execute("UPDATE materials SET status='superseded', superseded_by=?, "
                            "reason='被新版本取代' WHERE id=?", (superseded_by, material_id))
            self.db.commit()
            return {"id": material_id, "deleted_chunks": n_chunks, "deleted_cards": n_cards}

    def pending_materials(self, phone):
        return self.all("SELECT * FROM materials WHERE phone=? AND status IN ('pending','failed')"
                        " ORDER BY created_at", (phone,))

    def delete_material(self, material_id, phone):
        """删除本人素材及其切块/卡片；非本人或不存在返回 None。"""
        with self._lock:
            row = self.db.execute(
                "SELECT * FROM materials WHERE id=? AND phone=?", (material_id, phone)).fetchone()
            if not row:
                return None
            mat = dict(row)
            n_chunks = self.db.execute(
                "DELETE FROM chunks WHERE material_id=? AND phone=?", (material_id, phone)).rowcount
            n_cards = self.db.execute(
                "DELETE FROM cards WHERE material_id=? AND phone=?", (material_id, phone)).rowcount
            self.db.execute("DELETE FROM materials WHERE id=? AND phone=?", (material_id, phone))
            self.db.commit()
            return {"id": mat["id"], "path": mat["path"], "filename": mat["filename"],
                    "size": mat["size"], "deleted_chunks": n_chunks, "deleted_cards": n_cards}

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
    def get_idempotent(self, map_key, ttl_sec):
        """窗口内命中返回 job_id；过期或不存在返回 None。"""
        row = self.one("SELECT job_id, created_at FROM idempotency WHERE map_key=?", (map_key,))
        if row and row["created_at"] >= time.time() - max(0, int(ttl_sec)):
            return row["job_id"]
        return None

    def put_idempotent(self, map_key, job_id, phone):
        """登记幂等映射；撞唯一键（并发同 key）返回 False，调用方应改读既有映射。"""
        try:
            self.exec("INSERT INTO idempotency(map_key, job_id, phone, created_at) VALUES(?,?,?,?)",
                      (map_key, job_id, phone, time.time()))
        except sqlite3.IntegrityError:
            return False
        # 顺手清掉七天前的旧映射，表不膨胀
        self.exec("DELETE FROM idempotency WHERE created_at<?", (time.time() - 7 * 86400,))
        return True

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
    def add_upload(self, upload_id, phone, filename, size, path, chunk_size=0,
                   supersedes=None, keep_history=False):
        now = time.time()
        self.exec("INSERT INTO uploads(id, phone, filename, size, offset, path, status, chunk_size,"
                  " supersedes, keep_history, created_at, updated_at)"
                  " VALUES(?,?,?,?,0,?, 'uploading', ?, ?, ?, ?, ?)",
                  (upload_id, phone, filename, size, str(path), int(chunk_size or 0),
                   supersedes or None, 1 if keep_history else 0, now, now))

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
