"""Interactive Chrome login: CDP cookie capture, cleanup, and ssh hand-off."""

import io
import json
import os
import stat
import sys
from pathlib import Path

import pytest
from websockets.asyncio.server import serve

from amazon_lyr import interactive_login as il
from amazon_lyr.cli_main import main
from amazon_lyr.exceptions import AmazonError
from amazon_lyr.marketplace import get_marketplace
from amazon_lyr.session_state import get_cookies_path, load_cookies

CA = get_marketplace("amazon.ca")
PRE_LOGIN = [
    {"name": "session-id", "value": "s", "domain": ".amazon.ca"},
    {"name": "ubid-acbca", "value": "u", "domain": ".amazon.ca"},
]
LOGGED_IN = PRE_LOGIN + [
    {"name": "at-acbca", "value": "t", "domain": ".amazon.ca"},
    {"name": "at-main", "value": "other-store", "domain": ".amazon.com"},
    {"name": "tracker", "value": "x", "domain": ".doubleclick.net"},
]


class FakeDevTools:
    """Answers Storage.getCookies with pre-login cookies until `login_after` calls."""

    def __init__(self, login_after: int):
        self.login_after = login_after
        self.calls = 0
        self.closed = False

    async def handler(self, ws):
        async for raw in ws:
            msg = json.loads(raw)
            if msg["method"] == "Browser.close":
                self.closed = True
                continue
            self.calls += 1
            cookies = LOGGED_IN if self.calls > self.login_after else PRE_LOGIN
            await ws.send(json.dumps({"id": msg["id"], "result": {"cookies": cookies}}))


def test_filter_cookies_keeps_only_storefront_domain():
    assert il.filter_cookies(LOGGED_IN, CA) == {
        "session-id": "s",
        "ubid-acbca": "u",
        "at-acbca": "t",
    }


async def test_capture_waits_for_login_cookie():
    fake = FakeDevTools(login_after=2)
    async with serve(fake.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        cookies = await il.capture_cookies(
            f"ws://127.0.0.1:{port}/devtools/browser/x", CA, 10, 0.01
        )
    assert cookies == {"session-id": "s", "ubid-acbca": "u", "at-acbca": "t"}
    assert fake.calls >= 3


async def test_capture_times_out():
    fake = FakeDevTools(login_after=10**6)
    async with serve(fake.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        with pytest.raises(il.LoginTimeoutError, match="amazon.ca"):
            await il.capture_cookies(f"ws://127.0.0.1:{port}/x", CA, 0.05, 0.01)


def _fake_chrome(tmp_path: Path, port: int, record: Path) -> str:
    """An executable that behaves like Chrome's DevTools bootstrap and records argv."""
    script = tmp_path / "fake-chrome"
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys, time, json, pathlib\n"
        f"pathlib.Path({str(record)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        "prof = next(a.split('=', 1)[1] for a in sys.argv if a.startswith('--user-data-dir='))\n"
        f"pathlib.Path(prof, 'DevToolsActivePort').write_text('{port}\\n/devtools/browser/x')\n"
        "time.sleep(60)\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


async def test_interactive_login_end_to_end_cleans_up(tmp_path, monkeypatch):
    fake = FakeDevTools(login_after=1)
    record = tmp_path / "argv.json"
    async with serve(fake.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setenv("CHROME_PATH", _fake_chrome(tmp_path, port, record))
        monkeypatch.setattr(il, "capture_cookies", _fast(il.capture_cookies))
        cookies = await il.interactive_login(CA, timeout=10)
    assert cookies["at-acbca"] == "t"
    argv = json.loads(record.read_text())
    profile = Path(next(a.split("=", 1)[1] for a in argv if a.startswith("--user-data-dir=")))
    assert not profile.exists(), "temporary Chrome profile must be deleted"
    assert "--remote-debugging-port=0" in argv
    assert argv[-1] == "https://www.amazon.ca/gp/css/homepage.html"
    assert fake.closed


async def test_interactive_login_cleans_up_on_timeout(tmp_path, monkeypatch):
    fake = FakeDevTools(login_after=10**6)
    record = tmp_path / "argv.json"
    async with serve(fake.handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setenv("CHROME_PATH", _fake_chrome(tmp_path, port, record))
        monkeypatch.setattr(il, "capture_cookies", _fast(il.capture_cookies))
        with pytest.raises(il.LoginTimeoutError):
            await il.interactive_login(CA, timeout=0.05)
    argv = json.loads(record.read_text())
    profile = Path(next(a.split("=", 1)[1] for a in argv if a.startswith("--user-data-dir=")))
    assert not profile.exists()


def _fast(fn):
    async def wrapper(ws_url, market, timeout, poll=2.0):
        return await fn(ws_url, market, timeout, 0.01)

    return wrapper


def test_find_chrome_reports_missing(monkeypatch):
    monkeypatch.setenv("CHROME_PATH", "/nonexistent/chrome")
    with pytest.raises(AmazonError, match="Chrome not found"):
        il.find_chrome()


def test_send_over_ssh_uses_stdin_not_argv(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen.update(cmd=cmd, **kw)
        return type("R", (), {"returncode": 0, "stdout": "mode: full\n", "stderr": ""})()

    monkeypatch.setattr(il.subprocess, "run", fake_run)
    out = il.send_cookies_over_ssh({"at-acbca": "secret"}, "hermes")
    assert out == "mode: full\n"
    assert seen["cmd"][:4] == ["ssh", "-o", "BatchMode=yes", "--"]
    assert seen["cmd"][4] == "hermes"
    assert "secret" not in " ".join(seen["cmd"])
    assert json.loads(seen["input"]) == {"at-acbca": "secret"}


@pytest.mark.parametrize("host", ["-oProxyCommand=touch /tmp/x", "", "a b", "host;rm"])
def test_send_over_ssh_rejects_bad_hosts(host):
    with pytest.raises(AmazonError, match="Invalid ssh destination"):
        il.send_cookies_over_ssh({}, host)


def test_cli_cookies_file_stdin(isolated_config, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"at-acbca": "t", "session-id": "s"})))
    main(["login", "--cookies-file", "-"])
    assert load_cookies()["at-acbca"] == "t"
    assert "amazon.ca" in capsys.readouterr().out
    assert stat.S_IMODE(os.stat(get_cookies_path()).st_mode) == 0o600


def test_cli_interactive_send_to_does_not_save_locally(isolated_config, monkeypatch, capsys):
    async def fake_login(market, timeout):
        assert market.domain == "amazon.ca" and timeout == 30
        return {"at-acbca": "t", "session-id": "s", "ubid-acbca": "u"}

    sent = {}
    monkeypatch.setattr(il, "interactive_login", fake_login)
    monkeypatch.setattr(
        il, "send_cookies_over_ssh", lambda c, h: sent.update(c=c, h=h) or "remote ok\n"
    )
    main(
        [
            "login",
            "--interactive",
            "--marketplace",
            "amazon.ca",
            "--send-to",
            "hermes",
            "--timeout",
            "30",
        ]
    )
    assert sent["h"] == "hermes" and sent["c"]["at-acbca"] == "t"
    assert not get_cookies_path().exists(), "--send-to must not leave cookies on this machine"
    assert "remote ok" in capsys.readouterr().out
