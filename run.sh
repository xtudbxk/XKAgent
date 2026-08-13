#!/usr/bin/env bash
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  XKAgent — Launch script (精简版)
#
#  所有参数统一透传给 codes.main 严格解析（必须 --xxx 形式）：
#     ./run.sh                          → REPL (default)
#     ./run.sh --mode web               → Web
#     ./run.sh --mode web --port 9090   → Web + port
#     ./run.sh --mode web --workdir /path
#     ./run.sh --help                   → 帮助
#
#  位置参数 / 未知参数 → main.py 直接报错退出（无向后兼容）
#
#  路径语义（v2）：
#     - 不再 cd 到脚本目录：进程 cwd 保持 = 用户启动脚本时的工作目录，
#       因此 --workdir 相对路径基于【用户启动目录】解析
#     - codes 包导入由 main.py 基于 __file__ 注入 sys.path 完成（不依赖 PYTHONPATH）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
set -e
# export DEEPSEEK_API_KEY="your-api-key"
# 
# # ── API key 检查 ──
# if [ -z "$DEEPSEEK_API_KEY" ]; then
#     echo "Error: DEEPSEEK_API_KEY is not set." >&2
#     echo "Please set it first:" >&2
#     echo "  export DEEPSEEK_API_KEY=\"sk-...\"" >&2
#     exit 1
# fi

# ── 定位脚本目录（不 cd，仅用于引用项目内文件） ──
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── Install dependencies if missing（核心依赖 requests，不再需要 litellm） ──
if [ ! -d "$(python3 -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')/requests" ] 2>/dev/null; then
    echo "Installing dependencies..."
    pip install -r "$SCRIPT_DIR/requirements.txt" -q
fi

# ── 启动：以绝对路径执行 main.py（不再依赖 -m 与 cwd），参数全部透传 ──
which python3
exec python3 "$SCRIPT_DIR/codes/main.py" "$@"
