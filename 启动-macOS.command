#!/bin/bash
# ============================================================
#  Wiki-USB v1.2  --  macOS launcher (双击运行)
#  依赖宿主 Python 3.10+，自动检测并按需引导安装缺失依赖
# ============================================================
set -u
cd "$(dirname "$0")" || exit 1
export PYTHONIOENCODING=utf-8
export PYTHONUTF8=1
export PYTHONDONTWRITEBYTECODE=1

# Gatekeeper 首次启动提示
if [ "$(uname -s)" = "Darwin" ]; then
  xattr -d com.apple.quarantine "$0" 2>/dev/null || true
fi

pick_python() {
  for c in python3.12 python3.11 python3.10 python3 python; do
    if command -v "$c" >/dev/null 2>&1; then
      v=$("$c" -c 'import sys;print("%d%d"%sys.version_info[:2])' 2>/dev/null)
      if [ -n "$v" ] && [ "$v" -ge 310 ] 2>/dev/null; then echo "$c"; return 0; fi
    fi
  done
  return 1
}

PY="$(pick_python)" || {
  echo "=================================================="
  echo " Wiki-USB 需要 Python 3.10 或更高版本"
  echo " 未检测到可用 Python。请先安装："
  echo "   brew install python@3.11"
  echo " 或访问 https://www.python.org/downloads/"
  echo "=================================================="
  read -r -p "按回车键退出..." _
  exit 1
}

echo "[Wiki-USB] 使用解释器: $PY ($("$PY" -V 2>&1))"
exec "$PY" "app/launcher.py" "$@"
