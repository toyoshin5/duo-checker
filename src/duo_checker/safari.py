"""Safari を AppleScript で操作し、Apple Store のページ内から在庫 API を呼び出す。

Apple の在庫 API は、ボット対策を通過したブラウザセッション以外には HTTP 541 を返す。
利用者が普段使っている Safari に専用ウィンドウを 1 つ開き、そのページから同一オリジンの
fetch を行うことで、購入ページ自身が出すリクエストと同じ形で在庫を取得する。
"""

import json
import logging
import subprocess
import time
import uuid
from pathlib import Path

from .catalog import BUY_PAGE_URL
from .inventory import HttpResponse, InventoryError, RateLimitError
from .state import StateStore

log = logging.getLogger(__name__)
SCRIPTS = Path(__file__).parent / "applescript"
SHOP_PREFIX = "https://www.apple.com/jp/shop/"


class SafariError(Exception):
    """Safari の操作に失敗した（一時的なエラー）。"""


class SafariSetupError(SafariError):
    """利用者の操作（権限の許可など）が必要なエラー。"""


class WindowGone(SafariError):
    """監視用ウィンドウが閉じられた。"""


def _osascript(script: str, *args: str, timeout: float = 60) -> str:
    try:
        proc = subprocess.run(
            ["osascript", str(SCRIPTS / script), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise SafariError(f"osascript {script} がタイムアウトしました") from None
    if proc.returncode != 0:
        err = proc.stderr.strip()
        if "-1743" in err or "Not authorized" in err:
            raise SafariSetupError(
                "Safari を操作する権限がありません。システム設定 → プライバシーとセキュリティ → オートメーション で、"
                "このプログラム（ターミナルや python）の「Safari」をオンにしてください"
            )
        if "JavaScript" in err and ("Apple Event" in err or "Apple イベント" in err):
            raise SafariSetupError(
                "Safari で「Apple Events からの JavaScript を許可」が無効です。Safari の設定 → 詳細 →"
                "「Web デベロッパ用の機能を表示」をオンにし、メニューの 開発 →「Apple Events からの JavaScript を許可」を選んでください"
            )
        if "-1728" in err or "-1719" in err:
            raise WindowGone(err)
        raise SafariError(f"osascript {script} が失敗しました: {err}")
    return proc.stdout.rstrip("\n")


class SafariSession:
    def __init__(self, state: StateStore, reload_minutes: int = 30, fetch_timeout: float = 25) -> None:
        self.state = state
        self.reload_seconds = reload_minutes * 60
        self.fetch_timeout = fetch_timeout
        self.loaded_at = 0.0

    # --- ウィンドウ管理 -------------------------------------------------------------

    @property
    def window_id(self) -> int:
        return int(self.state.data.get("safari_window_id") or 0)

    def _set_window_id(self, value: int) -> None:
        self.state.data["safari_window_id"] = value
        self.state.save()

    def _ensure_window(self) -> bool:
        """専用ウィンドウを用意する。新規作成した場合は True。"""
        out = _osascript("ensure_window.applescript", str(self.window_id))
        wid, how = out.split("|", 1)
        if int(wid) != self.window_id:
            self._set_window_id(int(wid))
        if how == "created":
            log.info("Safari に監視用ウィンドウを開きました（Dock に最小化, window id=%s）", wid)
        return how == "created"

    def _current_url(self) -> str:
        return _osascript("eval_js.applescript", str(self.window_id), "location.href")

    def reload(self) -> None:
        """購入ページを開き直してセッション（Cookie・ボット判定）を更新する。"""
        out = _osascript("navigate.applescript", str(self.window_id), BUY_PAGE_URL, SHOP_PREFIX, "45", timeout=90)
        if out == "__TIMEOUT__":
            raise SafariError("購入ページの読み込みがタイムアウトしました")
        time.sleep(4)  # ボット対策スクリプト（/shop/shld）の完了を待つ
        self.loaded_at = time.monotonic()
        log.info("購入ページを読み込み直しました")

    def prepare(self) -> None:
        for attempt in range(2):
            try:
                created = self._ensure_window()
                stale = time.monotonic() - self.loaded_at > self.reload_seconds
                if created or stale or not self._current_url().startswith(SHOP_PREFIX):
                    self.reload()
                return
            except WindowGone:
                log.warning("監視用ウィンドウが閉じられていたため作り直します")
                self._set_window_id(0)
                if attempt:
                    raise

    # --- 在庫 API ------------------------------------------------------------------

    def _fetch_once(self, path: str) -> tuple[int, str]:
        token = uuid.uuid4().hex
        start_js = (
            "(function(){var k=%(token)s;window.__duoChecker={token:k,result:null};"
            "fetch(%(path)s,{credentials:'include',headers:{'x-skip-redirect':'true',"
            "'x-aos-ui-fetch-call-1':Math.random().toString(36).slice(2,12)}})"
            ".then(function(r){return r.text().then(function(t){"
            "window.__duoChecker={token:k,result:JSON.stringify({status:r.status,body:t})};});})"
            ".catch(function(e){window.__duoChecker={token:k,result:JSON.stringify({status:0,body:String(e)})};});"
            "return 'started';})()"
        ) % {"token": json.dumps(token), "path": json.dumps(path)}
        poll_js = (
            "(window.__duoChecker&&window.__duoChecker.token===%s&&window.__duoChecker.result)||''" % json.dumps(token)
        )
        out = _osascript(
            "run_js.applescript",
            str(self.window_id),
            start_js,
            poll_js,
            str(self.fetch_timeout),
            timeout=self.fetch_timeout + 20,
        )
        if out == "__TIMEOUT__":
            return 0, "timeout"
        try:
            result = json.loads(out)
            return int(result["status"]), result["body"]
        except (TypeError, KeyError, json.JSONDecodeError) as error:
            raise SafariError(f"Safari からの在庫 API 結果を解釈できません: {error}") from None

    def fetch_response(self, path: str) -> HttpResponse:
        """在庫 API を呼び、HTTP応答を返す。

        既存動作との互換性のため、異常応答の直後にページを読み込み直して
        1回だけ再試行する。541を受けた後に無制限に別方式へ切り替えない判断は、
        呼び出し側の RateLimitError とクールダウンで行う。
        """

        self.prepare()
        started = time.monotonic()
        status, body = self._fetch_once(path)
        if status != 200 or not body.lstrip().startswith("{"):
            log.warning("在庫 API の応答が異常 (status=%s)。ページを読み込み直して再試行します", status)
            self.reload()
            status, body = self._fetch_once(path)
        return HttpResponse(status, body, time.monotonic() - started)

    def fetch_json(self, path: str) -> dict:
        """在庫 API を呼ぶ。失敗したらページを読み込み直して 1 回だけ再試行する。"""
        response = self.fetch_response(path)
        status, body = response.status_code, response.body
        if status == 541:
            raise RateLimitError(f"在庫 API の応答が異常です (status=541, body={body[:120]!r})")
        if status != 200 or not body.lstrip().startswith("{"):
            raise InventoryError(f"在庫 API の応答が異常です (status={status}, body={body[:120]!r})", status_code=status)
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            raise InventoryError(f"在庫 API の応答が JSON ではありません: {e}") from None


def mac_notify(title: str, body: str) -> None:
    try:
        _osascript("notify.applescript", title, body, timeout=10)
    except SafariError as e:
        log.warning("Mac の通知に失敗しました: %s", e)
