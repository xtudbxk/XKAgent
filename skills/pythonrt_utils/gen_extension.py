"""gen_extension.py — 生成 pythonrt 库接入骨架（register_extension 五种接入点）。

功能：根据库名与判定结果，生成可直接粘贴到 codes/sandbox.py 的接入代码。
纯标准库实现，可在受限沙箱运行。

用法：
    python gen_extension.py <库名>              # 生成基础骨架（无入口 patch）
    python gen_extension.py <库名> --entry-patch # 生成含入口路径 gate 的骨架
    python gen_extension.py <库名> --subprocess  # 生成含 subprocess stub 的骨架

设计意图（对应 pythonrt_utils.hacking.md 3.1 五种接入点）：
    输出 ② 模块级注册 + ⑤ setup 函数 + ⑥ 参数开关注释，
    提醒 ① 依赖处理与 ③ ImportGate 自动放行、④ 开关实现。
"""

import json
import sys


def gen_skeleton(lib_name: str, need_entry_patch: bool = False,
                 need_subprocess_stub: bool = False) -> str:
    """生成接入骨架代码字符串。

    Args:
        lib_name: 顶层模块名（如 'dulwich'、'yaml'）。
        need_entry_patch: 库的 IO 入口是路径级 → 生成 gate 模板。
        need_subprocess_stub: 导入链裸 import subprocess → 提示 stub。

    Returns:
        str: 可粘贴的代码骨架（含注释）。

    Raises:
        ValueError: lib_name 非法。
    """
    if not lib_name or not lib_name.isidentifier():
        raise ValueError(f"gen_skeleton(): 非法模块名: {lib_name!r}")

    lines = []
    lines.append(f"# ═══ 接入 {lib_name}（pythonrt_utils 生成骨架）═══")
    lines.append(f"# 前置：先跑 audit_lib.py 判定（{lib_name} 是否影响硬盘 IO）")
    if need_subprocess_stub:
        lines.append(f"# 注意：{lib_name} 导入链裸 import subprocess → 需先注册")
        lines.append('#   register_extension("subprocess", setup=_subprocess_stub_setup)')
    lines.append("")
    lines.append(f"def _{lib_name}_setup(sandbox):")
    lines.append(f'    """{lib_name} 内建扩展 setup：入口路径校验（复用 sandbox._resolve）。')
    lines.append("")
    lines.append("    依赖放行论证（audit 结论）：")
    if need_entry_patch:
        lines.append("      - IO 入口为路径级 → 需在库入口层校验路径（防绕过 builtins.open）")
    else:
        lines.append("      - IO 入口为 fd 级或纯 Python → builtins.open 闸门天然覆盖")
    lines.append("      - 危险依赖：见 audit_lib.py 报告")
    lines.append('    """')
    lines.append(f"    try:")
    lines.append(f"        import {lib_name}")
    lines.append(f"    except ImportError:")
    lines.append(f"        return")
    lines.append(f"    sandbox._{lib_name} = {lib_name}")
    lines.append(f"    _resolve = sandbox._resolve")
    if need_entry_patch:
        lines.append(f"    _real_open = {lib_name}.open_fn  # TODO: 替换为库的实际入口")
        lines.append(f"")
        lines.append(f"    def gate(path, *args, **kwargs):")
        lines.append(f"        _resolve(path, require_writable=True)  # 可写校验")
        lines.append(f"        return _real_open(path, *args, **kwargs)")
        lines.append(f"    {lib_name}.open_fn = gate")
    else:
        lines.append(f"    # 纯 Python 库写文件已走 builtins.open 闸门；")
        lines.append(f"    # 如需前置校验路径，在此 patch 入口函数（参考 examples.md）")
    lines.append("")
    lines.append(f"# 模块级默认注册（必须在 _EXTENSIONS 遍历前完成，否则 RuntimeError）")
    lines.append(f'register_extension("{lib_name}", setup=_{lib_name}_setup)')
    lines.append("")
    lines.append(f"# 构造参数开关（可选，对齐 allow_sqlite/allow_git）：")
    lines.append(f"#   __init__ 签名加: allow_{lib_name}: bool = True")
    lines.append(f"#   存储:          self._allow_{lib_name} = allow_{lib_name}")
    lines.append(f"#   门控:          if self._allow_{lib_name}:")
    lines.append(f'#                      if "{lib_name}" not in _EXTENSIONS:')
    lines.append(f'#                          register_extension("{lib_name}", setup=_{lib_name}_setup)')
    lines.append(f"#                  else:")
    lines.append(f'#                      unregister_extension("{lib_name}")')
    lines.append(f"# worker 透传:   codes/tools.py params[\"allow_{lib_name}\"] = True")
    return "\n".join(lines)


def main() -> None:
    """CLI 入口：python gen_extension.py <库名> [--entry-patch|--subprocess]"""
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    if not args:
        print(json.dumps({"error": "用法: gen_extension.py <库名> [--entry-patch|--subprocess]"},
                         ensure_ascii=False))
        sys.exit(1)
    try:
        skeleton = gen_skeleton(
            args[0],
            need_entry_patch="--entry-patch" in flags,
            need_subprocess_stub="--subprocess" in flags,
        )
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        sys.exit(1)
    print(skeleton)


if __name__ == "__main__":
    main()
