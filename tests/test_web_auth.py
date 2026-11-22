"""Exercise the real Streamlit entry point with isolated server-side accounts."""

from collections import deque
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from web import auth


APP = Path(__file__).resolve().parents[1] / "web" / "app.py"
PASSWORD = "a long test password"


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    path = tmp_path / "web-accounts.json"
    monkeypatch.setattr(auth, "account_path", lambda: path)
    monkeypatch.setattr(auth, "_attempts", deque())
    return path


def _login(app, password):
    app.text_input[0].set_value("alice")
    app.text_input[1].set_value(password)
    return app.button[0].click().run()


@pytest.mark.parametrize("bad_file", [None, "{broken", '{"alice": "plaintext"}'])
def test_unconfigured_or_invalid_account_file_blocks_the_app(accounts, bad_file):
    if bad_file is not None:
        accounts.write_text(bad_file, encoding="utf-8")
    app = AppTest.from_file(APP).run()
    assert not app.exception
    assert not app.sidebar.selectbox
    assert not app.sidebar.date_input
    assert app.info if bad_file is None else app.error


def test_login_logout_and_password_rotation(accounts):
    auth._update_account(accounts, "alice", PASSWORD)
    assert PASSWORD not in accounts.read_text(encoding="utf-8")

    app = AppTest.from_file(APP).run()
    assert not app.sidebar.selectbox
    _login(app, "incorrect")
    assert app.error[0].value == "账号或密码错误。"
    assert not app.sidebar.selectbox

    _login(app, PASSWORD)
    assert not app.exception
    assert app.sidebar.selectbox(key="llm_provider_idx")
    assert app.sidebar.text[0].value == "已登录：alice"
    assert not AppTest.from_file(APP).run().sidebar.selectbox

    app.sidebar.button(key="web_logout").click().run()
    assert not app.sidebar.selectbox
    assert app.text_input[1].value == ""

    _login(app, PASSWORD)
    auth._update_account(accounts, "alice", "a different long password")
    app.run()
    assert not app.sidebar.selectbox
    _login(app, "a different long password")
    assert app.sidebar.selectbox(key="llm_provider_idx")
    auth._update_account(accounts, "alice", None)
    app.run()
    assert not app.sidebar.selectbox


def test_attempt_limit_survives_new_browser_sessions(accounts, monkeypatch):
    auth._update_account(accounts, "alice", PASSWORD)
    monkeypatch.setattr(auth, "_ATTEMPTS_PER_MINUTE", 1)
    _login(AppTest.from_file(APP).run(), "incorrect")
    second = _login(AppTest.from_file(APP).run(), PASSWORD)
    assert second.warning[0].value == "登录尝试过多，请稍后再试。"
    assert not second.sidebar.selectbox


def test_reinstall_does_not_overwrite_account_file(accounts):
    auth._update_account(accounts, "alice", PASSWORD, initialize=True)
    initial = accounts.read_bytes()
    assert not auth._update_account(accounts, "alice", "a different long password", initialize=True)
    assert accounts.read_bytes() == initial
