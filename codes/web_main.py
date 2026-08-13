"""codes/web_main.py — Web 模式入口（委托层）。

解析命令行参数（复用 codes.main 的统一 parser），
强制以 web 模式调度 codes.main._run()，避免逻辑重复。

参数约定与 codes.main 一致：所有参数必须以 ``--xxx`` 显式形式传入，
位置参数/未知参数一律报错。即使命令行显式传入 --mode repl，
本入口也强制为 web，以保持 ``python3 -m codes.web_main`` 的入口语义。
"""

from codes.main import _build_parser, _check_optional_deps, _resolve_workdir, _run


def main() -> None:
    # ── 第 1 步：解析参数（严格模式），设置 workdir ──
    parser = _build_parser()
    args = parser.parse_args()
    args.mode = "web"  # 强制 web 模式
    args.workdir = _resolve_workdir(args.workdir)

    from codes import config
    config.set_workdir(args.workdir)

    # ── 第 2 步：日志 + 组件检查 ──
    from codes._log import logger, LOG_FILE
    logger.info(f"Web 应用启动，workdir={config.get_workdir()}")
    print(f"  📁 日志文件: {LOG_FILE}")

    _check_optional_deps()

    # ── 第 3 步：统一调度（内部含 Web 依赖 try/except） ──
    _run("web", args)


if __name__ == "__main__":
    main()
