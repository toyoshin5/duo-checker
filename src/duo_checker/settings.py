"""設定（.env と環境変数）の読み込みと、初回セットアップ。

利用者が編集する設定ファイルは持たない。SMTP の認証情報だけを .env（権限 600、git 管理外）に保存し、
環境変数が設定されていればそちらを優先する。それ以外はすべて既定値で動く。
"""

import getpass
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .catalog import STORES

ROOT = Path(os.environ.get("DUO_CHECKER_HOME") or Path(__file__).resolve().parents[2])
ENV_PATH = ROOT / ".env"
DATA_DIR = ROOT / "var"
LOG_DIR = ROOT / "logs"

@dataclass(frozen=True)
class Provider:
    name: str
    host: str
    port: int
    security: str
    url: str
    steps: tuple[str, ...]


_GMAIL = Provider(
    "Gmail", "smtp.gmail.com", 587, "starttls", "https://myaccount.google.com/apppasswords",
    (
        "1. 上のリンクを開き、Google アカウントにログインする",
        "2. アプリ名に「duo-checker」と入力して「作成」を押す",
        "3. 表示された 16 文字のパスワードをコピーする",
        "※ 2 段階認証プロセスを有効にしていないと作成できません",
    ),
)
_ICLOUD = Provider(
    "iCloud メール", "smtp.mail.me.com", 587, "starttls", "https://account.apple.com/",
    (
        "1. 上のリンクを開いてサインインし、「サインインとセキュリティ」→「アプリ用パスワード」を開く",
        "2. 名前に「duo-checker」と入力して作成する",
        "3. 表示されたパスワードをコピーする",
    ),
)
_YAHOO = Provider(
    "Yahoo!メール", "smtp.mail.yahoo.co.jp", 465, "ssl", "https://mail.yahoo.co.jp/",
    (
        "1. 上のリンクを開き、Yahoo!メールの 設定 →「IMAP/POP/SMTPアクセスとメール転送」を開く",
        "2. Yahoo! JAPAN 公式サービス以外からの SMTP アクセスを有効にする",
        "3. 画面の案内に従って、メールソフト用のパスワードを確認する",
    ),
)

# メールアドレスのドメインから SMTP サーバーと、メール送信用パスワードの作り方を決める
KNOWN_PROVIDERS = {
    "gmail.com": _GMAIL,
    "googlemail.com": _GMAIL,
    "icloud.com": _ICLOUD,
    "me.com": _ICLOUD,
    "mac.com": _ICLOUD,
    "yahoo.co.jp": _YAHOO,
}


class SettingsError(Exception):
    pass


@dataclass(frozen=True)
class SmtpSettings:
    host: str
    port: int
    security: str  # "starttls" | "ssl" | "none"
    username: str
    password: str
    sender: str


@dataclass(frozen=True)
class Settings:
    smtp: SmtpSettings
    mail_to: list[str]
    stores: list[str]
    interval_seconds: int
    page_reload_minutes: int
    error_alert_minutes: int
    mac_notification: bool


def load_dotenv(path: Path = ENV_PATH) -> list[str]:
    """KEY=VALUE 形式の .env を読み込む。既に設定済みの環境変数は上書きしない。戻り値は警告。"""
    warnings: list[str] = []
    if not path.exists():
        return warnings
    if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        warnings.append(f"{path} がほかのユーザーからも読めます。`chmod 600 {path}` を実行してください")
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (s.strip() for s in line.split("=", 1))
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)
    return warnings


def _split(value: str) -> list[str]:
    return [s.strip() for s in value.replace(" ", ",").split(",") if s.strip()]


def load_settings(env: dict[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    user = env.get("SMTP_USER", "")
    password = env.get("SMTP_PASSWORD", "")
    if not user or not password:
        raise SettingsError("メール送信の設定がありません。`uv run duo-checker setup` を実行してください")

    provider = KNOWN_PROVIDERS.get(user.rsplit("@", 1)[-1].lower())
    host = env.get("SMTP_HOST") or (provider.host if provider else "")
    if not host:
        raise SettingsError("SMTP_HOST が未設定です。`uv run duo-checker setup` を実行してください")
    security = (env.get("SMTP_SECURITY") or (provider.security if provider else "starttls")).lower()
    if security not in ("starttls", "ssl", "none"):
        raise SettingsError("SMTP_SECURITY は starttls / ssl / none のいずれかです")
    port = int(env.get("SMTP_PORT") or (provider.port if provider else {"starttls": 587, "ssl": 465, "none": 25}[security]))

    stores = [s.upper() for s in _split(env.get("DUO_STORES", ""))] or list(STORES)
    unknown = [s for s in stores if s not in STORES]
    if unknown:
        raise SettingsError(f"DUO_STORES に未知の店舗番号があります: {', '.join(unknown)}（一覧は README を参照）")

    return Settings(
        smtp=SmtpSettings(
            host=host,
            port=port,
            security=security,
            username=user,
            password=password,
            sender=env.get("MAIL_FROM") or user,
        ),
        mail_to=_split(env.get("MAIL_TO", "")) or [user],
        stores=stores,
        interval_seconds=max(30, int(env.get("DUO_INTERVAL_SECONDS") or 60)),
        page_reload_minutes=int(env.get("DUO_PAGE_RELOAD_MINUTES") or 30),
        error_alert_minutes=int(env.get("DUO_ERROR_ALERT_MINUTES") or 10),
        mac_notification=(env.get("DUO_MAC_NOTIFICATION", "1") not in ("0", "false", "no")),
    )


def run_setup_wizard(path: Path = ENV_PATH) -> None:
    """対話形式で SMTP 設定を聞き、.env を権限 600 で書き出す。"""
    print("=== duo-checker 初回セットアップ（メール送信の設定）===")
    print("在庫が復活したときに、あなたのメールアカウントから通知メールを送ります。")
    print("そのための送信元アドレスと、メール送信用のパスワードを設定します。\n")

    user = ""
    while "@" not in user:
        user = input("送信に使うメールアドレス: ").strip()
    provider = KNOWN_PROVIDERS.get(user.rsplit("@", 1)[-1].lower())
    lines = [
        "# duo-checker の設定（このファイルは git 管理外。権限 600 で保存）",
        f"SMTP_USER={user}",
    ]
    if provider:
        rule = "-" * 64
        print(f"\n{rule}")
        print(f"次のリンク先で、メール送信用のパスワードを作成してください（{provider.name}）")
        print(f"\n    {provider.url}\n")
        for step in provider.steps:
            print(f"  {step}")
        print("  ※ 普段のログイン用パスワードではありません。duo-checker がメールを送るためだけに使います")
        print(f"{rule}\n")
        prompt = "作成したメール送信用パスワードを貼り付けて Enter（入力内容は表示されません）: "
    else:
        host = input("SMTP サーバー（例: smtp.example.com）: ").strip()
        port = input("SMTP ポート [587]: ").strip() or "587"
        security = input("暗号化方式 starttls / ssl / none [starttls]: ").strip() or "starttls"
        lines += [f"SMTP_HOST={host}", f"SMTP_PORT={port}", f"SMTP_SECURITY={security}"]
        prompt = "SMTP のパスワード（入力内容は表示されません）: "

    password = ""
    while not password:
        password = getpass.getpass(prompt).replace(" ", "")
    lines.append(f"SMTP_PASSWORD={password}")

    mail_to = input(f"通知先メールアドレス（カンマ区切りで複数可）[{user}]: ").strip()
    lines.append(f"MAIL_TO={mail_to or user}")
    lines += [
        "",
        "# 任意設定（既定値で問題なければ不要）",
        "# DUO_STORES=R005,R718        # 監視する店舗番号。未指定なら全国の全店舗",
        "# DUO_INTERVAL_SECONDS=60     # 確認間隔（秒）",
        "# DUO_MAC_NOTIFICATION=1      # メールに加えて Mac の通知センターにも表示する",
        "",
    ]

    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    os.chmod(path, 0o600)
    for key in ("SMTP_USER", "SMTP_PASSWORD", "SMTP_HOST", "SMTP_PORT", "SMTP_SECURITY", "MAIL_TO"):
        os.environ.pop(key, None)
    load_dotenv(path)
    print(f"\n{path} に保存しました。")
