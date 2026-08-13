"""
_log.py — 标准库 logging 全局日志配置（由 loguru 迁移）

为整个项目提供统一的日志配置，自动按启动时间创建日志文件。
日志文件写入 ``config.get_log_dir()``（即 workdir/.xkagent/logs/）。

使用方式:
    from codes._log import logger
    logger.info("消息")
    logger.error("异常信息", exc_info=True)

日志文件: <log_dir>/<启动时间>.log  (如 2025-07-25_02-10-04.log)

迁移说明（loguru -> logging）:
  - 仅用 logging 顶层组件（FileHandler/StreamHandler）；
    禁用 logging.handlers —— 其顶层 import pickle，被 pythonrt 受限沙箱拦截。
  - rotation="100 MB" / retention="7 days" 由轻量 _SizeRotatingFileHandler
    近似实现（maxBytes=100MB, backupCount=7），不依赖第三方库。
  - enqueue(异步) 降级为同步写（logging 自带线程锁，线程安全）；
    backtrace/diagnose 由 logger.exception 的 traceback 近似；
    colorize 降级为纯文本。
"""

import sys
import logging
from datetime import datetime
from pathlib import Path

from codes import config as _config

# ── 日志格式（%-style，兼容标准库 Formatter） ──
# 时间 | 级别 | 模块:函数:行号 | 消息
LOG_FORMAT = (
    "%(asctime)s.%(msecs)03d | "
    "%(levelname)-8s | "
    "%(module)s:%(funcName)s:%(lineno)d | "
    "%(message)s"
)
_DATE_FMT = "%Y-%m-%d %H:%M:%S"


class _SizeRotatingFileHandler(logging.FileHandler):
    """轻量大小轮转 handler。

    设计意图: 复刻 loguru rotation="100 MB" 语义，但规避 logging.handlers
    （其顶层 import pickle 在 pythonrt 受限沙箱中被禁）。
    达到 max_bytes 时滚动 baseFilename -> baseFilename.1 ... .N。
    """

    def __init__(self, filename, max_bytes=100 * 1024 * 1024,
                 backup_count=7, encoding="utf-8"):
        self._max_bytes = max_bytes
        self._backup_count = backup_count
        super().__init__(filename, encoding=encoding)

    def emit(self, record):
        # 写入前检查大小，超过阈值先轮转
        if self.stream and self.stream.tell() >= self._max_bytes:
            self._rotate()
        super().emit(record)

    def _rotate(self):
        self.close()
        base = self.baseFilename
        for i in range(self._backup_count - 1, 0, -1):
            src, dst = f"{base}.{i}", f"{base}.{i + 1}"
            if Path(src).exists():
                Path(src).rename(dst)
        if Path(base).exists():
            Path(base).rename(f"{base}.1")
        self.stream = self._open()


# ── 全局 logger（名称 "codes"，防重复配置） ──
logger = logging.getLogger("codes")
logger.setLevel(logging.DEBUG)
# 先初始化，避免 logger 已被其他模块配置时该名称未定义。
STDERR_HANDLER_ID = next(
    (h for h in logger.handlers if isinstance(h, logging.StreamHandler)
     and not isinstance(h, logging.FileHandler)),
    None,
)

if not logger.handlers:  # 幂等：重复导入/多线程不重复挂 handler
    # ── 日志目录：通过 config 获取（基于 workdir） ──
    _LOG_DIR = _config.get_log_dir()

    # ── 日志文件名：人类可读的启动时间 ──
    _start_time_str = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    LOG_FILE = _LOG_DIR / f"{_start_time_str}.log"

    _formatter = logging.Formatter(LOG_FORMAT, datefmt=_DATE_FMT)

    # ── 文件 Handler（DEBUG+，大小轮转近似 rotation/retention） ──
    _file_handler = _SizeRotatingFileHandler(str(LOG_FILE))
    _file_handler.setLevel(logging.DEBUG)
    _file_handler.setFormatter(_formatter)
    logger.addHandler(_file_handler)

    # ── 控制台 Handler（INFO+，终端实时可见） ──
    STDERR_HANDLER_ID = logging.StreamHandler(sys.stderr)
    STDERR_HANDLER_ID.setLevel(logging.INFO)
    STDERR_HANDLER_ID.setFormatter(_formatter)
    logger.addHandler(STDERR_HANDLER_ID)


def set_stderr_level(level: str | int) -> None:
    """设置 stderr handler 最低级别（兼容 loguru 的字符串级别名，如 "ERROR"）。"""
    if isinstance(level, str):
        level = getattr(logging, level.upper())
    if STDERR_HANDLER_ID is not None:
        STDERR_HANDLER_ID.setLevel(level)


def get_log_file() -> str:
    """返回当前进程对应的日志文件绝对路径（/logfile 命令用）。

    设计考虑: LOG_FILE 在模块导入时按启动时间生成，与本次进程一一对应；
    /logfile 直接引用它，保证"查看的就是本次运行写入的那个文件"。
    """
    return str(LOG_FILE)


def tail_log_file(n: int = 60) -> str:
    """读取当前日志文件尾部 n 行，供 /logfile 命令展示（避免整读大文件）。

    参数:
        n: 读取行数，默认 60。超出文件行数时返回全部。
    返回:
        字符串（行间用换行连接）；读取失败返回错误提示而非抛异常。
    """
    try:
        lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-n:])
    except Exception as e:
        return f"<读取日志失败: {e}>"


# ── 导出（保持原接口兼容） ──
__all__ = ["logger", "STDERR_HANDLER_ID", "LOG_FILE", "LOG_FORMAT",
           "set_stderr_level", "get_log_file", "tail_log_file"]
