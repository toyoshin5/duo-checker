"""監視ループ: 在庫取得 → 状態遷移の判定 → 通知 → ログ。"""

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

from .catalog import STORES, Variant
from .inventory import MAX_PARTS_PER_REQUEST, InventoryError, StoreAvailability, build_query, parse_pickup_response
from .notifier import Notifier
from .safari import SafariError, SafariSession, mac_notify
from .state import AvailabilityTracker, StateStore

log = logging.getLogger(__name__)
JST = ZoneInfo("Asia/Tokyo")
MAX_BACKOFF_SECONDS = 600


class InventorySource(Protocol):
    def fetch(self, parts: list[str], store: str) -> list[StoreAvailability]: ...


class SafariInventorySource:
    def __init__(self, session: SafariSession) -> None:
        self.session = session

    def fetch(self, parts: list[str], store: str) -> list[StoreAvailability]:
        return parse_pickup_response(self.session.fetch_json(build_query(parts, store)), parts)


@dataclass
class CheckResult:
    checked_at: datetime
    observations: list[StoreAvailability] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    restocked: list[StoreAvailability] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing


class Monitor:
    def __init__(
        self,
        variants: list[Variant],
        stores: list[str],
        source: InventorySource,
        state: StateStore,
        notifier: Notifier,
        interval_seconds: int = 30,
        error_alert_minutes: int = 10,
        mac_notification: bool = True,
        test_mode: bool = False,
        desktop_notify: Callable[[str, str], None] = mac_notify,
        now: Callable[[], datetime] = lambda: datetime.now(JST),
    ) -> None:
        self.variants = variants
        self.variants_by_part = {v.part_number: v for v in variants}
        self.stores = stores
        self.source = source
        self.state = state
        self.tracker = AvailabilityTracker(state)
        self.notifier = notifier
        self.interval = interval_seconds
        self.error_alert_seconds = error_alert_minutes * 60
        self.mac_notification = mac_notification
        self.test_mode = test_mode
        self.desktop_notify = desktop_notify
        self.now = now
        self.stop_event = threading.Event()

    # --- 1 回分の確認 ------------------------------------------------------------------

    def _collect(self, result: CheckResult) -> None:
        parts = list(self.variants_by_part)
        wanted = set(self.stores)
        observed: dict[str, StoreAvailability] = {}
        pending = list(self.stores)
        while pending:
            store = pending.pop(0)
            covered: set[str] = set()
            for i in range(0, len(parts), MAX_PARTS_PER_REQUEST):
                chunk = parts[i : i + MAX_PARTS_PER_REQUEST]
                try:
                    found = self.source.fetch(chunk, store)
                except (InventoryError, SafariError) as e:
                    # 1 件失敗したら残りも同じ原因で失敗する可能性が高いので、このサイクルは打ち切る
                    # （ブロック中に照会を重ねないため）。取得できた分の状態だけ更新する。
                    result.errors.append(f"{store}: {e}")
                    log.warning("在庫取得に失敗 (%s): %s", store, e)
                    pending = []
                    break
                for obs in found:
                    if obs.store_number in wanted:
                        observed[obs.key] = obs
                        covered.add(obs.store_number)
            # searchNearby=true で近隣店舗もまとめて返るので、取得済みの店舗は照会を省く
            pending = [s for s in pending if s not in covered]
        result.observations = list(observed.values())
        result.missing = [f"{s}:{p}" for s in self.stores for p in parts if f"{s}:{p}" not in observed]

    def check_once(self) -> CheckResult:
        result = CheckResult(checked_at=self.now())
        self._collect(result)
        result.restocked = self.tracker.update(result.observations, result.checked_at)
        if result.restocked:
            self._announce(result)
        self._log_result(result)
        return result

    def _announce(self, result: CheckResult) -> None:
        names = "、".join(sorted({f"Apple {o.store_name}" for o in result.restocked}))
        log.info("★ 在庫復活を検知: %s", names)
        if self.mac_notification:
            v = self.variants_by_part[result.restocked[0].part_number]
            prefix = "[テスト] " if self.test_mode else ""
            self.desktop_notify(f"{prefix}在庫あり: iPhone Duo {v.capacity_name} {v.color_name}", names)
        self.notifier.restock(result.restocked, self.variants_by_part, result.checked_at, test=self.test_mode)

    def _log_result(self, result: CheckResult) -> None:
        # 例: 確認 OK 11/11 在庫あり=1 [MK244J/A] R718丸の内=× R005名古屋栄=○ ...
        # 複数モデルのときは店舗ごとに [ ] 内のモデル順で ○× を並べる
        parts = list(self.variants_by_part)
        by_key = {o.key: o for o in result.observations}
        cells = []
        for store in self.stores:
            marks = "".join(
                "?" if (o := by_key.get(f"{store}:{p}")) is None else ("○" if o.available else "×") for p in parts
            )
            cells.append(f"{store}{STORES.get(store, '')}={marks}")
        log.info(
            "確認 %s %d/%d 在庫あり=%d [%s] %s",
            "OK" if result.ok else "一部失敗",
            len(result.observations),
            len(self.stores) * len(parts),
            sum(o.available for o in result.observations),
            ",".join(parts),
            " ".join(cells),
        )

    # --- 異常の継続を知らせる ------------------------------------------------------------

    def _update_health(self, ok: bool, error: str) -> None:
        health = self.state.data.setdefault("health", {"failing_since": None, "alerted": False, "last_error": ""})
        now = self.now()
        if ok:
            if health["alerted"]:
                since = health["failing_since"]
                self.notifier.alert(
                    "【duo-checker】在庫の監視が復旧しました",
                    f"{since} から続いていた在庫取得のエラーが解消し、監視を再開しました。\n復旧時刻: {now:%Y-%m-%d %H:%M:%S}",
                )
                log.info("監視が復旧しました")
            health.update(failing_since=None, alerted=False, last_error="")
        else:
            health["last_error"] = error
            if not health["failing_since"]:
                health["failing_since"] = now.isoformat(timespec="seconds")
            started = datetime.fromisoformat(health["failing_since"])
            if not health["alerted"] and (now - started).total_seconds() >= self.error_alert_seconds:
                self.notifier.alert(
                    "【duo-checker】在庫を取得できていません",
                    "在庫の取得に失敗し続けています。この間は在庫が復活しても通知できません。\n\n"
                    f"失敗が始まった時刻: {started:%Y-%m-%d %H:%M:%S}\n"
                    f"直近のエラー: {error}\n\n"
                    "確認すること:\n"
                    "・Mac がスリープしていないか、Safari が起動しているか\n"
                    "・Safari の 開発 →「Apple Events からの JavaScript を許可」がオンか\n"
                    "・Safari で https://www.apple.com/jp/shop/buy-iphone/iphone-duo を開いて「ストアでの受け取り」が表示されるか\n"
                    "・logs/monitor.log の内容\n\n"
                    "復旧したら改めてメールでお知らせします。",
                )
                health["alerted"] = True
                log.error("在庫取得の失敗が %d 分以上続いたため警告メールを送ります", self.error_alert_seconds // 60)
        self.state.save()

    # --- 常駐ループ -------------------------------------------------------------------

    def run_forever(self) -> None:
        failures = 0
        log.info(
            "監視を開始します: %s / 店舗 %s / %d 秒間隔",
            "、".join(v.display_name for v in self.variants),
            ",".join(self.stores),
            self.interval,
        )
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                result = self.check_once()
                ok, error = result.ok, "; ".join(result.errors) or "一部の店舗の在庫が応答に含まれていません"
            except Exception as e:  # 想定外のエラーでもループは止めない
                log.exception("確認中に想定外のエラー")
                ok, error = False, f"{type(e).__name__}: {e}"
            failures = 0 if ok else failures + 1
            try:
                self._update_health(ok, error)
                self.notifier.flush()
            except Exception:
                log.exception("通知処理で想定外のエラー")
            wait = self.interval if failures < 3 else min(self.interval * 2 ** (failures - 2), MAX_BACKOFF_SECONDS)
            if wait != self.interval:
                log.warning("失敗が %d 回続いているため、次の確認まで %d 秒待ちます", failures, wait)
            self.stop_event.wait(max(0.0, started + wait - time.monotonic()))
        log.info("監視を終了しました")
