"""Cron delivery to stateless (api_server) origins — the transcript-persist lane.

``APIServerAdapter.send()`` is a deliberate stub (``supports_async_delivery = False``; the
surface is stateless HTTP request/response), so a cron job created from a desktop/api session
with ``deliver: origin`` could NEVER deliver: the live lane logged "API server uses HTTP
request/response, not send()" and the standalone lane has no api_server sender. The fix routes
stateless targets to a transcript-persist lane — the client polls
``GET /api/sessions/{id}/messages`` and sees the delivery with no agent turn (the #85957
mechanism).

Contract pinned here:
* a target whose adapter declares ``supports_async_delivery = False`` never gets ``send()``
  called; the delivery text lands in the session transcript (role=user, cron-labelled) and the
  lane reports delivered;
* a push-capable adapter still takes the normal live-send lane — no transcript shortcut.
"""

from unittest.mock import MagicMock

from cron import scheduler_delivery as sched_delivery
from gateway.platforms.base import SendResult


class _StatelessAdapter:
    """Mirrors APIServerAdapter: push unsupported, send() is a failing stub."""

    supports_async_delivery = False

    def __init__(self):
        self.send_calls = 0

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.send_calls += 1
        return SendResult(
            success=False, error="API server uses HTTP request/response, not send()")


def _make_target(adapter, *, chat_id="76970c38-0000-0000-0000-000000000000"):
    return sched_delivery._TargetDelivery(
        job={"id": "d2357d831e71", "name": "irene-availability-save"},
        platform=None,
        platform_name="api_server",
        chat_id=chat_id,
        thread_id=None,
        transport=None,
        pconfig=None,
        runtime_adapter=adapter,
        target_adapters=None,
        config=None,
        loop=None,
        notify_delivery=False,
        origin={"platform": "api_server", "chat_id": chat_id},
        origin_target=True,
        origin_user_id=None,
        is_dm_target=True,
        mirror_text="",
        mirror_this_target=False,
        in_channel_surface=False,
        inchannel_continuable=False,
        opened_thread_id=None,
        live_adapter_ready=True,
    )


def test_stateless_target_persists_delivery_to_session_transcript():
    adapter = _StatelessAdapter()
    chat_id = "76970c38-0000-0000-0000-000000000000"
    # The origin session exists in production (the job was created from it); messages
    # are FK-bound to the sessions row, so create it the way the api_server would.
    from hermes_state_registry import acquire, release_or_close
    db = acquire()
    try:
        db.create_session(chat_id, source="api_server")
    finally:
        release_or_close(db)

    t = _make_target(adapter, chat_id=chat_id)
    target_errors: list = []
    delivery_errors: list = []

    delivered = sched_delivery._deliver_via_live_adapter(
        t, "\U0001F4EC New availability file arrived", [],
        target_errors=target_errors, delivery_errors=delivery_errors,
        unverified_targets=[])

    assert delivered is True
    assert adapter.send_calls == 0
    assert target_errors == []

    db = acquire()
    try:
        messages = db.get_messages(chat_id)
    finally:
        release_or_close(db)
    assert messages, "no transcript rows written for the origin session"
    last = messages[-1]
    assert last["role"] == "user"
    assert last["content"].startswith("[Cron delivery: irene-availability-save]")
    assert "\U0001F4EC New availability file arrived" in last["content"]


def test_push_capable_target_still_uses_live_send_lane(monkeypatch):
    adapter = MagicMock()  # no supports_async_delivery attr → push-capable by default
    t = _make_target(adapter)
    sent: dict = {}

    def fake_live_send_text(t_, text_to_send, route_thread_id, route_metadata, **kwargs):
        sent["text"] = text_to_send
        return True, False, "mid-1"

    monkeypatch.setattr(sched_delivery, "_live_send_text", fake_live_send_text)
    monkeypatch.setattr(sched_delivery, "_seed_live_delivery_sessions", lambda *a, **k: None)
    import gateway.mirror as mirror_mod
    mirror_calls: list = []
    monkeypatch.setattr(
        mirror_mod, "mirror_to_session", lambda *a, **k: mirror_calls.append((a, k)) or True)

    delivered = sched_delivery._deliver_via_live_adapter(
        t, "report text", [],
        target_errors=[], delivery_errors=[], unverified_targets=[])

    assert delivered is True
    assert sent.get("text") == "report text"
    assert mirror_calls == []
