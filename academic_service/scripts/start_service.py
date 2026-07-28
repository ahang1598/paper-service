#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""paper-service 一键启动脚本（在 WSL Linux 内运行）。

功能：
    1. 依赖检查：补全 uv 所在 PATH → 校验 .venv 与关键包 → 缺失则自动 uv sync。
    2. 端口冲突处理：若服务端口已被占用，先优雅关闭旧进程（SIGTERM→SIGKILL）。
    3. 后台启动 uvicorn：脱离脚本进程组（setsid），脚本退出后服务继续运行。
    4. 日志按启动时间戳归档：logs/service_YYYYMMDD_HHMMSS.log，每次启动一个新文件。
    5. 健康检查：轮询 /health 确认服务真正可用。

用法（在 WSL 内、项目根目录）：
    python3 start_service.py           # 启动
    python3 start_service.py --help    # 查看选项

环境前提（已在 WSL Ubuntu-22.04 探测确认）：
    - uv 位于 ~/.local/bin/uv（非交互 shell 的 PATH 里没有，本脚本会显式补）
    - .venv 由 uv 管理，uvicorn/fastapi 通常已装好
    - 端口检测用 ss（netstat 在 WSL 不可用），回退 lsof/fuser
"""

from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# =====================================================================
# 常量
# =====================================================================

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 12135
# 应用统一以 academic_service.app.* 为包根导入，故 uvicorn 加载的模块需带包前缀；
# 同时必须把项目父目录加入 PYTHONPATH（见 start_uvicorn），否则报
# "ModuleNotFoundError: No module named 'academic_service'"。
APP_MODULE = "academic_service.app.main:app"
HEALTH_PATH = "/health"

# 关键运行期包（import 失败说明 .venv 不完整，需要 uv sync）
REQUIRED_IMPORTS = ["fastapi", "uvicorn", "pydantic_settings", "requests", "yaml"]

# 健康检查轮询参数
HEALTH_CHECK_TIMEOUT = 20   # 总等待秒数
HEALTH_CHECK_INTERVAL = 0.5 # 每次探活间隔

# 关闭旧进程的等待参数
TERM_WAIT_SEC = 8           # SIGTERM 后最多等这么久


# =====================================================================
# 工具函数
# =====================================================================

def info(msg: str) -> None:
    print(f"[INFO] {msg}", flush=True)


def ok(msg: str) -> None:
    print(f"[OK]   {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"[WARN] {msg}", flush=True)


def fail(msg: str) -> None:
    print(f"[FAIL] {msg}", flush=True)


def ensure_uv_on_path() -> None:
    """把 uv 常见安装目录加入 PATH（非交互登录 shell 通常拿不到 ~/.local/bin）。"""
    candidates = [
        os.path.expanduser("~/.local/bin"),
        os.path.expanduser("~/.cargo/bin"),
    ]
    added = []
    for c in candidates:
        if c and Path(c).is_dir() and c not in os.environ.get("PATH", ""):
            os.environ["PATH"] = c + os.pathsep + os.environ["PATH"]
            added.append(c)
    if added:
        info(f"已将以下目录加入 PATH: {added}")


def project_root() -> Path:
    """项目根（含 pyproject.toml 的目录，即 academic_service/）。

    从脚本位置向上查找 pyproject.toml，使脚本可放在 scripts/ 等子目录中。
    """
    here = Path(__file__).resolve().parent
    for candidate in (here, *here.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate
    # 兜底：脚本所在目录的上一级（兼容直接放在根目录的旧布局）
    return here.parent


def project_parent() -> Path:
    """项目父目录（academic_service 的上一级，即 paper-service/）。

    应用以 ``academic_service.app.*`` 为包根导入，需把此目录加入 sys.path，
    ``academic_service`` 才能作为（命名空间）包被导入。
    """
    return project_root().parent


def _script_relpath() -> str:
    """脚本相对项目根的路径（如 scripts/start_service.py），用于提示信息。"""
    try:
        return str(Path(__file__).resolve().relative_to(project_root()))
    except ValueError:
        return Path(__file__).name


def venv_python(root: Path) -> Path:
    return root / ".venv" / "bin" / "python"


def venv_uvicorn(root: Path) -> Path:
    return root / ".venv" / "bin" / "uvicorn"


# =====================================================================
# 1. 依赖检查
# =====================================================================

def check_uv() -> str:
    """确认 uv 可用，返回 uv 可执行路径；不可用则报错退出。"""
    uv_path = shutil.which("uv")
    if uv_path:
        return uv_path
    fail("未找到 uv 命令。请先安装 uv：")
    print("    curl -LsSf https://astral.sh/uv/install.sh | sh")
    print("  安装后重新运行本脚本（脚本会自动把 ~/.local/bin 加入 PATH）。")
    sys.exit(1)


def venv_healthy(root: Path) -> bool:
    """.venv 存在且关键包可导入。"""
    py = venv_python(root)
    if not py.exists():
        return False
    code = "import " + ", ".join(REQUIRED_IMPORTS)
    try:
        subprocess.run(
            [str(py), "-c", code],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        return True
    except Exception:
        return False


def ensure_dependencies(root: Path) -> None:
    """依赖检查：.venv 不完整则 uv sync --extra dev。"""
    uv_path = check_uv()
    if venv_healthy(root):
        ok(f".venv 完整，关键包可导入（{', '.join(REQUIRED_IMPORTS)}）")
        return

    if not (root / ".venv").exists():
        info(".venv 不存在，执行 uv sync --extra dev 创建虚拟环境并安装依赖...")
    else:
        info(".venv 存在但关键包缺失，执行 uv sync --extra dev 修复...")

    try:
        subprocess.run(
            [uv_path, "sync", "--extra", "dev"],
            check=True,
            cwd=str(root),
        )
    except subprocess.CalledProcessError as e:
        fail(f"uv sync 失败（退出码 {e.returncode}）。请检查网络或 pyproject.toml。")
        sys.exit(1)

    if not venv_healthy(root):
        fail("uv sync 完成后关键包仍无法导入，请手动排查 .venv。")
        sys.exit(1)
    ok("依赖安装完成，.venv 已就绪")


# =====================================================================
# 2. 端口检测与关闭旧服务
# =====================================================================

def find_pid_on_port(port: int):
    """返回占用该端口的 PID（int），无占用返回 None。

    优先用 ss（WSL 原生），回退 lsof / fuser。
    """
    # ss: 解析 "pid=1234" 形式
    ss = shutil.which("ss")
    if ss:
        try:
            out = subprocess.run(
                [ss, "-tlnp"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            for line in out.splitlines():
                # 形如: LISTEN 0 128 0.0.0.0:12135 0.0.0.0:* users:(("uvicorn",pid=1234,fd=5))
                if f":{port} " in line or f":{port}\t" in line:
                    if "pid=" in line:
                        pid_str = line.split("pid=", 1)[1].split(",", 1)[0].split(")", 1)[0]
                        pid = int(pid_str.strip())
                        if pid > 0:
                            return pid
                    break
        except Exception:
            pass

    # lsof 回退
    lsof = shutil.which("lsof")
    if lsof:
        try:
            out = subprocess.run(
                [lsof, "-ti", f":{port}"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            pids = [int(x) for x in out.split() if x.isdigit()]
            if pids:
                return pids[0]
        except subprocess.CalledProcessError:
            pass  # lsof 无匹配时退出码非 0
        except Exception:
            pass

    # fuser 回退
    fuser = shutil.which("fuser")
    if fuser:
        try:
            out = subprocess.run(
                [fuser, f"{port}/tcp"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            pids = [int(x) for x in out.split() if x.isdigit()]
            if pids:
                return pids[0]
        except Exception:
            pass

    return None


def port_in_use(port: int) -> bool:
    return find_pid_on_port(port) is not None


def kill_pid(pid: int) -> None:
    """先 SIGTERM 优雅关闭，超时则 SIGKILL。"""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        fail(f"无权限关闭 PID={pid}（可能需要更高权限）。")
        sys.exit(1)

    deadline = time.time() + TERM_WAIT_SEC
    while time.time() < deadline:
        try:
            os.kill(pid, 0)  # 探测是否仍存活
        except ProcessLookupError:
            return
        time.sleep(0.3)

    # 仍存活 → SIGKILL
    warn(f"PID={pid} 在 {TERM_WAIT_SEC}s 内未退出，改用 SIGKILL 强制关闭")
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    time.sleep(0.5)


def ensure_port_free(port: int) -> None:
    """若端口被占用，关闭旧服务并确认释放。"""
    pid = find_pid_on_port(port)
    if pid is None:
        return
    warn(f"端口 {port} 已被占用（PID={pid}），正在关闭旧服务...")
    kill_pid(pid)
    # 确认释放
    time.sleep(0.5)
    if port_in_use(port):
        # 可能有子进程，再扫一次
        pid2 = find_pid_on_port(port)
        if pid2:
            warn(f"端口仍被 PID={pid2} 占用，尝试再次关闭")
            kill_pid(pid2)
            time.sleep(0.5)
    if port_in_use(port):
        fail(f"无法释放端口 {port}，请手动排查后重试。")
        sys.exit(1)
    ok(f"旧服务已关闭，端口 {port} 已释放")


# =====================================================================
# 3. 后台启动 + 时间戳日志
# =====================================================================

def start_timestamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def start_uvicorn(root: Path, host: str, port: int) -> tuple[int, Path]:
    """后台启动 uvicorn，返回 (pid, 日志路径)。

    使用 start_new_session=True（等价 setsid）使服务脱离脚本进程组，
    脚本退出后服务继续运行；stdout/stderr 重定向到时间戳日志文件。
    """
    logs_dir = root / "logs"
    logs_dir.mkdir(exist_ok=True)
    log_path = logs_dir / f"service_{start_timestamp()}.log"

    uvicorn_bin = venv_uvicorn(root)
    if not uvicorn_bin.exists():
        fail(f"未找到 uvicorn 可执行文件: {uvicorn_bin}")
        sys.exit(1)

    log_file = open(log_path, "ab", buffering=0)

    # 让 academic_service 作为包可被导入：把项目父目录加入 PYTHONPATH。
    # cwd 仍保持项目根（academic_service/），保证 .env 与 configs/ 的相对解析正确。
    env = os.environ.copy()
    parent = project_parent()
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        p for p in [str(parent), existing_pythonpath] if p
    )

    proc = subprocess.Popen(
        [
            str(uvicorn_bin), APP_MODULE,
            "--host", host,
            "--port", str(port),
        ],
        cwd=str(root),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,  # setsid，脱离脚本进程组
    )
    return proc.pid, log_path


# =====================================================================
# 4. 健康检查
# =====================================================================

def wait_until_healthy(host: str, port: int, timeout: float, log_path: Path) -> bool:
    """轮询 /health 直到返回 200 或超时。"""
    # host 为 0.0.0.0 时探活用 127.0.0.1
    probe_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    url = f"http://{probe_host}:{port}{HEALTH_PATH}"
    deadline = time.time() + timeout
    last_err = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if 200 <= resp.status < 300:
                    return True
        except Exception as e:
            last_err = str(e)
        time.sleep(HEALTH_CHECK_INTERVAL)
    warn(f"健康检查超时（{timeout}s）。最后错误: {last_err}")
    return False


def tail_log(log_path: Path, n: int = 30) -> None:
    """打印日志末尾 n 行，便于排查。"""
    try:
        with open(log_path, "rb") as f:
            lines = f.read().decode("utf-8", errors="replace").splitlines()
        print(f"\n----- {log_path} 末尾 {n} 行 -----")
        for line in lines[-n:]:
            print(line)
        print("-" * 40)
    except Exception as e:
        warn(f"读取日志失败: {e}")


# =====================================================================
# 主流程
# =====================================================================

def main() -> int:
    parser = argparse.ArgumentParser(
        description="paper-service 一键启动（WSL Linux 内运行）",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"监听地址（默认 {DEFAULT_HOST}）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"监听端口（默认 {DEFAULT_PORT}）")
    args = parser.parse_args()

    root = project_root()
    print("=" * 60)
    print(f"  paper-service 启动脚本  |  项目目录: {root}")
    print(f"  监听: {args.host}:{args.port}")
    print("=" * 60)

    # 1. 补 PATH + 依赖检查
    ensure_uv_on_path()
    ensure_dependencies(root)

    # 2. 端口冲突处理
    ensure_port_free(args.port)

    # 3. 后台启动 + 时间戳日志
    info("后台启动 uvicorn...")
    pid, log_path = start_uvicorn(root, args.host, args.port)
    info(f"服务进程已启动 PID={pid}，日志: {log_path}")

    # 4. 健康检查
    info(f"健康检查中（最多等待 {HEALTH_CHECK_TIMEOUT}s）...")
    if wait_until_healthy(args.host, args.port, HEALTH_CHECK_TIMEOUT, log_path):
        ok("服务已就绪 ✅")
        print()
        print(f"  API 文档: http://localhost:{args.port}/docs")
        print(f"  健康检查: http://localhost:{args.port}{HEALTH_PATH}")
        print(f"  日志文件: {log_path}")
        print(f"  进程 PID: {pid}")
        print()
        print("提示：服务在后台运行。停止服务可用:")
        print(f"  python3 {_script_relpath()} --port {args.port}  # 再次运行会先关闭旧服务")
        print(f"  或 kill {pid}")
        return 0
    else:
        fail("服务未能在超时内通过健康检查 ❌")
        tail_log(log_path)
        warn(f"完整日志: {log_path}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
