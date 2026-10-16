"""常時監視（launchd への登録）。

launchd から python を直接起動すると、Safari を操作する主体が Homebrew などの Python.app になる。
Python.app は Info.plist に NSAppleEventsUsageDescription を持たないため、macOS の許可ダイアログが
出ないまま Apple Event がタイムアウトし、システム設定でも許可できない。

そこで AppleScript のアプレット「DuoChecker.app」を osacompile で作り、その子プロセスとして監視を動かす。
Safari への操作は DuoChecker の名前で許可を求めることになる。色・容量は var/agent.json に置き、
アプリの中身（＝署名）を変えずに選び直せるようにして、一度与えた許可が引き継がれるようにする。
"""

import json
import logging
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .settings import DATA_DIR, LOG_DIR, ROOT

LABEL = "local.duo-checker"
BUNDLE_ID = "local.duo-checker"
APP_PATH = DATA_DIR / "DuoChecker.app"
AGENT_CONFIG = DATA_DIR / "agent.json"
log = logging.getLogger(__name__)
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
USAGE = "iPhone Duo の店頭受け取り在庫を確認するため、Safari で Apple Store のページを操作します。"


class AgentError(Exception):
    pass


def _applescript_string(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def applet_source() -> str:
    command = (
        f"cd {shlex.quote(str(ROOT))} && exec {shlex.quote(str(Path(sys.prefix) / "bin" / "python"))} -m duo_checker run --agent"
        f" >> {shlex.quote(str(LOG_DIR / 'launchd.log'))} 2>&1"
    )
    # 失敗してもダイアログを出さずに終了し、launchd に再起動させる
    return (
        "try\n"
        "\twith timeout of 315360000 seconds\n"
        f"\t\tdo shell script {_applescript_string(command)}\n"
        "\tend timeout\n"
        "end try\n"
    )


def build_app() -> bool:
    """DuoChecker.app を作る。内容が変わらなければ作り直さない（許可を引き継ぐため）。作った場合は True。"""
    source = applet_source()
    marker = APP_PATH / "Contents" / "Resources" / "duo-checker-source.applescript"
    if marker.exists() and marker.read_text(encoding="utf-8") == source:
        return False
    shutil.rmtree(APP_PATH, ignore_errors=True)
    APP_PATH.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "DuoChecker.applescript"
        src.write_text(source, encoding="utf-8")
        _run(["osacompile", "-o", str(APP_PATH), str(src)])
    info_path = APP_PATH / "Contents" / "Info.plist"
    with info_path.open("rb") as f:
        info = plistlib.load(f)
    info.update(
        CFBundleIdentifier=BUNDLE_ID,
        CFBundleName="DuoChecker",
        LSUIElement=True,  # Dock に表示しない
        NSAppleEventsUsageDescription=USAGE,
    )
    with info_path.open("wb") as f:
        plistlib.dump(info, f)
    marker.write_text(source, encoding="utf-8")
    _run(["codesign", "--force", "--deep", "--sign", "-", str(APP_PATH)])
    return True


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AgentError(f"{cmd[0]} が失敗しました: {proc.stderr.strip()}")


def save_selection(colors: list[str], capacities: list[str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    AGENT_CONFIG.write_text(json.dumps({"colors": colors, "capacities": capacities}, indent=2), encoding="utf-8")


def load_selection() -> tuple[str, str]:
    if not AGENT_CONFIG.exists():
        raise AgentError(f"{AGENT_CONFIG} がありません。`uv run duo-checker install` をやり直してください")
    data = json.loads(AGENT_CONFIG.read_text(encoding="utf-8"))
    return ",".join(data["colors"]), ",".join(data["capacities"])


def _domain() -> str:
    return f"gui/{os.getuid()}"


def register() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": LABEL,
        "ProgramArguments": [str(APP_PATH / "Contents" / "MacOS" / "applet")],
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 30,
        "StandardOutPath": str(LOG_DIR / "launchd.log"),
        "StandardErrorPath": str(LOG_DIR / "launchd.log"),
    }
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    unregister()
    with PLIST_PATH.open("wb") as f:
        plistlib.dump(plist, f)
    _run(["launchctl", "bootstrap", _domain(), str(PLIST_PATH)])


def unregister() -> None:
    subprocess.run(["launchctl", "bootout", f"{_domain()}/{LABEL}"], capture_output=True)
    if PLIST_PATH.exists():
        PLIST_PATH.unlink()


def status() -> dict[str, str] | None:
    proc = subprocess.run(["launchctl", "print", f"{_domain()}/{LABEL}"], capture_output=True, text=True)
    if proc.returncode != 0:
        return None
    return {k.strip(): v.strip() for k, _, v in (line.partition("=") for line in proc.stdout.splitlines()) if v}


def exit_when_orphaned(stop: Callable[[], None], grace_seconds: float = 15) -> None:
    """DuoChecker.app が終了したら（launchd による停止・再起動）監視も止める。

    launchd は DuoChecker.app を止めても子の python までは止めないため、親がいなくなったことを検知して終了する。
    確認の途中なら終わるまで待ち、grace_seconds を過ぎても終わらなければ強制終了する。
    """
    parent = os.getppid()

    def watch() -> None:
        while os.getppid() == parent:
            time.sleep(1)
        log.info("DuoChecker.app が終了したため監視を止めます")
        stop()
        time.sleep(grace_seconds)
        os._exit(0)

    threading.Thread(target=watch, daemon=True).start()
