"""codes/pythonrt_preflight.py — pythonrt 参数预检与自动修复（宿主侧）。

2026-08-30 新增。由 tools.exec_pythonrt 在 run_pythonrt 之前调用：
    code, fixes, guides = preflight(code, unrestricted=(agent.mode == 'build-unsafe'))
fixes/guides 由调用方以 '[auto-fix]' 前缀 prepend 到结果 stdout 头部（透明可见）。

规则（只做高置信修复，只增不改）：
  F1 漏 import 自动补     SAFE_MODULES / SYMBOL_IMPORTS 白名单（宁漏补不误补）
  F2 shutil/pathlib stub  sys.modules 注入（import 先查缓存 → 不触发 ImportGate 拒绝）
  F3 open 无 encoding     调用点 span 替换 open( → _af_open(（二进制模式跳过）
  F4 os.remove/unlink     span 替换 → _af_trash(（移入 .trash/，对齐删除规范）
  指引（不可修）          subprocess 执行 / exec/eval/compile —— 安全边界，无法等价修复
"""

import ast
import builtins as _bi

__all__ = ['preflight']

# ────────────────── F1 白名单 ──────────────────
SAFE_MODULES = frozenset({
    're', 'json', 'os', 'sys', 'time', 'math', 'random', 'collections',
    'itertools', 'functools', 'hashlib', 'base64', 'uuid', 'string',
    'textwrap', 'statistics', 'ast', 'csv', 'io', 'copy', 'zlib', 'gzip',
    'bz2', 'lzma', 'glob', 'tempfile', 'traceback', 'types', 'weakref',
    'difflib', 'secrets', 'decimal', 'fractions', 'heapq', 'bisect',
    'array', 'struct', 'binascii', 'errno', 'fnmatch', 'linecache',
    'shlex', 'pprint', 'contextlib', 'operator', 'enum',
})
SYMBOL_IMPORTS = {
    'Counter': ('collections', 'Counter'),
    'defaultdict': ('collections', 'defaultdict'),
    'OrderedDict': ('collections', 'OrderedDict'),
    'deque': ('collections', 'deque'),
    'namedtuple': ('collections', 'namedtuple'),
    'datetime': ('datetime', 'datetime'),
    'date': ('datetime', 'date'),
    'timedelta': ('datetime', 'timedelta'),
    'timezone': ('datetime', 'timezone'),
    'partial': ('functools', 'partial'),
    'reduce': ('functools', 'reduce'),
    'lru_cache': ('functools', 'lru_cache'),
    'wraps': ('functools', 'wraps'),
    'chain': ('itertools', 'chain'),
    'product': ('itertools', 'product'),
    'groupby': ('itertools', 'groupby'),
    'islice': ('itertools', 'islice'),
    'b64encode': ('base64', 'b64encode'),
    'b64decode': ('base64', 'b64decode'),
    'Decimal': ('decimal', 'Decimal'),
}

BUILTIN_NAMES = frozenset(dir(_bi)) | {
    '__name__', '__file__', '__doc__', '__builtins__', '__package__',
    '__spec__', '__loader__', '__debug__', 'self', 'cls',
}

BANNED_GUIDE = {
    'subprocess': 'subprocess 执行被沙箱禁止：ls→os.listdir、cat→open().read()、'
                  'cp→open 二进制、mkdir→os.makedirs、git→dulwich；确需执行→build-unsafe',
    'multiprocessing': 'multiprocessing 被沙箱禁止（进程逃逸面）；并发用列表循环或切 build-unsafe',
    'ctypes': 'ctypes 被沙箱禁止（C 层绕过面）；无等价修复',
    'cffi': 'cffi 被沙箱禁止（C 层绕过面）；无等价修复',
    'pickle': 'pickle 被沙箱禁止（反序列化 gadget）；结构化数据改用 json',
    'marshal': 'marshal 被沙箱禁止（反序列化 gadget）；结构化数据改用 json',
}

# ────────────────── F2 stub 源码（worker 内执行，全用放行库） ──────────────────
_STUB_SRC = """# [auto-fix] stub 注入：__LIBS__（沙箱内被禁 → 纯 stdlib 等价实现；未实现成员会 AttributeError）
import sys as _af_sys, os as _af_os, time as _af_time
import fnmatch as _af_fnmatch

class _af_Path(object):
    def __init__(self, *parts):
        self._p = _af_os.path.join(*[str(x) for x in parts]) if parts else '.'
    def __truediv__(self, other):
        return _af_Path(self._p, str(other))
    def __str__(self):
        return self._p
    def __repr__(self):
        return '_af_Path(%r)' % self._p
    def __fspath__(self):
        return self._p
    def __eq__(self, other):
        return str(self) == str(other)
    def __hash__(self):
        return hash(self._p)
    @property
    def parent(self):
        return _af_Path(_af_os.path.dirname(self._p) or '.')
    @property
    def name(self):
        return _af_os.path.basename(self._p)
    @property
    def suffix(self):
        return _af_os.path.splitext(self._p)[1]
    @property
    def stem(self):
        return _af_os.path.splitext(self._p)[0]
    def joinpath(self, *others):
        return _af_Path(self._p, *others)
    def exists(self):
        return _af_os.path.exists(self._p)
    def is_file(self):
        return _af_os.path.isfile(self._p)
    def is_dir(self):
        return _af_os.path.isdir(self._p)
    def resolve(self):
        return _af_Path(_af_os.path.realpath(self._p))
    def stat(self):
        return _af_os.stat(self._p)
    def mkdir(self, parents=False, exist_ok=False):
        if parents:
            _af_os.makedirs(self._p, exist_ok=exist_ok)
        else:
            try:
                _af_os.mkdir(self._p)
            except FileExistsError:
                if not exist_ok:
                    raise
    def glob(self, pattern):
        out = []
        if _af_os.path.isdir(self._p):
            for nm in sorted(_af_os.listdir(self._p)):
                if _af_fnmatch.fnmatch(nm, pattern):
                    out.append(_af_Path(self._p, nm))
        return out
    def iterdir(self):
        return [_af_Path(self._p, nm) for nm in sorted(_af_os.listdir(self._p))]
    def read_text(self, encoding='utf-8', errors='replace'):
        with open(self._p, 'r', encoding=encoding, errors=errors) as f:
            return f.read()
    def write_text(self, s, encoding='utf-8'):
        with open(self._p, 'w', encoding=encoding) as f:
            f.write(s)
    def read_bytes(self):
        with open(self._p, 'rb') as f:
            return f.read()
    def write_bytes(self, b):
        with open(self._p, 'wb') as f:
            f.write(b)
    def touch(self):
        with open(self._p, 'a'):
            pass
    def unlink(self, missing_ok=False):
        try:
            _af_os.remove(self._p)
        except FileNotFoundError:
            if not missing_ok:
                raise
    def with_suffix(self, s):
        return _af_Path(_af_os.path.splitext(self._p)[0] + s)
    def as_posix(self):
        return self._p.replace(chr(92), '/')

class _af_PathLibModule(object):
    Path = _af_Path
    PurePath = _af_Path

class _af_ShutilModule(object):
    @staticmethod
    def copyfile(src, dst):
        with open(src, 'rb') as f:
            data = f.read()
        with open(dst, 'wb') as f:
            f.write(data)
        return dst
    copy = copyfile
    @staticmethod
    def copytree(src, dst):
        _af_os.makedirs(dst, exist_ok=True)
        for root, dirs, files in _af_os.walk(src):
            rel = _af_os.path.relpath(root, src)
            tgt = dst if rel == '.' else _af_os.path.join(dst, rel)
            if rel != '.':
                _af_os.makedirs(tgt, exist_ok=True)
            for fn in files:
                _af_ShutilModule.copyfile(_af_os.path.join(root, fn), _af_os.path.join(tgt, fn))
        return dst
    @staticmethod
    def move(src, dst):
        _af_os.rename(src, dst)
        return dst
    @staticmethod
    def rmtree(path):
        ts = _af_time.strftime('%Y%m%d_%H%M%S')
        trash = _af_os.path.join(_af_os.path.dirname(path) or '.', '.trash')
        _af_os.makedirs(trash, exist_ok=True)
        tgt = _af_os.path.join(trash, _af_os.path.basename(path) + '.' + ts)
        _af_os.rename(path, tgt)
        return tgt
    @staticmethod
    def which(cmd):
        for d in _af_os.environ.get('PATH', '').split(_af_os.pathsep):
            p = _af_os.path.join(d, cmd)
            if _af_os.path.isfile(p):
                return p
        return None

for _af_nm in __LIBS__:
    if _af_nm == 'pathlib':
        _af_sys.modules['pathlib'] = _af_PathLibModule()
    elif _af_nm == 'shutil':
        _af_sys.modules['shutil'] = _af_ShutilModule()
# [auto-fix] stub end"""

_AF_OPEN_SRC = """# [auto-fix] open 自动补 encoding（二进制模式不受影响）
def _af_open(file, mode='r', *args, **kwargs):
    if 'b' not in mode and 'encoding' not in kwargs:
        kwargs['encoding'] = 'utf-8'
        kwargs.setdefault('errors', 'replace')
    return open(file, mode, *args, **kwargs)"""

_AF_TRASH_SRC = """# [auto-fix] 删除改为移入 .trash/（对齐删除规范，防误删）
def _af_trash(path, *args, **kwargs):
    import os as _af_os_m, time as _af_t
    _p = str(path)
    _d = _af_os_m.path.dirname(_p) or '.'
    _trash = _af_os_m.path.join(_d, '.trash')
    _af_os_m.makedirs(_trash, exist_ok=True)
    _tgt = _af_os_m.path.join(_trash, _af_os_m.path.basename(_p) + '.' + _af_t.strftime('%Y%m%d_%H%M%S%f'))
    return _af_os_m.rename(_p, _tgt)"""


def _collect_names(tree):
    """返回 (defined, used, two_seg, imported_tops)"""
    defined, used, two_seg, imported_tops = set(), set(), set(), set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name):
            (defined if isinstance(n.ctx, (ast.Store, ast.Del)) else used).add(n.id)
        elif isinstance(n, ast.Import):
            for a in n.names:
                defined.add((a.asname or a.name).split('.')[0])
                imported_tops.add(a.name.split('.')[0])
        elif isinstance(n, ast.ImportFrom):
            if n.module:
                imported_tops.add(n.module.split('.')[0])
            for a in n.names:
                if a.name != '*':
                    defined.add(a.asname or a.name)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(n.name)
        elif isinstance(n, ast.arg):
            defined.add(n.arg)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            defined.add(n.name)
        elif isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name):
            two_seg.add(n.value.id + '.' + n.attr)
    return defined, used, two_seg, imported_tops


def preflight(code, unrestricted=False):
    """pythonrt 代码预检与自动修复。返回 (new_code, fixes, guides)。"""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return code, [], []  # 语法错误放行，真实报错更有教育意义
    # __future__ import 必须位于文件最前，跳过头部注入（保守）
    if any(isinstance(n, ast.ImportFrom) and n.module == '__future__' for n in tree.body):
        return code, [], []

    fixes, guides, header = [], [], []
    defined, used, two_seg, imported_tops = _collect_names(tree)

    # ── F1 漏 import 自动补 ──
    missing = used - defined - BUILTIN_NAMES
    import_lines, fixed_names = [], []
    for name in sorted(missing):
        if name.startswith('_'):
            continue
        if name in SYMBOL_IMPORTS:
            mod, sym = SYMBOL_IMPORTS[name]
            if name == 'datetime' and 'datetime.datetime' in two_seg:
                import_lines.append('import datetime')
            else:
                import_lines.append('from %s import %s' % (mod, sym))
            fixed_names.append(name)
        elif name in SAFE_MODULES:
            import_lines.append('import %s' % name)
            fixed_names.append(name)
    if import_lines:
        header.append('# [auto-fix] 自动补充缺失 import（下次请顶部显式 import）\n' + '\n'.join(import_lines))
        fixes.append('补 import: ' + ', '.join(fixed_names))

    # ── F2 stub 注入 + 不可修指引（仅受限模式）──
    if not unrestricted:
        need_stubs = sorted(imported_tops & {'pathlib', 'shutil'})
        if need_stubs:
            header.append(_STUB_SRC.replace('__LIBS__', repr(need_stubs)))
            fixes.append('注入 stub: ' + ', '.join(need_stubs) + '（stdlib 等价实现，原代码零改动）')
        for top in sorted(imported_tops):
            if top in BANNED_GUIDE:
                guides.append(BANNED_GUIDE[top])
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ('exec', 'eval', 'compile'):
                guides.append('%s() 被 builtins 屏蔽：语法校验用 ast.parse；动态执行需求→build-unsafe' % n.func.id)
                break

    # ── F3/F4 调用点 span 替换 ──
    redefined = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in ('open', '_af_open', '_af_trash'):
            redefined.add(n.name)
        elif isinstance(n, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            for t in (n.targets if isinstance(n, ast.Assign) else [n.target]):
                for x in ast.walk(t):
                    if isinstance(x, ast.Name) and x.id in ('open', '_af_open', '_af_trash'):
                        redefined.add(x.id)
                    elif (isinstance(x, ast.Attribute) and x.attr in ('remove', 'unlink')
                          and isinstance(x.value, ast.Name) and x.value.id == 'os'):
                        redefined.add('os.remove')

    spans = []
    if not unrestricted and 'open' not in redefined:
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'open':
                if any(isinstance(a, ast.Starred) for a in n.args):
                    continue
                if any(k.arg == 'encoding' for k in n.keywords):
                    continue
                if (len(n.args) >= 2 and isinstance(n.args[1], ast.Constant)
                        and isinstance(n.args[1].value, str) and 'b' in n.args[1].value):
                    continue
                f = n.func
                spans.append((f.lineno, f.col_offset, f.end_lineno, f.end_col_offset, '_af_open'))
    if not unrestricted and 'os.remove' not in redefined:
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr in ('remove', 'unlink')
                    and isinstance(n.func.value, ast.Name) and n.func.value.id == 'os'):
                f = n.func
                spans.append((f.lineno, f.col_offset, f.end_lineno, f.end_col_offset, '_af_trash'))

    n_open = sum(1 for s in spans if s[4] == '_af_open')
    n_trash = sum(1 for s in spans if s[4] == '_af_trash')
    if spans:
        lines = code.splitlines(keepends=True)
        for ln, col, eln, ecol, repl in sorted(spans, key=lambda s: (s[0], s[1]), reverse=True):
            if ln == eln:
                line = lines[ln - 1]
                lines[ln - 1] = line[:col] + repl + line[ecol:]
            else:
                lines[ln - 1] = lines[ln - 1][:col] + repl
                for _m in range(ln, eln - 1):
                    lines[_m] = ''
                lines[eln - 1] = lines[eln - 1][ecol:]
        code = ''.join(lines)
    if n_open:
        header.append(_AF_OPEN_SRC)
        fixes.append('open 补编码: %d 处（_af_open 自动 utf-8/replace，二进制模式不受影响）' % n_open)
    if n_trash:
        header.append(_AF_TRASH_SRC)
        fixes.append('os.remove/unlink→_af_trash: %d 处（移入 .trash/，对齐删除规范）' % n_trash)

    new_code = ('\n\n'.join(header) + '\n\n' + code) if header else code
    return new_code, fixes, guides
