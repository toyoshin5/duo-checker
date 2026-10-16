"""メール通知。送信に失敗したメールは outbox に残し、次の確認サイクルで再送する。"""

import json
import logging
import smtplib
import socket
import ssl
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from .catalog import BUY_PAGE_URL, CARRIER_NAME, MODEL_NAME, Variant
from .inventory import StoreAvailability
from .settings import SmtpSettings

log = logging.getLogger(__name__)
MAX_OUTBOX = 50
RETRY_DELAYS = (0, 3, 10)  # 1 サイクル内での再試行間隔（秒）


def send_smtp(smtp: SmtpSettings, msg: EmailMessage) -> None:
    context = ssl.create_default_context()
    # local_hostname を省略すると smtplib が socket.getfqdn() で逆引きし、環境によっては数十秒止まる
    local = socket.gethostname() or "localhost"
    if smtp.security == "ssl":
        server: smtplib.SMTP = smtplib.SMTP_SSL(smtp.host, smtp.port, local_hostname=local, timeout=30, context=context)
    else:
        server = smtplib.SMTP(smtp.host, smtp.port, local_hostname=local, timeout=30)
    with server:
        if smtp.security == "starttls":
            server.starttls(context=context)
        server.ehlo()
        # 暗号化なし（社内リレー等）で AUTH 非対応のサーバーには認証せずに送る
        if smtp.username and smtp.password and (smtp.security != "none" or server.has_extn("auth")):
            server.login(smtp.username, smtp.password)
        server.send_message(msg)


def format_restock(items: list[StoreAvailability], variants: dict[str, Variant], checked_at: datetime, test: bool = False) -> tuple[str, str]:
    first = items[0]
    v0 = variants[first.part_number]
    others = len({i.store_number for i in items}) - 1
    subject = f"【在庫あり】{MODEL_NAME} {v0.capacity_name} {v0.color_name} — Apple {first.store_name}"
    if others > 0:
        subject += f" ほか{others}店舗"
    if test:
        subject = "[テスト] " + subject

    lines = [f"{MODEL_NAME} の店頭受け取り在庫が見つかりました。", ""]
    if test:
        lines = ["※ これは duo-checker のテスト送信です。実際の在庫ではありません。", ""] + lines
    for item in items:
        v = variants[item.part_number]
        lines += [
            f"■ Apple {item.store_name}（{item.store_number}）",
            f"  モデル　: {MODEL_NAME}（{CARRIER_NAME}）",
            f"  容量　　: {v.capacity_name}",
            f"  カラー　: {v.color_name}",
            f"  受け取り: {item.pickup_quote or '—'}",
            f"  購入ページ: {v.product_url}",
        ]
        if item.store_url:
            lines.append(f"  店舗ページ: {item.store_url}")
        lines.append("")
    lines += [
        f"在庫確認時刻: {checked_at.strftime('%Y-%m-%d %H:%M:%S')}（日本時間）",
        "",
        "在庫はすぐになくなることがあります。購入ページで「ストアでの受け取り」を選び、店舗を指定して注文してください。",
        f"商品一覧: {BUY_PAGE_URL}",
        "",
        "-- duo-checker",
    ]
    return subject, "\n".join(lines)


class Notifier:
    def __init__(
        self,
        smtp: SmtpSettings,
        mail_to: list[str],
        data_dir: Path,
        log_dir: Path,
        send: Callable[[SmtpSettings, EmailMessage], None] = send_smtp,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.smtp = smtp
        self.mail_to = mail_to
        self.outbox_path = data_dir / "outbox.json"
        self.history_path = log_dir / "notifications.jsonl"
        self._send = send
        self._sleep = sleep
        data_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)

    # --- outbox ---------------------------------------------------------------------

    def _load_outbox(self) -> list[dict]:
        if not self.outbox_path.exists():
            return []
        try:
            return json.loads(self.outbox_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.error("outbox.json が壊れていたため破棄します")
            return []

    def _save_outbox(self, items: list[dict]) -> None:
        tmp = self.outbox_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(items[-MAX_OUTBOX:], ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.outbox_path)

    def _history(self, entry: dict) -> None:
        with self.history_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def enqueue(self, kind: str, subject: str, body: str, meta: dict | None = None) -> None:
        outbox = self._load_outbox()
        outbox.append(
            {
                "id": uuid.uuid4().hex,
                "kind": kind,
                "subject": subject,
                "body": body,
                "meta": meta or {},
                "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "attempts": 0,
            }
        )
        self._save_outbox(outbox)

    def flush(self) -> int:
        """outbox のメールを送る。送れた件数を返す。失敗分は outbox に残る。"""
        outbox = self._load_outbox()
        if not outbox:
            return 0
        remaining: list[dict] = []
        sent = 0
        for item in outbox:
            error = self._deliver(item)
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            entry = {"time": now, "kind": item["kind"], "subject": item["subject"], "to": self.mail_to, **item["meta"]}
            if error is None:
                sent += 1
                self._history({**entry, "status": "sent", "attempts": item["attempts"]})
                log.info("メールを送信しました: %s", item["subject"])
            else:
                remaining.append(item)
                self._history({**entry, "status": "failed", "attempts": item["attempts"], "error": error})
                log.error("メール送信に失敗しました（次のサイクルで再送）: %s / %s", item["subject"], error)
        self._save_outbox(remaining)
        return sent

    def _deliver(self, item: dict) -> str | None:
        error = None
        for delay in RETRY_DELAYS:
            if delay:
                self._sleep(delay)
            item["attempts"] += 1
            msg = EmailMessage()
            msg["Subject"] = item["subject"]
            msg["From"] = self.smtp.sender
            msg["To"] = ", ".join(self.mail_to)
            msg["Date"] = formatdate(localtime=True)
            msg["Message-ID"] = make_msgid(domain="duo-checker.local")
            msg.set_content(item["body"])
            try:
                self._send(self.smtp, msg)
                return None
            except (smtplib.SMTPException, OSError) as e:
                error = f"{type(e).__name__}: {e}"
                if isinstance(e, smtplib.SMTPAuthenticationError):
                    break  # 認証エラーは再試行しても直らない
        return error

    # --- 通知の種類 -------------------------------------------------------------------

    def restock(self, items: list[StoreAvailability], variants: dict[str, Variant], checked_at: datetime, test: bool = False) -> None:
        subject, body = format_restock(items, variants, checked_at, test=test)
        meta = {"items": [{"store": i.store_number, "store_name": i.store_name, "part": i.part_number, "quote": i.pickup_quote} for i in items]}
        self.enqueue("test" if test else "restock", subject, body, meta)

    def alert(self, subject: str, body: str) -> None:
        self.enqueue("alert", subject, body + "\n\n-- duo-checker")
