#!/usr/bin/env python3
# deps: stdlib only
"""python_checker — Python 脚本语法与逻辑检查核心脚本。

纯标准库，零依赖，兼容 WASM Python 沙箱。
通过 AST 解析对 Python 代码进行多维度检查，输出结构化错误报告。

Usage:
    python checker.py <filepath>                # 检查单个文件
    python checker.py <filepath> --json         # JSON 格式输出
    python checker.py <dirpath>                 # 递归检查目录
    python checker.py <dirpath> --summary       # 只输出汇总
"""

import ast
import json
import os
import sys
from collections import defaultdict

# ── 内置符号白名单 ──
BUILTINS = {
    'print','len','range','int','str','float','list','dict','set','tuple','bool','type',
    'isinstance','issubclass','hasattr','getattr','setattr','open','super','object',
    'property','staticmethod','classmethod','enumerate','zip','map','filter','sorted',
    'reversed','min','max','sum','abs','any','all','iter','next','input','ord','chr',
    'repr','callable','format','hash','id','pow','round','slice','vars','dir','eval','exec',
    'bytes','bytearray','memoryview','frozenset',
    'Exception','ValueError','TypeError','KeyError','IndexError','AttributeError',
    'ImportError','KeyboardInterrupt','StopIteration','RuntimeError','FileNotFoundError',
    'OSError','IOError','SystemExit','NotImplementedError','ZeroDivisionError',
    'NameError','SyntaxError','PermissionError','EOFError','BlockingIOError',
    'ConnectionError','TimeoutError','ConnectionRefusedError','ConnectionAbortedError',
    'ConnectionResetError','BrokenPipeError','ChildProcessError',
    'True','False','None','self','cls','NotImplemented','Ellipsis','__debug__',
}

# ── 已知标准库模块（白名单） ──
STDLIB_MODULES = {
    'json','os','re','select','subprocess','sys','shlex','time','queue',
    'datetime','argparse','ctypes','readline','atexit','ast','enum',
    'functools','hashlib','io','logging','math','pathlib','random',
    'sqlite3','string','textwrap','threading','typing','urllib','uuid',
    'warnings','collections','copy','itertools','inspect','base64','struct',
    'socket','csv','abc','dataclasses','traceback','tempfile','shutil',
    'fcntl','asyncio','secrets','traceback','ssl','http','html','xml',
    'glob','gzip','bz2','lzma','zipfile','tarfile','statistics',
    'difflib','filecmp','textwrap','unicodedata','stringprep',
    'pprint','dis','tokenize','keyword','symbol','token',
    'pickle','shelve','marshal','dbm','sqlite3',
}

# ── 已知第三方库常用子模块（灰名单） ──
KNOWN_THIRD_PARTY = {
    'numpy': {'array','zeros','ones','empty','eye','identity','arange','linspace',
              'reshape','concatenate','stack','split','transpose','dot','matmul',
              'linalg','random','fft','ndarray','int64','float32','float64','newaxis',
              'where','divide','zeros_like','ones_like','full_like','expand_dims',
              'squeeze','clip','abs','sum','mean','std','var','max','min','argmax',
              'argmin','sort','argsort','shape','size','dtype','ndim','copy'},
    'faiss': {'IndexFlatIP','IndexFlatL2','IndexIVFFlat','IndexHNSWFlat',
              'normalize_L2','read_index','write_index','search','add',
              'MetricType','METRIC_INNER_PRODUCT','METRIC_L2'},
    'requests': {'get','post','put','delete','patch','head','options','session',
                 'Response','request'},
    'flask': {'Flask','request','jsonify','render_template','redirect','url_for',
              'Response','abort','make_response'},
    'fastapi': {'FastAPI','Request','Response','WebSocket','WebSocketDisconnect',
                'HTTPException','Query','Path','Body','Header','Cookie','Form',
                'File','UploadFile','Depends','status'},
    'fastapi.responses': {'HTMLResponse','JSONResponse','RedirectResponse',
                          'PlainTextResponse','FileResponse','StreamingResponse'},
    'transformers': {'AutoTokenizer','AutoModel','AutoModelForCausalLM',
                     'AutoConfig','pipeline','set_seed','PreTrainedTokenizer'},
    'onnxruntime': {'InferenceSession','SessionOptions','ExecutionMode'},
    'litellm': {'completion','acompletion','completion_cost','token_counter'},
    'uvicorn': {'run','Server','Config'},

}

# ═══════════════════════════════════════════════
#  1. 语法检查
# ═══════════════════════════════════════════════
def check_syntax(filepath: str, content: str) -> list:
    """语法检查：ast.parse 基础校验"""
    errors = []
    try:
        ast.parse(content, filename=filepath)
    except SyntaxError as e:
        errors.append({
            "type": "syntax",
            "severity": "P0",
            "line": e.lineno,
            "col": e.offset,
            "message": f"语法错误: {e.msg}",
            "detail": e.text.strip() if e.text else "",
        })
    except Exception as e:
        errors.append({
            "type": "syntax",
            "severity": "P0",
            "line": 0,
            "col": 0,
            "message": f"文件读取错误: {e}",
            "detail": "",
        })
    return errors

# ═══════════════════════════════════════════════
#  2. 导入检查
# ═══════════════════════════════════════════════
def _collect_imports(tree, content_lines):
    """收集文件中的所有导入语句"""
    imports = []  # [{type, module, names, as_names, line}]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append({
                    "type": "import",
                    "module": alias.name,
                    "name": alias.asname or alias.name,
                    "line": node.lineno,
                })
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if alias.name == '*':
                    imports.append({
                        "type": "from_*",
                        "module": node.module,
                        "name": '*',
                        "line": node.lineno,
                    })
                else:
                    imports.append({
                        "type": "from",
                        "module": node.module,
                        "name": alias.asname or alias.name,
                        "source_name": alias.name,
                        "line": node.lineno,
                    })
    return imports

def _collect_name_refs(tree):
    """收集所有 Name 引用（Load 上下文）+ 属性访问中的模块名"""
    refs = set()
    class RefCollector(ast.NodeVisitor):
        def visit_Name(self, node):
            if isinstance(node.ctx, ast.Load):
                refs.add(node.id)
            self.generic_visit(node)
        def visit_Attribute(self, node):
            if isinstance(node.value, ast.Name) and isinstance(node.ctx, ast.Load):
                refs.add(node.value.id)
            self.generic_visit(node)
    RefCollector().visit(tree)
    return refs

def check_imports(filepath: str, content: str, tree) -> list:
    """导入检查：缺失导入/未使用/重复/通配符/外部库符号"""
    errors = []
    lines = content.split('\n')
    
    imports = _collect_imports(tree, lines)
    name_refs = _collect_name_refs(tree)
    
    # 构建导入符号表
    imported_symbols = {}  # {name: (module, line, type)}
    for imp in imports:
        imported_symbols[imp["name"]] = (imp["module"], imp["line"], imp["type"])
    
    # 2a. 检查缺失的导入（只查作为属性访问前缀的模块名）
    # 收集所有 Attribute.value 中作为模块引用的 Name
    module_refs = set()
    class ModuleRefCollector(ast.NodeVisitor):
        def visit_Attribute(self, node):
            if (isinstance(node.value, ast.Name) and 
                isinstance(node.ctx, ast.Load)):
                name = node.value.id
                # 只检查看起来像模块名的引用：
                # 1. 不以 '_' 开头
                # 2. 不是 self
                # 3. 不是明显的局部变量名或循环变量
                # 4. 长度 2-8 个字符（模块名特征）
                COMMON_LOCALS = {'data', 'result', 'value', 'values', 'item', 'items',
                              'content', 'config', 'response', 'request', 'error',
                              'info', 'msg', 'message', 'status', 'tmp', 'temp',
                              'target', 'source', 'input', 'output', 'token',
                              'user', 'key', 'index', 'count', 'name', 'path',
                              'file', 'text', 'args', 'kwargs', 'ctx', 'log',
                              'logger', 'url', 'uri', 'addr', 'app', 'db',
                              'model', 'weights', 'grad', 'loss',
                              'train', 'test', 'val', 'batch', 'epoch',
                              'handler', 'attrs', 'methods', 'params', 'refs',
                              'errors', 'results', 'lines', 'stmt', 'stmts',
                              'imp', 'imps', 'imports', 'alias', 'aliases',
                              'arg', 'args', 'kwargs', 'opt', 'opts',
                              'node', 'nodes', 'tree', 'root', 'leaf',
                              'graph', 'edge', 'edges', 'path', 'paths',
                              'gen', 'gens', 'iter', 'itr',
                              'sc', 'col', 'row', 'val', 'var', 'vars',
                              'schema', 'cfg', 'doc', 'docs', 'desc',
                              'out', 'inp', 'buf', 'size', 'len',
                              'idx', 'pos', 'loc', 'tag', 'tags',
                              'fn', 'func', 'cls', 'obj', 'attr',
                              'src', 'dst', 'prev', 'next', 'cur',
                              'old', 'new', 'tmp', 'bak', 'cfg',
                              'host', 'port', 'addr', 'uri', 'url',
                              'db', 'conn', 'cur', 'cursor', 'row',
                              'fmt', 'form', 'formats', 'typ', 'mode',
                              # for/推导式常用变量
                              'i', 'j', 'k', 'x', 'y', 'z', 'v', 'c', 's',
                              't', 'm', 'n', 'r', 'f', 'd', 'p', 'w', 'b', 'a',
                              'e', 'ex', 'err', 'exc', 'idx', 'pos', 'loc',
                              'el', 'elt', 'ele', 'elems',
                              # 在属性访问中常见的变量名（非模块）
                              'line', 'filename', 'checker', 'mod', 'visited',
                              'stack', 'var_name', 'py_files', 'local', 'parser',
                              'stripped', 'lib', 'default', 'neighbor', 'pattern'}
                if (not name.startswith('_') and name != 'self' and 
                    name not in COMMON_LOCALS and 2 <= len(name) <= 8):
                    module_refs.add(name)
            self.generic_visit(node)
    ModuleRefCollector().visit(tree)
    
    for ref in module_refs:
        if ref not in imported_symbols and ref not in BUILTINS:
            # 检查是否在已知第三方库列表中
            found = False
            for lib, syms in KNOWN_THIRD_PARTY.items():
                if ref == lib.split('.')[0]:
                    found = True
                    break
            if not found:
                # 排除常见的局部变量
                COMMON_LOCALS = {'data', 'result', 'value', 'values', 'item', 'items',
                              'content', 'config', 'response', 'request', 'error',
                              'info', 'msg', 'message', 'status', 'tmp', 'temp',
                              'target', 'source', 'input', 'output', 'token',
                              'user', 'key', 'index', 'count', 'name', 'path',
                              'file', 'text', 'args', 'kwargs', 'ctx', 'log',
                              'logger', 'url', 'uri', 'addr', 'app', 'db',
                              'model', 'weights', 'grad', 'loss',
                              'train', 'test', 'val', 'batch', 'epoch',
                              # 已知模块别名（非库名，但常用作别名）
                              }
                if ref not in COMMON_LOCALS:
                    imported_modules = {imp["name"] for imp in imports if imp["type"] == "import"}
                    if ref not in imported_modules:
                        errors.append({
                            "type": "import_missing",
                            "severity": "P0",
                            "line": 0,
                            "message": f"可能缺失导入: '{ref}' 作为模块名使用但未导入",
                            "detail": f"代码中使用 '{ref}.xxx' 的形式，但没有 import {ref} 语句。若为第三方库，请添加 import {ref}",
                        })
    # 2b. 检查未使用的导入
    for imp in imports:
        name = imp["name"]
        if name in BUILTINS or name == '*':
            continue
        # 检查该名称是否被引用
        used = name in name_refs
        # 对于 import X，检查是否有 X.Y 属性访问
        if not used and imp["type"] == "import":
            class AttrCheck(ast.NodeVisitor):
                def __init__(self):
                    self.found = False
                def visit_Attribute(self, node):
                    if (isinstance(node.value, ast.Name) and 
                        node.value.id == name and 
                        isinstance(node.ctx, ast.Load)):
                        self.found = True
                    self.generic_visit(node)
            checker = AttrCheck()
            checker.visit(tree)
            used = checker.found
        
        if not used:
            errors.append({
                "type": "import_unused",
                "severity": "P2",
                "line": imp["line"],
                "message": f"未使用导入: '{imp['name']}'",
                "detail": f"第 {imp['line']} 行导入后未在代码中引用",
            })
    
    # 2c. 检查重复导入
    seen_imports = set()
    for imp in imports:
        key = (imp["module"], imp.get("source_name", imp["name"]))
        if key in seen_imports:
            errors.append({
                "type": "import_duplicate",
                "severity": "P2",
                "line": imp["line"],
                "message": f"重复导入: '{imp['module']}.{imp.get('source_name', imp['name'])}'",
                "detail": f"第 {imp['line']} 行存在重复导入",
            })
        seen_imports.add(key)
    
    # 2d. 检查通配符导入
    for imp in imports:
        if imp["type"] == "from_*":
            errors.append({
                "type": "import_wildcard",
                "severity": "P1",
                "line": imp["line"],
                "message": f"通配符导入: from {imp['module']} import *",
                "detail": "通配符导入会污染命名空间，建议显式导入具体符号",
            })
    
    # 2e. 检查导入的符号在目标文件中是否存在（简单版本）
    # 只在跨文件场景下检查，此处作标注
    for imp in imports:
        if imp["type"] == "from" and imp.get("source_name"):
            # 标注外部依赖
            module_parts = imp["module"].split('.')
            if module_parts[0] in STDLIB_MODULES:
                pass  # stdlib 信任
            elif module_parts[0] in KNOWN_THIRD_PARTY:
                # 检查灰名单中是否有该子模块/符号
                known_syms = KNOWN_THIRD_PARTY.get(imp["module"], 
                                                    KNOWN_THIRD_PARTY.get(module_parts[0], {}))
                if known_syms and imp["source_name"] not in known_syms:
                    errors.append({
                        "type": "import_symbol_not_found",
                        "severity": "P1",
                        "line": imp["line"],
                        "message": f"外部符号 '{imp['source_name']}' 在 '{imp['module']}' 中未确认存在",
                        "detail": f"从 {imp['module']} 导入 {imp['source_name']}，但该符号不在已知列表中，需验证",
                    })
    
    return errors

# ═══════════════════════════════════════════════
#  3. 属性检查
# ═══════════════════════════════════════════════
def check_attributes(filepath: str, content: str, tree) -> list:
    """属性检查：类成员属性访问合法性"""
    errors = []
    
    # 收集类定义
    class_defs = {}  # {class_name: {methods: set, attrs: set, lineno: int}}
    
    class ClassCollector(ast.NodeVisitor):
        def visit_ClassDef(self, node):
            methods = set()
            attrs = set()
            for item in ast.iter_child_nodes(node):
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.add(item.name)
                elif isinstance(item, ast.Assign):
                    for t in item.targets:
                        if isinstance(t, ast.Name):
                            attrs.add(t.id)
                elif isinstance(item, ast.AnnAssign):
                    if isinstance(item.target, ast.Name):
                        attrs.add(item.target.id)
            class_defs[node.name] = {
                "methods": methods,
                "attrs": attrs,
                "lineno": node.lineno,
            }
            self.generic_visit(node)
    
    ClassCollector().visit(tree)
    
    # 检查 self.xxx 访问是否在类方法中定义了对应的属性/方法
    class MethodChecker(ast.NodeVisitor):
        def __init__(self):
            self.current_class = None
            
        def visit_FunctionDef(self, node):
            old_class = self.current_class
            self.generic_visit(node)
            self.current_class = old_class
            
        def visit_ClassDef(self, node):
            old_class = self.current_class
            self.current_class = node.name
            self.generic_visit(node)
            self.current_class = old_class
            
        def visit_Attribute(self, node):
            if (isinstance(node.value, ast.Name) and 
                node.value.id == 'self' and 
                isinstance(node.ctx, ast.Load) and
                self.current_class):
                attr_name = node.attr
                cls_info = class_defs.get(self.current_class, {"methods": set(), "attrs": set()})
                # 只检查 dunder 方法之外的属性
                if not attr_name.startswith('__'):
                    if (attr_name not in cls_info["methods"] and 
                        attr_name not in cls_info["attrs"] and
                        # 在 __init__ 中的 self.xxx = ... 先不报
                        attr_name != 'xxx'):
                        # 检查是否在 __init__ 或当前方法中通过 self.xxx = ... 定义过
                        pass  # 过于复杂的追踪，暂时不做跨方法分析
            self.generic_visit(node)
    
    MethodChecker().visit(tree)
    
    # 检查可能的属性拼写错误（如 self.messges vs self.messages）
    # 在类方法中收集所有 self.xxx 引用，找发音/拼写相近的
    class SpellingChecker(ast.NodeVisitor):
        def __init__(self):
            self.self_attrs = defaultdict(set)  # {class_name: {attr_names}}
            self.current_class = None
            
        def visit_ClassDef(self, node):
            self.current_class = node.name
            self.generic_visit(node)
            self.current_class = None
            
        def visit_FunctionDef(self, node):
            self.generic_visit(node)
            
        def visit_Attribute(self, node):
            if (isinstance(node.value, ast.Name) and 
                node.value.id == 'self' and self.current_class):
                self.self_attrs[self.current_class].add(node.attr)
            self.generic_visit(node)
    
    sc = SpellingChecker()
    sc.visit(tree)
    
    # 简单拼写检查：在一个类中，如果一个属性只出现一次且其他属性有类似拼写，标记
    for cls_name, attrs in sc.self_attrs.items():
        for attr in attrs:
            count = sum(1 for a in attrs if a == attr)
            if count >= 3:
                continue  # 高频使用，不太可能拼错
            # 找拼写相似的属性
            for other in attrs:
                if other != attr and len(other) > 3 and len(attr) > 3:
                    # 简单的编辑距离检查：如果只差 1 个字符
                    if abs(len(other) - len(attr)) <= 1:
                        diff = sum(1 for i in range(min(len(other), len(attr))) 
                                   if other[i] != attr[i])
                        if diff == 1 and len(attr) > 3:
                            errors.append({
                                "type": "attribute_typo",
                                "severity": "P1",
                                "line": 0,
                                "message": f"可能的属性拼写错误: '{attr}' 和 '{other}' 很相似",
                                "detail": f"在类 {cls_name} 中，'{attr}' 和 '{other}' 只差一个字符",
                            })
                            break
    
    return errors

# ═══════════════════════════════════════════════
#  4. 逻辑检查
# ═══════════════════════════════════════════════
def check_logic(filepath: str, content: str, tree) -> list:
    """逻辑检查：未定义引用/可变默认参数/裸except/==None/死代码"""
    errors = []
    
    # 4a. 未定义引用（作用域栈追踪）
    imports = _collect_imports(tree, content.split('\n'))
    imported_symbols = {imp["name"] for imp in imports}
    
    initial_scope = set(BUILTINS)
    initial_scope.update(imported_symbols)
    
    # 添加文件顶层导出
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            initial_scope.add(node.name)
        elif isinstance(node, ast.ClassDef):
            initial_scope.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    initial_scope.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            initial_scope.add(node.target.id)
    
    undefined_refs = []
    
    class ScopeAnalyzer(ast.NodeVisitor):
        def __init__(self):
            self.scopes = [set(initial_scope)]
            
        def visit_FunctionDef(self, node):
            params = set()
            for arg in node.args.args + node.args.posonlyargs + node.args.kwonlyargs:
                params.add(arg.arg)
            if node.args.vararg: params.add(node.args.vararg.arg)
            if node.args.kwarg: params.add(node.args.kwarg.arg)
            # 函数名在外层作用域可见（也加入内层以支持递归）
            self.scopes[-1].add(node.name)
            self.scopes.append(params)
            self.generic_visit(node)
            self.scopes.pop()
            
        def visit_AsyncFunctionDef(self, node):
            self.visit_FunctionDef(node)
            
        def visit_Lambda(self, node):
            self.scopes.append({arg.arg for arg in node.args.args})
            self.generic_visit(node)
            self.scopes.pop()
            
        def visit_ExceptHandler(self, node):
            if node.name:
                self.scopes.append({node.name})
                self.generic_visit(node)
                self.scopes.pop()
            else:
                self.generic_visit(node)
                
        def visit_Assign(self, node):
            self.generic_visit(node)
            for t in node.targets:
                if isinstance(t, ast.Name):
                    self.scopes[-1].add(t.id)
                    
        def visit_AnnAssign(self, node):
            self.generic_visit(node)
            if isinstance(node.target, ast.Name):
                self.scopes[-1].add(node.target.id)
                
        def visit_For(self, node):
            # 先添加循环变量到作用域，再遍历子节点
            if isinstance(node.target, ast.Name):
                self.scopes[-1].add(node.target.id)
            elif isinstance(node.target, ast.Tuple):
                for el in node.target.elts:
                    if isinstance(el, ast.Name):
                        self.scopes[-1].add(el.id)
            self.generic_visit(node)
                        
        def visit_ClassDef(self, node):
            self.scopes[-1].add(node.name)
            self.generic_visit(node)

        def visit_With(self, node):
            self.generic_visit(node)
            for item in node.items:
                if item.optional_vars and isinstance(item.optional_vars, ast.Name):
                    self.scopes[-1].add(item.optional_vars.id)
                    
        def visit_ListComp(self, node):
            local = set()
            for gen in node.generators:
                if isinstance(gen.target, ast.Name):
                    local.add(gen.target.id)
            self.scopes.append(local)
            self.generic_visit(node)
            self.scopes.pop()

        def _comp_helper(self, node):
            self.visit_ListComp(node)
        visit_SetComp = _comp_helper
        visit_DictComp = _comp_helper
        visit_GeneratorExp = _comp_helper
        
        def visit_Name(self, node):
            if isinstance(node.ctx, ast.Load):
                name = node.id
                if not any(name in s for s in self.scopes):
                    if len(name) > 2 and not name.startswith('_'):
                        undefined_refs.append({
                            "type": "undefined_reference",
                            "severity": "P0",
                            "line": node.lineno,
                            "message": f"未定义引用: '{name}'",
                            "detail": f"在第 {node.lineno} 行使用了 '{name}'，但在任何作用域中都未找到定义",
                        })
            self.generic_visit(node)
    
    ScopeAnalyzer().visit(tree)
    errors.extend(undefined_refs)
    
    # 4b. 可变默认参数
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for default in node.args.defaults + node.args.kw_defaults:
                if default and isinstance(default, (ast.List, ast.Set, ast.Dict)):
                    errors.append({
                        "type": "mutable_default_arg",
                        "severity": "P1",
                        "line": node.lineno,
                        "message": f"函数 '{node.name}' 使用了可变默认参数",
                        "detail": f"第 {node.lineno} 行: 默认参数为可变对象(list/set/dict)，多次调用会共享同一对象",
                    })
                    break  # 每个函数只报一次
    
    # 4c. 裸 except
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            for handler in node.handlers:
                if handler.type is None:
                    errors.append({
                        "type": "bare_except",
                        "severity": "P1",
                        "line": handler.lineno,
                        "message": "裸 except: 未指定异常类型",
                        "detail": f"第 {handler.lineno} 行: bare except 会捕获包括 KeyboardInterrupt 在内的所有异常，建议使用 except Exception:",
                    })
    
    # 4d. == None / == True / == False
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for i, op in enumerate(node.ops):
                if isinstance(op, ast.Eq) or isinstance(op, ast.Is):
                    comparator = node.comparators[i]
                    if isinstance(comparator, ast.Constant):
                        if comparator.value is None and isinstance(op, ast.Eq):
                            errors.append({
                                "type": "compare_none",
                                "severity": "P2",
                                "line": node.lineno,
                                "message": "使用 == None，应使用 is None",
                                "detail": f"第 {node.lineno} 行: 应使用 'is None' 而非 '== None'",
                            })
    
    # 4e. 死代码（return/raise 后的语句）
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            dead = False
            for stmt in body:
                if dead:
                    errors.append({
                        "type": "dead_code",
                        "severity": "P1",
                        "line": stmt.lineno,
                        "message": f"死代码: '{type(stmt).__name__}' 在 return/raise 之后",
                        "detail": f"第 {stmt.lineno} 行的代码永远不会被执行",
                    })
                    break
                if isinstance(stmt, (ast.Return, ast.Raise)):
                    dead = True
    
    return errors

# ═══════════════════════════════════════════════
#  5. 安全审计
# ═══════════════════════════════════════════════
def check_security(filepath: str, content: str, tree) -> list:
    """安全审计：eval/exec/assert/硬编码凭据"""
    errors = []
    
    # 5a. eval/exec 调用
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in ('eval', 'exec'):
                errors.append({
                    "type": "security_eval",
                    "severity": "P1",
                    "line": node.lineno,
                    "message": f"调用了 {node.func.id}() — 安全风险",
                    "detail": f"第 {node.lineno} 行: {node.func.id}() 会动态执行任意代码，建议避免使用",
                })
    
    # 5b. assert 在生产代码中
    filename = os.path.basename(filepath)
    if not filename.startswith('test_') and 'test' not in filename:
        for node in ast.walk(tree):
            if isinstance(node, ast.Assert):
                errors.append({
                    "type": "security_assert",
                    "severity": "P2",
                    "line": node.lineno,
                    "message": f"生产代码中使用 assert",
                    "detail": f"第 {node.lineno} 行: assert 在 python -O 模式下会被跳过，建议使用 if + raise",
                })
    
    # 5c. 硬编码凭据（简单模式）
    lines = content.split('\n')
    cred_patterns = ['password', 'secret', 'api_key', 'api.secret', 'token', 'auth']
    # 变量名白名单（这些名字虽然含子串但不是凭据）
    safe_vars = {'tokens', 'tokenizer', 'tokenize', 'tocken', 'authored', 'author'}
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if '=' in stripped and not stripped.startswith('#'):
            parts = stripped.split('=', 1)
            var_name = parts[0].strip().split()[-1].strip('*_') if parts else ''
            if var_name in safe_vars:
                continue
            for pattern in cred_patterns:
                # 只检查左侧变量名是否匹配，不检查右侧值
                if pattern in var_name.lower():
                    # 检查右侧是否是字符串字面量
                    parts = stripped.split('=', 1)
                    if len(parts) == 2:
                        val = parts[1].strip()
                        if (val.startswith('"') or val.startswith("'") or 
                            val.startswith('sk-') or len(val) > 20):
                            errors.append({
                                "type": "security_hardcoded",
                                "severity": "P1",
                                "line": i,
                                "message": f"可能的硬编码凭据: '{parts[0].strip()}'",
                                "detail": f"第 {i} 行: 发现疑似凭据的硬编码，建议使用环境变量",
                            })
                            break
    
    return errors

# ═══════════════════════════════════════════════
#  6. 跨文件检查接口
# ═══════════════════════════════════════════════
def check_cross_file(filepath: str, content: str, tree, project_files: list = None) -> list:
    """跨文件检查：循环引用检测、导入符号存在性"""
    errors = []
    imports = _collect_imports(tree, content.split('\n'))
    
    if project_files:
        # 构建项目文件映射 {module_name: filepath}
        project_map = {}
        for pf in project_files:
            base = os.path.splitext(os.path.basename(pf))[0]
            project_map[base] = pf
            project_map[f'codes.{base}'] = pf
        
        # 检查内部导入的模块在项目中是否存在
        for imp in imports:
            if imp["type"] in ("import", "from", "from_*"):
                module_root = imp["module"].split('.')[0]
                if module_root == 'codes' and len(imp["module"].split('.')) > 1:
                    target_mod = imp["module"].split('.')[1]
                    if target_mod not in project_map and f'codes.{target_mod}' not in project_map:
                        errors.append({
                            "type": "cross_module_not_found",
                            "severity": "P1",
                            "line": imp["line"],
                            "message": f"导入的内部模块 '{imp['module']}' 在项目中不存在",
                            "detail": f"第 {imp['line']} 行: 模块 '{imp['module']}' 未在文件列表中找到",
                        })
    
    return errors


# ═══════════════════════════════════════════════
#  主入口
# ═══════════════════════════════════════════════
def run_checks(filepath: str, project_files: list = None, output_format: str = "text"):
    """对单个 Python 文件执行全部检查。
    
    Args:
        filepath: 文件路径
        project_files: 可选，项目所有文件路径列表，用于跨文件检查
        output_format: "text" 或 "json"
    
    Returns:
        结构化检查结果（字符串）
    """
    if not os.path.isfile(filepath):
        return json.dumps({"error": f"文件不存在: {filepath}"})
    
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
    
    all_errors = []
    
    # 1. 语法检查（最优先）
    all_errors.extend(check_syntax(filepath, content))
    
    # 语法有问题就不继续了
    if any(e["type"] == "syntax" for e in all_errors):
        result = {
            "file": filepath,
            "total_errors": len(all_errors),
            "has_syntax_error": True,
            "errors": all_errors,
        }
        return json.dumps(result, ensure_ascii=False, indent=2)
    
    # 构建 AST
    try:
        tree = ast.parse(content, filename=filepath)
    except SyntaxError:
        # 前面已经检查过了，这里不会走到
        result = {
            "file": filepath,
            "total_errors": len(all_errors),
            "has_syntax_error": True,
            "errors": all_errors,
        }
        return json.dumps(result, ensure_ascii=False, indent=2)
    
    # 2. 导入检查
    all_errors.extend(check_imports(filepath, content, tree))
    
    # 3. 属性检查
    all_errors.extend(check_attributes(filepath, content, tree))
    
    # 4. 逻辑检查
    all_errors.extend(check_logic(filepath, content, tree))
    
    # 5. 安全审计
    all_errors.extend(check_security(filepath, content, tree))
    
    # 6. 跨文件检查
    all_errors.extend(check_cross_file(filepath, content, tree, project_files))
    
    # 按严重级别排序
    severity_order = {"P0": 0, "P1": 1, "P2": 2}
    all_errors.sort(key=lambda e: severity_order.get(e.get("severity", "P2"), 99))
    
    result = {
        "file": filepath,
        "total_errors": len(all_errors),
        "has_syntax_error": False,
        "errors": all_errors,
    }
    
    if output_format == "json":
        return json.dumps(result, ensure_ascii=False, indent=2)
    else:
        return _format_text_report(result)


def _format_text_report(result: dict) -> str:
    """将检查结果格式化为可读文本报告"""
    lines = []
    lines.append(f"📄 {result['file']}")
    lines.append(f"   总问题数: {result['total_errors']}")
    lines.append("")
    
    if result['total_errors'] == 0:
        lines.append("   ✅ 未发现问题")
        return '\n'.join(lines)
    
    for err in result['errors']:
        severity = err.get("severity", "?")
        line = err.get("line", 0)
        msg = err.get("message", "")
        detail = err.get("detail", "")
        
        if severity == "P0":
            tag = "❌ P0"
        elif severity == "P1":
            tag = "⚠️ P1"
        else:
            tag = "ℹ️ P2"
        
        if line:
            lines.append(f"  {tag} L{line}: {msg}")
        else:
            lines.append(f"  {tag}: {msg}")
        if detail:
            lines.append(f"      {detail}")
    
    return '\n'.join(lines)


def check_project(project_dir: str, output_format: str = "text") -> str:
    """递归检查项目目录中的所有 .py 文件"""
    py_files = []
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in ('__pycache__', '.venv', 'venv',
                                                  '.git', '.hg', '.svn', 'node_modules',
                                                  '.mypy_cache', '.pytest_cache')]
        for f in sorted(files):
            if f.endswith('.py'):
                py_files.append(os.path.join(root, f))
    
    project_files = py_files.copy()
    all_results = []
    
    for pf in py_files:
        result_json = run_checks(pf, project_files, output_format="json")
        all_results.append(json.loads(result_json))
    
    # 跨文件：检查循环引用
    import_graph = defaultdict(set)
    for pf in py_files:
        with open(pf, 'r', encoding='utf-8') as f:
            try:
                tree = ast.parse(f.read())
            except SyntaxError:
                continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                mod = node.module
                if mod.startswith('codes.'):
                    target = mod.split('.')[1]
                    source = os.path.splitext(os.path.basename(pf))[0]
                    import_graph[source].add(target)
    
    # 找环
    def find_cycle(graph, start, visited, stack):
        visited.add(start)
        stack.add(start)
        for neighbor in graph.get(start, set()):
            if neighbor not in visited:
                cycle = find_cycle(graph, neighbor, visited, stack)
                if cycle:
                    return cycle
            elif neighbor in stack:
                # 找到环
                return [n for n in list(stack) if n == neighbor or (list(stack).index(n) <= list(stack).index(neighbor))]
        stack.remove(start)
        return None
    
    cycle_errors = []
    visited = set()
    for node in import_graph:
        if node not in visited:
            stack = set()
            cycle = find_cycle(import_graph, node, set(), set())
            if cycle:
                cycle_errors.append({
                    "type": "cross_import_cycle",
                    "severity": "P0",
                    "line": 0,
                    "message": f"检测到循环引用: {' → '.join(cycle)}",
                    "detail": f"模块间存在循环导入依赖，可能导致 ImportError",
                })
    
    if cycle_errors:
        all_results.append({"file": "跨文件分析", "total_errors": len(cycle_errors),
                            "has_syntax_error": False, "errors": cycle_errors})
    
    # 汇总
    total = sum(r["total_errors"] for r in all_results)
    summary = {
        "project": project_dir,
        "files_checked": len(py_files),
        "total_errors": total,
        "file_results": all_results,
    }
    
    if output_format == "json":
        return json.dumps(summary, ensure_ascii=False, indent=2)
    else:
        text = f"📊 项目检查报告: {project_dir}\n"
        text += f"   检查文件: {len(py_files)} 个\n"
        text += f"   发现问题: {total} 个\n\n"
        for r in all_results:
            text += _format_text_report(r) + "\n\n"
        return text


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description="Python 语法与逻辑检查工具")
    parser.add_argument("path", help="文件或目录路径")
    parser.add_argument("--json", action="store_true", help="JSON 格式输出")
    parser.add_argument("--summary", action="store_true", help="仅输出汇总")
    args = parser.parse_args()
    
    output = "json" if args.json else "text"
    
    if os.path.isfile(args.path):
        print(run_checks(args.path, output_format=output))
    elif os.path.isdir(args.path):
        print(check_project(args.path, output_format=output))
    else:
        print(f"❌ 路径不存在: {args.path}")
