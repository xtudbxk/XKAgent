"""provider_config.py — Provider 与 API Key 的外部配置文件解析。

设计意图
--------
将 provider / api_key / model 别名从代码中剥离，改为通过外部配置文件
``.xkagent/provider.config``（或项目根 ``provider.config``）以
``key: value`` 的简单格式配置，同时支持 OpenAI 与 Anthropic 两种调用范式。

查找优先级（fallback 链）:
  1. <workdir>/.xkagent/provider.config      # 当前会话目录，最优先
  2. <项目根>/provider.config                # 代码根目录兜底

环境变量仅参与已配置 Provider 的 API Key 解析。

配置文件格式（INI 风格，``#`` 或 ``;`` 为注释）:
  [default]
  provider: openai            # 全局默认 provider 名（新 session 初始值）
  api_key: sk-xxx             # （可选）全局兜底 api_key

  [openai]
  type: openai                # openai | anthropic；缺省按 model 前缀自动推导
  api_key: sk-xxx             # 显式 key（优先级最高）
  api_key_env: OPENAI_API_KEY # （可选）改从该环境变量读取
  base_url: https://api.openai.com/v1   # （可选）自定义端点/中转
  default_model: gpt-4o
  models.flash: gpt-4o-mini   # 点号键自动嵌套 → {"models": {"flash": ...}}
  models.pro: gpt-4o

  [anthropic]
  type: anthropic
  api_key: sk-ant-xxx
  default_model: claude-sonnet-4

不引入第三方依赖（标准库 re / os / pathlib 实现），保证任何环境可直接 import。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Optional
from codes._log import logger
from codes import config as _config

# ────────────────────────────────────────────────────────────────
# 常量与内置默认
# ────────────────────────────────────────────────────────────────

# 配置文件候选文件名（固定，不做参数化——查找链是产品约定）
_CONFIG_FILENAME = "provider.config"


# 模块级缓存：{配置文件路径: (mtime_ns, 解析结果 dict)}
# 通过 mtime 检测实现"文件改动即热加载"，避免每次 LLM 调用都重新读盘。
_CACHE: dict[Path, tuple[int, dict[str, Any]]] = {}

# 代码根目录（本文件所在目录的上级），用于 fallback 查找链第 2 层
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# reasoning_effort 无白名单：配置写什么就传什么（如 :max/:high/:low），
# 由 API/中转站决定是否支持；本模块只负责拆分为 (纯模型名, effort)。


# ────────────────────────────────────────────────────────────────
# 基础工具
# ────────────────────────────────────────────────────────────────

def mask_key(key: str, keep: int = 4) -> str:
    """脱敏 api_key：只保留前 3 字符与后 keep 字符，中间以 *** 代替。

    用于日志 / CLI 输出，避免明文 key 泄露。空值返回 "(not set)"。
    """
    if not key:
        return "(not set)"
    key = str(key)
    if len(key) <= keep + 4:
        return "***"
    return f"{key[:3]}***{key[-keep:]}"


# ────────────────────────────────────────────────────────────────
# 配置文件查找与解析
# ────────────────────────────────────────────────────────────────

def find_config_file() -> Optional[Path]:
    """按查找链定位配置文件，返回第一个存在的路径；都没有则返回 None。

    查找链:
      1. <workdir>/.xkagent/provider.config      （当前会话目录）
      2. <项目根>/provider.config                （代码根目录 fallback）
    """
    workdir = _config.get_workdir()
    candidates = [
        workdir / _config.DATA_DIR_NAME / _CONFIG_FILENAME,
        _PROJECT_ROOT / _CONFIG_FILENAME,
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


def _parse_config_text(text: str) -> dict[str, dict[str, str]]:
    """解析 INI 风格文本 → {section: {key: value}}。

    规则:
      - 空行与 ``#`` / ``;`` 注释行跳过
      - ``[section]`` 开启新节（节名即 provider 名，``default`` 为全局节）
      - ``key: value`` 或 ``key = value`` 赋值给当前节
      - 顶层（未开节）的 key 归入 ``_global`` 节，作为全局兜底
      - 行尾 ``  # 注释`` 会被剥离（避免误伤 key 本身）
    解析失败（无法识别的行）抛出带行号的 ValueError，方便用户排查配置文件。
    """
    sections: dict[str, dict[str, str]] = {}
    current: Optional[str] = None
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        m = re.match(r"^\[([^\]]+)\]\s*$", line)
        if m:
            current = m.group(1).strip()
            sections.setdefault(current, {})
            continue
        m = re.match(r"^([A-Za-z0-9_.\-]+)\s*[:=]\s*(.*)$", line)
        if not m:
            raise ValueError(
                f"provider.config 第 {lineno} 行无法解析: {raw!r} "
                f"(应为 '[section]' 或 'key: value')"
            )
        key, value = m.group(1).strip(), m.group(2).strip()
        # 剥离行尾注释（值后出现 '  #' 或 ' ;' 时截断）
        for sep in ("  #", " #", "  ;", " ;"):
            idx = value.find(sep)
            if idx >= 0:
                value = value[:idx].strip()
                break
        if current is None:
            current = "_global"
            sections.setdefault(current, {})
        sections[current][key] = value
    return sections


def _expand_dotted(data: dict[str, str]) -> dict[str, Any]:
    """把 ``{'models.flash': 'x'}`` 展开为 ``{'models': {'flash': 'x'}}``。

    用点号键表达嵌套结构，保持配置文件单行扁平、易读。
    """
    result: dict[str, Any] = {}
    for key, value in data.items():
        parts = key.split(".")
        node = result
        for p in parts[:-1]:
            node = node.setdefault(p, {})
            if not isinstance(node, dict):
                raise ValueError(f"配置键冲突: {key!r} 与已存在的非字典值冲突")
        node[parts[-1]] = value
    return result


def _load_config_file() -> dict[str, Any]:
    """读取并解析配置文件（带 mtime 缓存）。

    返回结构: {"default": {...}, "providers": {<name>: {...}}}
    文件不存在时返回空结构，由上层 fallback 到环境变量/内置默认。
    """
    path = find_config_file()
    if path is None:
        return {"default": {}, "providers": {}}

    # mtime 缓存：文件未变化则复用解析结果，避免频繁读盘
    mtime = path.stat().st_mtime_ns
    cached = _CACHE.get(path)
    if cached and cached[0] == mtime:
        return cached[1]

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        # 读失败不阻断启动：降级为无文件，日志由调用方输出
        return {"default": {}, "providers": {}}

    sections = _parse_config_text(text)
    global_cfg = _expand_dotted(sections.get("_global", {}))
    default_cfg = {**global_cfg, **_expand_dotted(sections.get("default", {}))}
    providers: dict[str, dict[str, Any]] = {}

    for name, kv in sections.items():
        if name in ("default", "_global"):
            continue
        if not kv:
            # 空节（有 [节名] 但无任何 key）→ 无效 provider：
            # 跳过并告警，避免 Available 列出不可用项 / 幽灵 provider
            logger.warning(f"provider.config 中 [{name}] 节为空（无 type/api_key 等配置），已跳过")
            continue
        providers[name] = _expand_dotted(kv)
    result = {"default": default_cfg, "providers": providers}
    _CACHE[path] = (mtime, result)
    return result


def reload() -> None:
    """清空配置缓存，强制下次读取重新解析（供测试/外部变更后调用）。"""
    _CACHE.clear()


# ────────────────────────────────────────────────────────────────
# 对外查询接口
# ────────────────────────────────────────────────────────────────

def _infer_type(cfg: dict[str, Any]) -> str:
    """推导调用范式类型。

    显式 ``type`` 字段优先；缺省时按 default_model 前缀判断：
    ``claude-`` / ``anthropic/`` → anthropic，其余视为 openai。
    """
    t = cfg.get("type")
    if t:
        t = str(t).strip().lower()
        if t in ("openai", "anthropic"):
            return t
        raise ValueError(f"未知 type: {t!r}（仅支持 openai / anthropic）")
    model = str(cfg.get("default_model", ""))
    if model.startswith("claude-") or model.startswith("anthropic/"):
        return "anthropic"
    return "openai"


def _resolve_api_key(name: str, cfg: dict[str, Any], default_cfg: dict[str, Any]) -> str:
    """按优先级解析 api_key。

    1. 配置文件中的 api_key（provider 节 > default 全局节）
    2. 配置文件中的 api_key_env → 环境变量
    3. 推导环境变量 {NAME.upper()}_API_KEY
    4. 内置默认的 api_key_env → 环境变量
    5. 空字符串（上层报错）
    """
    # 1. 显式 key
    key = cfg.get("api_key") or default_cfg.get("api_key")
    if key:
        return str(key).strip()

    # 2. api_key_env 指定的环境变量
    env_names: list[str] = []
    env_name = cfg.get("api_key_env") or default_cfg.get("api_key_env")
    if env_name:
        env_names.append(str(env_name))
    # 3. 推导环境变量名（openai → OPENAI_API_KEY）
    env_names.append(f"{name.upper()}_API_KEY")

    for var in env_names:
        value = os.environ.get(var)
        if value:
            return value
    return ""


def get_provider(name: str) -> dict[str, Any]:
    """返回指定 provider 的完整配置（配置文件 + 环境变量）。

    返回字段: type / api_key / api_key_env / default_model / base_url / models
    无配置文件或未知 provider 时抛出带引导的 ValueError。
    """
    data = _load_config_file()
    file_cfg = data["providers"].get(name)
    default_cfg = data["default"]

    if file_cfg is None:
        available = sorted(data["providers"])
        hint = (
            f"Unknown provider \x27{name}\x27. Available: {available}. "
            f"请检查 provider.config 文件（.xkagent/provider.config "
            f"或项目根 provider.config）。"
        )
        # 增强诊断：默认 provider 未设置 / 如何新增 provider 的引导
        if not data["default"].get("provider"):
            hint += " 提示：当前 [default].provider 未设置，新 session 将无默认 provider。"
        if available:
            hint += f" 如需使用 \x27{name}\x27，请在配置文件中添加 [{name}] 节（type / api_key / base_url / default_model）。"
        raise ValueError(hint)

    # 配置文件为唯一来源（不再内置默认）
    merged: dict[str, Any] = dict(file_cfg)

    # 补全必需字段
    merged["type"] = _infer_type(merged)
    merged["api_key"] = _resolve_api_key(name, merged, default_cfg)
    merged.setdefault("api_key_env", "")
    merged.setdefault("default_model", "")
    merged.setdefault("base_url", "")
    merged.setdefault("models", {})
    return merged

def get_default_provider() -> str:
    """返回默认 provider 名：配置文件 [default].provider。

    未配置时返回空字符串（表示'未选择'，由调用方引导用户配置）。
    """
    data = _load_config_file()
    name = data["default"].get("provider")
    return str(name).strip() if name else ""


def get_default_model() -> str:
    """返回默认模型名：配置文件 [default].model。

    未配置时返回空字符串（表示'跟随 provider 的 default_model'）。
    """
    data = _load_config_file()
    model = data["default"].get("model")
    return str(model).strip() if model else ""

def list_providers() -> list[str]:
    """返回配置文件中的全部 provider 名（按出现顺序）。"""
    data = _load_config_file()
    return list(data["providers"].keys())

def build_model_aliases(provider: str | None = None) -> dict[str, str]:
    """返回指定 provider 的模型别名表；provider 缺省时用默认 provider。

    别名与 provider 绑定：'flash' 在 openai 下是 gpt-4o-mini、
    在 anthropic 下是 claude-haiku-4。全局平铺会互相覆盖，故按 provider 隔离。
    """
    name = provider or get_default_provider()
    if not name:
        return {}
    cfg = get_provider(name)
    return {str(k): str(v) for k, v in (cfg.get("models") or {}).items()}


def split_model_effort(raw: str) -> tuple[str, str | None]:
    """拆分 \"模型名:effort\" 语法 → (纯模型名, effort 或 None)。

    - 无冒号 → (原文, None)，向后兼容旧配置
    - 冒号后非空内容 → 拆分为模型名与 effort；effort 不在本模块做白名单限制，
      由具体 API/中转站决定是否支持。
    """
    raw = str(raw).strip()
    if not raw:
        return raw, None
    if ":" in raw:
        name, _, suffix = raw.partition(":")
        # 无白名单：后缀原样透传（写啥传啥），空后缀视为无 effort
        return name.strip(), (suffix.strip() or None)
    return raw, None


def get_model_effort(provider: str, model: str) -> str | None:
    """查询指定 provider 下某模型配置的 reasoning_effort（无则 None）。

    在 default_model 与全部别名值中找纯模型名匹配项，返回其后缀 effort。
    模型/别名带后缀时（如 gpt-5.4:high），LLM 调用前由本函数实时取回。
    """
    model = str(model).strip()
    if not model or not provider:
        return None
    # 传入 model 可能带 :effort 后缀（旧 session 残留 gpt-5.4:max），取纯名匹配
    model = split_model_effort(model)[0]
    try:
        cfg = get_provider(provider)
    except ValueError:
        return None
    candidates = [str(cfg.get("default_model") or "")]
    candidates += [str(v) for v in (cfg.get("models") or {}).values()]
    for raw in candidates:
        m, effort = split_model_effort(raw)
        if m == model:
            if effort:
                return effort
            # 匹配但该配置无 effort：继续找其他候选
            # （如 default_model 无后缀但别名值带后缀，取别名上的 effort）
    return None


def find_provider_by_model(model: str, cur_prov: str | None = None) -> str | None:
    """根据完整模型名反查所属 provider。

    匹配优先级（冲突消解）:
      1. default_model 精确匹配的 provider（多个时取第一个）
      2. 别名表值精确匹配的 provider（多个时取第一个）
    若 ``cur_prov`` 自身也匹配，优先返回 ``cur_prov``（用户已在目标 provider，
    避免无意义切换）。

    Returns
    -------
    匹配的 provider 名；无匹配返回 None。
    """
    # 输入可能带 :effort 后缀（gpt-5.4:max），取纯名参与匹配
    model = split_model_effort(str(model).strip())[0]
    if not model:
        return None
    names = list_providers()
    if not names:
        return None
    # 收集各 provider 的 default_model 与别名表（分两档记录，便于优先级）
    dm_match: list[str] = []   # default_model 精确匹配
    al_match: list[str] = []   # 别名值精确匹配
    for name in names:
        cfg = get_provider(name)
        # 配置值可能带 :effort 后缀（gpt-5.4:high），比较时取纯模型名
        if split_model_effort(str(cfg.get("default_model") or ""))[0] == model:
            dm_match.append(name)
        for v in (cfg.get("models") or {}).values():
            if split_model_effort(str(v))[0] == model:
                al_match.append(name)
                break
    # 优先级：cur_prov 优先 → default_model 匹配 → 别名值匹配
    if cur_prov in dm_match:
        return cur_prov
    if dm_match:
        return dm_match[0]
    if cur_prov in al_match:
        return cur_prov
    if al_match:
        return al_match[0]
    return None


def resolve_model_target(arg: str, cur_prov: str | None = None) -> tuple[str, str]:
    """解析 /model 命令参数，返回 (provider, model) 二元组。

    与旧 ``resolve_model_arg`` 的区别：返回值携带 provider，调用方据此
    联动 ``set_provider`` + ``set_model``，实现"输入模型名即自动切换 provider"。

    解析优先级:
      1. ``provider.alias`` 限定（如 'openai.flash'）→ (openai, 展开后模型)
      2. ``provider.model`` 限定（如 'openai.gpt-4o'）→ (openai, gpt-4o)
         （rest 匹配该 provider 的 default_model 或别名值，均视为完整模型名）
      3. 含点完整模型名反查（如 'gpt-5.4'、'claude-sonnet-4'，前缀非 provider）
         → (所属 provider, 原文)
      4. 当前 provider 别名（如 'flash'）→ (cur_prov, 展开后模型)
      5. 无点完整模型名反查 → (所属 provider, 原文)；反查无果 → (cur_prov, 原文) 兜底

    解析失败（未知 provider 前缀 / 未知别名 / 反查无果的含点输入）抛 ValueError。
    """
    arg = arg.strip()
    if not arg:
        raise ValueError("model 参数不能为空")

    # 输入可能带 :effort 后缀（/model gpt-4o:high）——取纯模型名参与解析，
    # effort 由调用方（repl/web）另行提取并作为临时覆盖传给 set_model。
    arg = split_model_effort(arg)[0]

    providers = list_providers()

    # 1/2. provider 限定语法：prefix 是已知 provider
    if "." in arg:
        prefix, _, rest = arg.partition(".")
        if prefix in providers:
            aliases = build_model_aliases(prefix)
            if rest in aliases:
                # 别名值可能带 :effort 后缀（gpt-4o-mini:low）→ 返回纯模型名
                return prefix, split_model_effort(aliases[rest])[0]
            # provider.完整模型名：rest 匹配该 provider 的 default_model 或别名值
            # （配置值可能带 :effort 后缀，比较时取纯模型名）
            cfg = get_provider(prefix)
            if split_model_effort(str(cfg.get("default_model") or ""))[0] == rest:
                return prefix, rest
            for _v in (cfg.get("models") or {}).values():
                if split_model_effort(str(_v))[0] == rest:
                    return prefix, rest
            raise ValueError(
                f"Provider '{prefix}' 没有别名/模型 '{rest}'。可用别名: "
                f"{', '.join(aliases) or '(无)'}；default_model: "
                f"{cfg.get('default_model') or '(未配置)'}"
            )
        # 3. 含点但前缀不是 provider：整体反查（模型名本身可能含点，如 gpt-5.4）
        found = find_provider_by_model(arg, cur_prov)
        if found:
            return found, arg
        raise ValueError(
            f"'{arg}' 无法解析：'{prefix}' 不是已知 provider"
            f"（可用: {', '.join(providers) or '(无)'}），"
            f"且没有 provider 的 default_model/别名匹配 '{arg}'。"
        )

    # 4. 当前 provider 别名
    name = cur_prov or get_default_provider()
    aliases = build_model_aliases(name) if name else {}
    if arg in aliases:
        return name, split_model_effort(aliases[arg])[0]

    # 5. 无点完整模型名反查 → 兜底
    found = find_provider_by_model(arg, cur_prov)
    if found:
        return found, arg
    return name, arg


def resolve_model_arg(arg: str, provider: str | None = None) -> str:
    """兼容包装：仅返回模型名（旧接口，repl/web 已改用 resolve_model_target）。

    解析失败时抛 ValueError（行为与旧版一致），保证其他调用方不受影响。
    """
    _, model = resolve_model_target(arg, provider)
    return model


def describe_provider(name: str) -> str:
    """格式化单个 provider 的概要：默认模型 + 别名映射。"""
    cfg = get_provider(name)
    lines = [f"  {name} (type={cfg.get('type', '?')})"]
    dm = cfg.get("default_model") or "(未配置)"
    lines.append(f"    default_model: {dm}")
    models = cfg.get("models") or {}
    if models:
        alias_str = ", ".join(f"{k}={v}" for k, v in models.items())
        lines.append(f"    aliases: {alias_str}")
    return "\n".join(lines)


def format_providers() -> str:
    """格式化全部 provider 的概要（供 /model 无参显示）。"""
    names = list_providers()
    if not names:
        return "  (无 provider 配置，请创建 provider.config)"
    return "\n".join(describe_provider(n) for n in names)
