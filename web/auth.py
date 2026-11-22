"""Server-side accounts and session gate for the Streamlit UI."""

from __future__ import annotations

import argparse
from collections import deque
from getpass import GetPassWarning, getpass
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import tempfile
from threading import Lock
import time
import warnings

from tradingagents.dataflows.files import locked


_SESSION_KEY = "_web_login"
_SESSION_SECONDS = 8 * 60 * 60
_PASSWORD_MAX = 256
_ROUNDS = 600_000
_USERNAME = re.compile(r"[a-zA-Z0-9_.@-]{1,64}")
_ATTEMPTS_PER_MINUTE = 10
_attempts: deque[float] = deque()
_attempt_lock = Lock()
_logger = logging.getLogger(__name__)


def account_path() -> Path:
    configured = os.getenv("TRADINGAGENTS_WEB_ACCOUNTS_FILE")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".tradingagents" / "web-accounts.json"


def _load_accounts(path: Path) -> dict[str, str]:
    """Read the latest account records so password resets revoke active sessions."""
    try:
        accounts = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(accounts, dict) or any(
        not _USERNAME.fullmatch(name) or not isinstance(record, str)
        or not re.fullmatch(r"pbkdf2-sha256-v1\$[a-f0-9]{32}\$[a-f0-9]{64}", record)
        for name, record in accounts.items()
    ):
        raise ValueError("Invalid Web account file format")
    return accounts


def _derive(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), _ROUNDS).hex()


def _verify(password: str, record: str | None) -> bool:
    # Unknown users also run the password hash to avoid a username timing signal.
    salt, expected = record.split("$")[1:] if record else ("0" * 32, "0" * 64)
    matches = hmac.compare_digest(_derive(password, salt), expected)
    return record is not None and matches


def _reserve_attempt() -> bool:
    """Bound password guesses across browser sessions in this Web process."""
    with _attempt_lock:
        now = time.monotonic()
        while _attempts and now - _attempts[0] >= 60:
            _attempts.popleft()
        if len(_attempts) >= _ATTEMPTS_PER_MINUTE:
            return False
        _attempts.append(now)
        return True


def _credential_version(record: str) -> str:
    return hashlib.sha256(record.encode("ascii")).hexdigest()


def require_login() -> None:
    """Stop the run before any protected widgets or application data are loaded."""
    import streamlit as st

    path = account_path()
    try:
        accounts = _load_accounts(path)
    except (OSError, ValueError):
        _logger.exception("Could not read Web account configuration")
        st.session_state.clear()
        st.error("登录配置无法读取，请联系管理员。")
        st.stop()

    session = st.session_state.get(_SESSION_KEY)
    if session:
        username, version, expires = session
        record = accounts.get(username)
        if record and time.monotonic() < expires and _credential_version(record) == version:
            with st.sidebar:
                st.text(f"已登录：{username}")
                if st.button("退出登录", key="web_logout"):
                    st.session_state.clear()
                    st.rerun()
            return
        st.session_state.clear()

    st.title("TradingAgents A 股分析")
    if not accounts:
        st.info("尚未配置访问账号。请在服务器执行 `python -m web.auth set 用户名`。")
        st.stop()

    with st.form("web_login", clear_on_submit=True):
        username = st.text_input("账号", max_chars=64).strip()
        password = st.text_input("密码", type="password", max_chars=_PASSWORD_MAX * 2)
        submitted = st.form_submit_button("登录", type="primary")

    if submitted:
        record = accounts.get(username)
        if not _reserve_attempt():
            st.warning("登录尝试过多，请稍后再试。")
        elif len(password) <= _PASSWORD_MAX and _verify(password, record):
            st.session_state.clear()
            st.session_state[_SESSION_KEY] = (
                username, _credential_version(record), time.monotonic() + _SESSION_SECONDS,
            )
            st.rerun()
        else:
            st.error("账号或密码错误。")
    st.stop()


def _update_account(
    path: Path, username: str, password: str | None, *, initialize: bool = False,
) -> bool:
    if not _USERNAME.fullmatch(username):
        raise ValueError("用户名须为 1–64 个字母、数字或 _ . @ -")
    record = None
    if password is not None:
        if not 12 <= len(password) <= _PASSWORD_MAX:
            raise ValueError(f"密码须为 12–{_PASSWORD_MAX} 个字符")
        salt = secrets.token_hex(16)
        record = f"pbkdf2-sha256-v1${salt}${_derive(password, salt)}"

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with locked(path):
        accounts = _load_accounts(path)
        if initialize and accounts:
            return False
        if record is None:
            if username not in accounts:
                raise ValueError("账号不存在")
            del accounts[username]
        else:
            accounts[username] = record

        # mkstemp creates a private file on POSIX. On Windows it inherits the directory ACL.
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(accounts, handle, indent=2)
            os.replace(temp_name, path)
        finally:
            Path(temp_name).unlink(missing_ok=True)
    return True


def main() -> None:
    from dotenv import load_dotenv
    import sys

    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=True)
    parser = argparse.ArgumentParser(description="管理 Web 访问账号")
    parser.add_argument("action", choices=("init", "set", "remove"),
                        help="init 仅首次创建账号，set 添加或重置，remove 移除")
    parser.add_argument("username")
    args = parser.parse_args()
    try:
        path = account_path()
        if args.action == "init" and _load_accounts(path):
            print("已有 Web 访问账号。")
            return
        password = None
        if args.action != "remove":
            if not sys.stdin.isatty():
                parser.error("请在交互式终端输入密码")
            with warnings.catch_warnings():
                warnings.simplefilter("error", GetPassWarning)
                password = getpass("新密码（至少 12 个字符）：")
                if password != getpass("确认密码："):
                    parser.error("两次输入的密码不同")
        if not _update_account(path, args.username, password, initialize=args.action == "init"):
            print("已有 Web 访问账号。")
            return
    except (GetPassWarning, EOFError):
        parser.exit(1, "无法隐藏密码输入，请使用交互式终端。\n")
    except (OSError, ValueError) as exc:
        parser.exit(1, f"更新 Web 账号失败：{exc}\n")
    print(f"Web 账号已更新。配置文件：{path}")


if __name__ == "__main__":
    main()
