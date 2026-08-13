# -*- coding: utf-8 -*-
"""codes/agent_worker.py — agent 工具的子进程 worker 入口（v2 subprocess 化）。

设计：exec_agent 通过 subprocess.Popen 启动本模块（与 pythonrt/sandbox 同构）：
  - 从 stdin 读 JSON params（纯数据，无函数/对象跨进程传递）
  - 构造轻量 AgentProxy（仅提供 exec_* 工具所需属性），绑定 per-worker 工具集
  - 调用 codes.agent_runner.run_agent（依赖注入 llm_complete=complete / find_tool）
  - 图片支持：images 参数 → base64 data URI 块注入 prompt（与主 agent 多模态一致）
  - stdout 输出 __AGENT_RESULT__ marker + JSON

收益：进程级隔离（子 agent 崩溃/死循环不影响主进程）、可并行（多 worker
各自独立进程）、中断语义与 pythonrt 一致（kill 子进程）。
"""
import sys
import os
import json
import base64
import mimetypes
import threading
import copy


def _image_to_block(path):
    """本地图片路径 → OpenAI 风格 image_url 块（与 agent.py _make_image_url_block
    等价；内联实现避免 import 整个 agent.py 的连带开销）。

    Returns:
        {"type": "image_url", "image_url": {"url": "data:<mime>;base64,<b64>"}} 或 None
    """
    try:
        if not path or not os.path.isfile(path):
            return None
        mime = mimetypes.guess_type(path)[0] or "image/png"
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        return {"type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{b64}"}}
    except Exception:
        return None


class AgentProxy:
    """子进程内轻量 agent 代理：提供 exec_* 工具所需属性，不复制整个 Agent 类。

    字段对齐 agent.py Agent.__init__ 中被 exec_pythonrt / exec_agent / exec_exit
    实际使用的属性（cwd/mode/session/model/provider/_perm_volumes/_dyn_mounts/
    _sandbox_extensions/_interrupt_event/_exit_requested/_exit_reason/_in_tool_exec/
    _cmd_effort + 工具集 _tool_defs）。
    """

    def __init__(self, params):
        self.cwd = params.get("cwd") or os.getcwd()
        self.mode = params.get("mode") or "plan"
        self.session = params.get("session") or "default"
        self.model = params.get("model")
        self.provider = params.get("provider")
        self._perm_volumes = list(params.get("perm_volumes") or [])
        self._dyn_mounts = list(params.get("dyn_mounts") or [])
        self._sandbox_extensions = list(params.get("sandbox_extensions") or [])
        self._interrupt_event = threading.Event()
        self._exit_requested = False
        self._exit_reason = ""
        self._in_tool_exec = False
        self._cmd_effort = None
        self._tool_defs = self._build_tools()

    def _build_tools(self):
        """深拷贝模块级 schema 并绑定本 proxy（与 agent.py L482-492 同构）。"""
        from codes.tools import (
            TOOL_PYTHONRT_SCHEMA, TOOL_EXIT_SCHEMA, TOOL_SEARCHSKILL_SCHEMA,
            TOOL_AGENT_SCHEMA, exec_pythonrt, exec_exit, exec_searchskill, exec_agent,
        )
        out = []
        for _schema, _fn in (
            (TOOL_PYTHONRT_SCHEMA, exec_pythonrt),
            (TOOL_EXIT_SCHEMA, exec_exit),
            (TOOL_SEARCHSKILL_SCHEMA, exec_searchskill),
            (TOOL_AGENT_SCHEMA, exec_agent),
        ):
            _t = copy.deepcopy(_schema)
            _t.execute = (lambda _fn=_fn: (lambda **kw: _fn(self, **kw)))()
            out.append(_t)
        return out

    def _all_tools(self):
        return self._tool_defs

    def _find_tool(self, name):
        for t in self._tool_defs:
            if t.name == name:
                return t
        return None

    def _resolve_effort(self):
        # reasoning_effort 已由主进程解析后传入 params，子进程内无需再解析
        return None

    def _log_error(self, *args):
        # run_agent 约定 log_error: Callable[[str], None]（单参数 detail）；
        # 兼容旧三参数调用 (source, session, detail)，避免子 agent LLM 报错时二次崩溃。
        if len(args) == 1:
            source, session, detail = "llm", "?", args[0]
        elif len(args) == 3:
            source, session, detail = args
        else:
            source, session, detail = "agent", "?", " ".join(str(a) for a in args)
        print(f"[agent_worker][session={session}][{source}] {detail}",
              file=sys.stderr, flush=True)


def main():
    """worker 入口：读 stdin JSON → 构造 proxy → run_agent → 输出 marker+JSON。"""
    try:
        params = json.load(sys.stdin)
    except Exception as e:
        print("__AGENT_RESULT__" + json.dumps(
            {"status": "error", "content": None, "steps": 0,
             "usage": {"prompt_tokens": 0, "completion_tokens": 0},
             "error": f"[agent_worker] params 解析失败: {e}"}, ensure_ascii=False))
        return

    from codes.agent_runner import run_agent
    from codes.llm import complete
    from codes.tools import _tool_result_to_str

    prompt = params.get("prompt") or ""
    imgs = params.get("images") or []
    if imgs:
        # 图片注入：prompt → [text, image_url...] blocks（与主 agent 多模态注入一致）
        blocks = [{"type": "text", "text": prompt}]
        for ip in imgs:
            blk = _image_to_block(ip)
            if blk is not None:
                blocks.append(blk)
        prompt = blocks

    proxy = AgentProxy(params)
    data = run_agent(
        prompt=prompt,
        system_prompt=params.get("system_prompt"),
        tools=params.get("tools"),
        max_steps=params.get("max_steps") or 10,
        timeout=params.get("timeout") or 120,
        model=params.get("model"),
        provider=params.get("provider"),
        allow_agent_tool=bool(params.get("allow_agent_tool")),
        allow_exit=bool(params.get("allow_exit")),
        interrupt_event=proxy._interrupt_event,
        llm_complete=complete,
        reasoning_effort=params.get("reasoning_effort"),
        list_all_tools=proxy._all_tools,
        find_tool=proxy._find_tool,
        tool_result_to_str=_tool_result_to_str,
        log_error=proxy._log_error,
    )
    print("__AGENT_RESULT__" + json.dumps(data, ensure_ascii=False))


if __name__ == "__main__":
    main()
