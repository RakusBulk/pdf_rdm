"""Optional Telegram integration: notify admins of new access requests and
let them approve/reject with one tap.

Disabled unless TELEGRAM_BOT_TOKEN is set. Configuration (env vars, normally
in /etc/pdf-drm.env):

    TELEGRAM_BOT_TOKEN         bot token from @BotFather
    TELEGRAM_ALLOWED_USER_IDS  comma-separated Telegram user IDs that may
                               receive notifications AND press the buttons.
                               Private chat id == user id, so notifications
                               go to each of these users' private chat with
                               the bot. Anyone else pressing a button is
                               ignored (approving grants access to document
                               keys, so this allowlist is the security
                               boundary -- not "who can message the bot").
    TELEGRAM_APPROVE_DAYS      license lifetime when approved (default 180)

Button presses are received by long polling (getUpdates), so no inbound
endpoint is exposed. Messages are plain text (no parse_mode) so user-supplied
names/emails can't inject formatting.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Callable

import requests

from server.database import get_conn

log = logging.getLogger("pdf-drm.telegram")

MAX_GROUP_BUTTONS = 40  # Telegram caps inline keyboards at 100 buttons
NOTIFY_BURST = 8  # max notifications per window (/request-access is public)
NOTIFY_WINDOW_S = 60
DEDUPE_S = 600  # same machine re-requesting within this window: no new ping

_lock = threading.Lock()
_selected: dict[int, set[str]] = {}  # request_id -> chosen group_ids
_sent_times: deque[float] = deque()
_last_by_fp: dict[str, float] = {}
_started = False

# Injected by app.py so approval reuses the exact dashboard logic.
_approve: Callable[[int, list[str], str], dict] | None = None
_reject: Callable[[int], dict] | None = None


# ---------- config ----------

def _token() -> str:
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()


def _allowed() -> set[int]:
    raw = os.environ.get("TELEGRAM_ALLOWED_USER_IDS", "")
    return {int(x) for x in raw.replace(" ", "").split(",") if x.strip().lstrip("-").isdigit()}


def _days() -> int:
    try:
        return max(1, int(os.environ.get("TELEGRAM_APPROVE_DAYS", "180")))
    except ValueError:
        return 180


def enabled() -> bool:
    return bool(_token())


# ---------- Telegram HTTP ----------

def _scrub(text: str) -> str:
    tok = _token()
    return text.replace(tok, "***") if tok else text


def _api(method: str, http_timeout: int = 15, **payload):
    """Call a Bot API method. Never lets the token leak into exception text.

    `http_timeout` is the socket timeout; `payload["timeout"]` (getUpdates) is Telegram's long-poll wait.
    """
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{_token()}/{method}", json=payload, timeout=http_timeout
        )
        data = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"{method} failed: {_scrub(type(exc).__name__ + ': ' + str(exc))}") from None
    if not data.get("ok"):
        raise RuntimeError(f"{method} failed: {_scrub(str(data.get('description')))}")
    return data["result"]


def _clean(value: str | None, limit: int) -> str:
    value = "".join(ch for ch in (value or "") if ch.isprintable())
    return value if len(value) <= limit else value[: limit - 1] + "…"


# ---------- data helpers ----------

def _requested_doc(req) -> tuple[str, str] | None:
    """The viewer sends the document's doc_id as the note when a file was denied."""
    note = (req["note"] or "").strip()
    if not note:
        return None
    with get_conn() as conn:
        row = conn.execute("SELECT doc_id, title FROM documents WHERE doc_id = ?", (note,)).fetchone()
    return (row["doc_id"], row["title"]) if row else None


def _groups(doc_id: str | None = None) -> list[dict]:
    """All groups; `has_doc` marks the ones containing the document the user asked for."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT g.group_id, g.name, COUNT(gd.doc_id) AS doc_count,
                      COALESCE(MAX(gd.doc_id = ?), 0) AS has_doc
               FROM groups g LEFT JOIN group_documents gd ON gd.group_id = g.group_id
               GROUP BY g.group_id ORDER BY g.name COLLATE NOCASE""",
            (doc_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def _group_docs(group_ids: set[str]) -> list[str]:
    if not group_ids:
        return []
    marks = ",".join("?" * len(group_ids))
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT doc_id FROM group_documents WHERE group_id IN ({marks})",
            tuple(group_ids),
        ).fetchall()
    return [r["doc_id"] for r in rows]


def _request(request_id: int):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM pending_requests WHERE id = ?", (request_id,)).fetchone()


# ---------- message building ----------

def _text(req, selected: set[str], groups: list[dict]) -> str:
    fp = req["machine_fingerprint"]
    lines = [
        f"Yêu cầu truy cập mới #{req['id']}",
        f"Tên: {_clean(req['username'], 60)}",
        f"Email: {_clean(req['email'], 80)}",
        f"Máy: {fp[:8]}...{fp[-4:]}",
    ]
    doc = _requested_doc(req)
    if doc:
        lines.append(f"Muốn mở tài liệu: {_clean(doc[1], 100)}")
    elif req["note"]:
        lines.append(f"Ghi chú: {_clean(req['note'], 100)}")
    lines.append(f"Thời gian: {datetime.now().astimezone():%Y-%m-%d %H:%M}")
    lines.append("")
    if groups:
        lines.append(f"Chọn nhóm tài liệu để chia sẻ (bấm để chọn/bỏ chọn), rồi bấm Duyệt. Hạn dùng: {_days()} ngày.")
        if any(g.get("has_doc") for g in groups):
            lines.append("★ = nhóm có chứa tài liệu người dùng muốn mở.")
        if len(groups) > MAX_GROUP_BUTTONS:
            lines.append(f"(Chỉ hiện {MAX_GROUP_BUTTONS}/{len(groups)} nhóm - các nhóm còn lại duyệt trên dashboard.)")
    else:
        lines.append("Chưa có nhóm tài liệu nào. Hãy tạo nhóm trên dashboard hoặc duyệt thủ công.")
    return "\n".join(lines)


def _keyboard(req_id: int, selected: set[str], groups: list[dict]) -> dict:
    rows = []
    for g in groups[:MAX_GROUP_BUTTONS]:
        mark = "[x]" if g["group_id"] in selected else "[ ]"
        star = "★ " if g.get("has_doc") else ""
        label = f"{mark} {star}{_clean(g['name'], 40)} ({g['doc_count']} tài liệu)"
        rows.append([{"text": label, "callback_data": f"t:{req_id}:{g['group_id']}"}])
    ok_label = f"Duyệt ({len(selected)} nhóm, {_days()} ngày)" if selected else "Duyệt (chọn nhóm trước)"
    rows.append([
        {"text": ok_label, "callback_data": f"ok:{req_id}"},
        {"text": "Từ chối", "callback_data": f"no:{req_id}"},
    ])
    return {"inline_keyboard": rows}


# ---------- outbound notification ----------

def _may_notify(fingerprint: str) -> bool:
    now = time.monotonic()
    with _lock:
        if now - _last_by_fp.get(fingerprint, -1e9) < DEDUPE_S:
            return False
        while _sent_times and now - _sent_times[0] > NOTIFY_WINDOW_S:
            _sent_times.popleft()
        if len(_sent_times) >= NOTIFY_BURST:
            return False
        _sent_times.append(now)
        _last_by_fp[fingerprint] = now
        if len(_last_by_fp) > 5000:  # bound memory under abuse
            for k in sorted(_last_by_fp, key=_last_by_fp.get)[:2500]:
                _last_by_fp.pop(k, None)
    return True


def notify_new_request(request_id: int, fingerprint: str) -> None:
    """Called in the background after /request-access stores a request.
    Must never raise: a Telegram outage must not affect the request itself."""
    if not enabled() or not _allowed():
        return
    try:
        if not _may_notify(fingerprint):
            log.info("notification for request %s suppressed (rate limit / duplicate)", request_id)
            return
        req = _request(request_id)
        if req is None or req["status"] != "pending":
            return
        doc = _requested_doc(req)
        groups = _groups(doc[0] if doc else None)
        for user_id in _allowed():
            try:
                _api("sendMessage", chat_id=user_id, text=_text(req, set(), groups),
                     reply_markup=_keyboard(request_id, set(), groups))
            except Exception as exc:  # noqa: BLE001
                log.warning("could not notify user %s: %s", user_id, exc)
    except Exception as exc:  # noqa: BLE001
        log.warning("notify_new_request failed: %s", _scrub(str(exc)))


# ---------- inbound: button presses ----------

def _edit(chat_id, message_id, text: str, markup: dict | None = None) -> None:
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    payload["reply_markup"] = markup if markup is not None else {"inline_keyboard": []}
    try:
        _api("editMessageText", **payload)
    except RuntimeError as exc:
        if "not modified" not in str(exc):
            log.warning("edit failed: %s", exc)


def _who(user: dict) -> str:
    return _clean(f"@{user['username']}" if user.get("username") else user.get("first_name", str(user["id"])), 40)


def _handle_callback(cb: dict) -> None:
    user = cb.get("from") or {}
    msg = cb.get("message") or {}
    chat_id, message_id = (msg.get("chat") or {}).get("id"), msg.get("message_id")
    cb_id = cb["id"]

    if user.get("id") not in _allowed():
        log.warning("callback from non-allowed user %s ignored", user.get("id"))
        _api("answerCallbackQuery", callback_query_id=cb_id, text="Bạn không có quyền thực hiện.", show_alert=True)
        return

    parts = (cb.get("data") or "").split(":")
    action = parts[0]
    try:
        req_id = int(parts[1])
    except (IndexError, ValueError):
        _api("answerCallbackQuery", callback_query_id=cb_id)
        return

    req = _request(req_id)
    if req is None:
        _api("answerCallbackQuery", callback_query_id=cb_id, text="Không tìm thấy yêu cầu.", show_alert=True)
        return
    if req["status"] != "pending":
        _api("answerCallbackQuery", callback_query_id=cb_id, text=f"Yêu cầu đã được xử lý ({req['status']}).")
        _edit(chat_id, message_id, f"Yêu cầu #{req_id} ({_clean(req['username'], 60)}): đã {req['status']}.")
        return

    doc = _requested_doc(req)
    groups = _groups(doc[0] if doc else None)
    if action == "t" and len(parts) == 3:
        gid = parts[2]
        with _lock:
            chosen = _selected.setdefault(req_id, set())
            chosen.symmetric_difference_update({gid} & {g["group_id"] for g in groups})
            snapshot = set(chosen)
        _api("answerCallbackQuery", callback_query_id=cb_id)
        _edit(chat_id, message_id, _text(req, snapshot, groups), _keyboard(req_id, snapshot, groups))

    elif action == "ok":
        with _lock:
            chosen = set(_selected.get(req_id, set())) & {g["group_id"] for g in groups}
        if not chosen:
            _api("answerCallbackQuery", callback_query_id=cb_id, text="Hãy chọn ít nhất 1 nhóm.", show_alert=True)
            return
        doc_ids = _group_docs(chosen)
        expires = (datetime.now(timezone.utc) + timedelta(days=_days())).isoformat()
        try:
            result = _approve(req_id, doc_ids, expires)  # same code path as the dashboard
        except Exception as exc:  # noqa: BLE001
            detail = getattr(exc, "detail", str(exc))
            _api("answerCallbackQuery", callback_query_id=cb_id, text=f"Lỗi: {_clean(str(detail), 150)}", show_alert=True)
            return
        with _lock:
            _selected.pop(req_id, None)
        names = ", ".join(_clean(g["name"], 40) for g in groups if g["group_id"] in chosen)
        _api("answerCallbackQuery", callback_query_id=cb_id, text="Đã duyệt.")
        _edit(chat_id, message_id,
              f"ĐÃ DUYỆT yêu cầu #{req_id}\n{_clean(req['username'], 60)} <{_clean(req['email'], 80)}>\n"
              f"Nhóm: {names}\n{len(result.get('granted', []))} tài liệu, hạn dùng {_days()} ngày "
              f"(đến {(datetime.now() + timedelta(days=_days())):%Y-%m-%d}).\nBởi {_who(user)}.")
        log.info("request %s approved via telegram by %s: groups=%s docs=%s",
                 req_id, user.get("id"), sorted(chosen), len(result.get("granted", [])))

    elif action == "no":
        try:
            _reject(req_id)
        except Exception as exc:  # noqa: BLE001
            detail = getattr(exc, "detail", str(exc))
            _api("answerCallbackQuery", callback_query_id=cb_id, text=f"Lỗi: {_clean(str(detail), 150)}", show_alert=True)
            return
        with _lock:
            _selected.pop(req_id, None)
        _api("answerCallbackQuery", callback_query_id=cb_id, text="Đã từ chối.")
        _edit(chat_id, message_id,
              f"ĐÃ TỪ CHỐI yêu cầu #{req_id}\n{_clean(req['username'], 60)} <{_clean(req['email'], 80)}>\nBởi {_who(user)}.")
        log.info("request %s rejected via telegram by %s", req_id, user.get("id"))
    else:
        _api("answerCallbackQuery", callback_query_id=cb_id)


def _handle_message(msg: dict) -> None:
    text = (msg.get("text") or "").strip().lower()
    user = msg.get("from") or {}
    if not (text.startswith("/start") or text.startswith("/id")):
        return
    uid = user.get("id")
    status = ("Bạn đã được cấp quyền nhận thông báo và duyệt yêu cầu."
              if uid in _allowed() else
              "Bạn CHƯA được cấp quyền. Gửi ID này cho quản trị viên để thêm vào TELEGRAM_ALLOWED_USER_IDS.")
    _api("sendMessage", chat_id=(msg.get("chat") or {}).get("id", uid),
         text=f"Telegram user ID của bạn: {uid}\n{status}")


def _poll_loop() -> None:
    offset = None
    backoff = 2
    while True:
        try:
            payload = {"timeout": 25, "allowed_updates": ["message", "callback_query"]}
            if offset is not None:
                payload["offset"] = offset
            updates = _api("getUpdates", http_timeout=40, **payload)
            backoff = 2
            for upd in updates:
                offset = upd["update_id"] + 1
                try:
                    if "callback_query" in upd:
                        _handle_callback(upd["callback_query"])
                    elif "message" in upd:
                        _handle_message(upd["message"])
                except Exception as exc:  # noqa: BLE001
                    log.warning("update %s failed: %s", upd.get("update_id"), _scrub(str(exc)))
        except Exception as exc:  # noqa: BLE001
            log.warning("polling error (retry in %ss): %s", backoff, _scrub(str(exc)))
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


def start(approve: Callable[[int, list[str], str], dict], reject: Callable[[int], dict]) -> None:
    """Begin polling in a daemon thread (no-op when disabled or already started)."""
    global _approve, _reject, _started
    _approve, _reject = approve, reject
    if not enabled():
        return
    if not _allowed():
        log.warning("TELEGRAM_BOT_TOKEN set but TELEGRAM_ALLOWED_USER_IDS empty: "
                    "bot only answers /start with the sender's ID, no notifications/approvals")
    if _started:
        return
    _started = True
    threading.Thread(target=_poll_loop, name="telegram-poll", daemon=True).start()
    log.info("telegram bot started (allowed users: %d, approve days: %d)", len(_allowed()), _days())
