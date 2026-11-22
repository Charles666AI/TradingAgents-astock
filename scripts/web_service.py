"""Install, start, stop or uninstall the Docker Compose Web service."""

import argparse
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent.parent


def _compose(*args: str) -> None:
    subprocess.run(["docker", "compose", *args], cwd=ROOT, check=True)


def _start() -> None:
    _compose("up", "--detach", "--no-build", "--pull", "never", "--wait",
             "--wait-timeout", "60", "web")
    print("Web 服务已启动。访问地址的端口映射如下。", flush=True)
    _compose("port", "web", "8501")


def main() -> int:
    parser = argparse.ArgumentParser(description="通过 Docker Compose 管理 TradingAgents Web 服务")
    parser.add_argument("action", choices=("install", "start", "stop", "uninstall"))
    parser.add_argument("--username", default="admin", help="首次安装时创建的用户名（默认 admin）")
    args = parser.parse_args()

    if not (ROOT / ".env").exists() and args.action != "install":
        print("找不到 .env，请先执行 install。", file=sys.stderr)
        return 1
    try:
        _compose("version", "--short")
        if args.action == "install":
            env = ROOT / ".env"
            if not env.exists():
                template = (ROOT / ".env.example").read_bytes()
                try:
                    with env.open("xb") as handle:
                        handle.write(template)
                except FileExistsError:
                    pass
                else:
                    print("已创建 .env。可按所选模型供应商在其中补充配置。", flush=True)
            _compose("build", "web")
            _compose("run", "--rm", "--no-deps", "--entrypoint", "python", "web",
                     "-m", "web.auth", "init", args.username)
            _start()
        elif args.action == "start":
            _start()
        elif args.action == "stop":
            _compose("stop", "web")
        else:
            _compose("down", "--rmi", "local", "web")
            print("Web 服务已卸载。账号、配置和分析数据已保留。")
    except FileNotFoundError as exc:
        print(f"缺少运行所需的命令或文件。请确认 Docker 和 Compose 可用。{exc}", file=sys.stderr)
        return 1
    except subprocess.CalledProcessError as exc:
        print("Docker Compose 操作失败。请查看上方错误。", file=sys.stderr)
        return exc.returncode
    except OSError as exc:
        print(f"无法读取或写入项目配置。{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
