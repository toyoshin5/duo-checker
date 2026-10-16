"""状態遷移（重複通知の抑止・再入荷時の再通知）と、ローカル SMTP サーバーへの実送信のテスト。"""

import smtplib
import socket
import time
from datetime import datetime, timedelta
from email import message_from_bytes, policy
from pathlib import Path

import pytest
from aiosmtpd.controller import Controller

from duo_checker.catalog import STORES, Variant
from duo_checker.inventory import InventoryError, StoreAvailability
from duo_checker.monitor import JST, Monitor
from duo_checker.notifier import Notifier, send_smtp
from duo_checker.settings import SmtpSettings
from duo_checker.state import StateStore

PART = "MK244J/A"


class Inbox:
    def __init__(self) -> None:
        self.messages = []

    async def handle_DATA(self, server, session, envelope):
        self.messages.append(message_from_bytes(envelope.content, policy=policy.default))
        return "250 OK"


@pytest.fixture
def smtp_server():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    inbox = Inbox()
    # server_hostname を固定して、起動時の逆引き（socket.getfqdn）で待たされないようにする
    controller = Controller(inbox, hostname="127.0.0.1", port=port, server_hostname="localhost", ready_timeout=10)
    controller.start()
    yield inbox, port
    controller.stop()


class FakeSource:
    """店舗ごとの在庫状態を外から書き換えられる在庫ソース。"""

    def __init__(self, stores: list[str]) -> None:
        self.status = {s: False for s in stores}
        self.failing: set[str] = set()
        self.calls: list[str] = []

    def fetch(self, parts, store):
        self.calls.append(store)
        if store in self.failing:
            raise InventoryError("status=541")
        return [
            StoreAvailability(s, STORES[s], p, self.status[s], "available" if self.status[s] else "unavailable",
                              "本日" if self.status[s] else "現在ご利用いただけません", "")
            for s in self.status
            if s not in self.failing
            for p in parts
        ]


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 10, 17, 10, 0, tzinfo=JST)

    def __call__(self) -> datetime:
        return self.t


def make_monitor(tmp_path: Path, port: int, stores: list[str], send=None):
    smtp = SmtpSettings("127.0.0.1", port, "none", "", "", "duo@example.com")
    kwargs = {"send": send} if send else {}
    notifier = Notifier(smtp, ["me@example.com"], tmp_path / "var", tmp_path / "logs", sleep=lambda _: None, **kwargs)
    source = FakeSource(stores)
    clock = Clock()
    monitor = Monitor(
        [Variant("starwhite", "256gb")],
        stores,
        source,
        StateStore(tmp_path / "var" / "state.json"),
        notifier,
        error_alert_minutes=10,
        mac_notification=False,
        now=clock,
    )
    return monitor, source, clock


def cycle(monitor: Monitor):
    result = monitor.check_once()
    monitor._update_health(result.ok, "; ".join(result.errors))
    monitor.notifier.flush()
    return result


def test_restock_dedup_and_renotify(tmp_path, smtp_server):
    inbox, port = smtp_server
    monitor, source, clock = make_monitor(tmp_path, port, ["R005", "R718"])

    cycle(monitor)  # 在庫なし
    assert inbox.messages == []

    source.status["R005"] = True
    cycle(monitor)  # 復活 → 通知
    assert len(inbox.messages) == 1

    cycle(monitor)  # 在庫ありが継続 → 通知しない
    cycle(monitor)
    assert len(inbox.messages) == 1

    source.status["R005"] = False
    cycle(monitor)  # 売り切れ
    source.status["R005"] = True
    cycle(monitor)  # 再入荷 → 再通知
    assert len(inbox.messages) == 2

    source.status["R718"] = True
    cycle(monitor)  # 別の店舗で復活 → 通知
    assert len(inbox.messages) == 3
    assert "丸の内" in inbox.messages[2]["Subject"]


def test_email_contents(tmp_path, smtp_server):
    inbox, port = smtp_server
    monitor, source, clock = make_monitor(tmp_path, port, ["R005"])
    source.status["R005"] = True
    cycle(monitor)

    msg = inbox.messages[0]
    body = msg.get_content()
    assert msg["Subject"] == "【在庫あり】iPhone Duo 256GB スターホワイト — Apple 名古屋栄"
    assert msg["To"] == "me@example.com"
    for expected in ("Apple 名古屋栄（R005）", "iPhone Duo（SIMフリー）", "256GB", "スターホワイト", "受け取り: 本日",
                     "2026-10-17 10:00:00", "https://www.apple.com/jp/shop/buy-iphone/iphone-duo/"):
        assert expected in body, expected
    history = (tmp_path / "logs" / "notifications.jsonl").read_text(encoding="utf-8")
    assert '"status": "sent"' in history


def test_fetch_error_does_not_reset_state(tmp_path, smtp_server):
    """取得エラーで状態を「在庫なし」に戻さない（エラー明けに重複通知しない）。"""
    inbox, port = smtp_server
    monitor, source, clock = make_monitor(tmp_path, port, ["R005"])
    source.status["R005"] = True
    cycle(monitor)
    source.failing.add("R005")
    result = cycle(monitor)
    assert not result.ok
    source.failing.clear()
    cycle(monitor)
    assert len(inbox.messages) == 1


def test_state_survives_restart(tmp_path, smtp_server):
    inbox, port = smtp_server
    monitor, source, _ = make_monitor(tmp_path, port, ["R005"])
    source.status["R005"] = True
    cycle(monitor)
    monitor2, source2, _ = make_monitor(tmp_path, port, ["R005"])
    source2.status["R005"] = True
    cycle(monitor2)  # 再起動後も在庫ありが継続 → 通知しない
    assert len(inbox.messages) == 1


def test_nearby_grouping_skips_covered_stores(tmp_path, smtp_server):
    _, port = smtp_server
    monitor, source, _ = make_monitor(tmp_path, port, ["R718", "R079", "R224"])
    cycle(monitor)
    assert source.calls == ["R718"]  # 1 回の照会で近隣店舗もまとめて取得できる


def test_cycle_stops_at_first_failure(tmp_path, smtp_server):
    """ブロック中に照会を重ねないよう、失敗したらそのサイクルの残りの照会をやめる。"""
    _, port = smtp_server
    monitor, source, _ = make_monitor(tmp_path, port, ["R718", "R005", "R048"])
    source.failing.add("R718")
    result = cycle(monitor)
    assert source.calls == ["R718"]
    assert not result.ok and len(result.missing) == 3


def test_smtp_failure_is_retried_next_cycle(tmp_path, smtp_server):
    inbox, port = smtp_server
    attempts = {"n": 0}

    def flaky_send(smtp, msg):
        attempts["n"] += 1
        if attempts["n"] <= 3:  # 1 サイクル目の 3 回の試行はすべて失敗
            raise smtplib.SMTPServerDisconnected("connection lost")
        send_smtp(smtp, msg)

    monitor, source, _ = make_monitor(tmp_path, port, ["R005"], send=flaky_send)
    source.status["R005"] = True
    cycle(monitor)
    assert inbox.messages == []
    assert (tmp_path / "var" / "outbox.json").read_text() != "[]"
    cycle(monitor)  # 在庫ありが継続していても、未送信分は再送される
    assert len(inbox.messages) == 1
    assert "在庫あり" in inbox.messages[0]["Subject"]


def test_error_alert_and_recovery(tmp_path, smtp_server):
    inbox, port = smtp_server
    monitor, source, clock = make_monitor(tmp_path, port, ["R005"])
    source.failing.add("R005")
    for _ in range(10):
        cycle(monitor)
        clock.t += timedelta(minutes=1)
    assert inbox.messages == []
    cycle(monitor)  # 10 分経過 → 警告
    cycle(monitor)
    assert [m["Subject"] for m in inbox.messages] == ["【duo-checker】在庫を取得できていません"]
    source.failing.clear()
    cycle(monitor)
    assert inbox.messages[-1]["Subject"] == "【duo-checker】在庫の監視が復旧しました"
    assert len(inbox.messages) == 2


def test_run_forever_survives_unexpected_errors(tmp_path, smtp_server):
    _, port = smtp_server
    monitor, source, _ = make_monitor(tmp_path, port, ["R005"])
    monitor.interval = 0
    calls = {"n": 0}

    def boom(parts, store):
        calls["n"] += 1
        if calls["n"] >= 3:
            monitor.stop_event.set()
        raise RuntimeError("unexpected")

    source.fetch = boom
    started = time.monotonic()
    monitor.run_forever()  # 例外で落ちずに回り続け、stop_event で終了する
    assert calls["n"] == 3
    assert time.monotonic() - started < 5
