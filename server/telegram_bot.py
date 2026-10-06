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
    TELEGRAM_APPROVE_DAYS      default license lifetime in days (default 180); the admin can pick 30/60/180 or type a number per request

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
DEDUPE_S = 600  # same machine sending the same request again within this window: no new ping
DAY_PRESETS = (30, 60, 180)
MAX_DAYS = 3650

_lock = threading.Lock()
_selected: dict[int, set[str]] = {}  # request_id -> chosen group_ids, plus "doc" = the requested document
_days_sel: dict[int, int] = {}  # request_id -> license days chosen by the admin (default TELEGRAM_APPROVE_DAYS)
_awaiting: dict[int, tuple[int, int, int]] = {}  # admin user id -> (request_id, chat_id, message_id) awaiting a typed number of days
_sent_times: deque[float] = deque()
_last_by_key: dict[str, float] = {}
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


def _day_options() -> list[int]:
    return sorted(set(DAY_PRESETS) | {_days()})


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

def _text(req, selected: set[str], groups: list[dict], days: int) -> str:
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
    lines.append(f"Hạn dùng: {days} ngày (đến {(datetime.now() + timedelta(days=days)):%Y-%m-%d}).")
    if doc:
        lines.append("Bấm 'Chỉ tài liệu' để cấp đúng tài liệu này, và/hoặc chọn thêm nhóm, rồi bấm Duyệt.")
    elif groups:
        lines.append("Chọn nhóm tài liệu để chia sẻ (bấm để chọn/bỏ chọn), rồi bấm Duyệt.")
    if groups:
        if any(g.get("has_doc") for g in groups):
            lines.append("★ = nhóm có chứa tài liệu người dùng muốn mở.")
        if len(groups) > MAX_GROUP_BUTTONS:
            lines.append(f"(Chỉ hiện {MAX_GROUP_BUTTONS}/{len(groups)} nhóm - các nhóm còn lại duyệt trên dashboard.)")
    elif not doc:
        lines.append("Chưa có nhóm tài liệu nào. Hãy tạo nhóm trên dashboard hoặc duyệt thủ công.")
    return "\n".join(lines)


def _keyboard(req_id: int, selected: set[str], groups: list[dict], days: int,
              doc: tuple[str, str] | None = None) -> dict:
    rows = []
    if doc:
        mark = "[x]" if "doc" in selected else "[ ]"
        rows.append([{"text": f"{mark} Chỉ tài liệu: {_clean(doc[1], 45)}", "callback_data": f"d:{req_id}"}])
    for g in groups[:MAX_GROUP_BUTTONS]:
        mark = "[x]" if g["group_id"] in selected else "[ ]"
        star = "★ " if g.get("has_doc") else ""
        label = f"{mark} {star}{_clean(g['name'], 40)} ({g['doc_count']} tài liệu)"
        rows.append([{"text": label, "callback_data": f"t:{req_id}:{g['group_id']}"}])
    options = _day_options()
    rows.append([{"text": ("● " if d == days else "") + f"{d} ngày", "callback_data": f"e:{req_id}:{d}"}
                 for d in options])
    custom = f"Tự nhập số ngày (đang: {days})" if days not in options else "Tự nhập số ngày..."
    rows.append([{"text": custom, "callback_data": f"ec:{req_id}"}])
    ok_label = f"Duyệt ({len(selected)} mục, {days} ngày)" if selected else "Duyệt (chọn tài liệu/nhóm trước)"
    rows.append([
        {"text": ok_label, "callback_data": f"ok:{req_id}"},
        {"text": "Từ chối", "callback_data": f"no:{req_id}"},
    ])
    return {"inline_keyboard": rows}


def _view(req_id: int, req):
    """Everything needed to (re)draw a request message and validate presses."""
    doc = _requested_doc(req)
    groups = _groups(doc[0] if doc else None)
    valid = {g["group_id"] for g in groups} | ({"doc"} if doc else set())
    with _lock:
        selected = set(_selected.get(req_id, set())) & valid
        days = _days_sel.get(req_id, _days())
    return doc, groups, valid, selected, days


# ---------- outbound notification ----------

def _may_notify(key: str) -> bool:
    now = time.monotonic()
    with _lock:
        if now - _last_by_key.get(key, -1e9) < DEDUPE_S:
            return False
        while _sent_times and now - _sent_times[0] > NOTIFY_WINDOW_S:
            _sent_times.popleft()
        if len(_sent_times) >= NOTIFY_BURST:
            return False
        _sent_times.append(now)
        _last_by_key[key] = now
        if len(_last_by_key) > 5000:  # bound memory under abuse
            for k in sorted(_last_by_key, key=_last_by_key.get)[:2500]:
                _last_by_key.pop(k, None)
    return True


def notify_new_request(request_id: int, fingerprint: str) -> None:
    """Called in the background after /request-access stores a request.
    Must never raise: a Telegram outage must not affect the request itself."""
    if not enabled() or not _allowed():
        return
    try:
        req = _request(request_id)
        if req is None or req["status"] != "pending":
            return
        # Dedupe per (machine, note): a document request right after the
        # first-run registration from the same machine is a different request.
        if not _may_notify(f"{fingerprint}|{(req['note'] or '')[:80]}"):
            log.info("notification for request %s suppressed (rate limit / duplicate)", request_id)
            return
        doc = _requested_doc(req)
        if doc:  # the user asked for this document: pre-select it, the admin still has to press Duyệt
            with _lock:
                _selected[request_id] = {"doc"}
        doc, groups, _valid, selected, days = _view(request_id, req)
        for user_id in _allowed():
            try:
                _api("sendMessage", chat_id=user_id, text=_text(req, selected, groups, days),
                     reply_markup=_keyboard(request_id, selected, groups, days, doc))
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


def _refresh(req_id: int, req, chat_id, message_id) -> None:
    doc, groups, _valid, selected, days = _view(req_id, req)
    _edit(chat_id, message_id, _text(req, selected, groups, days), _keyboard(req_id, selected, groups, days, doc))


def _who(user: dict) -> str:
    return _clean(f"@{user['username']}" if user.get("username") else user.get("first_name", str(user["id"])), 40)


def _forget(req_id: int) -> None:
    with _lock:
        _selected.pop(req_id, None)
        _days_sel.pop(req_id, None)
        for uid in [u for u, v in _awaiting.items() if v[0] == req_id]:
            _awaiting.pop(uid, None)


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
        _forget(req_id)
        return

    doc, groups, valid, selected, days = _view(req_id, req)

    if action in ("t", "d"):
        key = parts[2] if action == "t" and len(parts) == 3 else "doc"
        with _lock:
            chosen = _selected.setdefault(req_id, set())
            chosen.symmetric_difference_update({key} & valid)
        _api("answerCallbackQuery", callback_query_id=cb_id)
        _refresh(req_id, req, chat_id, message_id)

    elif action == "e" and len(parts) == 3:
        try:
            chosen_days = int(parts[2])
        except ValueError:
            chosen_days = 0
        if chosen_days in _day_options():
            with _lock:
                _days_sel[req_id] = chosen_days
        _api("answerCallbackQuery", callback_query_id=cb_id)
        _refresh(req_id, req, chat_id, message_id)

    elif action == "ec":
        with _lock:
            _awaiting[user["id"]] = (req_id, chat_id, message_id)
        _api("answerCallbackQuery", callback_query_id=cb_id,
             text=f"Gửi số ngày (1-{MAX_DAYS}) cho bot trong chat này.", show_alert=True)

    elif action == "ok":
        if not selected:
            _api("answerCallbackQuery", callback_query_id=cb_id, text="Hãy chọn ít nhất 1 tài liệu hoặc nhóm.", show_alert=True)
            return
        group_ids = {s for s in selected if s != "doc"}
        doc_ids = _group_docs(group_ids)
        if "doc" in selected and doc and doc[0] not in doc_ids:
            doc_ids.append(doc[0])
        expires = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
        try:
            result = _approve(req_id, doc_ids, expires)  # same code path as the dashboard
        except Exception as exc:  # noqa: BLE001
            detail = getattr(exc, "detail", str(exc))
            _api("answerCallbackQuery", callback_query_id=cb_id, text=f"Lỗi: {_clean(str(detail), 150)}", show_alert=True)
            return
        _forget(req_id)
        what = [f"Tài liệu: {_clean(doc[1], 60)}"] if "doc" in selected and doc else []
        names = ", ".join(_clean(g["name"], 40) for g in groups if g["group_id"] in group_ids)
        if names:
            what.append(f"Nhóm: {names}")
        _api("answerCallbackQuery", callback_query_id=cb_id, text="Đã duyệt.")
        _edit(chat_id, message_id,
              f"ĐÃ DUYỆT yêu cầu #{req_id}\n{_clean(req['username'], 60)} <{_clean(req['email'], 80)}>\n"
              + "\n".join(what)
              + f"\n{len(result.get('granted', []))} tài liệu, hạn dùng {days} ngày "
              f"(đến {(datetime.now() + timedelta(days=days)):%Y-%m-%d}).\nBởi {_who(user)}.")
        log.info("request %s approved via telegram by %s: groups=%s doc=%s granted=%s days=%s",
                 req_id, user.get("id"), sorted(group_ids), "doc" in selected, len(result.get("granted", [])), days)

    elif action == "no":
        try:
            _reject(req_id)
        except Exception as exc:  # noqa: BLE001
            detail = getattr(exc, "detail", str(exc))
            _api("answerCallbackQuery", callback_query_id=cb_id, text=f"Lỗi: {_clean(str(detail), 150)}", show_alert=True)
            return
        _forget(req_id)
        _api("answerCallbackQuery", callback_query_id=cb_id, text="Đã từ chối.")
        _edit(chat_id, message_id,
              f"ĐÃ TỪ CHỐI yêu cầu #{req_id}\n{_clean(req['username'], 60)} <{_clean(req['email'], 80)}>\nBởi {_who(user)}.")
        log.info("request %s rejected via telegram by %s", req_id, user.get("id"))
    else:
        _api("answerCallbackQuery", callback_query_id=cb_id)


def _handle_message(msg: dict) -> None:
    raw = (msg.get("text") or "").strip()
    text = raw.lower()
    user = msg.get("from") or {}
    uid = user.get("id")
    chat = (msg.get("chat") or {}).get("id", uid)

    # Admin pressed "Tự nhập số ngày" and now sends the number.
    if uid in _allowed() and raw.isdigit():
        with _lock:
            waiting = _awaiting.pop(uid, None)
        if waiting:
            req_id, msg_chat, msg_id = waiting
            days = int(raw)
            req = _request(req_id)
            if req is None or req["status"] != "pending":
                _api("sendMessage", chat_id=chat, text="Yêu cầu này đã được xử lý.")
            elif not 1 <= days <= MAX_DAYS:
                with _lock:
                    _awaiting[uid] = waiting
                _api("sendMessage", chat_id=chat, text=f"Số ngày phải từ 1 đến {MAX_DAYS}. Gửi lại số ngày.")
            else:
                with _lock:
                    _days_sel[req_id] = days
                _refresh(req_id, req, msg_chat, msg_id)
                _api("sendMessage", chat_id=chat, text=f"Đã đặt hạn dùng {days} ngày cho yêu cầu #{req_id}.")
        return

    if not (text.startswith("/start") or text.startswith("/id")):
        return
    status = ("Bạn đã được cấp quyền nhận thông báo và duyệt yêu cầu."
              if uid in _allowed() else
              "Bạn CHƯA được cấp quyền. Gửi ID này cho quản trị viên để thêm vào TELEGRAM_ALLOWED_USER_IDS.")
    _api("sendMessage", chat_id=chat, text=f"Telegram user ID của bạn: {uid}\n{status}")


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
