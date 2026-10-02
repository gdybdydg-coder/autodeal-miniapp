"""A slow Telegram attempt must not make idle send workers wait for its batch."""
import asyncio
import threading
from types import SimpleNamespace

from backend import monitor, worker, telegram_setup


def test_slow_recipient_does_not_hold_next_jobs_for_other_workers(monkeypatch):
    blocked, release, progressed = threading.Event(), threading.Event(), threading.Event()
    lock = threading.Lock()
    calls = []
    def deliver(*args):
        with lock:
            number = len(calls)
            calls.append(number)
        if number == 0:
            blocked.set()
            assert release.wait(5)
        elif number >= 5:
            progressed.set()
        return "sent"
    monkeypatch.setattr(worker, "deliver_one", deliver)
    monkeypatch.setattr(worker, "enqueue", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker, "TelegramSender", lambda *args: None)
    monkeypatch.setattr(monitor.Monitor, "tick", lambda *args: False)
    monkeypatch.setattr(telegram_setup, "webhook_status", lambda *args: {"status": "configured"})
    settings = SimpleNamespace(live=True, monitor_enabled=True, bot_token="fixture-only",
        ria_active_window_enabled=False, ria_recent_publications_enabled=False,
        ria_ai_price_enabled=True, ria_confirmed_deals_only=True)
    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(monitor.run(None, settings, stop))
        try:
            assert await asyncio.to_thread(blocked.wait, 1)
            assert await asyncio.to_thread(progressed.wait, 1.5), (
                "One blocked recipient prevented idle workers from taking a fifth/sixth job")
            assert not release.is_set()
        finally:
            release.set()
            stop.set()
            await asyncio.wait_for(task, 3)
    asyncio.run(scenario())
