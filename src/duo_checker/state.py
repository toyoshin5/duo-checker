"""永続状態（var/state.json）と、在庫の重複通知を防ぐ状態遷移。"""

import json
import os
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from .inventory import StoreAvailability


class StateStore:
    """小さな JSON ファイルにアトミックに読み書きする辞書。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                broken = path.with_suffix(".broken.json")
                path.replace(broken)
                self.data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


class AvailabilityTracker:
    """店舗×製品ごとに在庫の有無を記録し、「在庫なし（または初回）→ 在庫あり」の遷移だけを返す。

    - 在庫ありが続く間は再通知しない
    - 在庫なしを観測したら再び通知可能な状態に戻す
    - 取得に失敗して観測できなかった組み合わせは状態を変えない（通信エラーで重複通知しないため）
    """

    def __init__(self, store: StateStore) -> None:
        self.store = store
        self.items: dict[str, dict[str, Any]] = store.data.setdefault("availability", {})

    def update(self, observations: Iterable[StoreAvailability], now: datetime) -> list[StoreAvailability]:
        restocked: list[StoreAvailability] = []
        for obs in observations:
            prev = self.items.get(obs.key)
            was_available = bool(prev and prev["available"])
            if obs.available and not was_available:
                restocked.append(obs)
            if prev is None or prev["available"] != obs.available:
                self.items[obs.key] = {
                    "available": obs.available,
                    "since": now.isoformat(timespec="seconds"),
                    "store_name": obs.store_name,
                    "pickup_quote": obs.pickup_quote,
                }
        self.store.save()
        return restocked
