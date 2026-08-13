"""config.py — 全局 workdir 配置，控制所有数据目录位置。

用法
----
应用入口处（main.py / web_main.py）在最前面调用：
    from codes import config
    config.set_workdir(args.workdir)

其他模块通过 getter 获取路径，首次调用时自动创建目录。
"""

from __future__ import annotations

import os
from pathlib import Path

DATA_DIR_NAME = ".xkagent"
_WORKDIR: Path | None = None


def set_workdir(path: str | Path | None) -> None:
    """设置工作目录。应在应用启动后、导入其他业务模块前调用。

    Parameters
    ----------
    path : str or Path or None
        工作目录路径。None 表示使用当前目录。
    """
    global _WORKDIR
    if path is None:
        _WORKDIR = None
    else:
        _WORKDIR = Path(path).resolve()


def get_workdir() -> Path:
    """获取工作目录，未设置时回退到 ``os.getcwd()``。"""
    if _WORKDIR is not None:
        return _WORKDIR
    return Path.cwd()


def get_data_dir() -> Path:
    """获取 ``.xkagent`` 数据目录（自动创建）。"""
    d = get_workdir() / DATA_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_log_dir() -> Path:
    """获取日志目录（``.xkagent/logs/``，自动创建）。"""
    d = get_data_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_historys_dir() -> Path:
    """获取会话数据库目录（``.xkagent/historys/``，自动创建）。"""
    d = get_data_dir() / "historys"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_skills_dir() -> Path:
    """获取用户级技能目录（``.xkagent/skills/``，自动创建）。

    用户级技能目录优先于代码目录内的内置 skills/：
    同名技能时用户级覆盖内置。目录不存在时自动创建（与
    get_historys_dir / get_log_dir 的行为一致）。
    """
    d = get_data_dir() / "skills"
    d.mkdir(parents=True, exist_ok=True)
    return d
