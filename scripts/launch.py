"""One-click launcher: a novice double-clicks 启动.bat and nothing else.

Everything a first run needs is done here, in order and with plain-language
progress output:

0. on macOS / Linux, switch into a project-local virtual environment first;
1. check the Python packages, install them if they are missing;
2. check the database, import the seed CSV and compute indicators if it is empty;
3. check the built frontend, build it only if it is really missing;
4. pick a free port, start the local server, open the browser.

The console window *is* the program's UI. It stays open, says what it is doing,
and tells the user how to quit. Nothing here is meant to be imported by the app.
"""

from __future__ import annotations

import importlib.util
import os
import socket
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from backend.config import DEFAULT_PORT, DEFAULT_PRICE_SYMBOL  # noqa: E402

# The console window is this program's interface, so its encoding is part of the
# contract: on a Windows console whose code page is not Chinese, printing the
# Chinese progress lines would raise UnicodeEncodeError and kill the startup.
# The batch file switches the console to UTF-8 first; this makes Python agree.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # pragma: no cover - exotic console
        pass

LINE = "=" * 62

# The double-click entry point has a different name per platform; every message
# below names it, so a Mac user is never told to double-click a .bat.
LAUNCH_FILE = "启动.bat" if sys.platform == "win32" else "启动.command"

# macOS and Linux system Pythons refuse `pip install` into themselves
# (PEP 668, "externally-managed-environment"), which would make the first run
# fail at the dependency step. A project-local environment sidesteps that and
# leaves the user's system Python untouched. See ensure_isolated_env().
VENV_DIR = PROJECT_ROOT / ".venv"

# tzdata is not optional on Windows: there is no system tz database, so
# ZoneInfo("America/New_York") raises without it.
REQUIRED_MODULES = ("fastapi", "uvicorn", "pandas", "tzdata")
REQUIRED_MODULE_NAMES = {
    "fastapi": "FastAPI",
    "uvicorn": "uvicorn",
    "pandas": "pandas",
    "tzdata": "tzdata",
}


def say(text: str = "") -> None:
    print(text, flush=True)


def step(number: int, total: int, text: str) -> None:
    say(f"  [{number}/{total}] {text}")


def ok(text: str) -> None:
    say(f"        {text}")


def fail(title: str, lines: list[str]) -> int:
    say()
    say(LINE)
    say(f"  {title}")
    say(LINE)
    for line in lines:
        say(f"  {line}")
    say(LINE)
    return 1


# --------------------------------------------------------------------------
# 0. Isolated environment (macOS / Linux only)
# --------------------------------------------------------------------------


def _inside_project_venv() -> bool:
    try:
        return VENV_DIR.resolve() in Path(sys.executable).resolve().parents
    except OSError:  # pragma: no cover - exotic filesystem
        return False


def ensure_isolated_env() -> None:
    """Re-launch inside a project-local venv on macOS / Linux.

    Two independent reasons, both fatal on a first run otherwise:

    * macOS ships no `python` and recent Pythons are "externally managed", so
      `pip install -r requirements.txt` aborts instead of installing;
    * installing into the system Python would also need admin rights and can
      break other tools the user has.

    Windows is deliberately left alone: the interpreter the .bat found already
    works there, and changing it would be a regression risk for no gain.
    """
    if sys.platform == "win32" or _inside_project_venv():
        return

    python = VENV_DIR / "bin" / "python"
    if not python.exists():
        say("  第一次在 Mac / Linux 上运行：正在创建独立的 Python 环境")
        say("  （只放在项目文件夹里，不会动你系统上的 Python，请稍等）…")
        result = subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)])
        if result.returncode != 0 or not python.exists():
            say("  独立环境没建成，改用当前 Python 继续（依赖可能装不上）。")
            return

    # Hand over to the venv interpreter so that what we install and what we
    # later import are guaranteed to be the same environment. Never returns on
    # success; the fresh process re-enters main() and sees it is already inside.
    os.execv(str(python), [str(python), str(Path(__file__).resolve())])


# --------------------------------------------------------------------------
# 1. Python packages
# --------------------------------------------------------------------------


def install_requirements() -> bool:
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", str(PROJECT_ROOT / "requirements.txt")],
        cwd=str(PROJECT_ROOT),
    )
    return result.returncode == 0


def ensure_dependencies() -> bool | None:
    """Returns True if ready, None if the caller should abort with guidance."""
    missing = [m for m in REQUIRED_MODULES if importlib.util.find_spec(m) is None]
    if not missing:
        ok("依赖齐全，无需安装。")
        return True

    names = "、".join(REQUIRED_MODULE_NAMES.get(m, m) for m in missing)
    ok(f"缺少 {names}，正在自动安装（第一次会慢一些，请稍等）…")
    if install_requirements():
        still = [m for m in REQUIRED_MODULES if importlib.util.find_spec(m) is None]
        if not still:
            ok("安装完成。")
            return True
        ok(f"仍然缺少：{'、'.join(still)}")
        return None
    return None


def dependency_help() -> int:
    return fail(
        "依赖安装失败，无法启动",
        [
            "常见原因：断网、公司网络限制、或者 pip 需要代理。",
            "",
            "请手动执行一次这句命令，看它报什么错：",
            f'    "{sys.executable}" -m pip install -r requirements.txt',
            "",
            f"装好之后再双击一次 {LAUNCH_FILE} 即可。",
        ],
    )


# --------------------------------------------------------------------------
# 2. Database
# --------------------------------------------------------------------------


def ensure_cot_data() -> bool | None:
    """Import the seed CSV when the position tables are empty.

    Returns True when data is usable, None when there is nothing to import.
    """
    from backend.config import RAW_CFTC_DIR, REPORT_TYPE_DISAGGREGATED
    from backend.db import init_db, open_con

    init_db()
    con = open_con()
    try:
        weekly = con.execute("SELECT COUNT(*) FROM cot_weekly").fetchone()[0]
        if weekly:
            ok(f"持仓数据已就绪：{weekly} 期报告。")
            return True

        seeds = sorted(RAW_CFTC_DIR.glob("*.csv"))
        if not seeds:
            return None

        ok(f"首次使用，正在导入官方数据：{seeds[-1].name}")
        from backend.data.cftc_import import run_import

        summary = run_import(con, seeds[-1], report_type=REPORT_TYPE_DISAGGREGATED)
        ok(
            f"导入完成：读入 {summary['rows_read']} 行，"
            f"新增 {summary['rows_inserted']}，未变化 {summary['rows_unchanged']}。"
        )
        if summary["status"] == "partial":
            ok("注意：有记录与库内不一致，已按保守策略保留旧值（详见数据管理页）。")
        return True
    finally:
        try:
            con.close()
        except Exception:
            pass


def ensure_price() -> bool:
    """Fill the gold price series if it is still empty.

    Price is an optional input, so every failure is swallowed and reported in one
    plain line. Returns True only when new price rows were actually written, so
    the caller knows the price-linked indicators have to be recomputed.
    """
    from backend.db import open_con

    con = open_con()
    try:
        existing = con.execute("SELECT COUNT(*) FROM price_daily").fetchone()[0]
    finally:
        con.close()

    if existing:
        ok(f"黄金价格已就绪：{existing} 个交易日。")
        return False

    ok("正在获取黄金期货价格（用于「价格与持仓」对比）…")
    from backend.data.price_client import update_price

    con = open_con()
    try:
        summary = update_price(con, symbol=DEFAULT_PRICE_SYMBOL)
    except Exception as exc:  # noqa: BLE001 - price must never block startup
        ok(f"价格暂时拿不到（{type(exc).__name__}），持仓分析不受影响。")
        return False
    finally:
        try:
            con.close()
        except Exception:
            pass

    if summary.get("status") == "ok":
        ok(f"价格已写入：{summary.get('rows_valid', 0)} 个交易日。")
        return True
    ok("价格暂时拿不到（可能没联网），持仓分析不受影响，稍后可在「数据管理」重试。")
    return False


def ensure_indicators(force: bool) -> bool | None:
    """Compute the indicator rows. Recomputed whenever new inputs arrived."""
    from backend.config import REPORT_TYPE_DISAGGREGATED
    from backend.db import open_con

    con = open_con()
    try:
        if not force:
            existing = con.execute("SELECT COUNT(*) FROM cot_indicators").fetchone()[0]
            # Second condition: price rows exist but the price-linked indicators
            # were computed before that price arrived, so they are still recorded
            # as invalid. Recomputing once fixes it and cannot loop, because a
            # successful recompute leaves valid divergence rows behind.
            stale = False
            if existing:
                has_price = (
                    con.execute("SELECT COUNT(*) FROM price_daily").fetchone()[0] > 0
                )
                has_priced = (
                    con.execute(
                        "SELECT COUNT(*) FROM cot_indicators "
                        "WHERE indicator_code = 'divergence' AND is_valid = 1"
                    ).fetchone()[0]
                    > 0
                )
                stale = has_price and not has_priced
            if existing and not stale:
                ok(f"指标已就绪：{existing} 行，无需重算。")
                return True

        ok("正在计算指标（净持仓、历史分位、COT Index、背离等）…")
        from backend.analysis.indicators import CALCULATION_VERSION, IndicatorParams
        from scripts.recompute_indicators import recompute

        # All markets, not just the default one: the interface lets the user
        # switch between COMEX Gold, Micro Gold and the Coinbase contract.
        result = recompute(
            con, REPORT_TYPE_DISAGGREGATED, None, IndicatorParams(), CALCULATION_VERSION
        )
        ok(f"指标完成：写入 {result.get('rows_written', 0)} 行。")
        return True
    finally:
        try:
            con.close()
        except Exception:
            pass


def database_help() -> int:
    from backend.config import RAW_CFTC_DIR

    return fail(
        "还没有可用的持仓数据",
        [
            "这个工具需要一份 CFTC 官方导出的持仓 CSV 才能开始。",
            "",
            "请把下载到的 CSV 文件（文件名通常类似",
            "Disaggregated_-_Futures_Only_YYYYMMDD.csv）放进这个文件夹：",
            f"    {RAW_CFTC_DIR}",
            "",
            f"放好之后再双击一次 {LAUNCH_FILE}，会自动导入。",
        ],
    )


# --------------------------------------------------------------------------
# 3. Frontend
# --------------------------------------------------------------------------


def ensure_frontend() -> bool:
    """The interface is served from frontend/dist, so it must exist."""
    from backend.config import FRONTEND_DIST

    if (FRONTEND_DIST / "index.html").exists():
        ok("界面文件已就绪。")
        return True

    npm = "npm.cmd" if sys.platform == "win32" else "npm"
    ok("界面文件缺失，正在尝试自动构建（需要 Node.js，可能要几分钟）…")
    try:
        frontend = FRONTEND_DIST.parent
        for command in ([npm, "install", "--no-audit", "--no-fund"], [npm, "run", "build"]):
            result = subprocess.run(command, cwd=str(frontend))
            if result.returncode != 0:
                raise RuntimeError(f"{' '.join(command)} 返回 {result.returncode}")
    except Exception as exc:
        ok(f"自动构建没成功（{exc}）。")
        return False

    return (FRONTEND_DIST / "index.html").exists()


def frontend_help() -> None:
    say()
    say("  提示：界面没构建起来，服务仍会启动，但浏览器里只会看到一句提示。")
    say("        想看到完整界面，需要先装 Node.js（https://nodejs.org），")
    say("        然后在 frontend 文件夹里执行：npm install 和 npm run build。")


# --------------------------------------------------------------------------
# 4. Server
# --------------------------------------------------------------------------


def pick_port(preferred: int = DEFAULT_PORT, tries: int = 12) -> int:
    for offset in range(tries):
        candidate = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", candidate))
            except OSError:
                continue
        return candidate
    return preferred


def serve(port: int) -> None:
    import uvicorn

    from backend.main import app

    url = f"http://127.0.0.1:{port}"
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    say()
    say(LINE)
    say("  启动成功了")
    say(LINE)
    say(f"  浏览器会自动打开：{url}")
    say("  如果没自动打开，手动把上面这行地址复制到浏览器里。")
    say()
    say("  【怎么退出】用完之后，回到这个黑色窗口，")
    say("              按 Ctrl+C，或者直接点右上角的 × 关掉窗口。")
    say(LINE)
    say()

    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    except KeyboardInterrupt:
        pass

    say()
    say("  已停止。数据都保存在 data/gold_cot.sqlite 里，下次打开还是这些数据。")
    say(f"  下次使用：双击 {LAUNCH_FILE} 即可。")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main() -> int:
    try:
        ensure_isolated_env()
    except OSError as exc:  # e.g. a half-deleted .venv: keep going, don't die here
        say(f"  独立环境启动失败（{exc}），改用当前 Python 继续。")

    say()
    say(LINE)
    say("  Gold COT Lab · 黄金持仓分析实验室")
    say("  黄金期货持仓分析 · 本地运行 · 不预测金价")
    say(LINE)
    say()

    try:
        step(1, 4, "检查运行环境…")
        if ensure_dependencies() is None:
            return dependency_help()

        step(2, 4, "检查持仓数据…")
        if ensure_cot_data() is None:
            return database_help()

        step(3, 4, "检查黄金价格…")
        price_filled = ensure_price()

        step(4, 4, "检查指标与界面文件…")
        ensure_indicators(price_filled)
        if not ensure_frontend():
            frontend_help()

        port = pick_port()
        serve(port)
        return 0
    except KeyboardInterrupt:
        say()
        say("  已取消。")
        return 0
    except Exception as exc:  # last resort: never vanish without telling the user
        import traceback

        return fail(
            "启动过程中出错了",
            [
                f"{type(exc).__name__}: {exc}",
                "",
                "完整错误信息：",
                *traceback.format_exc().splitlines()[-12:],
                "",
                "可以把这段信息发给我，我来看是什么问题。",
            ],
        )


if __name__ == "__main__":
    raise SystemExit(main())