"""SafariSession の再試行・復旧の挙動（osascript を差し替えて検証）。"""

import json

import pytest

from duo_checker import safari
from duo_checker.inventory import InventoryError
from duo_checker.safari import SafariSession, SafariSetupError, WindowGone
from duo_checker.state import StateStore

OK_BODY = json.dumps({"body": {"content": {"pickupMessage": {"stores": []}}}})


class FakeSafari:
    def __init__(self, responses):
        self.responses = list(responses)  # run_js の戻り値（status, body）を順に返す
        self.calls: list[str] = []
        self.window_exists = True

    def __call__(self, script, *args, timeout=60):
        self.calls.append(script)
        if script == "ensure_window.applescript":
            return f"{args[0] if args[0] != '0' else 7}|{'existing' if args[0] != '0' else 'created'}"
        if script == "eval_js.applescript":
            if not self.window_exists:
                self.window_exists = True
                raise WindowGone("-1728")
            return "https://www.apple.com/jp/shop/buy-iphone/iphone-duo"
        if script == "navigate.applescript":
            return "complete|https://www.apple.com/jp/shop/buy-iphone/iphone-duo"
        if script == "run_js.applescript":
            item = self.responses.pop(0)
            if isinstance(item, Exception):
                raise item
            status, body = item
            return json.dumps({"status": status, "body": body})
        raise AssertionError(script)


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(safari.time, "sleep", lambda _: None)

    def make(responses):
        fake = FakeSafari(responses)
        monkeypatch.setattr(safari, "_osascript", fake)
        return SafariSession(StateStore(tmp_path / "state.json")), fake

    return make


def test_first_use_creates_window_and_loads_page(session):
    s, fake = session([(200, OK_BODY)])
    assert s.fetch_json("/x") == json.loads(OK_BODY)
    assert fake.calls == ["ensure_window.applescript", "navigate.applescript", "run_js.applescript"]
    assert s.window_id == 7


def test_541_reloads_page_and_retries(session):
    s, fake = session([(200, OK_BODY), (541, "<html>Page Not Found</html>"), (200, OK_BODY)])
    s.fetch_json("/x")
    fake.calls.clear()
    assert s.fetch_json("/x") == json.loads(OK_BODY)
    assert fake.calls.count("navigate.applescript") == 1  # 541 → 読み込み直し → 再試行で成功
    assert fake.calls.count("run_js.applescript") == 2


def test_persistent_541_raises_inventory_error(session):
    s, _ = session([(541, "<html>"), (541, "<html>")])
    with pytest.raises(InventoryError, match="status=541"):
        s.fetch_json("/x")


def test_timeout_is_retried(session):
    s, fake = session([(0, "timeout"), (200, OK_BODY)])
    assert s.fetch_json("/x") == json.loads(OK_BODY)
    assert fake.calls.count("navigate.applescript") == 2  # 初回の読み込み + タイムアウト後の読み込み直し


def test_closed_window_is_recreated(session):
    s, fake = session([(200, OK_BODY), (200, OK_BODY)])
    s.fetch_json("/x")
    fake.window_exists = False  # 利用者がウィンドウを閉じた
    assert s.fetch_json("/x") == json.loads(OK_BODY)
    assert fake.calls.count("ensure_window.applescript") == 3


def test_setup_error_propagates(session):
    s, _ = session([SafariSetupError("Apple Events からの JavaScript を許可")])
    with pytest.raises(SafariSetupError):
        s.fetch_json("/x")
