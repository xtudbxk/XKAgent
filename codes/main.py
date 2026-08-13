"""codes/main.py — 应用统一入口（REPL / Web 双模式）。

解析命令行参数 → 设置 workdir → 再导入其他模块。

这样确保 ``codes/_log.py`` / ``codes/history.py`` 等模块在
import 时使用的路径常量已基于 workdir 计算，而非硬编码。

参数约定：
    - 所有参数必须以 ``--xxx`` 显式形式传入，位置参数/未知参数一律报错
    - 不提供向后兼容（如 ``./run.sh /some/path`` 视为 workdir 的旧用法已移除）
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys

# ── 项目根手动注入 sys.path（不依赖 run.sh 的 cd / PYTHONPATH，保证 codes 可导入） ──
# 设计考虑：run.sh 不再 cd，进程 cwd = 用户启动脚本时的工作目录；
# 基于 __file__ 推导项目根注入 sys.path，必须在任何 `from codes import ...` 之前执行。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)



def _build_parser() -> argparse.ArgumentParser:
    """构建统一参数解析器（覆盖 REPL 与 Web 两模式参数）。

    设计考虑：REPL 与 Web 共用一套 parser，避免维护两套参数定义；
    严格模式（parse_args）确保位置参数/未知参数直接报错退出。
    """
    parser = argparse.ArgumentParser(description="XKAgent (REPL / Web 统一入口)")
    parser.add_argument("--mode", choices=["repl", "web"], default="repl",
                        help="运行模式 (default: repl)")
    # ── 通用参数 ──
    parser.add_argument("--workdir", help="工作目录（数据写入 .xkagent/ 下，支持 ~ 展开）")
    # ── REPL 专属参数 ──
    parser.add_argument("-s", "--session", help="Session name (repl mode)")
    parser.add_argument("--resume", action="store_true", help="Resume most recent session (repl mode)")
    parser.add_argument("-p", "--prompt", help="Single prompt mode (repl mode)")
    # ── Web 专属参数 ──
    parser.add_argument("--port", type=int, default=7860, help="Web 服务端口 (web mode)")
    parser.add_argument("--host", default="127.0.0.1", help="Web 服务监听地址 (web mode)")
    parser.add_argument("--password", help="Web Basic Auth 密码 (web mode)")
    parser.add_argument("--user", default="admin", help="Web Basic Auth 用户名 (default: admin)")
    return parser


def _check_optional_deps() -> None:
    """检查各可选功能包的安装状态，打印提示。"""
    print("  ─── 📋 可选组件检查 ───")

    # ── [BASE] 核心层：requests（LLM 调用层核心依赖，web 分支已去除 litellm） ──
    if importlib.util.find_spec("requests") is not None:
        print(f"  ✅ requests [核心LLM] — 已安装")
    else:
        print(f"  ❌ requests [核心LLM] — 未安装，请先: pip install requests")

    # ── [CORE] pythonrt（统一 Python 运行时，纯 stdlib 零依赖） ──
    print(f"  ✅ pythonrt [统一运行时] — 内置（纯 stdlib，无额外依赖）")

    # ── [FEATURE] 技能语义搜索（FAISS + ONNX + Transformers + numpy） ──
    emb_ok = True
    for mod_name in ["faiss", "onnxruntime", "transformers", "numpy"]:
        if importlib.util.find_spec(mod_name) is None:
            emb_ok = False
            break
    if emb_ok:
        print(f"  ✅ faiss + onnxruntime + transformers + numpy [技能语义搜索] — 已安装")
    else:
        print(f"  ⚠️  faiss / onnxruntime / transformers / numpy [技能语义搜索] — 未完全安装，skill 搜索将降级为 ngram/关键词匹配 (pip install faiss-cpu onnxruntime numpy transformers)")

    # ── [FEATURE] Web 界面（FastAPI + uvicorn） ──
    web_ok = True
    for mod_name in ["fastapi", "uvicorn"]:
        if importlib.util.find_spec(mod_name) is None:
            web_ok = False
            break
    if web_ok:
        print(f"  ✅ fastapi + uvicorn [Web界面] — 已安装，可使用 --mode web 启动")
    else:
        print(f"  ⚠️  fastapi / uvicorn [Web界面] — 未安装，仅支持 REPL 交互 (pip install fastapi uvicorn)")

    print()  # 空行分隔
def _resolve_workdir(path: str | None) -> str | None:
    """展开 workdir 中的 ~（argparse 不处理波浪号）。

    设计考虑：run.sh 精简后不再做路径预处理，此处在 Python 侧补齐
    ``~`` 展开；相对路径基准为进程 cwd（run.sh 不再 cd，cwd = 用户启动脚本时的工作目录）。
    """
    if path is None:
        return None
    return os.path.expanduser(path)


def _run(mode: str, args: argparse.Namespace) -> None:
    """按模式调度启动流程。

    Parameters
    ----------
    mode : str
        "repl" 或 "web"。
    args : argparse.Namespace
        已解析的命令行参数（含 workdir / session / port 等）。

    设计考虑：REPL 与 Web 均在入口阶段启动 skill/LLM 预热；Web 的 agent 实例仍由
    web._get_agent 按需创建，两类预热行为在此显式隔离。
    """
    if mode == "web":
        # ── Web 模式：缺依赖时友好提示并退出 ──
        try:
            from codes.web import run_web
        except ImportError as e:
            print(f"  ❌ Web 依赖缺失: {e}")
            print("  💡 请安装: pip install fastapi uvicorn")
            sys.exit(1)
        # ── 预热 LLM 与 embedding（对齐 REPL 分支） ──
        from codes.search import init_async
        from codes.llm import start_prewarm
        init_async()
        start_prewarm()
        run_web(args)
        return

    # ── REPL 模式：保持原有预热行为 ──
    from codes.search import init_async
    from codes.llm import start_prewarm
    init_async()
    start_prewarm()

    from codes.repl import run_repl
    run_repl(args)


def main() -> None:
    # ── 第 1 步：解析参数（严格模式，位置参数/未知参数报错） ──
    parser = _build_parser()
    args = parser.parse_args()
    args.workdir = _resolve_workdir(args.workdir)

    from codes import config
    config.set_workdir(args.workdir)

    # ── 第 2 步：现在才导入其他模块（路径常量已就绪） ──
    from codes._log import logger, LOG_FILE
    logger.info(f"应用启动 mode={args.mode}, workdir={config.get_workdir()}")
    print(f"  📁 日志文件: {LOG_FILE}")


    # ── ✨ 组件可用性检查 ──
    _check_optional_deps()

    # ── 第 3 步：按模式调度 ──
    _run(args.mode, args)


if __name__ == "__main__":
    main()