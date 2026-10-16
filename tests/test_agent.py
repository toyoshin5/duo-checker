"""常時監視まわり: 二重起動の防止、親（DuoChecker.app）終了時の停止、アプレットの内容の安定性。"""

import fcntl
import os
import subprocess
import sys
import textwrap
import time

import pytest

from duo_checker import agent, cli


def test_applet_does_not_depend_on_selection(monkeypatch, tmp_path):
    """色・容量を選び直してもアプレットの中身（＝署名）が変わらず、Safari の許可が引き継がれる。"""
    monkeypatch.setattr(agent, "AGENT_CONFIG", tmp_path / "agent.json")
    monkeypatch.setattr(agent, "DATA_DIR", tmp_path)
    agent.save_selection(["starwhite"], ["256gb"])
    first = agent.applet_source()
    agent.save_selection(["nightsky"], ["1tb", "2tb"])
    assert agent.applet_source() == first
    assert agent.load_selection() == ("nightsky", "1tb,2tb")
    assert "--agent" in first and "starwhite" not in first


def test_second_monitor_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "DATA_DIR", tmp_path)
    with open(tmp_path / "monitor.lock", "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)  # 先に動いている監視の代わり
        with pytest.raises(SystemExit, match="すでに別の監視が動いています"):
            cli.acquire_single_instance_lock(wait=False)


def test_lock_is_acquired_when_free(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "DATA_DIR", tmp_path)
    cli.acquire_single_instance_lock(wait=False)
    assert (tmp_path / "monitor.lock").read_text() == str(os.getpid())


def test_monitor_exits_when_parent_app_dies(tmp_path):
    """親プロセスが消えたら（launchd が DuoChecker.app を止めたら）子の監視も終わる。"""
    child_code = textwrap.dedent(
        """
        import sys, time
        from duo_checker import agent
        agent.exit_when_orphaned(lambda: None, grace_seconds=0)
        print("ready", flush=True)
        time.sleep(60)
        """
    )
    parent_code = textwrap.dedent(
        f"""
        import subprocess, sys
        p = subprocess.Popen([sys.executable, "-c", {child_code!r}], stdout=subprocess.PIPE, text=True)
        assert p.stdout.readline().strip() == "ready"
        print(p.pid, flush=True)
        """
    )
    out = subprocess.run([sys.executable, "-c", parent_code], capture_output=True, text=True, timeout=30)
    child_pid = int(out.stdout.strip())
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.2)
    os.kill(child_pid, 9)
    pytest.fail("親が終了しても子の監視が止まらなかった")
