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


# ── 工具输出截断（2026-09-11）──
# 防止超大工具输出（如 pythonrt 打印大目录/大文件，曾出现 5.1MB≈350 万 tokens（方舟口径））
# 撑爆 LLM 上下文（方舟 glm-5-3-flash 上限 1M tokens → 400 InvalidParameter）。
# 截断发生在历史回填统一入口 _tool_result_to_str，不影响流式进度/实时结果。
_TOOL_OUTPUT_MAX_CHARS = 100_000   # 保留前 100KB
_TOOL_OUTPUT_TAIL_CHARS = 2_000    # 保留尾 2KB（错误/结果常在尾部）


def _truncate_tool_text(text, max_chars=None, tail_chars=None, reason="输出过长"):
    """超长文本截断：头 max_chars + 截断标记 + 尾 tail_chars（公共纯函数）。

    2026-09-11: 提取为共享函数——tools._tool_result_to_str（历史回填截断）、
    agent._prune_oversized_messages / history_msgz 修剪接口（存量修剪）共用同一格式。
    """
    max_chars = _TOOL_OUTPUT_MAX_CHARS if max_chars is None else max_chars
    tail_chars = _TOOL_OUTPUT_TAIL_CHARS if tail_chars is None else tail_chars
    total = len(text)
    if total <= max_chars:
        return text
    head = text[:max_chars]
    tail = text[-tail_chars:]
    return (f"{head}\n\n...[{reason}: 原 {total} 字符，已截断保留前 "
            f"{max_chars} + 尾 {tail_chars}]...\n\n{tail}")


def _tool_result_to_str(r):
    """将 ToolResult 渲染为纯文本，供消息历史回填。

    2026-09-11: 超长输出截断（头部 + 截断标记 + 尾部），防止单条工具输出
    撑爆 LLM 上下文；截断标记告知 LLM 输出被截断，可据此缩小输出重试。
    """
    parts = []
    if r.stdout:
        parts.append(r.stdout)
    if r.stderr:
        parts.append('[stderr]\n' + r.stderr)
    if r.error:
        parts.append('[error] ' + r.error)
    text = _sanitize('\n'.join(parts))
    return _truncate_tool_text(text)


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
        "参数名必须精确（写错即 tool_args_missing 被拒；历史错误: code_or_filerank / code_filepath / code / 漏 workdir）:\n"
        "  - workdir(str, 必填, 执行前 chdir 到的目录)\n"
        "  - code_or_filepath(str, 必填, Python 代码字符串 或 .py 文件路径)\n"
        "  - timeout(int, 可选, 毫秒, 默认 30000)\n"
        "调用前必检：先核对 user msg「路径访问权限」段，确认 workdir/读写路径在当前 mode 可访问；"
        "越界 → 停止尝试，请求用户 /mount 挂载或切 build-unsafe（/mount 为用户命令，LLM 不可调用），勿反复硬试"
    ),
    parameters={
        "type": "object",
        "properties": {
            "workdir": {"type": "string", "description": "Working directory to chdir to before execution"},
            "code_or_filepath": {"type": "string", "description": "Python code string, or path to a .py file to read and execute. 参数名必须是 code_or_filepath（勿写成 code_filepath / code_or_filerank / code）"},
            "timeout": {"type": "integer", "description": "Execution timeout in milliseconds (default: 30000)"},
        },
        "required": ["workdir", "code_or_filepath"],
    },
    execute=None,
)


TOOL_CALLAGENT_SCHEMA = ToolDef(
    name="callagent",
    description=(
        "给其他 agent 会话发一封全异步邮件（立即返回，不等待对方处理）。"
        "用于跨会话协作/任务下发/自调度（to=自己+delay_seconds=定时循环）。"
        "信封四头由工具自动填充（id/from=本会话/reply_to）。回信时必须显式传 "
        "reply_to=对方来信的 msg_id、to=对方信封头 from 字段的值。"
        "返回 {\"status\":\"sent\",\"msg_id\"}；XKAGENT_MAIL=off 时返回 mail_disabled（不写信）。"
        "期望对方回复请显式置 need_reply=true（缺省 false=通知型邮件，不注入回信指引）。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "to": {
                "type": ["string", "array"],
                "items": {"type": "string"},
                "description": "收件会话名或会话名列表（列表=广播：一封信多收件人，各收件人独立投递/重试（per-to 状态），"
                         "收信方可见全体收件人；广播=通知型，不支持 need_reply=true；单名返回 msg_id。格式 "
                         "^[a-zA-Z0-9_\-.]+$ 且 ≤100 字符，谱系命名兼容），可为自己（自调度）。注意：to 必须填 session name（ASCII key）；session 的可读标题（title，支持中文）仅用于展示，不可用作 to。",
            },
            "message": {"type": "string", "description": "信件正文（≤3500 字节，超限返回 error）"},
            "reply_to": {"type": "string", "description": "回复的来信 msg_id（可选；回信必须填=对方来信的 msg_id，不填无法建立对话线）"},
            "delay_seconds": {"type": "number", "description": "延迟投递秒数（可选，默认 0 立即；to=自己+延迟=定时任务/循环唤醒；与 deliver_at 同时给出时 deliver_at 优先）"},
            "deliver_at": {"type": "number", "description": "绝对投递时间戳（可选，Unix 秒；定点定时精度 1s，跨容器有时钟偏移分钟级风险）"},
            "priority": {"type": "integer", "description": "投递优先级（可选，默认 0，大者优先）"},
            "provider": {"type": "string", "description": "收信方本次任务的 LLM provider（可选；纯 provider 名如 my-provider，"
                         "或 provider/model 复合如 my-provider/gpt-5.4；model 可带 :effort 后缀（如 gpt-5.4:max，effort 拆出注入本 turn）。"
                         "仅本次投递的 turn 生效，不修改收信会话自身配置；"
                         "收信会话未配置 provider 时可用它指定）"},
            "need_reply": {"type": "boolean", "description": "是否期望对方回复（默认 false）。true=指令中将注入 callagent "
                          "回信指引（to/reply_to/message 三要素），收信方会主动回信；false/缺省=通知型邮件，不诱导回复。"},
        },
        "required": ["to", "message"],
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
                "⚡ 本工具几乎零 LLM 调用成本（本地索引检索），可放心多调（更换关键词）——结果计入上下文，建议关键词一次覆盖多意图。"
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
        "⚡ 本工具几乎零 LLM 调用成本（本地索引检索），无启动开销；信息不足时可多次调用（更换目录/关键词）收集更全面片段——注意结果计入上下文，建议批量一次调用覆盖多目录。"
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


TOOL_SELECT_SKILL_SCHEMA = ToolDef(
    name="selectskill",
    description=(
        "从技能库选择并加载技能全文注入上下文。当「建议技能」中有合适技能、"
        "或需要按技能工作流执行时调用。已注入过的技能返回锚点摘要（不重复注入）。"
        "⚡ 本工具几乎零 LLM 调用成本（本地技能文件读取），可放心多次调用："
        "对比多个候选技能时逐个读取后再决策——技能全文计入上下文。"
        "参数: name(技能名, 必填), reason(选择理由, 可选)"
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "技能名（必填）"},
            "reason": {"type": "string", "description": "选择理由（可选）"},
        },
        "required": ["name"],
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


TOOL_ADDINFO_SCHEMA = ToolDef(
    name="addinfo",
    description=(
        "写入/更新本 session 的状态信息（session 级 KV：key=短标识符，value=单行≤200字符）。"
        "用于长时间运行中需跨回合记住的活状态（进度、当前分支、重试计数、用户偏好等）。"
        "写入后下回合起自动注入用户消息头部「状态信息」字段；同 key 重复调用为覆盖更新。"
        "与 summary 分工：短小运行态 KV 用本工具；结论/决策/长文本/跨会话信息用 summary。"
        "状态板按首次写入顺序展示（更新不改变位置）——建议首次写入即按语义排槽：总览/计划 → 进度 → 阻塞 → 其他。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "状态键名（[a-zA-Z_][a-zA-Z0-9_-]{0,31}，如 current_step、rejected_plan）"},
            "value": {"type": "string", "description": "状态值（单行，≤200 字符；多行/长文本请用 summary）"},
        },
        "required": ["key", "value"],
    },
    execute=None,
)


TOOL_LISTINFO_SCHEMA = ToolDef(
    name="listinfo",
    description="列出本 session 当前全部状态信息（key: value 及更新来源/时间）。无参数。",
    parameters={"type": "object", "properties": {}, "required": []},
    execute=None,
)


TOOL_RMINFO_SCHEMA = ToolDef(
    name="rminfo",
    description="删除本 session 的一条状态信息。参数 key 必填；key 不存在时返回错误提示。",
    parameters={
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "要删除的状态键名"},
        },
        "required": ["key"],
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

def exec_callagent(agent, to: str | list[str], message: str, reply_to: str = "",
                   delay_seconds: float = 0.0, deliver_at: float | None = None,
                   priority: int = 0, provider: str = "",
                   need_reply: bool = False) -> ToolResult:
    """callagent 执行：写 send 行到 mail.jsonl 全局总线（全异步立即返回）。

    - 信封自动填：id/from=当前会话/reply_to/deliver_at（now+delay_seconds）；
    - need_reply=true 时信封写入期望回复标志（收信方指令将含回信指引）；
    - XKAGENT_MAIL=off → mail_disabled（不写信，防堆积无人投）；
    - to 支持单会话名或列表（列表=广播：单封信多收件人，逐收件人独立投递/重试，
      收信方经 mail_meta.recipients 感知全体收件人）；逐个 validate_session_name，任一非法整体拒绝；
    - provider 支持 provider/model:effort 语法（effort 由收信侧拆出注入本 turn）；
    - 广播邮件不支持 need_reply=true（广播=通知型）。
    """
    mail_env = os.environ.get("XKAGENT_MAIL", "").strip().lower()
    if mail_env in ("off", "0", "false", "none"):
        return ToolResult(stdout=json.dumps(
            {"status": "mail_disabled", "error": "XKAGENT_MAIL=off，邮件功能已禁用"}, ensure_ascii=False))
    from codes.session_registry import validate_session_name
    # to 规范化：单名→列表；列表去重保序；任一非法 → 整体拒绝（原子性，不发送）
    if isinstance(to, str):
        to_list = [to]
    elif isinstance(to, (list, tuple)):
        to_list = []
        _seen = set()
        for _t in to:
            if isinstance(_t, str) and _t.strip() and _t not in _seen:
                _seen.add(_t)
                to_list.append(_t)
    else:
        return ToolResult(stdout=json.dumps(
            {"status": "error",
             "error": f"to 类型非法: {type(to).__name__}（应为字符串或字符串列表）"},
            ensure_ascii=False))
    if not to_list:
        return ToolResult(stdout=json.dumps(
            {"status": "error", "error": "to 列表为空"}, ensure_ascii=False))
    for _t in to_list:
        err = validate_session_name(_t)
        if err:
            return ToolResult(stdout=json.dumps(
                {"status": "error", "error": f"to '{_t}' 非法: {err}"}, ensure_ascii=False))
    provider = (provider or "").strip()
    if provider:
        from codes.mailbox import split_mail_provider
        pname, _m = split_mail_provider(provider)
        if not pname:
            return ToolResult(stdout=json.dumps(
                {"status": "error",
                 "error": f"provider 格式非法: {provider!r}（应为 provider 名或 provider/model）"},
                ensure_ascii=False))
        try:
            from codes import provider_config
            provider_config.get_provider(pname)
        except ValueError as e:
            return ToolResult(stdout=json.dumps(
                {"status": "error", "error": f"provider 不存在: {e}"}, ensure_ascii=False))
    # 广播校验：广播邮件不支持 need_reply（通知型，不诱导人人回信）
    if len(to_list) > 1 and need_reply:
        return ToolResult(stdout=json.dumps(
            {"status": "error",
             "error": "广播邮件不支持 need_reply=true（广播=通知型，收件人不被要求回复）"},
            ensure_ascii=False))
    try:
        from codes.mailbox import Mailbox
        mb = Mailbox()
        to_val = to_list if len(to_list) > 1 else to_list[0]
        ok, info = mb.add_send(from_=agent.session, to=to_val, body=message,
                               reply_to=(reply_to or None), delay_seconds=delay_seconds,
                               deliver_at=deliver_at, priority=priority,
                               provider=provider or None,
                               need_reply=bool(need_reply))
    except Exception as e:
        return ToolResult(stdout=json.dumps(
            {"status": "error", "error": f"{type(e).__name__}: {e}"}, ensure_ascii=False))
    if not ok:
        return ToolResult(stdout=json.dumps({"status": "error", "error": info}, ensure_ascii=False))
    return ToolResult(stdout=json.dumps({"status": "sent", "msg_id": info}, ensure_ascii=False))


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
        # 文件读取发生在宿主进程，必须先应用与沙箱一致的访问根校验；
        # 否则 plan/build 可借绝对路径绕过 worker VFS 读取宿主敏感文件。
        is_path = code_or_filepath.endswith(".py") and "\n" not in code_or_filepath
        base = os.path.realpath(workdir or agent.cwd)
        candidate = os.path.realpath(
            code_or_filepath if os.path.isabs(code_or_filepath)
            else os.path.join(base, code_or_filepath))
        if is_path and os.path.isfile(candidate):
            if agent.mode != "build-unsafe":
                roots = [agent.cwd, "/tmp"]
                roots.extend(path for path, _ in getattr(agent, "_perm_volumes", []))
                roots.extend(m.get("path", "") for m in getattr(agent, "_dyn_mounts", []))
                allowed = False
                for root in roots:
                    if not root:
                        continue
                    root = os.path.realpath(root)
                    try:
                        if os.path.commonpath([candidate, root]) == root:
                            allowed = True
                            break
                    except ValueError:
                        continue
                if not allowed:
                    return ToolResult(
                        error=f"Python file is outside allowed roots: {code_or_filepath}",
                        exit_code=1)
            try:
                with open(candidate, "r", encoding="utf-8") as f:
                    code = f.read()
            except Exception as e:
                return ToolResult(error=f"Failed to read {code_or_filepath}: {e}", exit_code=1)
        else:
            code = code_or_filepath
        # [auto-fix] 参数预检与自动修复（宿主侧静态处理，详见 codes/pythonrt_preflight.py）
        _fixes, _guides = [], []
        try:
            from codes.pythonrt_preflight import preflight
            code, _fixes, _guides = preflight(code, unrestricted=(agent.mode == 'build-unsafe'))
        except Exception as _pf_err:  # preflight 自身故障不阻塞执行
            logger.warning('pythonrt preflight failed: %s', _pf_err)
        _result = run_pythonrt(agent, code, workdir, timeout)
        if _fixes or _guides:
            _note = '\n'.join('[auto-fix] ' + x for x in (_fixes + _guides))
            _result.stdout = (_note + '\n' + _result.stdout) if _result.stdout else _note
        return _result


# ── search worker 化（2026-08-21）：searchinfo/searchskill 改为子进程执行 ──
# 背景：搜索类工具原本在 agent 进程内同步执行，阻塞期间无法响应中断事件
# （_exec_thread 不结束 → 回合不结束 → 前端 busy 恒 True）。与 pythonrt 同构：
# 经 _run_worker_streaming 启动子进程，50ms 轮询中断/超时 → SIGKILL。
_SEARCH_WORKER_MARKER = "__SEARCH_RESULT__"
_SEARCH_WORKER_CODE = (
    "import sys; sys.path.insert(0, "
    + repr(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    + "); from codes.search_worker import main; main()"
)


def _run_search_worker(agent, params: dict, timeout_ms: int = 120000) -> ToolResult:
    """启动 search worker 子进程（searchinfo/searchskill 统一入口，可中断可超时）。"""
    def _builder(data, stdout, stderr, rc):
        if data is None:
            return ToolResult(stderr=(stderr or stdout)[-2000:], exit_code=rc or 1)
        return ToolResult(stdout=json.dumps(data, ensure_ascii=False))
    return _run_worker_streaming(
        agent, _SEARCH_WORKER_CODE, params, timeout_ms,
        marker=_SEARCH_WORKER_MARKER, on_line=None,
        result_builder=_builder, log_source="search",
        timeout_msg=f"search timed out after {timeout_ms}ms",
    )


def _search_worker_payload(r: ToolResult) -> dict | None:
    """解析 search worker 返回的 JSON payload；失败返回 None。"""
    if r.exit_code != 0 or not r.stdout:
        return None
    try:
        return json.loads(r.stdout)
    except Exception:
        return None


def searchinfo_items(agent, dirs_paths: list, query: str, top_k: int = 5,
                     session: str | None = None) -> list:
    """worker 化 searchinfo，返回原始 items（供技能选择阶段收集信息）。"""
    params = {"kind": "searchinfo", "dirs_paths": list(dirs_paths),
              "query": str(query), "top_k": int(top_k),
              "session": session or getattr(agent, "session", None)}
    payload = _search_worker_payload(_run_search_worker(agent, params, timeout_ms=120000))
    return (payload or {}).get("items") or []


def searchskill_names(agent, query: str, top_k: int = 5) -> list:
    """worker 化 searchskill，返回技能名列表（供技能选择阶段使用）。"""
    params = {"kind": "searchskill", "query": str(query), "top_k": int(top_k),
              "session": getattr(agent, "session", None)}
    payload = _search_worker_payload(_run_search_worker(agent, params, timeout_ms=60000))
    return (payload or {}).get("names") or []


def searchskill_details(agent, query: str, top_k: int = 5) -> list:
    """worker 化 searchskill_detail，返回详情 dict 列表。"""
    params = {"kind": "searchskill_detail", "query": str(query), "top_k": int(top_k),
              "session": getattr(agent, "session", None)}
    payload = _search_worker_payload(_run_search_worker(agent, params, timeout_ms=60000))
    return (payload or {}).get("items") or []


def exec_searchskill(agent, query: str, top_k: int = 5) -> ToolResult:
    """搜索匹配的技能并返回格式化结果（worker 子进程，可中断）。"""
    names = searchskill_names(agent, query, top_k)
    if not names:
        all_skills = SkillLoader.list_skills()
        return ToolResult(
            stdout=f"未找到匹配的技能。\n当前可用技能: {', '.join(all_skills)}")
    lines = ["找到以下匹配技能:"]
    for name in names:
        desc = getskill(name)
        lines.append(f"  • {desc}")
    lines.append("")
    lines.append("💡 技能不匹配时可调用 searchskill 搜索技能库")
    return ToolResult(stdout="\n".join(lines))


def exec_selectskill(agent, name: str, reason: str = "") -> ToolResult:
    """selectskill：读取技能全文返回（无自述承诺，普通 tool 语义）。

    已注入过（marker 在上下文中，assistant/tool 角色均可）→ 返回锚点摘要；
    未注入 → 返回技能全文（带 marker 供后续去重判定）。
    纯只读（不 append messages）。
    """
    skill_dir = SkillLoader._find_skill_dir(name)
    if skill_dir is None:
        all_skills = SkillLoader.list_skills()
        return ToolResult(
            error=f"技能 {name} 不存在。可用技能: {', '.join(all_skills)}")
    skill_md = (skill_dir / "skill.md").read_text(encoding="utf-8")
    meta = SkillLoader.load_meta(name) or {}
    version = meta.get("version", "")
    # 2026-08-27: 记录技能使用回合（_skill_usage_alarm 状态缓存化，O(1) 判定）
    try:
        setattr(agent, "_last_skill_turn", getattr(agent, "_turn_count", 0))
    except Exception:
        pass
    marker = agent._skill_full_marker(name, version)
    # marker 已在上下文中（assistant 自述承诺或 tool 结果）→ 锚点，避免重复全文
    if any(marker in (m.get("content") or "") for m in agent.messages):
        body = agent._strip_frontmatter(skill_md)
        anchor = agent._skill_anchor(body)
        return ToolResult(
            stdout=f"✅ 已读技能 {name}（锚点: {anchor}），无需重复注入全文。")
    body = agent._strip_frontmatter(skill_md)
    return ToolResult(stdout=f"{marker}\n{body}")


def exec_searchinfo(agent, dirs_paths: list, query_or_keyword: str, top_k: int = 5) -> ToolResult:
    """按指定目录搜索文件内容并返回格式化结果（worker 子进程，可中断）。"""
    if not dirs_paths or not query_or_keyword:
        return ToolResult(stdout="searchinfo: dirs_paths 与 query_or_keyword 均必填。")
    items = searchinfo_items(agent, list(dirs_paths), str(query_or_keyword), top_k,
                             session=getattr(agent, "session", None))
    if not items:
        return ToolResult(stdout="未找到匹配内容。")
    lines = ["在指定目录中找到以下相关片段:"]
    for it in items:
        lines.append(f"  [{it.get('method', '?')}] {it.get('path', '?')} (score={it.get('score', '?')})")
        lines.append(f"      {it.get('snippet', '')}")
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


def exec_addinfo(agent, key: str = '', value='', **_ignored) -> ToolResult:
    """写入/更新本 session 状态信息（session 级 KV，下回合注入头部元信息区）。

    **_ignored 容错：LLM 幻觉传多余参数时不抛 TypeError，走正常校验反馈。
    """
    from codes.session_info import add_info
    sess = getattr(agent, "session", None)
    if not sess:
        return ToolResult(error="addinfo: 无法确定当前 session")
    if not str(key or '').strip():
        return ToolResult(stdout="❌ addinfo: 缺少必填参数 key")
    if value is None:
        return ToolResult(stdout="❌ addinfo: 缺少必填参数 value")
    return ToolResult(stdout=add_info(sess, key, value, by="llm"))


def exec_listinfo(agent, **_ignored) -> ToolResult:
    """列出本 session 当前全部状态信息（活跃条目 + 已删除存档 key 名）。"""
    from codes.session_info import list_info
    sess = getattr(agent, "session", None)
    if not sess:
        return ToolResult(error="listinfo: 无法确定当前 session")
    return ToolResult(stdout=list_info(sess))


def exec_rminfo(agent, key: str = '', **_ignored) -> ToolResult:
    """软删除本 session 的一条状态信息（状态板移除，存储保留存档）。"""
    from codes.session_info import remove_info
    sess = getattr(agent, "session", None)
    if not sess:
        return ToolResult(error="rminfo: 无法确定当前 session")
    if not str(key or '').strip():
        return ToolResult(stdout="❌ rminfo: 缺少必填参数 key")
    return ToolResult(stdout=remove_info(sess, key))


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
                        payload = raw.split(marker, 1)[1].strip()
                        marker_hit["found"] = True
                        marker_hit["raw"] = payload
                        try:
                            marker_hit["data"] = json.loads(payload)
                        except Exception:
                            marker_hit["data"] = None
                        continue
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
        "protected_dir": os.path.realpath(os.path.join(sandbox_cwd, config.DATA_DIR_NAME)),
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

    _IMAGE_MAX_BYTES = 10 * 1024 * 1024
    _IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
    safe_images: list[str] = []
    if images:
        from codes.path_guard import allowed_roots_for_agent, path_in_roots
        roots = allowed_roots_for_agent(agent)
        files_dir = os.path.realpath(os.path.join(agent.cwd, config.DATA_DIR_NAME, "files"))
        for ip in images:
            ip = str(ip or "").strip()
            if not ip:
                continue
            candidate = os.path.realpath(ip if os.path.isabs(ip) else os.path.join(agent.cwd, ip))
            if not (path_in_roots(candidate, roots) or candidate.startswith(files_dir + os.sep)):
                return ToolResult(error=f"image path outside allowed roots: {ip}", exit_code=1)
            if not os.path.isfile(candidate):
                return ToolResult(error=f"image not found: {ip}", exit_code=1)
            ext = os.path.splitext(candidate)[1].lower()
            if ext not in _IMAGE_EXTS:
                return ToolResult(error=f"unsupported image type: {ext or '(none)'}", exit_code=1)
            try:
                if os.path.getsize(candidate) > _IMAGE_MAX_BYTES:
                    return ToolResult(error=f"image too large (max {_IMAGE_MAX_BYTES} bytes): {ip}", exit_code=1)
            except OSError as e:
                return ToolResult(error=f"image stat failed: {e}", exit_code=1)
            safe_images.append(candidate)

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
        "images": safe_images,
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
