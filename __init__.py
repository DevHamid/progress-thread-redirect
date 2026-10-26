"""Redirect all progress bubbles to a dedicated Telegram thread.

Keeps the main chat clean: every tool-progress bubble (web_search, terminal,
delegate, etc.) is sent to thread 3235 of the target group instead of the
active conversation thread. A live activity bar (indeterminate, since the
iteration budget is unbounded) is rendered at the BOTTOM of the bubble and
ticks as tools run.
"""

import functools
import sys
import os
import json
import time
import asyncio

_LOG_PATH = os.environ.get('PROGRESS_PLUGIN_LOG', '/tmp/progress_plugin.log')
def _log(msg):
    try:
        with open(_LOG_PATH, 'a') as f:
            f.write(f"{msg}\n")
    except Exception:
        pass

# Target group chat id + thread id (from https://t.me/c/3628710660/3235).
TARGET_CHAT_ID = os.environ.get('PROGRESS_TARGET_CHAT_ID', "-1003628710660")
TARGET_THREAD_ID = os.environ.get('PROGRESS_TARGET_THREAD_ID', "3235")

FILLED = "▬"
EMPTY = "▭"
BAR_LENGTH = 10

_turn_progress_fill = 0
_last_sent_mid = None

# --- Persistent store for 24h auto-delete of progress bubbles. ---
# Each sent progress message is recorded as {message_id, sent_ts}. A background
# sweep (piggybacked on each send) deletes any message older than AUTO_DELETE_S.
AUTO_DELETE_S = 24 * 3600
_HISTORY_PATH = os.environ.get('PROGRESS_PLUGIN_HISTORY', '/tmp/progress_bubble_history.json')
_deleted_mids = set()


def _load_history():
    try:
        with open(_HISTORY_PATH) as f:
            return json.load(f)
    except Exception:
        return []


def _trim_history(h):
    """Keep entries younger than 2x the delete window. Count caps evict entries
    before they reach AUTO_DELETE_S when send volume is high, orphaning them."""
    cutoff = time.time() - 2 * AUTO_DELETE_S
    return [e for e in h if float(e.get("sent_ts", 0)) >= cutoff][-5000:]


def _append_history(mid, ts):
    try:
        h = _load_history()
        h.append({"message_id": str(mid), "sent_ts": ts})
        h = _trim_history(h)
        with open(_HISTORY_PATH, "w") as f:
            json.dump(h, f)
    except Exception as e:
        _log(f"HISTORY append err={e!r}")

def _render_bar(filled: int) -> str:
    """Return a left-to-right progress bar with `filled` filled cells (0..10).

    Unlike the old rolling-block, filled cells accumulate from the LEFT so it
    reads like a real progress bar. Percentage is derived from the fill level
    (unbounded budget, so it's an activity meter that fills and resets).
    """
    try:
        n = int(filled)
    except (TypeError, ValueError):
        n = 0
    n = max(0, min(n, BAR_LENGTH))
    return FILLED * n + EMPTY * (BAR_LENGTH - n)


def _pct(filled: int) -> int:
    try:
        n = int(filled)
    except (TypeError, ValueError):
        n = 0
    return max(0, min(n, BAR_LENGTH)) * 10


async def _sweep_expired(adapter, chat_id):
    """Delete recorded progress bubbles older than AUTO_DELETE_S. Returns count deleted."""
    now = time.time()
    h = _load_history()
    if not h:
        return 0
    stale = [e for e in h if now - float(e.get("sent_ts", 0)) >= AUTO_DELETE_S]
    if not stale:
        return 0
    deleted = 0
    for e in stale:
        mid = e.get("message_id")
        if not mid or mid in _deleted_mids:
            continue
        try:
            rdel = await adapter.delete_message(chat_id=chat_id, message_id=mid)
            ok = bool(rdel)
            _log(f"SWEEP delete mid={mid} ok={ok}")
            if ok:
                _deleted_mids.add(mid)
                deleted += 1
        except Exception as ex:
            _log(f"SWEEP delete mid={mid} err={ex!r}")
    # prune processed entries from history (time-trimmed, unbounded count)
    keep = [e for e in h if str(e.get("message_id")) not in _deleted_mids]
    try:
        with open(_HISTORY_PATH, "w") as f:
            json.dump(_trim_history(keep), f)
    except Exception as e:
        _log(f"SWEEP prune err={e!r}")
    return deleted


def _patch_turnrunner_progress() -> None:
    from gateway.run_turn_runner import TurnRunner

    # --- 1. Patch _progress_text to append the bar at the bottom on EVERY
    # render (send and re-edit), so the bar ticks in place.
    # NOTE: _progress_text is a @staticmethod — no self. The bar animates
    # from the number of accrued progress lines, which grows per tool. ---
    orig_text = TurnRunner._progress_text

    if not getattr(orig_text, "_progress_thread_redirect_patched", False):
        @staticmethod
        def _progress_text(lines):
            # lines is typically a single multi-line string (caption + command).
            # Keep it all as the message body; bar first... no — bar goes below
            # the body, then a short context line.
            base = orig_text(lines) if lines else ""
            global _turn_progress_fill
            fill = min(_turn_progress_fill, BAR_LENGTH)
            _turn_progress_fill += 1
            bar = f"{_render_bar(fill)} {_pct(fill)}%"
            # Strip any previously-rendered bar/caption so we never double-render.
            import re as _re
            _clean = []
            for _ln in base.split("\n"):
                if _re.fullmatch(r"[▬▭]{10} \d+%", _ln.strip()):
                    continue
                if _ln.strip() == "Working…":
                    continue
                _clean.append(_ln)
            base = "\n".join(_clean).rstrip("\n")
            body = f"{base}\n{bar}" if base else bar
            out = f"{body}\nWorking…"
            _log(f"RENDER lines={lines!r} out_len={len(out)}")
            return out

        _progress_text._progress_thread_redirect_patched = True
        TurnRunner._progress_text = _progress_text

    # --- 1b. Patch _edit_progress_message to edit in the target chat. ---
    orig_edit = TurnRunner._edit_progress_message

    if not getattr(orig_edit, "_progress_thread_redirect_patched", False):
        @functools.wraps(orig_edit)
        async def _edit_progress_message(self, st, message_id: str, content: str):
            ctx = self._ctx
            # Render the bar from the accumulated buffer, not the raw join.
            content = self._progress_text(st.progress_lines)
            kwargs = {"chat_id": TARGET_CHAT_ID, "message_id": message_id, "content": content}
            if getattr(st.adapter, "REQUIRES_EDIT_FINALIZE", False):
                kwargs["finalize"] = True
            if st._edit_accepts_metadata:
                metadata = dict(ctx._progress_metadata or {})
                metadata["thread_id"] = TARGET_THREAD_ID
                kwargs["metadata"] = metadata
            _log(f"EDIT target={TARGET_CHAT_ID} thread={TARGET_THREAD_ID} mid={message_id} len={len(content)} first={content!r}")
            r = await st.adapter.edit_message(**kwargs)
            _log(f"EDIT result success={r.success} err={getattr(r,'error',None)} retry={getattr(r,'retryable',None)}")
            return r

        _edit_progress_message._progress_thread_redirect_patched = True
        TurnRunner._edit_progress_message = _edit_progress_message

    # --- 2. Patch _send_progress_text for the cross-chat redirect. ---
    orig_send = TurnRunner._send_progress_text

    if not getattr(orig_send, "_progress_thread_redirect_patched", False):
        @functools.wraps(orig_send)
        async def _send_progress_text(self, st, text: str):
            ctx = self._ctx
            metadata = dict(ctx._progress_metadata or {})
            metadata["thread_id"] = TARGET_THREAD_ID
            metadata.pop("telegram_reply_to_message_id", None)
            metadata.pop("direct_messages_topic_id", None)
            metadata.pop("telegram_dm_topic_reply_fallback", None)
            reply_to = None if str(ctx.source.chat_id) != TARGET_CHAT_ID else ctx._progress_reply_to
            # Render the bar from the accumulated buffer instead of the raw join.
            content = self._progress_text(st.progress_lines)
            _log(f"SEND target={TARGET_CHAT_ID} thread={TARGET_THREAD_ID} len={len(content)} first={content!r}")
            result = await st.adapter.send(
                chat_id=TARGET_CHAT_ID, content=content, reply_to=reply_to,
                metadata=metadata,
            )
            _log(f"SEND result success={result.success} mid={getattr(result,'message_id',None)} err={getattr(result,'error',None)} retry={getattr(result,'retryable',None)}")
            self._track_progress_result(result)
            # Record each bubble for the 24h auto-delete sweep. No immediate
            # deletion: bubbles accumulate and are purged once >24h old.
            new_mid = getattr(result, 'message_id', None)
            if new_mid:
                _append_history(new_mid, time.time())
            try:
                n_del = await _sweep_expired(st.adapter, TARGET_CHAT_ID)
                if n_del:
                    _log(f"SWEEP removed {n_del} expired bubble(s)")
            except Exception as e:
                _log(f"SWEEP call err={e!r}")
            return result

        _send_progress_text._progress_thread_redirect_patched = True
        TurnRunner._send_progress_text = _send_progress_text


    # --- 3. Instrument _progress_emit (queue feed) to prove whether progress even fires. ---
    orig_emit = TurnRunner._progress_emit
    def _progress_emit(self, msg):
        _log(f"EMIT fired msg={msg[:80]!r} queue={self._ctx.progress_queue is not None}")
        return orig_emit(self, msg)
    _progress_emit._progress_thread_redirect_patched = True
    TurnRunner._progress_emit = _progress_emit

    # --- 4. Instrument send_progress_messages (drain) entry. ---
    orig_drain = TurnRunner.send_progress_messages
    async def send_progress_messages(self):
        ctx = self._ctx
        global _turn_progress_fill
        _turn_progress_fill = 0
        _log(f"DRAIN enter ctx={ctx is not None} queue={ctx.progress_queue is not None if ctx else 'n/a'} "
             f"still_current={ctx._run_still_current() if ctx else 'n/a'}")
        r = await orig_drain(self)
        _log(f"DRAIN exit r={r}")
        return r
    send_progress_messages._progress_thread_redirect_patched = True
    TurnRunner.send_progress_messages = send_progress_messages



    # --- 5. Instrument _progress_send_or_edit entry/exit. ---
    orig_send_or_edit = TurnRunner._progress_send_or_edit
    async def _progress_send_or_edit(self, st, msg):
        _log(f"SEND_OR_EDIT enter msg={str(msg)[:60]!r} can_edit={st.can_edit} mid={st.progress_msg_id} lines={len(st.progress_lines)}")
        try:
            r = await orig_send_or_edit(self, st, msg)
            _log(f"SEND_OR_EDIT exit ok={r}")
            return r
        except Exception as e:
            _log(f"SEND_OR_EDIT EXC {type(e).__name__}: {e}")
            raise
    _progress_send_or_edit._progress_thread_redirect_patched = True
    TurnRunner._progress_send_or_edit = _progress_send_or_edit



def register(ctx) -> None:
    import uuid
    with open("/tmp/pg_register_marker.txt","a") as f:
        f.write(uuid.uuid4().hex[:8] + "\n")
    try:
        _patch_turnrunner_progress()
    except Exception:
        import logging
        logging.getLogger(__name__).exception("progress-thread-redirect: patch failed")


__all__ = ["register"]
