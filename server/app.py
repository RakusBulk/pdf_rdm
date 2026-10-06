"""License server for pdf-drm.

Run with:  uvicorn server.app:app --host 0.0.0.0 --port 8443
(put a real TLS-terminating reverse proxy in front in production --
 keys travel over this API and must never go over plain HTTP).

Admin endpoints require header:  X-Admin-Token: <PDF_DRM_ADMIN_TOKEN>
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from common import crypto
from server import telegram_bot
from server.database import get_conn, init_db

app = FastAPI(title="pdf-drm license server")
app.mount(
    "/admin/ui",
    StaticFiles(directory=str(Path(__file__).parent / "static"), html=True),
    name="admin_ui",
)

STORAGE_DIR = Path(__file__).parent / "storage"
MAX_UPLOAD_BYTES = 300 * 1024 * 1024  # 300MB


@app.get("/")
def root():
    return RedirectResponse("/admin/ui/")

ADMIN_TOKEN_ENV = "PDF_DRM_ADMIN_TOKEN"
SESSION_TTL_HOURS = 24


@app.on_event("startup")
def _startup() -> None:
    init_db()
    STORAGE_DIR.mkdir(exist_ok=True)
    if not os.environ.get(ADMIN_TOKEN_ENV):
        raise RuntimeError(
            f"Set the {ADMIN_TOKEN_ENV} environment variable before starting "
            "the server (it protects the admin endpoints)."
        )
    telegram_bot.start(approve=_telegram_approve, reject=_telegram_reject)


def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    salt = salt or os.urandom(16)
    # N=2**17 (OWASP's current minimum for scrypt, ~128MiB/hash) rather than
    # the much cheaper 2**14 this used to run at.
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**17, r=8, p=1, dklen=32, maxmem=2**28)
    return digest.hex(), salt.hex()


def verify_password(password: str, password_hash: str, salt_hex: str) -> bool:
    digest, _ = hash_password(password, bytes.fromhex(salt_hex))
    return hmac.compare_digest(digest, password_hash)


def _valid_session(conn, session_token: str) -> bool:
    row = conn.execute(
        "SELECT expires_at FROM admin_sessions WHERE session_token = ?", (session_token,)
    ).fetchone()
    return bool(row) and parse_iso(row["expires_at"]) > now_utc()


def _prune_expired_sessions(conn) -> None:
    conn.execute("DELETE FROM admin_sessions WHERE expires_at <= ?", (now_utc().isoformat(),))


def require_admin(x_admin_token: str = Header(default="")) -> None:
    if not x_admin_token:
        raise HTTPException(status_code=403, detail="Invalid admin token")
    expected = os.environ.get(ADMIN_TOKEN_ENV, "")
    if expected and hmac.compare_digest(x_admin_token, expected):
        return
    with get_conn() as conn:
        if _valid_session(conn, x_admin_token):
            return
    raise HTTPException(status_code=403, detail="Invalid admin token")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_iso(value: str) -> datetime:
    # Python 3.10's fromisoformat() can't parse the trailing "Z" that
    # JavaScript's Date.toISOString() produces (fixed in 3.11+).
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def normalize_iso(value: str) -> str:
    # Store a canonical "+00:00"-style offset so every reader (server on any
    # Python version, and the viewer app) can parse it with fromisoformat()
    # regardless of whether the caller sent a trailing "Z".
    return parse_iso(value).isoformat()


# ---------- schemas ----------

class RegisterDocument(BaseModel):
    doc_id: str
    title: str
    key_hex: str


class GrantLicense(BaseModel):
    doc_id: str
    machine_fingerprint: str
    expires_at: str  # ISO-8601, e.g. 2026-08-01T00:00:00+00:00
    label: str | None = None


class RevokeLicense(BaseModel):
    doc_id: str
    machine_fingerprint: str


class LicenseRequest(BaseModel):
    doc_id: str
    machine_fingerprint: str


class BulkGrantLicense(BaseModel):
    grants: list[GrantLicense]


class AccessRequest(BaseModel):
    machine_fingerprint: str
    username: str
    email: str
    note: str | None = None


class ApprovePending(BaseModel):
    request_id: int
    doc_ids: list[str]
    expires_at: str
    label: str | None = None


class RejectPending(BaseModel):
    request_id: int


class CreateGroup(BaseModel):
    name: str
    doc_ids: list[str] = []


class GroupDocIds(BaseModel):
    doc_ids: list[str]


class GrantByGroup(BaseModel):
    group_id: str
    machine_fingerprint: str
    expires_at: str
    label: str | None = None


class CreateAdminUser(BaseModel):
    username: str
    password: str


class LoginRequest(BaseModel):
    username: str
    password: str


# ---------- admin endpoints ----------

@app.post("/admin/documents", dependencies=[Depends(require_admin)])
def register_document(body: RegisterDocument):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO documents (doc_id, title, key_hex) VALUES (?, ?, ?)",
            (body.doc_id, body.title, body.key_hex),
        )
    return {"status": "ok", "doc_id": body.doc_id}


@app.post("/admin/documents/upload", dependencies=[Depends(require_admin)])
async def upload_document(title: str = Form(...), file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=422, detail="Only .pdf files are accepted")

    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(status_code=422, detail="Uploaded file is empty")
    if len(pdf_bytes) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 300MB)")

    key = crypto.generate_key()
    doc_id, blob = crypto.encrypt_pdf(pdf_bytes, key)
    (STORAGE_DIR / f"{doc_id}.cpdf").write_bytes(blob)

    with get_conn() as conn:
        conn.execute(
            """INSERT INTO documents (doc_id, title, key_hex, original_filename, file_size)
               VALUES (?, ?, ?, ?, ?)""",
            (doc_id, title, key.hex(), file.filename, len(blob)),
        )
    return {"status": "ok", "doc_id": doc_id, "title": title, "file_size": len(blob)}


@app.post("/admin/licenses", dependencies=[Depends(require_admin)])
def grant_license(body: GrantLicense):
    expires_at = normalize_iso(body.expires_at)
    with get_conn() as conn:
        doc = conn.execute(
            "SELECT 1 FROM documents WHERE doc_id = ?", (body.doc_id,)
        ).fetchone()
        if not doc:
            raise HTTPException(status_code=404, detail="Unknown doc_id")
        conn.execute(
            """INSERT INTO licenses (doc_id, machine_fingerprint, expires_at, label)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(doc_id, machine_fingerprint)
               DO UPDATE SET expires_at = excluded.expires_at,
                              label = excluded.label,
                              revoked = 0""",
            (body.doc_id, body.machine_fingerprint, expires_at, body.label),
        )
    return {"status": "ok"}


@app.get("/admin/documents", dependencies=[Depends(require_admin)])
def list_documents():
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT d.doc_id, d.title, d.created_at, d.original_filename, d.file_size,
                      COUNT(l.id) AS license_count
               FROM documents d
               LEFT JOIN licenses l ON l.doc_id = d.doc_id
               GROUP BY d.doc_id
               ORDER BY d.created_at DESC"""
        ).fetchall()

        # Fetched separately (not joined into the query above) so the
        # license_count aggregate doesn't get multiplied by a doc's number
        # of group memberships.
        group_rows = conn.execute(
            """SELECT gd.doc_id, g.group_id, g.name
               FROM group_documents gd
               JOIN groups g ON g.group_id = gd.group_id"""
        ).fetchall()
        groups_by_doc: dict[str, list[dict]] = {}
        for r in group_rows:
            groups_by_doc.setdefault(r["doc_id"], []).append({"group_id": r["group_id"], "name": r["name"]})

        result = []
        for row in rows:
            doc = dict(row)
            doc["groups"] = groups_by_doc.get(doc["doc_id"], [])
            result.append(doc)
        return result


@app.get("/admin/documents/{doc_id}/download", dependencies=[Depends(require_admin)])
def download_document(doc_id: str):
    with get_conn() as conn:
        doc = conn.execute("SELECT title FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
    if not doc:
        raise HTTPException(status_code=404, detail="Unknown doc_id")
    file_path = STORAGE_DIR / f"{doc_id}.cpdf"
    if not file_path.exists():
        raise HTTPException(
            status_code=404,
            detail="No file stored on the server for this document (it was likely created via the CLI, "
                   "which keeps the .cpdf on the machine that ran the encrypt command, not on the server).",
        )
    safe_name = "".join(c for c in doc["title"] if c.isalnum() or c in " ._-").strip() or doc_id
    return FileResponse(file_path, filename=f"{safe_name}.cpdf", media_type="application/octet-stream")


@app.delete("/admin/documents/{doc_id}", dependencies=[Depends(require_admin)])
def delete_document(doc_id: str):
    with get_conn() as conn:
        doc = conn.execute("SELECT 1 FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
        if not doc:
            raise HTTPException(status_code=404, detail="Unknown doc_id")
        # children first to satisfy foreign-key constraints
        conn.execute("DELETE FROM licenses WHERE doc_id = ?", (doc_id,))
        conn.execute("DELETE FROM group_documents WHERE doc_id = ?", (doc_id,))
        conn.execute("DELETE FROM access_log WHERE doc_id = ?", (doc_id,))
        conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
    file_path = STORAGE_DIR / f"{doc_id}.cpdf"
    file_path.unlink(missing_ok=True)
    return {"status": "ok"}


# ---------- document groups ----------
# A group is just a named, reusable set of doc_ids -- purely an admin-side
# convenience for bulk-selecting documents. Access is still granted/revoked
# per document; a group has no license semantics of its own.

@app.post("/admin/groups", dependencies=[Depends(require_admin)])
def create_group(body: CreateGroup):
    group_id = str(uuid.uuid4())
    with get_conn() as conn:
        conn.execute("INSERT INTO groups (group_id, name) VALUES (?, ?)", (group_id, body.name))
        known_docs = {
            r["doc_id"] for r in conn.execute("SELECT doc_id FROM documents").fetchall()
        }
        added, skipped = [], []
        for doc_id in body.doc_ids:
            if doc_id not in known_docs:
                skipped.append(doc_id)
                continue
            conn.execute(
                "INSERT OR IGNORE INTO group_documents (group_id, doc_id) VALUES (?, ?)",
                (group_id, doc_id),
            )
            added.append(doc_id)
    return {"group_id": group_id, "name": body.name, "added": added, "skipped": skipped}


@app.get("/admin/groups", dependencies=[Depends(require_admin)])
def list_groups():
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT g.group_id, g.name, g.created_at,
                      COUNT(gd.doc_id) AS doc_count
               FROM groups g
               LEFT JOIN group_documents gd ON gd.group_id = g.group_id
               GROUP BY g.group_id
               ORDER BY g.created_at DESC"""
        ).fetchall()
        return [dict(r) for r in rows]


@app.get("/admin/groups/{group_id}", dependencies=[Depends(require_admin)])
def get_group(group_id: str):
    with get_conn() as conn:
        group = conn.execute("SELECT * FROM groups WHERE group_id = ?", (group_id,)).fetchone()
        if not group:
            raise HTTPException(status_code=404, detail="Unknown group_id")
        docs = conn.execute(
            """SELECT d.doc_id, d.title FROM group_documents gd
               JOIN documents d ON d.doc_id = gd.doc_id
               WHERE gd.group_id = ? ORDER BY d.title""",
            (group_id,),
        ).fetchall()
        return {**dict(group), "documents": [dict(d) for d in docs]}


@app.post("/admin/groups/{group_id}/documents", dependencies=[Depends(require_admin)])
def add_group_documents(group_id: str, body: GroupDocIds):
    with get_conn() as conn:
        if not conn.execute("SELECT 1 FROM groups WHERE group_id = ?", (group_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Unknown group_id")
        known_docs = {
            r["doc_id"] for r in conn.execute("SELECT doc_id FROM documents").fetchall()
        }

        # "Members" of a group = machines with an active (non-revoked,
        # non-expired) license on at least one document already in the
        # group. When a new document is added to the group, extend those
        # members' access to it too -- otherwise adding material to a
        # group would silently leave existing members locked out of it,
        # defeating the point of granting "by group" in the first place.
        existing_doc_ids = [
            r["doc_id"] for r in conn.execute(
                "SELECT doc_id FROM group_documents WHERE group_id = ?", (group_id,)
            ).fetchall()
        ]
        members: dict[str, sqlite3.Row] = {}
        if existing_doc_ids:
            placeholders = ",".join("?" * len(existing_doc_ids))
            rows = conn.execute(
                f"""SELECT * FROM licenses WHERE doc_id IN ({placeholders})
                    AND revoked = 0 AND expires_at > ?""",
                (*existing_doc_ids, now_utc().isoformat()),
            ).fetchall()
            for r in rows:
                fp = r["machine_fingerprint"]
                if fp not in members or r["expires_at"] > members[fp]["expires_at"]:
                    members[fp] = r

        added, skipped = [], []
        auto_granted = 0
        for doc_id in body.doc_ids:
            if doc_id not in known_docs:
                skipped.append(doc_id)
                continue
            conn.execute(
                "INSERT OR IGNORE INTO group_documents (group_id, doc_id) VALUES (?, ?)",
                (group_id, doc_id),
            )
            added.append(doc_id)
            for fp, member in members.items():
                conn.execute(
                    """INSERT INTO licenses (doc_id, machine_fingerprint, expires_at, label)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(doc_id, machine_fingerprint)
                       DO UPDATE SET expires_at = excluded.expires_at,
                                      label = excluded.label,
                                      revoked = 0""",
                    (doc_id, fp, member["expires_at"], member["label"]),
                )
                auto_granted += 1
    return {"added": added, "skipped": skipped, "members_extended": len(members), "auto_granted": auto_granted}


@app.post("/admin/groups/{group_id}/documents/remove", dependencies=[Depends(require_admin)])
def remove_group_documents(group_id: str, body: GroupDocIds):
    with get_conn() as conn:
        for doc_id in body.doc_ids:
            conn.execute(
                "DELETE FROM group_documents WHERE group_id = ? AND doc_id = ?",
                (group_id, doc_id),
            )
    return {"status": "ok"}


@app.delete("/admin/groups/{group_id}", dependencies=[Depends(require_admin)])
def delete_group(group_id: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM group_documents WHERE group_id = ?", (group_id,))
        cur = conn.execute("DELETE FROM groups WHERE group_id = ?", (group_id,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Unknown group_id")
    return {"status": "ok"}


@app.post("/admin/licenses/grant_by_group", dependencies=[Depends(require_admin)])
def grant_by_group(body: GrantByGroup):
    expires_at = normalize_iso(body.expires_at)
    with get_conn() as conn:
        if not conn.execute("SELECT 1 FROM groups WHERE group_id = ?", (body.group_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Unknown group_id")
        doc_ids = [
            r["doc_id"] for r in conn.execute(
                "SELECT doc_id FROM group_documents WHERE group_id = ?", (body.group_id,)
            ).fetchall()
        ]
        granted = []
        for doc_id in doc_ids:
            conn.execute(
                """INSERT INTO licenses (doc_id, machine_fingerprint, expires_at, label)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(doc_id, machine_fingerprint)
                   DO UPDATE SET expires_at = excluded.expires_at,
                                  label = excluded.label,
                                  revoked = 0""",
                (doc_id, body.machine_fingerprint, expires_at, body.label),
            )
            granted.append(doc_id)
    return {"granted": granted}


@app.post("/admin/licenses/bulk", dependencies=[Depends(require_admin)])
def grant_licenses_bulk(body: BulkGrantLicense):
    normalized = [normalize_iso(g.expires_at) for g in body.grants]  # validate all before writing any

    granted = 0
    skipped: list[dict] = []
    with get_conn() as conn:
        known_docs = {
            r["doc_id"] for r in conn.execute("SELECT doc_id FROM documents").fetchall()
        }
        for grant, expires_at in zip(body.grants, normalized):
            if grant.doc_id not in known_docs:
                skipped.append({"doc_id": grant.doc_id, "reason": "unknown_doc"})
                continue
            conn.execute(
                """INSERT INTO licenses (doc_id, machine_fingerprint, expires_at, label)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(doc_id, machine_fingerprint)
                   DO UPDATE SET expires_at = excluded.expires_at,
                                  label = excluded.label,
                                  revoked = 0""",
                (grant.doc_id, grant.machine_fingerprint, expires_at, grant.label),
            )
            granted += 1
    return {"granted": granted, "skipped": skipped}


@app.post("/admin/licenses/revoke", dependencies=[Depends(require_admin)])
def revoke_license(body: RevokeLicense):
    with get_conn() as conn:
        cur = conn.execute(
            """UPDATE licenses SET revoked = 1
               WHERE doc_id = ? AND machine_fingerprint = ?""",
            (body.doc_id, body.machine_fingerprint),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="License not found")
    return {"status": "ok"}


@app.get("/admin/licenses", dependencies=[Depends(require_admin)])
def list_licenses(doc_id: str | None = None):
    with get_conn() as conn:
        if doc_id:
            rows = conn.execute(
                "SELECT * FROM licenses WHERE doc_id = ? ORDER BY id", (doc_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM licenses ORDER BY id").fetchall()
        return [dict(r) for r in rows]


@app.get("/admin/access_log", dependencies=[Depends(require_admin)])
def access_log(doc_id: str | None = None, limit: int = 200):
    with get_conn() as conn:
        if doc_id:
            rows = conn.execute(
                "SELECT * FROM access_log WHERE doc_id = ? ORDER BY id DESC LIMIT ?",
                (doc_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM access_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]


@app.get("/admin/pending", dependencies=[Depends(require_admin)])
def list_pending(status: str = "pending"):
    with get_conn() as conn:
        # The viewer puts the doc_id in `note` when a file was denied; resolve it
        # to the document so the dashboard can show what the user wants to open.
        rows = conn.execute(
            """SELECT p.*, d.doc_id AS requested_doc_id, d.title AS requested_doc_title
               FROM pending_requests p LEFT JOIN documents d ON d.doc_id = p.note
               WHERE p.status = ? ORDER BY p.id""",
            (status,),
        ).fetchall()
        return [dict(r) for r in rows]


@app.post("/admin/pending/approve", dependencies=[Depends(require_admin)])
def approve_pending(body: ApprovePending):
    expires_at = normalize_iso(body.expires_at)
    with get_conn() as conn:
        req = conn.execute(
            "SELECT * FROM pending_requests WHERE id = ?", (body.request_id,)
        ).fetchone()
        if not req:
            raise HTTPException(status_code=404, detail="Unknown request_id")
        if req["status"] != "pending":
            raise HTTPException(status_code=409, detail=f"Request already {req['status']}")

        known_docs = {
            r["doc_id"] for r in conn.execute("SELECT doc_id FROM documents").fetchall()
        }
        default_label = f"{req['username']} <{req['email']}>" if req["username"] else req["email"]
        label = body.label or default_label
        granted = []
        skipped = []
        for doc_id in body.doc_ids:
            if doc_id not in known_docs:
                skipped.append(doc_id)
                continue
            conn.execute(
                """INSERT INTO licenses (doc_id, machine_fingerprint, expires_at, label)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(doc_id, machine_fingerprint)
                   DO UPDATE SET expires_at = excluded.expires_at,
                                  label = excluded.label,
                                  revoked = 0""",
                (doc_id, req["machine_fingerprint"], expires_at, label),
            )
            granted.append(doc_id)

        conn.execute(
            "UPDATE pending_requests SET status = 'approved', decided_at = datetime('now') WHERE id = ?",
            (body.request_id,),
        )
    return {"granted": granted, "skipped": skipped}


@app.post("/admin/pending/reject", dependencies=[Depends(require_admin)])
def reject_pending(body: RejectPending):
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE pending_requests SET status = 'rejected', decided_at = datetime('now') "
            "WHERE id = ? AND status = 'pending'",
            (body.request_id,),
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Unknown or already-decided request_id")
    return {"status": "ok"}


def _telegram_approve(request_id: int, doc_ids: list[str], expires_at: str) -> dict:
    return approve_pending(ApprovePending(request_id=request_id, doc_ids=doc_ids, expires_at=expires_at))


def _telegram_reject(request_id: int) -> dict:
    return reject_pending(RejectPending(request_id=request_id))


# ---------- admin user accounts (login with username/password instead of
# the shared PDF_DRM_ADMIN_TOKEN). The token still works as a bootstrap /
# break-glass credential -- it's needed to create the first account, and
# always remains valid so a lost password can't lock everyone out. ----------

@app.post("/admin/users", dependencies=[Depends(require_admin)])
def create_admin_user(body: CreateAdminUser):
    if len(body.password) < 8:
        raise HTTPException(status_code=422, detail="Password must be at least 8 characters")
    password_hash, salt = hash_password(body.password)
    with get_conn() as conn:
        try:
            conn.execute(
                "INSERT INTO admin_users (username, password_hash, salt) VALUES (?, ?, ?)",
                (body.username, password_hash, salt),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=409, detail="Username already exists")
    return {"status": "ok", "username": body.username}


@app.get("/admin/users", dependencies=[Depends(require_admin)])
def list_admin_users():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT username, created_at FROM admin_users ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]


@app.delete("/admin/users/{username}", dependencies=[Depends(require_admin)])
def delete_admin_user(username: str):
    with get_conn() as conn:
        # sessions reference admin_users via FK, so they must go first
        conn.execute("DELETE FROM admin_sessions WHERE username = ?", (username,))
        cur = conn.execute("DELETE FROM admin_users WHERE username = ?", (username,))
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Unknown username")
    return {"status": "ok"}


@app.post("/admin/auth/login")
def login(body: LoginRequest):
    with get_conn() as conn:
        user = conn.execute(
            "SELECT * FROM admin_users WHERE username = ?", (body.username,)
        ).fetchone()
        if not user or not verify_password(body.password, user["password_hash"], user["salt"]):
            raise HTTPException(status_code=401, detail="Invalid username or password")
        _prune_expired_sessions(conn)  # opportunistic sweep -- login is a natural, low-frequency touchpoint
        session_token = secrets.token_hex(32)
        expires_at = (now_utc() + timedelta(hours=SESSION_TTL_HOURS)).isoformat()
        conn.execute(
            "INSERT INTO admin_sessions (session_token, username, expires_at) VALUES (?, ?, ?)",
            (session_token, body.username, expires_at),
        )
    return {"session_token": session_token, "username": body.username, "expires_at": expires_at}


@app.post("/admin/auth/logout")
def logout(x_admin_token: str = Header(default="")):
    with get_conn() as conn:
        conn.execute("DELETE FROM admin_sessions WHERE session_token = ?", (x_admin_token,))
    return {"status": "ok"}


# ---------- public self-service endpoint ----------

@app.post("/request-access")
def request_access(body: AccessRequest, background_tasks: BackgroundTasks):
    if "@" not in body.email or len(body.email) > 254:
        raise HTTPException(status_code=422, detail="Invalid email")
    if not body.username.strip():
        raise HTTPException(status_code=422, detail="Invalid username")
    if len(body.machine_fingerprint) != 64:
        raise HTTPException(status_code=422, detail="Invalid machine_fingerprint")
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO pending_requests (machine_fingerprint, username, email, note)
               VALUES (?, ?, ?, ?)""",
            (body.machine_fingerprint, body.username, body.email, body.note),
        )
        request_id = cur.lastrowid
    # Runs after the response is sent and the row is committed; a Telegram
    # outage can never fail or slow the request itself.
    background_tasks.add_task(telegram_bot.notify_new_request, request_id, body.machine_fingerprint)
    return {"request_id": request_id, "status": "pending"}


# ---------- client endpoint ----------

@app.post("/license/request")
def request_license(body: LicenseRequest):
    with get_conn() as conn:
        doc = conn.execute(
            "SELECT * FROM documents WHERE doc_id = ?", (body.doc_id,)
        ).fetchone()
        if not doc:
            _log(conn, body, "unknown_doc")
            raise HTTPException(status_code=404, detail="Unknown document")

        lic = conn.execute(
            """SELECT * FROM licenses
               WHERE doc_id = ? AND machine_fingerprint = ?""",
            (body.doc_id, body.machine_fingerprint),
        ).fetchone()

        if not lic:
            _log(conn, body, "no_license")
            raise HTTPException(
                status_code=403, detail="This computer is not licensed for this document"
            )
        if lic["revoked"]:
            _log(conn, body, "revoked")
            raise HTTPException(status_code=403, detail="License revoked")
        if parse_iso(lic["expires_at"]) <= now_utc():
            _log(conn, body, "expired")
            raise HTTPException(status_code=403, detail="License expired")

        _log(conn, body, "granted")
        return {
            "key_hex": doc["key_hex"],
            "title": doc["title"],
            "expires_at": lic["expires_at"],
            "watermark_label": lic["label"] or body.machine_fingerprint[:12],
        }


def _log(conn, body: LicenseRequest, result: str) -> None:
    conn.execute(
        "INSERT INTO access_log (doc_id, machine_fingerprint, result) VALUES (?, ?, ?)",
        (body.doc_id, body.machine_fingerprint, result),
    )
