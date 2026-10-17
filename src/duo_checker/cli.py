"""コマンドライン。

  uv run duo-checker                      色・容量を選んで監視開始（初回はメール設定から）
  uv run duo-checker --color starwhite --capacity 256gb
  uv run duo-checker install              ログイン中は常時監視する（launchd に登録）
  uv run duo-checker uninstall            常時監視を解除
  uv run duo-checker status               常時監視の状態と直近のログ
  uv run duo-checker setup                メール設定をやり直す
  uv run duo-checker test-email           テストメールを送る
  uv run duo-checker simulate             在庫復活を模擬して通知の流れを確認する
  uv run duo-checker diagnose             API・通信方式を比較する（状態を変更しない）
"""

import argparse
import fcntl
import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from . import agent
from .catalog import CAPACITIES, COLORS, STORES, Variant
from .inventory import ApiInventorySource, CurlCffiTransport, InventoryError, StandardHttpTransport, StoreAvailability
from .monitor import JST, Monitor, SafariInventorySource, make_inventory_source
from .notifier import Notifier
from .safari import SafariError, SafariSession, SafariSetupError
from .settings import (
    DATA_DIR,
    ENV_PATH,
    LOG_DIR,
    Settings,
    SettingsError,
    load_dotenv,
    load_inventory_settings,
    load_settings,
    run_setup_wizard,
)
from .state import StateStore

log = logging.getLogger("duo_checker")


# --- 共通 -------------------------------------------------------------------------------


def setup_logging(console: bool) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = TimedRotatingFileHandler(LOG_DIR / "monitor.log", when="midnight", backupCount=30, encoding="utf-8")
    file_handler.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(fmt)
        root.addHandler(stream)


def choose(title: str, options: dict[str, str], given: str | None, flag: str) -> list[str]:
    """--color などの指定値を検証する。未指定なら番号メニューで選ばせる（複数選択可）。"""
    if given:
        values = [s.strip().lower() for s in given.split(",") if s.strip()]
        bad = [v for v in values if v not in options]
        if bad:
            sys.exit(f"{flag} の値が不正です: {', '.join(bad)}（{' / '.join(options)}）")
        return values
    if not sys.stdin.isatty():
        sys.exit(f"{flag} を指定してください（{' / '.join(options)}）")
    keys = list(options)
    print(f"\n{title}")
    for i, k in enumerate(keys, 1):
        print(f"  {i}) {options[k]}")
    while True:
        raw = input("番号（複数ならカンマ区切り）[1]: ").strip() or "1"
        try:
            nums = [int(x) for x in raw.replace("、", ",").split(",") if x.strip()]
        except ValueError:
            nums = []
        if nums and all(1 <= n <= len(keys) for n in nums):
            return list(dict.fromkeys(keys[n - 1] for n in nums))
        print(f"  1〜{len(keys)} の番号を入力してください")


def select_variants(args: argparse.Namespace) -> list[Variant]:
    colors = choose("カラーを選んでください:", COLORS, args.color, "--color")
    capacities = choose("容量を選んでください:", CAPACITIES, args.capacity, "--capacity")
    return [Variant(c, cap) for c in colors for cap in capacities]


def ensure_settings(interactive: bool) -> Settings:
    for warning in load_dotenv(ENV_PATH):
        print(f"警告: {warning}", file=sys.stderr)
    try:
        return load_settings()
    except SettingsError as e:
        if not interactive or not sys.stdin.isatty():
            sys.exit(str(e))
    run_setup_wizard(ENV_PATH)
    settings = load_settings()
    send_test_email(settings)
    return settings


def make_notifier(settings: Settings, data_dir: Path = DATA_DIR) -> Notifier:
    return Notifier(settings.smtp, settings.mail_to, data_dir, LOG_DIR)


def send_test_email(settings: Settings) -> bool:
    print(f"\nテストメールを {', '.join(settings.mail_to)} に送信します…")
    with tempfile.TemporaryDirectory() as tmp:
        notifier = make_notifier(settings, Path(tmp))
        item = StoreAvailability("R005", STORES["R005"], "MK244J/A", True, "available", "本日", "https://www.apple.com/jp/retail/nagoyasakae")
        notifier.restock([item], {"MK244J/A": Variant("starwhite", "256gb")}, datetime.now(JST), test=True)
        if notifier.flush():
            print("送信しました。届いているか確認してください（迷惑メールフォルダも）。")
            return True
    print(f"送信に失敗しました。詳細は {LOG_DIR / 'notifications.jsonl'} を確認し、`uv run duo-checker setup` で設定し直してください。")
    return False


def acquire_single_instance_lock(wait: bool) -> None:
    """監視は同時に 1 つだけ動かす（重複通知と状態ファイルの競合を防ぐ）。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd = os.open(DATA_DIR / "monitor.lock", os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + (60 if wait else 0)
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.ftruncate(fd, 0)
            os.write(fd, str(os.getpid()).encode())
            return  # fd はプロセス終了まで開いたままにしてロックを保持する
        except BlockingIOError:
            if time.monotonic() >= deadline:
                break
            time.sleep(2)
    sys.exit(
        "すでに別の監視が動いています。常時監視を登録している場合は `uv run duo-checker status` で確認し、"
        "手動で動かしたいときは `uv run duo-checker uninstall` で解除してください"
    )


def keep_awake() -> None:
    """このプロセスが動いている間、Mac のスリープを防ぐ（ディスプレイは消灯してよい）。"""
    try:
        subprocess.Popen(["caffeinate", "-i", "-s", "-w", str(os.getpid())])
    except OSError:
        log.warning("caffeinate を起動できませんでした。Mac がスリープすると監視が止まります")


def preflight(source, variants: list[Variant], store: str) -> None:
    """起動時に 1 回在庫を取得し、取得方式の問題を早めに知らせる。"""
    parts = [v.part_number for v in variants][:3]
    print(f"在庫取得方式を確認しています…（{getattr(source, 'name', 'auto')}）")
    found = source.fetch(parts, store)
    print(f"  OK: {len({f.store_number for f in found})} 店舗の在庫情報を取得できました")


# --- コマンド -----------------------------------------------------------------------------


def cmd_run(args: argparse.Namespace) -> None:
    interactive = sys.stdin.isatty() and not args.agent
    settings = ensure_settings(interactive)
    if args.agent:  # DuoChecker.app から起動された常時監視。色・容量は install 時の選択を使う
        try:
            args.color, args.capacity = agent.load_selection()
        except agent.AgentError as e:
            sys.exit(str(e))
    variants = select_variants(args)
    # 常時監視の再起動時は、前のプロセスが終わるまで待つ
    acquire_single_instance_lock(wait=args.agent)
    setup_logging(console=sys.stderr.isatty())
    state = StateStore(DATA_DIR / "state.json")
    session = SafariSession(state, reload_minutes=settings.page_reload_minutes)
    source = make_inventory_source(
        settings.api_mode,
        settings.transport,
        session,
        timeout_seconds=settings.request_timeout_seconds,
        curl_impersonate=settings.curl_impersonate,
        cooldown_seconds=settings.rate_limit_cooldown_seconds,
    )

    if interactive and not args.once:
        try:
            preflight(source, variants, settings.stores[0])
        except SafariSetupError as e:
            sys.exit(f"\n{e}")
        except (SafariError, InventoryError) as e:
            print(f"  警告: 在庫を取得できませんでした（監視は開始し、自動で再試行します）: {e}")

    monitor = Monitor(
        variants,
        settings.stores,
        source,
        state,
        make_notifier(settings),
        interval_seconds=settings.interval_seconds,
        error_alert_minutes=settings.error_alert_minutes,
        mac_notification=settings.mac_notification,
    )
    if args.once:
        result = monitor.check_once()
        monitor.notifier.flush()
        sys.exit(0 if result.ok else 1)

    keep_awake()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: monitor.stop_event.set())
    if args.agent:
        agent.exit_when_orphaned(monitor.stop_event.set)
    if interactive:
        print(f"\n監視中です（{settings.interval_seconds} 秒ごと）。止めるには Ctrl+C。ログ: {LOG_DIR / 'monitor.log'}\n")
    monitor.run_forever()


def cmd_setup(args: argparse.Namespace) -> None:
    run_setup_wizard(ENV_PATH)
    send_test_email(load_settings())


def cmd_test_email(args: argparse.Namespace) -> None:
    settings = ensure_settings(interactive=True)
    if not send_test_email(settings):
        sys.exit(1)


def _diagnostic_variants(args: argparse.Namespace) -> list[Variant]:
    """diagnose は対話を起こさず、未指定なら代表 SKU を1つだけ使う。"""

    color = args.color or "starwhite"
    capacity = args.capacity or "256gb"
    colors = choose("", COLORS, color, "--color")
    capacities = choose("", CAPACITIES, capacity, "--capacity")
    return [Variant(color_name, capacity_name) for color_name in colors for capacity_name in capacities]


def cmd_diagnose(args: argparse.Namespace) -> None:
    """API/通信方式を比較する。StateStore・Notifier・AvailabilityTracker は使わない。"""

    for warning in load_dotenv(ENV_PATH):
        print(f"警告: {warning}", file=sys.stderr)
    try:
        inventory = load_inventory_settings()
    except SettingsError as error:
        sys.exit(str(error))
    variants = _diagnostic_variants(args)
    parts = [v.part_number for v in variants]
    store = args.store.upper()

    # Safari のウィンドウIDだけは既存状態から読み取るが、書き込み先は一時ファイルにする。
    window_id = 0
    persistent_state = DATA_DIR / "state.json"
    if persistent_state.exists():
        try:
            window_id = int(json.loads(persistent_state.read_text(encoding="utf-8")).get("safari_window_id") or 0)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            window_id = 0

    cases = [
        ("fulfillment", "safari"),
        ("pickup", "http"),
        ("pickup", "safari"),
        ("fulfillment", "curl_cffi"),
    ]
    print(f"診断: SKU={','.join(parts)} 店舗={store}（状態・通知は更新しません）")
    with tempfile.TemporaryDirectory(prefix="duo-checker-diagnose-") as temp_dir:
        temp_state_path = Path(temp_dir) / "state.json"
        temp_state_path.write_text(json.dumps({"safari_window_id": window_id}), encoding="utf-8")
        safari_session = SafariSession(StateStore(temp_state_path), reload_minutes=30)
        for api_mode, transport_name in cases:
            source = None
            try:
                if transport_name == "safari":
                    source = SafariInventorySource(safari_session, api_mode)
                elif transport_name == "http":
                    source = ApiInventorySource(api_mode, StandardHttpTransport(inventory.request_timeout_seconds))
                else:
                    source = ApiInventorySource(
                        api_mode,
                        CurlCffiTransport(
                            inventory.request_timeout_seconds,
                            impersonate=inventory.curl_impersonate,
                        ),
                    )
                observations = source.fetch(parts, store)
                metrics = source.last_metrics
                if metrics is None:
                    print(f"{api_mode}/{transport_name}: 結果情報なし")
                    continue
                sku = ",".join(f"{part}={metrics.sku_status.get(part, '?')}" for part in parts)
                print(
                    f"{api_mode}/{transport_name}: status={metrics.status_code} "
                    f"elapsed={metrics.elapsed_seconds:.3f}s stores={metrics.store_count} "
                    f"sku=[{sku}] valid={metrics.success} observations={len(observations)}"
                )
            except (InventoryError, SafariError) as error:
                metrics = getattr(source, "last_metrics", None)
                if metrics is None:
                    print(f"{api_mode}/{transport_name}: error={error}")
                    continue
                elapsed = "-" if metrics.elapsed_seconds is None else f"{metrics.elapsed_seconds:.3f}s"
                sku = ",".join(f"{part}={metrics.sku_status.get(part, '?')}" for part in parts)
                print(
                    f"{api_mode}/{transport_name}: status={metrics.status_code} elapsed={elapsed} "
                    f"stores={metrics.store_count} sku=[{sku}] valid=False error={metrics.error or error}"
                )


class ScriptedSource:
    """先頭の店舗だけ、在庫の有無を sequence の順に変化させる偽の在庫ソース（simulate 用）。"""

    def __init__(self, stores: list[str], sequence: list[bool]) -> None:
        self.stores = stores
        self.sequence = sequence
        self.step = 0

    def fetch(self, parts: list[str], store: str) -> list[StoreAvailability]:
        results = []
        for s in self.stores:
            available = self.sequence[self.step] and s == self.stores[0]
            for p in parts:
                results.append(
                    StoreAvailability(
                        store_number=s,
                        store_name=STORES[s],
                        part_number=p,
                        available=available,
                        pickup_display="available" if available else "unavailable",
                        pickup_quote="本日" if available else "現在ご利用いただけません",
                        store_url="",
                    )
                )
        return results


def cmd_simulate(args: argparse.Namespace) -> None:
    settings = ensure_settings(interactive=True)
    variants = select_variants(args)
    sequence = [False, True, True, False, True]
    labels = {False: "在庫なし", True: "在庫あり"}
    stores = settings.stores[:3]
    print(f"\n{STORES[stores[0]]} の在庫を {' → '.join(labels[s] for s in sequence)} と変化させ、通知を確認します。")
    print("期待される結果: 2 回目（復活）と 5 回目（再入荷）の 2 通だけメールが届き、3 回目（在庫ありが継続）は通知されない。\n")
    with tempfile.TemporaryDirectory() as tmp:
        source = ScriptedSource(stores, sequence)
        notifier = make_notifier(settings, Path(tmp))
        monitor = Monitor(
            variants,
            stores,
            source,
            StateStore(Path(tmp) / "state.json"),
            notifier,
            mac_notification=settings.mac_notification,
            test_mode=True,
        )
        sent_total = 0
        for step, available in enumerate(sequence):
            source.step = step
            result = monitor.check_once()
            sent = notifier.flush()
            sent_total += sent
            print(f"  {step + 1} 回目: {labels[available]} → 通知{'あり' if result.restocked else 'なし'}（送信 {sent} 通）")
    print(f"\n合計 {sent_total} 通送信しました（期待値 2 通）。{', '.join(settings.mail_to)} の受信箱を確認してください。")
    if sent_total != 2:
        sys.exit(1)


def cmd_install(args: argparse.Namespace) -> None:
    settings = ensure_settings(interactive=True)
    variants = select_variants(args)
    agent.save_selection(list(dict.fromkeys(v.color for v in variants)), list(dict.fromkeys(v.capacity for v in variants)))
    try:
        built = agent.build_app()
        agent.register()
    except agent.AgentError as e:
        sys.exit(f"常時監視の登録に失敗しました: {e}")
    print(f"\n常時監視を登録しました: {', '.join(v.display_name for v in variants)}")
    print(f"  通知先: {', '.join(settings.mail_to)} / 店舗: {len(settings.stores)} 店舗")
    print("  ログインしている間は自動で動き続け、再起動後も自動で開始します。")
    if built:
        print("  まもなく「“DuoChecker”が“Safari”を制御しようとしています」という確認が出るので「OK」を押してください。")
    print(f"  ログ: {LOG_DIR / 'monitor.log'}")
    print("  状態確認: uv run duo-checker status    解除: uv run duo-checker uninstall")


def cmd_uninstall(args: argparse.Namespace) -> None:
    agent.unregister()
    print("常時監視を解除しました。")


def cmd_status(args: argparse.Namespace) -> None:
    info = agent.status()
    if info is None:
        print("常時監視: 未登録")
    else:
        print(f"常時監視: 登録済み（state={info.get('state', '?')}, pid={info.get('pid', '-')}）")
    log_file = LOG_DIR / "monitor.log"
    if log_file.exists():
        print(f"\n直近のログ（{log_file}）:")
        for line in log_file.read_text(encoding="utf-8").splitlines()[-8:]:
            print("  " + line[:220])


def main() -> None:
    parser = argparse.ArgumentParser(prog="duo-checker", description="iPhone Duo の店頭受け取り在庫を監視してメールで通知します")
    sub = parser.add_subparsers(dest="command")

    def variant_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--color", help=f"カラー（{' / '.join(COLORS)}、カンマ区切りで複数可）")
        p.add_argument("--capacity", help=f"容量（{' / '.join(CAPACITIES)}、カンマ区切りで複数可）")

    run = sub.add_parser("run", help="監視を開始する（既定）")
    variant_args(run)
    run.add_argument("--once", action="store_true", help="1 回だけ確認して終了する")
    run.add_argument("--agent", action="store_true", help=argparse.SUPPRESS)  # DuoChecker.app からの起動用
    variant_args(sub.add_parser("install", help="常時監視を launchd に登録する"))
    variant_args(sub.add_parser("simulate", help="在庫復活を模擬して通知を確認する"))
    sub.add_parser("uninstall", help="常時監視を解除する")
    sub.add_parser("status", help="常時監視の状態を表示する")
    sub.add_parser("setup", help="メール設定をやり直す")
    sub.add_parser("test-email", help="テストメールを送る")
    diagnose = sub.add_parser("diagnose", help="API・通信方式を比較する（状態・通知を変更しない）")
    variant_args(diagnose)
    diagnose.add_argument("--store", default="R718", help="診断対象の店舗番号（既定: R718）")

    commands = {
        "run": cmd_run,
        "install": cmd_install,
        "uninstall": cmd_uninstall,
        "status": cmd_status,
        "setup": cmd_setup,
        "test-email": cmd_test_email,
        "diagnose": cmd_diagnose,
        "simulate": cmd_simulate,
    }
    argv = sys.argv[1:]
    if not argv or (argv[0] not in commands and argv[0] not in ("-h", "--help")):
        argv = ["run", *argv]  # サブコマンド省略時は run
    args = parser.parse_args(argv)
    try:
        commands[args.command](args)
    except KeyboardInterrupt:
        print()
