from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from pathlib import Path

# 内置技能目录：随代码走的默认技能（项目根/skills）
BUILTIN_SKILLS_DIR = str(Path(__file__).resolve().parent.parent / "skills")
# 兼容别名（语义 = 内置目录；新代码请用 _skill_dirs()）
SKILLS_DIR = BUILTIN_SKILLS_DIR
# frontmatter 解析缓存：{skill.md 路径: (mtime_ns, size, 解析结果)}
# 同一消息内 searchskill/list_skills 会重复解析同一 skill.md（日志可见 3-6 次），
# 用 mtime+size 作为失效键消除重复解析，技能文件改动后自动失效（热加载）。
_FM_CACHE: dict[Path, tuple[int, int, dict]] = {}
_FM_CACHE_LOCK = threading.Lock()


def _skill_dirs() -> list[str]:
    """返回技能目录优先级列表（用户级优先，内置级兜底）。

    用户级目录为 $workdir/.xkagent/skills，
    同名技能时用户级覆盖内置级；用户级目录不存在时仅返回内置级，
    保持与改造前完全一致的行为。

    只读语义：直接拼接路径 + is_dir() 判断，不调用 config.get_skills_dir()
    （后者会 mkdir 创建目录——搜索/列举是只读操作，不应有写副作用）。
    """
    from codes import config
    dirs: list[str] = []
    user_dir = str(config.get_workdir() / config.DATA_DIR_NAME / "skills")
    if Path(user_dir).is_dir():
        dirs.append(user_dir)
    dirs.append(BUILTIN_SKILLS_DIR)
    return dirs


@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict
    execute: callable

    def to_openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolResult:
    stdout: str = ""
    stderr: str = ""
    error: str | None = None
    exit_code: int = 0
    stop_turn: bool = False  # True → 工具执行成功后立即结束本轮（回到等待用户输入）


@dataclass
class Skill:
    name: str
    description: str
    prompt: str


class SkillLoader:

    @classmethod
    def list_skills(cls) -> list[str]:
        """列出全部可用技能名（用户级 + 内置级，去重合并）。

        用户级目录优先：同名技能只出现一次（用户级覆盖内置级）。
        """
        names: set[str] = set()
        for base in _skill_dirs():
            p = Path(base)
            logger.debug(f"扫描技能目录: {p}")
            if p.is_dir():
                names.update(
                    d.name for d in p.iterdir()
                    if d.is_dir() and (d / "skill.md").exists()
                )
        return sorted(names)

    @classmethod
    def _find_skill_dir(cls, name: str) -> Path | None:
        """按优先级返回技能所在目录（用户级优先），None=不存在。"""
        for base in _skill_dirs():
            p = Path(base) / name
            if (p / "skill.md").exists():
                return p
        return None

    # ── Frontmatter parser ────────────────────────────────────

    @staticmethod
    def _parse_frontmatter(content: str) -> dict:
        logger.debug(f"解析 frontmatter: {len(content)} chars")
        """Minimal YAML frontmatter parser.
        Handles scalars, lists (with `  - ` prefix), and nested dicts.
        """
        if not content.startswith("---"):
            return {}
        parts = content.split("---", 2)
        if len(parts) < 3:
            return {}

        meta = {}
        current_key = None
        current_list_key = None
        current_dict_key = None
        dict_parent_key = None

        for line in parts[1].strip().splitlines():
            stripped = line.strip()

            # Skip empty lines
            if not stripped:
                continue

            # List item: `  - value`
            list_match = re.match(r"^\s+-\s+(.+)$", line)
            if list_match and current_list_key:
                if meta.get(current_list_key) is None:
                    meta[current_list_key] = []
                meta[current_list_key].append(list_match.group(1).strip())
                continue

            # Dict value: `    subkey: value` (indented under a key like `requires:`)
            dict_match = re.match(r"^\s{4,}(.+?):\s*(.*)$", line)
            if dict_match and current_key:
                subk = dict_match.group(1).strip()
                subv = dict_match.group(2).strip()
                if isinstance(meta.get(current_key), dict):
                    meta[current_key][subk] = subv
                else:
                    meta[current_key] = {subk: subv}
                continue

            # Top-level key: `key: value` or `key:`
            top_match = re.match(r"^(\S+?):\s*(.*)$", line)
            if top_match:
                k = top_match.group(1).strip()
                v = top_match.group(2).strip()
                current_key = k
                current_list_key = None

                if v:
                    meta[k] = v
                else:
                    # Could be a list or dict parent; use placeholder
                    meta[k] = None  # placeholder
                    current_list_key = k  # assume list or dict follows
                continue

        return meta

    @classmethod
    def _parse_frontmatter_cached(cls, skill_file: Path) -> dict:
        """带 mtime+size 缓存的 frontmatter 解析（skill.md 未变则复用结果）。"""
        try:
            st = skill_file.stat()
            key = (st.st_mtime_ns, st.st_size)
        except OSError:
            return {}
        with _FM_CACHE_LOCK:
            cached = _FM_CACHE.get(skill_file)
            if cached and cached[0] == key[0] and cached[1] == key[1]:
                return cached[2]
        content = skill_file.read_text(encoding="utf-8")
        meta = cls._parse_frontmatter(content)
        with _FM_CACHE_LOCK:
            _FM_CACHE[skill_file] = (key[0], key[1], meta)
        return meta

    # ── Load meta only (lightweight, no __init__.py import) ────

    @classmethod
    def load_meta(cls, name: str) -> dict | None:
        """只读取 frontmatter 元数据，不加载 prompt body 和 tools。"""
        base = cls._find_skill_dir(name)
        if base is None:
            return None
        skill_file = base / "skill.md"
        if not skill_file.exists():
            return None
        meta = cls._parse_frontmatter_cached(skill_file)
        meta.setdefault("name", name)
        meta.setdefault("description", "")
        meta.setdefault("category", "unknown")
        return meta

    # ── Validate skill format ─────────────────────────────────


    @classmethod
    def validate(cls, name: str) -> list[str]:
        """校验技能定义是否符合规范，返回错误列表（空 = 合法）"""
        errors = []
        base = cls._find_skill_dir(name)
        if base is None:
            return [f"Missing skill.md in '{name}'"]
        skill_file = base / "skill.md"

        if not skill_file.exists():
            return [f"Missing skill.md in '{name}'"]

        frontmatter = cls._parse_frontmatter_cached(skill_file)

        if not frontmatter:
            return [f"No valid YAML frontmatter found in '{name}/skill.md' (must start with '---')"]

        # 必填字段
        required = ["name", "version", "description", "category"]
        for field in required:
            if field not in frontmatter or frontmatter[field] is None:
                errors.append(f"Missing required frontmatter field: '{field}'")

        # name 一致性
        fm_name = frontmatter.get("name")
        if fm_name and fm_name != name:
            errors.append(f"Frontmatter name '{fm_name}' != directory name '{name}'")

        # category 有效性
        valid_categories = {"tool", "workflow"}
        cat = frontmatter.get("category")
        if cat and cat not in valid_categories:
            errors.append(f"Invalid category '{cat}'. Must be one of: {valid_categories}")

        # version 格式（语义化版本）
        ver = frontmatter.get("version")
        if ver and not re.match(r"^\d+\.\d+\.\d+$", str(ver)):
            errors.append(f"Invalid version format '{ver}'. Use semver (e.g. 1.0.0)")


        return errors

    # ── Full load ─────────────────────────────────────────────

    @classmethod
    def load(cls, name: str) -> Skill | None:
        logger.info(f"加载技能文件: {name}")
        base = cls._find_skill_dir(name)
        if base is None:
            return None
        skill_file = base / "skill.md"
        if not skill_file.exists():
            return None

        content = skill_file.read_text(encoding="utf-8")

        frontmatter = cls._parse_frontmatter_cached(skill_file)
        body = content
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                body = parts[2].strip()

        skill_name = frontmatter.get("name", name)
        description = frontmatter.get("description", "")


        return Skill(name=skill_name, description=description, prompt=body)

from codes._log import logger





def getskill(name: str) -> str:
    """获取技能的紧凑描述字符串。

    Args:
        name: 技能名

    Returns:
        "name(description)" 或 "name"（无描述时）
    """
    meta = SkillLoader.load_meta(name)
    if not meta:
        return name
    desc = meta.get("description", "") or ""
    return f"{name}({desc})" if desc else name
