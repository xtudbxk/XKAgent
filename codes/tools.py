"""codes/tools.py — 工具定义与执行（pythonrt 统一运行时）。

设计意图（工具收敛后的单执行器架构）：
  1. 唯一核心执行工具 pythonrt（基于 codes/sandbox.py 纯 stdlib 沙箱），
     不再暴露 bash / bash_raw / python(pyeryx) / sandboxpy / workflow；
  2. shell 能力由用户显式 `!xxx` 命令通道提供（所有 mode 可用，安全由用户负责），
     不属于 LLM 工具面；
  3. mode 决定 pythonrt 的执行 profile：
       plan         → 只读受限（workdir + permission.txt + /mount 均只读，
                       stdlib 白名单，禁 subprocess/ctypes/socket 等）
       build        → 可写受限（workdir 与 rw 挂载可写，仍限制危险能力）
       permission.txt 语法 v2（<path> <ro|ro/rw>）：ro=plan/build 均只读，
           ro/rw=plan 只读 / build 可写；旧词 read/write 兼容（read→ro, write→ro/rw）。
       build-unsafe → 无限制（unrestricted：任意路径/import/subprocess/网络）
  4. /mount 是 pythonrt 的统一挂载表（软边界），单一事实源；
  5. 多步逻辑/文件操作/数据处理在一个脚本内完成，结构化输出用 print + JSON 约定。
"""

import json
import os
import re
import subprocess
import sys
import time
import threading

from codes.llm import complete
from codes.skill import ToolDef, ToolResult, getskill, SkillLoader
from codes.search import searchskill
from codes._log import logger
from codes.agent_runner import run_agent
from codes import config

# ── 文本工具（exec_agent 与 agent.py 共用，单一定义源）──

def _sanitize(s):
    """清理代理字符（流式输出可能产生非法 Unicode 代理项，会导致 JSON/DB 异常）。"""
    return re.sub(r'[\ud800-\udfff]', '', s)


def _tool_result_to_str(r):
    """将 ToolResult 渲染为纯文本，供消息历史回填。"""
    parts = []
    if r.stdout:
        parts.append(r.stdout)
    if r.stderr:
        parts.append('[stderr]\n' + r.stderr)
    if r.error:
        parts.append('[error] ' + r.error)
    return _sanitize('\n'.join(parts))


def _is_valid_json(s: str) -> bool:
    """判断字符串是否为合法 JSON（exec_agent 最终回复校验用）。"""
    if not s:
        return False
    try:
        json.loads(s)
        return True
    except Exception:
        return False


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Tool definitions (schemas)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TOOL_PYTHONRT_SCHEMA = ToolDef(
    name="pythonrt",
    description=(
        "统一 Python 运行时（唯一核心执行工具）。按当前 mode 自动切换执行 profile：\n"
        "  plan         → 只读受限：仅 workdir + permission.txt + /mount（全部只读），\n"
        "                 stdlib 白名单，禁 ctypes/cffi/pickle/marshal 等危险能力；\n"
        "                 subprocess 可 import 但命令执行被拒；mmap 受限放行（fd级库）\n"
        "  build        → 可写受限：workdir 与 rw 挂载可写，仍限制危险 import/进程\n"
        "  build-unsafe → 无限制：完整宿主 Python（任意路径/import/subprocess/网络）\n"
        "网络能力：plan/build 受限模式放行受控网络（socket/_socket/_ssl/ssl/asyncio 及\n"
        "         纯py网络库 urllib/http/ftplib/smtplib/email + 第三方 requests/urllib3\n"
        "         + certifi/idna/charset_normalizer/h11）；硬盘IO仍受路径白名单约束\n"
        "         （仅 workdir + permission.txt + /tmp 可写；/etc 等越界写被拦）\n"
        "多步逻辑、文件操作、数据处理均可在一个脚本内完成；需要结构化输出时用 print + 末尾 JSON 约定。\n"
        "支持一次回复提交多个调用（数组形式，引擎串行执行、结果一起返回）；独立探查/读取必须数组提交，禁止逐个小步调用\n"
        "参数: workdir(str, 执行前 chdir 到的目录), code_or_filepath(str, Python 代码或 .py 文件路径), "
        "timeout(int, 毫秒, 默认 30000)\n"
        "调用前必检：先核对 user msg「路径访问权限」段，确认 workdir/读写路径在当前 mode 可访问；"
        "越界 → 停止尝试，请求用户 /mount 挂载或切 build-unsafe（/mount 为用户命令，LLM 不可调用），勿反复硬试"
    ),
    parameters={
        "type": "object",
        "properties": {
            "workdir": {"type": "string", "description": "Working directory to chdir to before execution"},
            "code_or_filepath": {"type": "string", "description": "Python code string, or path to a .py file to read and execute"},
            "timeout": {"type": "integer", "description": "Execution timeout in milliseconds (default: 30000)"},
        },
        "required": ["workdir", "code_or_filepath"],
    },
    execute=None,
)


TOOL_EXIT_SCHEMA = ToolDef(
    name="exit",
    description="Exit the current conversation loop. Call this when you detect an infinite loop, "
                "an unrecoverable error, or when you cannot fulfill the user's request.",
    parameters={
        "type": "object",
        "properties": {
            "reason": {"type": "string", "description": "Why the exit was requested (optional)"},
            "key": {"type": "string", "description": "调用完成后直接展示给用户的信息：为什么调用 exit（死循环/不可恢复错误/无法完成任务）、当前进度或结论摘要。"},
        },
    },
    execute=None,
)


TOOL_SEARCHSKILL_SCHEMA = ToolDef(
    name="searchskill",
    description="搜索技能库中与用户需求最匹配的技能。当「建议技能」列表中的技能都不适用时，"
                "调用此工具来发现其他可用的 skill。返回按匹配度排序的技能名列表。"
                "级联策略: ngram 关键词主通道 + Embedding(FAISS) 附加（faiss/numpy 缺失时自动降级纯 ngram）。",
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词，从用户消息中提取核心意图（中英文均可）"
            },
            "top_k": {
                "type": "integer",
                "description": "返回前 N 个结果（默认 5）",
                "default": 5
            }
        },
        "required": ["query"]
    },
    execute=None,
)


TOOL_SEARCHINFO_SCHEMA = ToolDef(
    name="searchinfo",
    description=(
        "按指定目录列表 + 关键词/查询搜索文件内容。当「推荐信息」或「建议技能」不符合要求、"
        "或需要进一步查看相关目录/文件信息时调用。返回匹配的文件相对路径与片段。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "dirs_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "要搜索的目录路径列表（相对或绝对路径）",
            },
            "query_or_keyword": {
                "type": "string",
                "description": "查询文本或关键词",
            },
            "top_k": {
                "type": "integer",
                "description": "返回前 N 条（默认 5）",
                "default": 5,
            },
        },
        "required": ["dirs_paths", "query_or_keyword"],
    },
    execute=None,
)


TOOL_SUMMARY_SCHEMA = ToolDef(
    name="summary",
    description=(
        f"将重要信息持久化到 {config.DATA_DIR_NAME}/docs（跨会话可检索）。仅当出现以下四类内容时调用："
        "①跨会话需记住的决策/结论；②达成的约定/规则；③无法通过 searchinfo 搜索恢复的外部事实"
        "（如实验参数、账号、真实时间线）；④值得长期保留的关键思路/流程。"
        "⚠️ 调用成功后本轮 LLM 响应立即终止，无需再生成回复；日常琐碎信息不要调用。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "文档标题（简短概括本条信息）",
            },
            "content": {
                "type": "string",
                "description": "要持久化的正文内容",
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选标签，便于检索分类",
            },
            "key": {
                "type": "string",
                "description": "调用完成后直接展示给用户的信息：为什么调用 summary、当前进度或总结结论摘要。",
            },
        },
        "required": ["title", "content"],
    },
    execute=None,
)


TOOL_AGENT_SCHEMA = ToolDef(
    name="agent",
    description=(
        "启动一个子 LLM 执行独立任务（子 agent），返回 JSON 格式结果。"
        "可为其设定 system_prompt 与可用工具（名称列表），子 LLM 独立循环执行"
        "（含工具调用），与主对话上下文隔离。"
        "返回 JSON: {\"status\": \"ok\"|\"timeout\"|\"interrupted\"|\"max_steps\"|\"error\", "
        "\"content\": <子 LLM 最终 JSON 回复>, \"steps\": N, \"usage\": {prompt_tokens, completion_tokens}, "
        "\"error\": \"错误说明\"}"
    ),
    parameters={
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "给子 LLM 的任务指令（必填）",
            },
            "system_prompt": {
                "type": "string",
                "description": "子 LLM 的 system prompt（可选；缺省用内置默认，含'最终回复必须输出合法 JSON'约束）",
            },
            "tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "子 LLM 可用工具名列表，如 ['pythonrt']（可选；缺省=纯文本单轮回复）",
            },
            "max_steps": {
                "type": "integer",
                "description": "子 LLM 最大工具循环轮数（默认 10，防死循环）",
            },
            "timeout": {
                "type": "integer",
                "description": "子 LLM 总执行超时秒数（默认 120）",
            },
            "model": {
                "type": "string",
                "description": "覆盖子 LLM 模型（可选；缺省复用当前 provider/model）。支持 provider/model 前缀语法（如 xiaomi/mimo-v2.5、opencodego/mimo-v2.5:max）；带 images 时必须显式指定支持多模态的模型，默认纯文本模型不支持图片",
            },
            "allow_exit": {
                "type": "boolean",
                "description": "是否允许子 LLM 调用 exit 工具（默认 false=禁止，防子 agent 退出主会话）",
            },
            "allow_agent_tool": {
                "type": "boolean",
                "description": "是否允许子 LLM 再调用 agent 工具（默认 false=禁递归）",
            },
            "images": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选：本地图片路径列表，注入子 agent 的多模态输入（base64 data URI）",
            },
        },
        "required": ["prompt"],
    },
    execute=None,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  执行函数（依赖注入 agent 实例）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def exec_exit(agent, reason="", key=""):
        logger.info(f"exit 请求: reason={reason!r}, key={key!r}")
        agent._exit_requested = True
        agent._exit_reason = reason
        agent._exit_note = key or ""   # 供 run_stream 的 turn_end_by_tool 事件携带展示
        parts = []
        if key:
            parts.append(f"🔚 {key}")
        parts.append(f"[exit requested: {reason or 'no reason given'}]")
        return ToolResult(stdout="\n".join(parts))


def exec_pythonrt(agent, workdir: str, code_or_filepath: str, timeout: int = 30000) -> ToolResult:
        """执行 Python 代码（pythonrt 统一运行时）。

        mode → profile 映射：
          plan         → run_pythonrt 受限只读
          build        → run_pythonrt 受限可写
          build-unsafe → run_pythonrt 无限制（unrestricted）

        If code_or_filepath ends with .py and is an existing file, it will be
        read and executed. Otherwise it is treated as raw Python code.
        """
        # 防御：仅当参数是"纯文件路径"（无换行符）且文件存在时才读文件，
        # 避免以 .py 结尾的多行代码字符串命中同名文件时被误读为路径。
        if (code_or_filepath.endswith(".py") and "\n" not in code_or_filepath
                and os.path.isfile(code_or_filepath)):
            try:
                with open(code_or_filepath, "r", encoding="utf-8") as f:
                    code = f.read()
            except Exception as e:
                return ToolResult(error=f"Failed to read {code_or_filepath}: {e}", exit_code=1)
        else:
            code = code_or_filepath
        return run_pythonrt(agent, code, workdir, timeout)


def exec_searchskill(agent, query: str, top_k: int = 5) -> ToolResult:
        """搜索匹配的技能并返回格式化结果。"""
        from codes.search import searchskill
        from codes.skill import getskill, SkillLoader
        names = searchskill(query, top_k=top_k)
        if not names:
            all_skills = SkillLoader.list_skills()
            return ToolResult(
                stdout=f"未找到匹配的技能。\n当前可用技能: {', '.join(all_skills)}"
            )
        lines = ["找到以下匹配技能:"]
        for name in names:
            desc = getskill(name)
            lines.append(f"  • {desc}")
        lines.append("")
        lines.append("💡 请在回复首行切换技能选择: 🎯 技能选择: <技能名>")
        return ToolResult(stdout="\n".join(lines))


def exec_searchinfo(agent, dirs_paths: list, query_or_keyword: str, top_k: int = 5) -> ToolResult:
    """按指定目录搜索文件内容并返回格式化结果。"""
    from codes.search import searchinfo
    if not dirs_paths or not query_or_keyword:
        return ToolResult(stdout="searchinfo: dirs_paths 与 query_or_keyword 均必填。")
    session = getattr(agent, "session", None)
    try:
        items = searchinfo(list(dirs_paths), str(query_or_keyword), top_k=top_k, session=session)
    except Exception as e:
        return ToolResult(error=f"searchinfo 执行失败: {e}")
    if not items:
        return ToolResult(stdout="未找到匹配内容。")
    lines = ["在指定目录中找到以下相关片段:"]
    for it in items:
        lines.append(f"  [{it['method']}] {it['path']} (score={it['score']})")
        lines.append(f"      {it['snippet']}")
    return ToolResult(stdout="\n".join(lines))


def exec_summary(agent, title: str, content: str, tags: list | None = None, key: str = "") -> ToolResult:
    """将重要信息持久化到 .xkagent/docs；成功后 stop_turn=True（本轮立即结束）。"""
    from codes.search import write_doc
    try:
        rel = write_doc(
            session=getattr(agent, "session", None),
            content=str(content or ""),
            source="summary",
            title=str(title or ""),
            tags=tags,
            model=getattr(agent, "_last_model", "") or "",
        )
    except Exception as e:
        return ToolResult(error=f"summary 写入失败: {e}")
    agent._exit_note = key or ""   # 供 run_stream 的 turn_end_by_tool 事件携带展示
    parts = []
    if key:
        parts.append(f"📌 {key}")
    parts.append(f"✅ 已持久化到 {rel}（该文档已进入 searchinfo/recommend_info 检索范围）。本轮响应结束。")
    # 2026-08-12: 展开 summary 实际写入内容（title/content/tags）——
    # tool_result 框默认展开即可见全文；首行保持稳定（📌/✅ 已持久化）以兼容前端 isTerminal 正则
    if title:
        parts.append(f"📄 {title}")
    if content:
        parts.append(str(content))
    if tags:
        parts.append(f"🏷️ tags: {', '.join(str(t) for t in tags)}")
    return ToolResult(stdout="\n".join(parts), stop_turn=True)


def _kill_and_reap(proc) -> None:
    """SIGKILL 终止 worker 并回收其 stdout/stderr 管道，防僵尸/管道残留。

    设计意图：中断/超时后 worker 已无法正常返回，需强制终止；同时消费
    PIPE（communicate）避免子进程输出滞留管道缓冲导致资源泄漏。kill 后
    communicate 立即返回（进程已终止），timeout 仅为防御性兜底。
    """
    try:
        proc.kill()
    except Exception:
        pass
    try:
        proc.communicate(timeout=2)
    except Exception:
        pass
    try:
        proc.wait()
    except Exception:
        pass


def _run_worker_streaming(agent, worker_code: str, params: dict, timeout_ms: int,
                          marker: str = "__SANDBOX_RESULT__",
                          on_line=None, result_builder=None,
                          log_source: str = "tool",
                          timeout_msg: str | None = None) -> ToolResult:
    """启动 worker 子进程并逐行流式读取 stdout/stderr（方案2 进度透传）。

    与旧 communicate() 一次性读的区别：
      1. 读线程持续读，避免输出 >PIPE 缓冲（~64KB）时写端阻塞导致死锁
         （原实现潜在 bug，大输出 pythonrt 可能卡死）；
      2. stream_output 模式下 worker 实时透传用户 print 行 → on_line 回调
         （agent.py 线程化后写入进度队列 → tool_progress 事件）；
      3. marker 行（__SANDBOX_RESULT__ / __AGENT_RESULT__）不回调、不累积，
         其 payload 用于组装最终 ToolResult（LLM 上下文保持完整）。

    on_line(line, stream): stream ∈ {"stdout","stderr"}，逐行回调（尽力而为，异常吞掉）。
    result_builder(data, stdout, stderr, rc) -> ToolResult：自定义 marker payload 解析
        （exec_agent 的 payload 是 run_agent dict，非 sandbox {"ok":...} 结构）。
    """
    proc = None
    try:
        proc = subprocess.Popen(
            [sys.executable, "-c", worker_code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
        )
        proc.stdin.write(json.dumps(params, ensure_ascii=False))
        proc.stdin.flush()
        proc.stdin.close()
        proc.stdin = None

        out_buf: list[str] = []
        err_buf: list[str] = []
        marker_hit = {"found": False, "data": None, "raw": ""}

        def _read(stream, target):
            try:
                for raw in stream:
                    if marker in raw:
                        marker_hit["found"] = True
                        payload = raw.split(marker, 1)[1].strip()
                        marker_hit["raw"] = payload
                        try:
                            marker_hit["data"] = json.loads(payload)
                        except Exception:
                            marker_hit["data"] = None
                        break  # marker 后无更多内容
                    target.append(raw)
                    if on_line is not None:
                        try:
                            on_line(raw.rstrip("\n"),
                                    "stdout" if stream is proc.stdout else "stderr")
                        except Exception:
                            pass
            except Exception:
                pass

        t1 = threading.Thread(target=_read, args=(proc.stdout, out_buf), daemon=True)
        t2 = threading.Thread(target=_read, args=(proc.stderr, err_buf), daemon=True)
        t1.start()
        t2.start()

        # 主线程轮询：中断事件 → kill worker；超时 → kill worker（与旧语义一致）
        deadline = time.time() + max(1, timeout_ms / 1000) + 2
        while proc.poll() is None:
            if agent._interrupt_event.is_set():
                _kill_and_reap(proc)
                return ToolResult(stderr="[interrupted by user]", exit_code=130)
            if time.time() >= deadline:
                _kill_and_reap(proc)
                agent._log_error(log_source + "_timeout", agent.session,
                                 f"timeout={timeout_ms}ms")
                return ToolResult(
                    error=timeout_msg or f'Command timed out after {timeout_ms}ms',
                    exit_code=124)
            time.sleep(0.05)
        t1.join(timeout=2)
        t2.join(timeout=2)

        stdout = "".join(out_buf)
        stderr = "".join(err_buf)
        if marker_hit["found"]:
            if result_builder is not None:
                return result_builder(marker_hit["data"], stdout, stderr, proc.returncode or 0)
            data = marker_hit["data"]
            if data is not None:
                if data.get("ok"):
                    return ToolResult(stdout=data.get("stdout", ""),
                                      stderr=data.get("stderr", ""), exit_code=0)
                return ToolResult(stderr=data.get("stderr", ""),
                                  exit_code=data.get("exit_code", 1))
            return ToolResult(stderr=(stderr or stdout)[-2000:]
                              + f"\n[marker 解析失败] {marker_hit['raw'][:300]}", exit_code=1)
        # worker 异常退出（无 marker）
        return ToolResult(stderr=(stderr or stdout)[-2000:], exit_code=proc.returncode or 1)
    except Exception as e:
        if agent._interrupt_event.is_set():
            return ToolResult(stderr="[interrupted by user]", exit_code=130)
        agent._log_error(log_source + "_worker", agent.session, f"error={e}")
        return ToolResult(error=str(e), exit_code=-1)
    finally:
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass


def run_pythonrt(agent, code: str, workdir: str | None = None,
                 timeout: int = 30000) -> ToolResult:
    """通过 codes/sandbox.py 的 worker main() 执行 Python（pythonrt 统一引擎）。

    进程隔离框架：worker 子进程 + 中断 kill（与旧 run_eryx 一致语义）。
    mode 决定是否受限：
      - build-unsafe → params["unrestricted"]=True，sandbox.main() 直接 exec，无任何限制
      - plan / build → 受限：rw/ro roots = sandbox_cwd(agent.cwd) + permission.txt + /mount（软边界）；
        workdir 参数仅作 chdir/相对路径基准，不改变访问根

    扩展：agent._sandbox_extensions 可配置扩展模块名列表（如 ["my_ext"]），
    其模块内调用 codes.sandbox.register_extension() 注册自定义库与处理。
    """
    try:
        from codes.sandbox import main as sandbox_main  # noqa: F401
    except ImportError as e:
        return ToolResult(error=f"sandbox.py 不可用: {e}", exit_code=1)

    unrestricted = (agent.mode == "build-unsafe")
    read_only = (agent.mode == "plan")
    # ── 双 workdir 语义（v2）──
    # sandbox_cwd：沙箱访问根（挂载根），恒为 agent.cwd（由 --workdir / config 决定），
    #               LLM 传入的 workdir 参数【不能】改变它 —— 防访问根被缩窄/放大。
    # exec_cwd：   仅作为 pythonrt 内 os.chdir 与相对路径基准（params["cwd"]），
    #               默认 = sandbox_cwd；越界目录 chdir 成功但越界读会被 roots 拦截。
    sandbox_cwd = str(agent.cwd)
    exec_cwd = workdir or sandbox_cwd
    if workdir is not None:
        try:
            _wd_in = os.path.commonpath([os.path.realpath(workdir),
                                         os.path.realpath(sandbox_cwd)]) == os.path.realpath(sandbox_cwd)
        except ValueError:
            _wd_in = False
        if not _wd_in:
            logger.warning(f"workdir {workdir!r} 超出 agent.cwd({sandbox_cwd})，仅用于 chdir，访问根不变")

    # 权限映射（仅受限模式需要）：permission.txt + /mount 动态挂载 + plan 只读
    rw_roots, ro_roots = [], []
    if not unrestricted:
        # v3 去重：/mount save 后同 path 可能同时出现在 permission 与 dynamic（session db），
        # 去重避免 roots 重复；rw 优先于 ro（sandbox 合并时 rw 覆盖 ro，语义一致）
        for path, w in agent._perm_volumes:
            _tgt = ro_roots if (read_only or not w) else rw_roots
            if path not in _tgt:
                _tgt.append(path)
        for m in agent._dyn_mounts:
            _tgt = ro_roots if (read_only or not m["writable"]) else rw_roots
            if m["path"] not in _tgt:
                _tgt.append(m["path"])
        # sandbox_cwd（访问根）: plan 只读 / build 可写；/tmp 始终可写
        if read_only:
            ro_roots.append(sandbox_cwd)
        else:
            rw_roots.append(sandbox_cwd)
        rw_roots.append("/tmp")
        # 双保险：数据目录显式只读。
        _protected = os.path.join(sandbox_cwd, config.DATA_DIR_NAME)
        if _protected not in ro_roots:
            ro_roots.append(_protected)

    params = {
        "cwd": exec_cwd,
        "rw_roots": rw_roots,
        "ro_roots": ro_roots,
        "net_policy": "allow",       # 受限模式默认放行受控网络（socket 扩展）
        "allow_sqlite": True,        # sqlite3 放行（入口 patch）
        "allow_git": True,           # dulwich 放行（纯py git，入口路径校验）
        "extensions": list(getattr(agent, "_sandbox_extensions", []) or []),
        "allow_roots": ["codes", "skills"],   # 受限模式放行项目自身包（skill 脚本依赖）
        "timeout_ms": timeout,
        "code": code,
        "unrestricted": unrestricted,
        "stream_output": True,   # 方案2: worker 实时透传 stdout/stderr（进度事件）
    }

    worker_code = (
        "import sys; sys.path.insert(0, "
        + repr(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        + "); from codes.sandbox import main; main()"
    )

    agent._in_tool_exec = True
    try:
        # ── 方案2: 流式进度 —— worker stdout/stderr 逐行实时回调 ──
        # agent.py 线程化后设置 agent._tool_progress_q；读线程 put 进度行，
        # agent 线程轮询队列 yield tool_progress 事件（尽力而为，丢失不影响结果）。
        progress_q = getattr(agent, "_tool_progress_q", None)

        def _on_line(line, stream):
            if progress_q is not None:
                try:
                    progress_q.put((stream, line))
                except Exception:
                    pass

        return _run_worker_streaming(
            agent, worker_code, params, timeout,
            marker="__SANDBOX_RESULT__", on_line=_on_line, log_source="pythonrt",
        )
    finally:
        agent._in_tool_exec = False

def exec_agent(
    agent,
    prompt: str,
    system_prompt: str | None = None,
    tools: list | None = None,
    max_steps: int = 10,
    timeout: int = 120,
    model: str | None = None,
    allow_agent_tool: bool = False,
    allow_exit: bool = False,
    images: list | None = None,
) -> ToolResult:
    """启动子 LLM 执行独立任务（子 agent），返回 JSON 格式结果（agent 工具）。

    v2 子进程化（与 pythonrt sandbox 同构）：通过 subprocess.Popen 启动
    codes/agent_worker.py，进程级隔离（子 agent 崩溃/死循环不影响主进程）、
    可并行（多 worker 各自独立进程）、中断 kill。
    图片支持：images 参数传本地图片路径列表，worker 内转 base64 data URI
    注入 prompt（多模态，与主 agent /image 注入一致）。
    """
    if not isinstance(prompt, str) or not prompt.strip():
        return ToolResult(error="agent prompt 必须是非空字符串", exit_code=1)
    if not isinstance(max_steps, int) or max_steps < 1:
        max_steps = 10
    if not isinstance(timeout, (int, float)) or timeout <= 0:
        timeout = 120

    worker_code = (
        "import sys; sys.path.insert(0, "
        + repr(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        + "); from codes.agent_worker import main; main()"
    )
    params = {
        "prompt": prompt,
        "system_prompt": system_prompt,
        "tools": tools,
        "max_steps": max_steps,
        "timeout": timeout,
        "model": model or agent.model,
        "provider": agent.provider,
        "reasoning_effort": agent._resolve_effort(),
        "allow_agent_tool": allow_agent_tool,
        "allow_exit": allow_exit,
        "images": images or [],
        # AgentProxy 构造数据（纯数据，跨进程 JSON 传递）
        "cwd": str(agent.cwd),
        "mode": agent.mode,
        "session": agent.session,
        "perm_volumes": list(getattr(agent, "_perm_volumes", []) or []),
        "dyn_mounts": list(getattr(agent, "_dyn_mounts", []) or []),
        "sandbox_extensions": list(getattr(agent, "_sandbox_extensions", []) or []),
    }
    # ── 方案2: 流式进度（与 run_pythonrt 同构，marker=__AGENT_RESULT__）──
    progress_q = getattr(agent, "_tool_progress_q", None)

    def _on_line(line, stream):
        if progress_q is not None:
            try:
                progress_q.put((stream, line))
            except Exception:
                pass

    def _agent_builder(data, stdout, stderr, rc):
        """agent worker 的 marker payload 是 run_agent 返回 dict（非 sandbox {"ok":...} 结构）。"""
        if data is None:
            return ToolResult(stderr=(stderr or stdout)[-2000:], exit_code=rc or 1)
        return ToolResult(stdout=json.dumps(data, ensure_ascii=False))

    return _run_worker_streaming(
        agent, worker_code, params, int(float(timeout) * 1000),
        marker="__AGENT_RESULT__", on_line=_on_line,
        result_builder=_agent_builder, log_source="agent",
        timeout_msg=f"agent timed out after {timeout}s",
    )
