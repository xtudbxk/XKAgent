"""技能 Python API 检查器（makeskill Phase 6 辅助脚本）

对生成的技能脚本执行「API 文档化」约定检查：
1. 语法检查（ast.parse）
2. deps 依赖声明核对（头部 # deps: 行 vs 实际 import 清单）
3. 公开 API docstring 完整性（每个公开函数/类须含 Args/Returns/Example）
4. skill.md 使用示例一致性（示例引用的 API 是否真实存在、是否覆盖公开 API）
5. 冒烟测试（importlib 加载 + 无参调用无必需参数的公开函数）

Deps:
    stdlib only

Usage:
    # 🔥 build-unsafe 直接 import
    from skills.makeskill.verify_skill_api import verify_skill
    # 🔵🟢 plan/build 沙箱 importlib 动态加载
    import importlib.util, sys
    spec = importlib.util.spec_from_file_location(
        "verify_skill_api", "skills/makeskill/verify_skill_api.py")
    m = importlib.util.module_from_spec(spec); sys.modules["verify_skill_api"] = m
    spec.loader.exec_module(m)
    m.verify_skill("skills/<name>", ["<script>.py"], "skills/<name>/skill.md")
"""
import ast
import importlib.util
import json
import os
import re
import sys


def _read(path):
    """读取文本文件（容错编码）"""
    with open(path, encoding='utf-8', errors='replace') as fh:
        return fh.read()


def _extract_imports(src):
    """提取脚本实际 import 的顶层模块名（ast 遍历）"""
    tree = ast.parse(src)
    names = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                names.add(a.name.split('.')[0])
        elif isinstance(n, ast.ImportFrom) and n.module:
            names.add(n.module.split('.')[0])
    return sorted(names)


def _extract_public_api(src):
    """提取模块级公开 API（非下划线开头的函数/类）"""
    tree = ast.parse(src)
    api = []
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not n.name.startswith('_'):
                api.append(n.name)
    return api


def _extract_deps_declaration(src):
    """提取依赖声明：docstring 的 Deps: 段 或 头部 # deps: 注释（返回声明文本或 None）"""
    tree = ast.parse(src)
    doc = ast.get_docstring(tree) or ''
    m = re.search(r'Deps:\s*(.*?)(?=\n\s*\n|\Z)', doc, re.S)
    if m and m.group(1).strip():
        return m.group(1).strip()
    for ln in src.splitlines()[:8]:
        if ln.strip().lower().startswith('# deps:'):
            return ln.split(':', 1)[1].strip()
    return None


def _parse_modes(md_src):
    """解析 skill.md frontmatter 的 compatible_modes（YAML list）"""
    m = re.search(r'compatible_modes:\s*\n((?:\s+-[^\n]*\n)*)', md_src)
    if not m:
        return []
    return [ln.strip()[2:].strip() for ln in m.group(1).splitlines()
            if ln.strip().startswith('-')]


def _importable(mod):
    """探测模块在当前环境是否可 import（沙箱内第三方被 _ImportGate 拦）"""
    import importlib
    try:
        importlib.import_module(mod)
        return True
    except Exception:
        return False


def check_syntax(src, path):
    """检查脚本语法（ast.parse 校验）

    Args:
        src (str): 脚本源码
        path (str): 脚本路径（报错定位用）
    Returns:
        str: JSON {"status": "PASS|FAIL", "detail": "..."}
    Example:
        check_syntax(SOURCE, "probe.py")  # -> {"status": "PASS", "detail": "语法通过"}
"""
    try:
        ast.parse(src, filename=path)
        return json.dumps({"status": "PASS", "detail": "语法通过"}, ensure_ascii=False)
    except SyntaxError as e:
        return json.dumps({"status": "FAIL", "detail": f"语法错误 L{e.lineno}: {e.msg}"},
                          ensure_ascii=False)


def check_deps(src, modes=None):
    """核对 deps 声明与实际 import 清单（模式感知：compatible_modes 含 build-unsafe 时第三方依赖合理）

    Args:
        src (str): 脚本源码
        modes (list|None): 技能声明的 compatible_modes（None=未知）
    Returns:
        str: JSON {"status": "PASS|WARN", "declared": "...", "imports": [...], "detail": "..."}
    Example:
        check_deps(SOURCE, ["plan", "build"])  # -> {"status": "PASS", ...}
    """
    imports = _extract_imports(src)
    declared = _extract_deps_declaration(src)
    stdlib = set(getattr(sys, 'stdlib_module_names', ()))
    third = [i for i in imports if i not in stdlib]
    modes = modes or []
    if not declared:
        status = 'WARN'
        detail = '缺依赖声明（docstring Deps: 段或 # deps: 注释，默认视为 stdlib only）'
    elif 'stdlib only' in declared.lower() and third:
        status = 'WARN'
        detail = f'声明 stdlib only 但 import 了非标准库: {third}'
    elif third:
        if 'build-unsafe' in modes:
            status = 'PASS'
            detail = f'第三方库 {third} 需 build-unsafe 运行（compatible_modes 已声明）'
        else:
            status = 'WARN'
            detail = f'第三方库 {third} 但 compatible_modes {modes or "未知"} 不含 build-unsafe，沙箱内不可用'
    else:
        status = 'PASS'
        detail = '声明与实际 import 一致'
    return json.dumps({"status": status, "detail": detail,
                       "declared": declared, "imports": imports}, ensure_ascii=False)


def check_docstrings(src):
    """检查公开 API 的 docstring 完整性（须含 Args/Returns/Example）

    Args:
        src (str): 脚本源码
    Returns:
        str: JSON {"status": "PASS|WARN", "api": [...], "issues": [...]}
    """
    tree = ast.parse(src)
    api = []
    issues = []
    for n in tree.body:
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if n.name.startswith('_'):
            continue
        api.append(n.name)
        doc = ast.get_docstring(n) or ''
        missing = [k for k in ('Args', 'Returns', 'Example') if k not in doc]
        if missing:
            issues.append(f'{n.name}: 缺 {", ".join(missing)}')
    status = 'PASS' if not issues else 'WARN'
    return json.dumps({"status": status, "detail": f'公开 API {len(api)} 个，{len(issues)} 个 docstring 不完整',
                       "api": api, "issues": issues}, ensure_ascii=False)


def check_md_examples(md_src, api, script_name, known_api=None):
    """核对 skill.md 使用示例与脚本公开 API 的一致性

    Args:
        md_src (str): skill.md 全文
        api (list): 脚本公开 API 函数名列表
        script_name (str): 脚本文件名（如 example_lib.py）
        known_api (list|None): 同技能其他脚本的公开 API 并集（跨脚本引用合法，不判为非法）
    Returns:
        str: JSON {"status": "PASS|WARN|FAIL", "uncovered": [...], "bad_refs": [...]}
    Example:
        check_md_examples(MD_SRC, ["f"], "x.py")  # -> {"status": "WARN", "uncovered": [...]}
    """
    blocks = re.findall(r'```[a-z]*\n(.*?)```', md_src, re.S)
    all_md = '\n'.join(blocks)
    uncovered = [a for a in api if not re.search(rf'\b{re.escape(a)}\b', all_md)]
    allowed = set(api) | set(known_api or [])
    bad_refs = []
    for name in re.findall(r'm\.([A-Za-z_]\w*)\s*\(', all_md):
        if name not in allowed:
            bad_refs.append(name)
    if bad_refs:
        status = 'FAIL'
    elif uncovered:
        status = 'WARN'
    else:
        status = 'PASS'
    return json.dumps({"status": status,
                       "detail": f'示例覆盖 {len(api)-len(uncovered)}/{len(api)} 个 API，非法引用 {len(bad_refs)} 个',
                       "uncovered": uncovered, "bad_refs": bad_refs}, ensure_ascii=False)


def _detect_write_ops(tree):
    """检测写文件操作（open w/a/x/+、os.remove/makedirs/rename、Path.write_*）"""
    ops = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name) and f.id == 'open':
                mode = None
                if len(n.args) > 1 and isinstance(n.args[1], ast.Constant):
                    mode = n.args[1].value
                for kw in n.keywords:
                    if kw.arg == 'mode' and isinstance(kw.value, ast.Constant):
                        mode = kw.value.value
                if isinstance(mode, str) and any(c in mode for c in 'wax+'):
                    ops.append('open(w/a)')
            elif isinstance(f, ast.Attribute):
                if f.attr in ('remove', 'unlink', 'rmdir', 'makedirs', 'rename', 'replace', 'mkdir'):
                    ops.append(f'os.{f.attr}')
                elif f.attr in ('write_text', 'write_bytes'):
                    ops.append(f'Path.{f.attr}')
    return sorted(set(ops))


def check_mode_fit(src):
    """分析脚本适用的运行模式（plan/build/build-unsafe）+ 原因 + 受影响库

    从脚本内容反向推断（非依赖声明）：第三方库/危险模块/动态执行/进程调用
    影响 plan/build（沙箱拦截），写操作影响 plan（只读），build-unsafe 无限制。

    Args:
        src (str): 脚本源码
    Returns:
        str: JSON {"status": "PASS", "fit": {...}, "reasons": {...}, "affected_libs": [...]}
    Example:
        check_mode_fit(SOURCE)  # -> {"fit": {"plan": "不可用", "build": "不可用", "build-unsafe": "OK"},
                                #     "affected_libs": ["numpy"]}
    """
    tree = ast.parse(src)
    stdlib = set(getattr(sys, 'stdlib_module_names', ()))
    imports = _extract_imports(src)
    third = [i for i in imports if i not in stdlib]
    DANGEROUS = {'ctypes', 'cffi', 'pickle', 'marshal', 'subprocess'}
    danger_hit = [i for i in imports if i in DANGEROUS]
    affected = sorted(set(third) | set(danger_hit))
    reasons = {'plan': [], 'build': [], 'build-unsafe': []}
    if third:
        msg = f'第三方库 {third} 被沙箱 _ImportGate 拦截'
        reasons['plan'].append(msg)
        reasons['build'].append(msg)
    if danger_hit:
        msg = f'危险模块 {danger_hit} 被沙箱禁用'
        reasons['plan'].append(msg)
        reasons['build'].append(msg)
    dyn = sorted({n.id for n in ast.walk(tree)
                  if isinstance(n, ast.Name) and n.id in ('exec', 'eval', 'compile')})
    if dyn:
        msg = f'动态执行 {dyn} 被沙箱禁用'
        reasons['plan'].append(msg)
        reasons['build'].append(msg)
    proc = sorted({n.attr for n in ast.walk(tree)
                   if isinstance(n, ast.Attribute) and n.attr in ('system', 'popen', 'Popen')})
    if proc:
        msg = f'进程调用 {proc} 被沙箱禁用'
        reasons['plan'].append(msg)
        reasons['build'].append(msg)
    writes = _detect_write_ops(tree)
    if writes:
        reasons['plan'].append(f'写操作 {writes} — plan 仅 /tmp 可写，项目目录只读')
    fit = {}
    for mode in ('plan', 'build', 'build-unsafe'):
        if mode == 'build-unsafe':
            fit[mode] = 'OK'
        elif reasons[mode]:
            # plan 仅写操作受限 → '受限'；含库/危险/动态/进程 → '不可用'
            blocking = bool(third or danger_hit or dyn or proc)
            fit[mode] = '不可用' if blocking else '受限'
        else:
            fit[mode] = 'OK'
    detail = f'plan={fit["plan"]}, build={fit["build"]}, build-unsafe={fit["build-unsafe"]}'
    return json.dumps({"status": "PASS", "detail": detail, "fit": fit,
                       "reasons": reasons, "affected_libs": affected}, ensure_ascii=False)


def smoke_test(script_path, api, modes=None):
    """冒烟测试：importlib 加载 + 无参调用无必需参数的公开函数/类（模式感知）

    Args:
        script_path (str): 脚本文件路径
        api (list): 公开 API 函数名列表
        modes (list|None): 技能 compatible_modes（用于判定第三方依赖可用性）
    Returns:
        str: JSON {"status": "PASS|WARN|SKIP", "results": [...]}
    Example:
        smoke_test("skills/x/x.py", ["f"])  # -> {"status": "PASS", "results": [...]}
    """
    import inspect
    # 模式感知：第三方依赖在当前环境不可 import → SKIP（需 build-unsafe 验证）
    src = _read(script_path)
    imports = _extract_imports(src)
    stdlib = set(getattr(sys, 'stdlib_module_names', ()))
    third = [i for i in imports if i not in stdlib]
    if third:
        unavail = [t for t in third if not _importable(t)]
        if unavail:
            return json.dumps({"status": "SKIP",
                               "detail": f'第三方库 {unavail} 在当前模式不可 import（需 build-unsafe 验证）',
                               "results": []}, ensure_ascii=False)
    spec = importlib.util.spec_from_file_location('_smoke', os.path.abspath(script_path))
    m = importlib.util.module_from_spec(spec)
    sys.modules['_smoke'] = m
    spec.loader.exec_module(m)
    results = []
    fails = 0

    def _no_required_args(obj):
        """判断调用是否无必需参数（类取 __init__ 签名）"""
        try:
            sig = inspect.signature(obj)
        except (TypeError, ValueError):
            return False
        for p in sig.parameters.values():
            if p.name in ('self', 'cls'):
                continue
            if p.default is p.empty and p.kind in (
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.KEYWORD_ONLY):
                return False
        return True

    for name in api:
        if name == 'main':
            continue  # CLI 入口（含 sys.exit），不是 API，跳过
        obj = getattr(m, name, None)
        if not callable(obj):
            continue
        kind = '类' if isinstance(obj, type) else '函数'
        if not _no_required_args(obj):
            results.append(f'SKIP: {name}（{kind}）需要参数')
            continue
        try:
            r = obj()
            results.append(f'PASS: {name}() -> {type(r).__name__}: {str(r)[:50]}')
        except BaseException as e:
            fails += 1
            results.append(f'FAIL: {name}() -> {type(e).__name__}: {str(e)[:60]}')
    status = 'PASS' if fails == 0 else 'WARN'
    return json.dumps({"status": status, "detail": f'冒烟 {len(results)} 项，失败 {fails} 项',
                       "results": results}, ensure_ascii=False)


def check_paths(skill_dir, scripts, md_path=None):
    """路径诊断：检测硬编码绝对路径 + 计算相对路径建议（规则 18）

    技能内文档/脚本路径应使用「相对技能库根」基准写法（skills/<name>/<file>），
    禁止硬编码环境相关绝对路径（/home/、/mnt/、/data/ 等，换环境即失效）。
    /tmp/ 为沙箱约定临时目录，仅提示。

    Args:
        skill_dir (str): 技能目录路径（绝对或相对）
        scripts (list): 脚本文件名列表
        md_path (str|None): skill.md 路径
    Returns:
        str: JSON {"status": "PASS|WARN", "absolute_paths": [...], "rel_dir": "...",
                   "rel_suggestions": {...}}
    Example:
        check_paths("/abs/skills/x", ["x.py"])  # -> {"status": "WARN", "absolute_paths": [...]}
    """
    abs_patterns = re.compile(r'/(?:home|mnt|data|Users|var|opt|root|workspace)/[^\s"\'`，、。：；]+')
    all_text = ''
    for script in scripts:
        p = os.path.join(skill_dir, script)
        if os.path.isfile(p):
            all_text += _read(p) + '\n'
    if md_path and os.path.isfile(md_path):
        all_text += _read(md_path)
    abs_paths = sorted(set(abs_patterns.findall(all_text)))
    env_abs = [p for p in abs_paths if not p.startswith('/tmp/')]
    rel_suggestions = {}
    rel_dir = skill_dir
    try:
        abs_skill_dir = os.path.abspath(skill_dir)
        cwd = os.getcwd()
        rel_dir = os.path.relpath(abs_skill_dir, cwd)
        for script in scripts:
            rel_suggestions[script] = os.path.join(rel_dir, script)
    except ValueError:
        rel_dir = skill_dir
    status = 'WARN' if env_abs else 'PASS'
    detail = f'环境相关绝对路径 {len(env_abs)} 处（应改相对基准）' if env_abs else f'无环境相关绝对路径；相对建议: {rel_dir}'
    return json.dumps({"status": status, "detail": detail,
                       "absolute_paths": abs_paths, "env_abs_paths": env_abs,
                       "rel_dir": rel_dir, "rel_suggestions": rel_suggestions},
                      ensure_ascii=False)


def verify_skill(skill_dir, scripts, md_path=None, smoke=True, modes=None):
    """对技能目录中的 Python 脚本执行「API 文档化」约定全量检查（模式感知）

    Args:
        skill_dir (str): 技能目录路径（如 skills/<name>）
        scripts (list): 脚本文件名列表（如 ["example_lib.py"]）
        md_path (str|None): skill.md 路径，默认 skill_dir/skill.md
        smoke (bool): 是否执行冒烟测试（默认 True）
        modes (list|None): 技能 compatible_modes；None 时自动从 skill.md frontmatter 解析
    Returns:
        str: JSON 报告字符串
    Example:
        verify_skill("skills/x", ["x.py"], "skills/x/skill.md")
        # -> {"skill": "...", "checks": {...}, "summary": "PASS x 3, WARN x 1, ..."}
    """
    md_path = md_path or os.path.join(skill_dir, 'skill.md')
    if modes is None and os.path.isfile(md_path):
        modes = _parse_modes(_read(md_path))
    # 收集全部脚本公开 API 并集（多脚本 skill.md 示例跨脚本引用合法）
    all_api = []
    for script in scripts:
        sp = os.path.join(skill_dir, script)
        if os.path.isfile(sp):
            all_api.extend(_extract_public_api(_read(sp)))
    report = {"skill": skill_dir, "checks": {}, "summary": "", "modes": modes,
              "paths": json.loads(check_paths(skill_dir, scripts, md_path))}
    counts = {"PASS": 0, "WARN": 0, "FAIL": 0, "SKIP": 0}
    for script in scripts:
        path = os.path.join(skill_dir, script)
        if not os.path.isfile(path):
            report["checks"][script] = {"status": "FAIL", "detail": '脚本文件缺失'}
            counts["FAIL"] += 1
            continue
        src = _read(path)
        api = _extract_public_api(src)
        checks = {
            'syntax': json.loads(check_syntax(src, path)),
            'deps': json.loads(check_deps(src, modes)),
            'docstrings': json.loads(check_docstrings(src)),
            'mode_fit': json.loads(check_mode_fit(src)),
        }
        if os.path.isfile(md_path):
            md_src = _read(md_path)
            checks['md_examples'] = json.loads(
                check_md_examples(md_src, api, script, all_api))
        if smoke:
            checks['smoke'] = json.loads(smoke_test(path, api, modes))
        for k, v in checks.items():
            counts[v.get("status", "WARN")] += 1
        report["checks"][script] = checks
    report["summary"] = (f'PASS x {counts["PASS"]}, WARN x {counts["WARN"]}, '
                         f'FAIL x {counts["FAIL"]}, SKIP x {counts["SKIP"]}')
    return json.dumps(report, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    args = sys.argv[1:]
    if len(args) >= 2:
        print(verify_skill(args[0], args[1].split(','),
                           args[2] if len(args) > 2 else None))
    else:
        print(verify_skill('skills/my_skill', ['example_lib.py']))
