"""scheduled_task_waiter - 循环等待条件满足后退出

在 pythonrt（统一 Python 运行时）中运行。
通过动态导入条件判断模块并反复调用 check_condition() 来检测条件是否满足。

⚠️ 运行模式：waiter 依赖 importlib（动态加载条件脚本），
   pythonrt 受限模式（plan/build）禁 importlib → waiter 必须在 build-unsafe（pythonrt 无限制）
   下运行，或由用户 `!xxx` 在宿主执行（`python3 -m skills.scheduled_task.scheduled_task_waiter ...`）。

Usage:
    from skills.scheduled_task.scheduled_task_waiter import wait_loop
    result = wait_loop(script_path="/path/to/check.py", interval=5, mode="build")

CLI:
    python skills/scheduled_task/scheduled_task_waiter.py \\
        --script /path/to/check.py [--interval 5] [--mode build]
"""
import importlib.util
import sys
import time
import json
import argparse
import os
from pathlib import Path


def resolve_script_path(script_path: str, mode: str = "build") -> str:
    """根据模式解析脚本的最终路径。

    如果 script_path 已是绝对路径或明确存在的路径，直接返回。
    否则根据 mode 补全路径。

    参数:
        script_path: 原始脚本路径
        mode: 运行模式（plan / build / build-unsafe）

    返回:
        解析后的绝对路径
    """
    p = Path(script_path)
    if p.is_absolute():
        return str(p.resolve())
    if p.exists():
        return str(p.resolve())

    # 尝试按模式补全
    if mode == "plan":
        # plan 模式下尝试 /tmp/ 路径
        alt = Path("/tmp") / p.name
        if alt.exists():
            return str(alt.resolve())
        # 如果 /tmp/ 下没有，返回原始路径（让后续报错）
        return str(p.resolve())

    # build / build-unsafe: 尝试项目目录下的 skills/ 路径
    alt = Path("skills/scheduled_task") / p.name
    if alt.exists():
        return str(alt.resolve())

    # 返回原始 resolve 结果
    return str(p.resolve())


def load_condition_func(script_path: str, mode: str = "build"):
    """动态加载条件判断脚本，返回 check_condition 可调用对象。

    参数:
        script_path: 条件判断脚本路径
        mode: 运行模式（plan / build / build-unsafe）
    """
    resolved_path = resolve_script_path(script_path, mode)
    resolved = Path(resolved_path)

    if not resolved.exists():
        raise FileNotFoundError(
            f"条件脚本不存在 (mode={mode}): {resolved}"
        )

    # 动态加载模块
    module_name = f"_scheduled_task_cond_{resolved.stem}_{int(time.time())}"
    spec = importlib.util.spec_from_file_location(module_name, str(resolved))
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载条件脚本: {resolved}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)

    if not hasattr(module, "check_condition"):
        raise AttributeError(
            f"条件脚本 {resolved} 必须定义 check_condition() 函数"
        )

    return module.check_condition


def wait_loop(script_path: str, interval: int = 5, mode: str = "build") -> str:
    """循环等待条件满足。

    参数:
        script_path: 条件判断脚本路径
        interval:    轮询间隔（秒）
        mode:        运行模式（plan / build / build-unsafe）

    返回:
        JSON 字符串: {"status": "met", "message": "...", "attempts": N}
        或
        {"status": "error", "message": "错误描述"}
    """
    try:
        check_func = load_condition_func(script_path, mode)
    except (FileNotFoundError, ImportError, AttributeError) as e:
        return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)

    attempt = 0
    while True:
        attempt += 1
        try:
            result = check_func()
        except Exception as e:
            return json.dumps(
                {"status": "error", "message": f"第 {attempt} 次检查异常: {e}"},
                ensure_ascii=False,
            )

        # 支持返回 bool 或 int
        if isinstance(result, bool):
            met = result
        elif isinstance(result, (int, float)):
            met = bool(result)
        else:
            met = False

        if met:
            return json.dumps(
                {
                    "status": "met",
                    "message": f"条件已满足（第 {attempt} 次检查后）",
                    "attempts": attempt,
                    "mode": mode,
                },
                ensure_ascii=False,
            )

        # 未满足，等待后继续
        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description="循环等待条件满足")
    parser.add_argument("--script", required=True, help="条件判断脚本路径")
    parser.add_argument("--interval", type=int, default=5, help="轮询间隔（秒）")
    parser.add_argument(
        "--mode", default="build",
        choices=["plan", "build", "build-unsafe"],
        help="运行模式（默认 build）"
    )
    args = parser.parse_args()

    result = wait_loop(
        script_path=args.script,
        interval=args.interval,
        mode=args.mode
    )
    print(result)
    parsed = json.loads(result)
    sys.exit(0 if parsed.get("status") == "met" else 1)


if __name__ == "__main__":
    main()
