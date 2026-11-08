"""Interactive login: you sign in to Amazon in a real Chrome window, we keep the cookies.

Flow:
1. Launch the installed Google Chrome with a fresh temporary profile (mode 700)
   and the DevTools protocol on a random loopback port, at the storefront's
   account page (which redirects to sign-in).
2. You log in yourself — password, OTP, CAPTCHA, passkey. Nothing is automated
   or typed for you, and no credentials pass through this code.
3. Poll ``Storage.getCookies`` over the local DevTools WebSocket until the
   storefront's login cookie (``at-main``, ``at-acbca``, ...) appears.
4. Keep only cookies for that storefront's domain, close Chrome and delete the
   temporary profile — also on timeout, Ctrl-C or error.

``send_cookies_over_ssh`` pipes the result to ``amazon-lyr login --cookies-file -``
on another machine via stdin, so cookies never appear in argv or on disk locally.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from amazon_lyr.exceptions import AmazonError
from amazon_lyr.logging_config import logger
from amazon_lyr.marketplace import Marketplace

CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
)
# ssh destination: [user@]host or an ~/.ssh/config alias. No leading "-" so it
# can't be parsed as an ssh option.
_SSH_HOST_RE = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_.@:-]{0,253}")
_REMOTE_LOGIN = 'PATH="$HOME/.local/bin:$PATH" amazon-lyr login --cookies-file -'


class LoginTimeoutError(AmazonError):
    """The user didn't finish signing in before the timeout."""


def find_chrome() -> str:
    """Path to a Chrome/Chromium binary. ``CHROME_PATH`` overrides the search."""
    override = os.getenv("CHROME_PATH")
    candidates = (override,) if override else CHROME_CANDIDATES
    for cand in candidates:
        path = cand if os.path.isabs(cand) else shutil.which(cand)
        if path and os.access(path, os.X_OK):
            return path
    raise AmazonError(
        "Google Chrome not found. Install it, or set CHROME_PATH to a Chrome/Chromium binary."
    )


def filter_cookies(raw: list[dict], market: Marketplace) -> dict[str, str]:
    """Keep only cookies scoped to the storefront's domain (e.g. .amazon.ca)."""
    out: dict[str, str] = {}
    for c in raw:
        domain = str(c.get("domain", "")).lstrip(".").lower()
        if domain == market.domain or domain.endswith("." + market.domain):
            out[str(c["name"])] = str(c["value"])
    return out


def is_logged_in(cookies: dict[str, str], market: Marketplace) -> bool:
    return bool(cookies.get(market.login_cookie)) and "session-id" in cookies


def _chrome_failure(problem: str, chrome: str, log: Path) -> AmazonError:
    """Error with Chrome's own last stderr lines, its version and likely fixes."""
    try:
        lines = [
            ln.strip()
            for ln in log.read_text(errors="replace").splitlines()
            if ln.strip() and "mach_o_image_annotations_reader" not in ln
        ]
    except OSError:
        lines = []
    try:
        version = subprocess.run(
            [chrome, "--version"], capture_output=True, text=True, timeout=10
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        version = ""
    parts = [problem]
    if version:
        parts.append(f"Browser: {version} ({chrome}).")
    if lines:
        parts.append("Chrome said: " + " | ".join(lines[-4:]))
    if os.getenv("SSH_CONNECTION"):
        parts.append(
            "This is an SSH session, where Chrome can't open a window. Run the login "
            "on a computer with a desktop and add --send-to <this host>."
        )
    parts.append(
        "If Chrome is outdated, update it (or point CHROME_PATH at a current "
        "Chrome/Chromium), or log in from another computer with --send-to."
    )
    return AmazonError(" ".join(parts))


async def _read_devtools_endpoint(
    profile: Path, proc: subprocess.Popen, deadline: float, chrome: str, log: Path
) -> str:
    """Chrome writes '<port>\\n<ws path>' to DevToolsActivePort once it's listening."""
    marker = profile / "DevToolsActivePort"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise _chrome_failure("Chrome exited before it was ready.", chrome, log)
        if marker.exists():
            lines = marker.read_text().split()
            if len(lines) >= 2 and lines[0].isdigit():
                return f"ws://127.0.0.1:{lines[0]}{lines[1]}"
        await asyncio.sleep(0.2)
    raise _chrome_failure("Chrome didn't open its DevTools port within 30s.", chrome, log)


async def capture_cookies(
    ws_url: str, market: Marketplace, timeout: float, poll: float = 2.0
) -> dict[str, str]:
    """Poll the browser's cookie store until the login cookie appears."""
    from websockets.asyncio.client import connect  # noqa: PLC0415

    deadline = time.monotonic() + timeout
    async with connect(ws_url, max_size=None, open_timeout=10) as ws:
        msg_id = 0
        while True:
            msg_id += 1
            await ws.send(json.dumps({"id": msg_id, "method": "Storage.getCookies"}))
            while True:
                reply = json.loads(await ws.recv())
                if reply.get("id") == msg_id:
                    break
            cookies = filter_cookies(reply.get("result", {}).get("cookies", []), market)
            if is_logged_in(cookies, market):
                # Let the post-login redirects finish setting the rest of the session.
                await asyncio.sleep(min(poll, 2.0))
                await ws.send(json.dumps({"id": msg_id + 1, "method": "Storage.getCookies"}))
                while True:
                    reply = json.loads(await ws.recv())
                    if reply.get("id") == msg_id + 1:
                        break
                final = filter_cookies(reply.get("result", {}).get("cookies", []), market)
                return final if is_logged_in(final, market) else cookies
            if time.monotonic() >= deadline:
                raise LoginTimeoutError(
                    f"Timed out after {int(timeout)}s waiting for you to sign in to "
                    f"{market.domain}. Run the command again to retry."
                )
            await asyncio.sleep(poll)


async def _close_browser(ws_url: str) -> None:
    from websockets.asyncio.client import connect  # noqa: PLC0415

    try:
        async with connect(ws_url, open_timeout=3) as ws:
            await ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
    except Exception:  # noqa: BLE001 - best effort; we terminate the process anyway
        pass


def _stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


async def interactive_login(market: Marketplace, timeout: float = 600) -> dict[str, str]:
    """Open Chrome on the storefront, wait for the user to sign in, return its cookies."""
    chrome = find_chrome()
    profile = Path(tempfile.mkdtemp(prefix="amazon-lyr-login-"))
    profile.chmod(0o700)
    start_url = f"{market.base_url}/gp/css/homepage.html"
    log_fd, log_name = tempfile.mkstemp(prefix="amazon-lyr-chrome-", suffix=".log")
    log = Path(log_name)
    proc = subprocess.Popen(
        [
            chrome,
            f"--user-data-dir={profile}",
            "--remote-debugging-port=0",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
            "--new-window",
            start_url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=log_fd,
    )
    os.close(log_fd)
    ws_url = None
    try:
        ws_url = await _read_devtools_endpoint(profile, proc, time.monotonic() + 30, chrome, log)
        logger.info("Waiting for sign-in to %s in the Chrome window…", market.domain)
        return await capture_cookies(ws_url, market, timeout)
    finally:
        if ws_url:
            await _close_browser(ws_url)
        _stop(proc)
        shutil.rmtree(profile, ignore_errors=True)
        log.unlink(missing_ok=True)


def send_cookies_over_ssh(cookies: dict[str, str], host: str) -> str:
    """Import cookies on ``host`` via ``amazon-lyr login --cookies-file -`` over ssh."""
    if not _SSH_HOST_RE.fullmatch(host):
        raise AmazonError(f"Invalid ssh destination {host!r} (use host, user@host or an alias).")
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "--", host, _REMOTE_LOGIN],
        input=json.dumps(cookies),
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-500:]
        raise AmazonError(f"Sending the session to {host} failed: {detail}")
    return result.stdout


def print_instructions(market: Marketplace, send_to: str | None) -> None:
    where = f" and sent to {send_to}" if send_to else ""
    print(
        f"A Chrome window is opening on {market.domain}. Sign in there (including any\n"
        f"one-time code). The session is captured automatically{where}, then the\n"
        "window closes and its temporary profile is deleted.",
        file=sys.stderr,
    )
