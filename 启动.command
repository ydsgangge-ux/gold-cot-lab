#!/bin/bash
# ==============================================================
#  Gold COT Lab · 黄金持仓分析实验室
#  苹果电脑（macOS）启动文件 —— 直接双击本文件即可。
#
#  如果双击后提示「权限不足 / 无法执行」，先给它一次执行权限：
#    打开「终端」，输入  chmod +x  然后把这个文件拖进终端窗口，回车。
#  （只要做这一次，之后就永远是双击。）
# ==============================================================

cd "$(dirname "$0")" || exit 1

# 让中文进度在任何语言环境的终端里都不乱码（Python 里还会再设一次）
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8

pick_python() {
  for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
        printf '%s' "$candidate"
        return 0
      fi
    fi
  done
  return 1
}

PY="$(pick_python)"

if [ -z "$PY" ]; then
  echo
  echo "  =============================================================="
  echo "   没有找到 Python 3.10 或更新的版本"
  echo "  =============================================================="
  echo
  echo "   这个工具需要 Python 才能运行。macOS 上二选一："
  echo
  echo "   方式一（推荐）：安装 Python 官方版"
  echo "       https://www.python.org/downloads/macos/"
  echo "       下载后双击安装包，一路点「继续」，装完再双击本文件。"
  echo
  echo "   方式二：如果刚才系统弹出了「安装命令行开发者工具」的窗口，"
  echo "       点「安装」，等它装完，再双击本文件。"
  echo
  echo "  =============================================================="
  echo
  read -n 1 -s -r -p "  按任意键关闭这个窗口…"
  echo
  exit 1
fi

"$PY" scripts/launch.py
CODE=$?

if [ "$CODE" -ne 0 ]; then
  echo
  echo "  启动器退出了（代码 $CODE），请阅读上面的提示。"
  echo
  read -n 1 -s -r -p "  按任意键关闭这个窗口…"
  echo
fi

exit "$CODE"