import sys

import pytest

from scripts import web_service


@pytest.mark.parametrize("action, commands", [
    ("install", [
        ("build", "web"),
        ("run", "--rm", "--no-deps", "--entrypoint", "python", "web", "-m", "web.auth", "init", "admin"),
        ("up", "--detach", "--no-build", "--pull", "never", "--wait", "--wait-timeout", "60", "web"),
        ("port", "web", "8501"),
    ]),
    ("start", [
        ("up", "--detach", "--no-build", "--pull", "never", "--wait", "--wait-timeout", "60", "web"),
        ("port", "web", "8501"),
    ]),
    ("stop", [("stop", "web")]),
    ("uninstall", [("down", "--rmi", "local", "web")]),
])
def test_only_manages_web_and_never_removes_config(action, commands, tmp_path, monkeypatch):
    monkeypatch.setattr(web_service, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["web_service.py", action])
    calls = []
    monkeypatch.setattr(web_service, "_compose", lambda *args: calls.append(args))
    config = tmp_path / ".env"
    config.write_bytes(b"EXISTING_CONFIG=1\n")
    assert web_service.main() == 0
    assert calls == [("version", "--short"), *commands]
    assert config.read_bytes() == b"EXISTING_CONFIG=1\n"


def test_first_install_creates_env_before_build(tmp_path, monkeypatch):
    monkeypatch.setattr(web_service, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["web_service.py", "install"])
    (tmp_path / ".env.example").write_bytes(b"MODEL_SETTING=\n")

    def compose(*args):
        if args[0] == "build":
            assert (tmp_path / ".env").read_bytes() == b"MODEL_SETTING=\n"

    monkeypatch.setattr(web_service, "_compose", compose)
    assert web_service.main() == 0
