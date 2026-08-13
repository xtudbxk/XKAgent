"""XKAgent — CLI + REPL loop + command routing."""

from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
import time
import tempfile
import atexit
import termios
import tty

from codes.agent import Agent, MODE_CYCLE, COMPACT_MARKER, COMPACT_PROMPT
from codes.manager import AgentManager
from codes.commands import dispatch, CommandContext
from codes._log import logger, set_stderr_level
# Style class defined inline below (no display.py dependency)
from codes.skill import SkillLoader, getskill
from codes.llm import friendly_error_hint
from codes.history import list_sessions, get_conn, get_chat_messages, sync_session, add_session, fork_session, rename_session, delete_session, session_exists, parse_user_prefix
import select
import re
from pathlib import Path


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  History + globals
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

from codes import config as _config

def _get_history_file() -> str:
    """获取 REPL 命令历史文件路径（workdir/.xkagent/history.txt）。"""
    d = _config.get_data_dir()
    return str(d / "history.txt")

_agent_ref = None
_tab_pressed = False
_session_cycle_dir = 0  # Ctrl+N(+1)/Ctrl+P(-1) session 循环切换标志，0=空闲
_restore_term = None  # saved termios for restore on exit





class Style:
    """ANSI terminal colors & styles. No external dependencies."""
    _enabled = os.isatty(sys.stdout.fileno()) if hasattr(sys.stdout, 'fileno') else False

    CYAN    = chr(27) + '[96m' if _enabled else ''
    GREEN   = chr(27) + '[92m' if _enabled else ''
    YELLOW  = chr(27) + '[93m' if _enabled else ''
    RED     = chr(27) + '[91m' if _enabled else ''
    BOLD    = chr(27) + '[1m'  if _enabled else ''
    DIM     = chr(27) + '[2m'  if _enabled else ''
    BLUE    = chr(27) + '[94m' if _enabled else ''
    MAGENTA = chr(27) + '[95m' if _enabled else ''
    RESET   = chr(27) + '[0m'  if _enabled else ''
    WINE_RED= chr(27) + '[38;5;88m' if _enabled else ''
    GRAY    = chr(27) + '[90m'  if _enabled else ''

    HEADER  = BOLD + CYAN
    OK      = BOLD + GREEN
    WARN    = BOLD + YELLOW
    FAIL    = BOLD + RED
    INFO    = BLUE
    MUTED   = DIM

    ICON_OK    = (GREEN + chr(0x2705) + RESET) if _enabled else chr(0x2705)
    ICON_FAIL  = (RED + chr(0x274c) + RESET) if _enabled else chr(0x274c)
    ICON_WARN  = (YELLOW + chr(0x26a0) + chr(0xfe0f) + RESET) if _enabled else chr(0x26a0) + chr(0xfe0f)
    ICON_INFO  = (BLUE + chr(0x2139) + chr(0xfe0f) + RESET) if _enabled else chr(0x2139) + chr(0xfe0f)
    ICON_TOOL  = (CYAN + chr(0x1f527) + RESET) if _enabled else chr(0x1f527)
    ICON_THINK = (GRAY + chr(0x23f3) + RESET) if _enabled else chr(0x23f3)
    ICON_STATS = (MAGENTA + chr(0x1f4ca) + RESET) if _enabled else chr(0x1f4ca)
    ICON_CMD   = (CYAN + chr(0x1f4bb) + RESET) if _enabled else chr(0x1f4bb)
    ICON_INPUT = (GREEN + chr(0x1f4ac) + RESET) if _enabled else chr(0x1f4ac)
    ICON_DESC  = (BLUE + chr(0x1f4dd) + RESET) if _enabled else chr(0x1f4dd)
    ICON_TITLE = (MAGENTA + chr(0x1f3af) + RESET) if _enabled else chr(0x1f3af)
    ICON_FILE  = (YELLOW + chr(0x1f4c1) + RESET) if _enabled else chr(0x1f4c1)
    ICON_GEAR  = (YELLOW + chr(0x2699) + chr(0xfe0f) + RESET) if _enabled else chr(0x2699) + chr(0xfe0f)
    ICON_PAUSE = (YELLOW + chr(0x23f8) + chr(0xfe0f) + RESET) if _enabled else chr(0x23f8) + chr(0xfe0f)

    @classmethod
    def ok(cls, text):
        return cls.OK + text + cls.RESET

    @classmethod
    def fail(cls, text):
        return cls.FAIL + text + cls.RESET

    @classmethod
    def warn(cls, text):
        return cls.WARN + text + cls.RESET

    @classmethod
    def info(cls, text):
        return cls.INFO + text + cls.RESET

    @classmethod
    def muted(cls, text):
        return cls.MUTED + text + cls.RESET

    @classmethod
    def header(cls, text):
        return cls.HEADER + text + cls.RESET

    @classmethod
    def bold(cls, text):
        return cls.BOLD + text + cls.RESET

    @classmethod
    def cmd(cls, text):
        return cls.CYAN + cls.BOLD + text + cls.RESET

    @classmethod
    def input_text(cls, text):
        return cls.GREEN + text + cls.RESET

    @classmethod
    def desc(cls, text):
        return cls.BLUE + text + cls.RESET

    @classmethod
    def title(cls, text):
        return cls.BOLD + cls.MAGENTA + text + cls.RESET

    @classmethod
    def stats(cls, text):
        return cls.DIM + cls.MAGENTA + text + cls.RESET

    @classmethod
    def thinking(cls, text):
        return cls.GRAY + text + cls.RESET

    @classmethod
    def tool(cls, text):
        return cls.CYAN + cls.BOLD + text + cls.RESET

    @classmethod
    def pause(cls, text):
        return cls.YELLOW + cls.BOLD + text + cls.RESET


def _build_prompt_str(mgr) -> str:
    """Build prompt using AgentManager's cached info.

    mgr: AgentManager instance with cached attributes.
    """
    mode_icons = {'plan': '📐', 'build': '🔧', 'build-unsafe': '🔥'}
    mode_colors = {'plan': Style.INFO, 'build': Style.WARN, 'build-unsafe': Style.FAIL}

    session = mgr.focus or '?'
    mode = getattr(mgr, '_focus_mode', 'plan')
    is_obs = getattr(mgr, '_focus_observing', False)
    holder_info = getattr(mgr, '_focus_holder_info', None)

    mode_icon = mode_icons.get(mode, '🔧')
    mode_color = mode_colors.get(mode, Style.WARN)
    skill_indicator = ' 🎯' if getattr(mgr, '_focus_skill_select', True) else ' 🚫'

    # Observing mode tag
    observe_tag = ""
    if is_obs:
        if holder_info and isinstance(holder_info, dict):
            name = holder_info.get("holder", "?")
            observe_tag = f" {Style.WARN}[⏳{name}]{Style.RESET}"
        else:
            observe_tag = f" {Style.WARN}[⏳被占用]{Style.RESET}"

    # 会话名使用独立醒目颜色（MAGENTA+BOLD），与模式标签色区分，方便翻阅历史时定位
    session_color = Style.MAGENTA + Style.BOLD
    prompt = (f"{session_color}{session}{Style.RESET} {mode_icon} {mode_color}[{mode}]{Style.RESET}"
              f"{observe_tag}{skill_indicator} >>> ")
    return prompt


def _lock_tag(session: str) -> str:
    """Return a compact lock suffix for /sessions display.

    用户决策: 本进程自持锁是正常驻留，不加符号；仅当锁被【其他进程】持有
    时显示 " 🔒"，空闲也不加符号。紧凑显示。
    """
    from codes.lock import is_locked as _is_locked
    from codes.lock import is_same_process as _is_same_process
    locked, meta = _is_locked(session)
    if locked and isinstance(meta, dict) and not _is_same_process(meta):
        return " \U0001f512"
    return ""
def _phase_tag(si) -> str:
    """Return a compact phase/in_tool suffix for /sessions display.

    展示 agent 实时状态：tool 执行中（🔧tool）/ LLM 处理中（⏳llm）/ 初始化（⏳starting），
    空闲不加符号（紧凑显示）。
    """
    if si.in_tool:
        return f" {Style.GRAY}🔧tool{Style.RESET}"
    if si.phase == "llm":
        return f" {Style.CYAN}⏳llm{Style.RESET}"
    if si.phase == "starting":
        return f" {Style.DIM}⏳starting{Style.RESET}"
    return ""


def _print_rounds(manager):
    """Print recent conversation rounds from the focused agent."""
    rounds = manager.get_focus_latest_rounds(n=3, timeout=1.0)
    if rounds:
        print(rounds)




def _cycle_session(manager, direction: int) -> bool:
    """在运行中的 session 间按 recency 循环切换（direction=±1，回绕）。

    复用 repl/web 共用的 manager.focus_session 统一入口；仅切换正在
    running 的会话，按 last_active 降序排列，到末尾回绕到开头。
    """
    infos = [s for s in manager.list_sessions() if s.status == "running"]
    infos.sort(key=lambda s: s.last_active, reverse=True)
    if not infos:
        print("  \u2139\ufe0f No running sessions to switch.")
        return False
    names = [s.session for s in infos]
    if len(names) == 1:
        print(f"  \u2139\ufe0f Only one running session: {Style.bold(names[0])}")
        return False
    current = manager.focus
    if current in names:
        idx = (names.index(current) + direction) % len(names)
        target = names[idx]
    else:
        target = names[0] if direction > 0 else names[-1]
    if target == current:
        return False
    info = manager.focus_session(target)  # 统一入口：switch_focus → resume → info
    msg_count = info.get("msg_count", 0) if info else 0
    print(f"  \u2705 Switched to session: {Style.bold(target)} ({msg_count} messages)")
    _print_rounds(manager)
    return True





# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Raw-mode user interaction helper (replaces input() for confirms/prompts)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _ask(prompt: str = "") -> str:
    """Read one line of user input in raw terminal mode.

    Replaces input() calls inside the raw-mode REPL loop. Instead of
    toggling termios back-and-forth (risky and complex), this function
    reads bytes directly from stdin, manually echoes them, and handles:
      - Enter (\n)      →  submit the line
      - Ctrl+C (\x03)   →  raise KeyboardInterrupt (caught by caller)
      - Ctrl+D (\x04)   →  cancel, return ''
      - Backspace (\x7f) →  delete last char (\b \b sequence)

    Args:
        prompt: Optional prompt text to display before reading.

    Returns:
        The input string (stripped of leading/trailing whitespace).
    """
    sys.stdout.write(prompt)
    sys.stdout.flush()
    buf: list[str] = []
    while True:
        try:
            raw = os.read(sys.stdin.fileno(), 1)
        except (EOFError, KeyboardInterrupt):
            return ""
        if not raw:
            return ""
        ch = raw.decode("utf-8", errors="replace")
        if ch == "\n":            # Enter →  submit
            sys.stdout.write("\n")
            sys.stdout.flush()
            break
        if ch == "\r":            # Carriage return (some terminals)
            sys.stdout.write("\n")
            sys.stdout.flush()
            break
        if ch == "\x03":          # Ctrl+C →  cancel
            sys.stdout.write("\n")
            sys.stdout.flush()
            raise KeyboardInterrupt
        if ch == "\x04":          # Ctrl+D →  EOF
            return ""
        if ch == "\x7f":          # Backspace
            if buf:
                buf.pop()
                sys.stdout.write("\b \b")
                sys.stdout.flush()
        elif ch.isprintable() or ch in (" ", "\t"):
            buf.append(ch)
            sys.stdout.write(ch)
            sys.stdout.flush()
    return "".join(buf).strip()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Terminal raw mode — non-blocking input reader
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_CJK_RANGES = [
    (0x2E80, 0x2EFF), (0x3000, 0x303F), (0x3040, 0x309F),
    (0x30A0, 0x30FF), (0x3100, 0x312F), (0x3130, 0x318F),
    (0x3190, 0x31FF), (0x3200, 0x32FF), (0x3300, 0x33FF),
    (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xA000, 0xA4CF),
    (0xAC00, 0xD7AF), (0xF900, 0xFAFF), (0xFE30, 0xFE6F),
    (0xFF01, 0xFF60), (0xFFE0, 0xFFE6),
]

def _char_width(ch: str) -> int:
    code = ord(ch)
    for lo, hi in _CJK_RANGES:
        if lo <= code <= hi:
            return 2
    return 1

def _buf_visible_width(buf: str, end: int = None) -> int:
    if end is None:
        end = len(buf)
    return sum(_char_width(c) for c in buf[:end])



_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')

def _visible_width(s: str) -> int:
    """真实可见宽度：先剥离 ANSI 转义序列（终端零宽）再计算。"""
    return _buf_visible_width(_ANSI_RE.sub('', s))

class _RawReader:
    """Raw terminal input reader — zero deps, UTF-8 safe, non-blocking.

    Replaces readline's input() with termios raw mode + own line editor.
    Main loop calls feed_byte() per byte; reader handles display itself.
    """



    def __init__(self):
        self.active = False
        self._main_prompt = ""
        self._prompt = ""
        self._utf8_pending = b""
        self._utf8_needed = 0
        self._escape_buf = b""
        self._history = []
        self._hist_idx = -1
        self._hist_pending = None
        self._result = None
        self._prev_rows = 1
        self._cursor_row = 0

    # ── Public API ──

    def start(self, prompt: str):
        self.active = True
        self._main_prompt = prompt
        self._prompt = prompt
        self.buffer = ""
        self.cursor = 0
        self._utf8_pending = b""
        self._utf8_needed = 0
        self._escape_buf = b""
        self._hist_idx = -1
        self._hist_pending = None
        self._result = None
        self._prev_rows = 1
        self._cursor_row = 0
        self._redisplay()

    def replace_prompt(self, new_prompt: str):
        if not self.active:
            return
        self._main_prompt = new_prompt
        self._prompt = self._main_prompt
        self._redisplay()

    def collect(self):
        r = self._result
        self._result = None
        return r

    def stop(self):
        self.active = False

    # ── Byte feeder ──

    def feed_byte(self, raw: bytes):
        if not raw:
            return
        byte = raw[0]

        # Handle pending UTF-8 continuation
        if self._utf8_needed > 0:
            self._utf8_pending += raw
            self._utf8_needed -= 1
            if self._utf8_needed == 0:
                ch = self._utf8_pending.decode("utf-8", errors="replace")
                self._insert_char(ch)
            return

        # ESC — start of escape sequence
        if byte == 0x1B:
            self._escape_buf = b"\x1b"
            return

        # In the middle of an escape sequence
        if self._escape_buf:
            self._escape_buf += raw
            if 0x40 <= byte <= 0x7E and byte != 0x5B:
                self._handle_escape(self._escape_buf)
                self._escape_buf = b""
            return

        # Control characters
        if byte < 0x20:
            self._handle_ctrl(byte)
            return

        # Backspace
        if byte == 0x7F:
            self._handle_backspace()
            return

        # UTF-8 multi-byte start (0xC2-0xF4)
        if byte & 0x80:
            if byte & 0xE0 == 0xC0:
                self._utf8_needed = 1
            elif byte & 0xF0 == 0xE0:
                self._utf8_needed = 2
            elif byte & 0xF8 == 0xF0:
                self._utf8_needed = 3
            else:
                return  # invalid start byte
            self._utf8_pending = raw
            return

        # ASCII printable
        self._insert_char(chr(byte))

    # ── Internal handlers ──

    def _handle_ctrl(self, byte: int):
        global _tab_pressed, _session_cycle_dir
        if byte == 0x0A or byte == 0x0D:        # Enter
            self._handle_enter()
        elif byte == 0x01:                        # Ctrl+A
            self.cursor = 0
            self._redisplay()
        elif byte == 0x03:                        # Ctrl+C
            raise KeyboardInterrupt
        elif byte == 0x04:                        # Ctrl+D
            if not self.buffer:
                raise EOFError
            self.buffer = ""
            self.cursor = 0
            self._redisplay()
        elif byte == 0x05:                        # Ctrl+E — open editor
            self._open_editor()
        elif byte == 0x09:                        # Tab
            _tab_pressed = True
        elif byte == 0x0E:                        # Ctrl+N — 下一个 session
            _session_cycle_dir = 1
        elif byte == 0x10:                        # Ctrl+P — 上一个 session
            _session_cycle_dir = -1
        elif byte == 0x15:                        # Ctrl+U
            self.buffer = self.buffer[self.cursor:]
            self.cursor = 0
            self._redisplay()
        elif byte == 0x17:                        # Ctrl+W (delete word)
            self._delete_word()
        elif byte == 0x0C:                        # Ctrl+L (clear screen)
            sys.stdout.write("\033[2J\033[H")
            self._redisplay()

    def _handle_escape(self, seq: bytes):
        if seq == b"\x1b[A":         # ↑
            self._history_up()
        elif seq == b"\x1b[B":       # ↓
            self._history_down()
        elif seq == b"\x1b[C":       # →
            if self.cursor < len(self.buffer):
                self.cursor += 1
                self._redisplay()
        elif seq == b"\x1b[D":       # ←
            if self.cursor > 0:
                self.cursor -= 1
                self._redisplay()
        elif seq in (b"\x1b[H", b"\x1b[1~"):   # Home
            self.cursor = 0
            self._redisplay()
        elif seq in (b"\x1b[F", b"\x1b[4~"):   # End
            self.cursor = len(self.buffer)
            self._redisplay()
        elif seq == b"\x1b[3~":      # Del
            if self.cursor < len(self.buffer):
                self.buffer = self.buffer[:self.cursor] + self.buffer[self.cursor+1:]
                self._redisplay()

    def _handle_enter(self):
        text = self.buffer

        # Backslash continuation: drop \ and insert newline
        if text.endswith("\\") and not text.endswith("\\\\"):
            self.buffer = text[:-1] + "\n"
            self.cursor = len(self.buffer)
            sys.stdout.write("\r\n")
            self._redisplay()
            return

        # Submit
        if text.strip():
            self._add_history(text)
        sys.stdout.write("\r\n")
        self._finish_input(text)

    def _finish_input(self, result: str):
        self.buffer = ""
        self.cursor = 0
        self.active = False
        if result:
            self._result = result

    def _handle_backspace(self):
        if self.cursor == 0:
            return
        self.buffer = self.buffer[:self.cursor-1] + self.buffer[self.cursor:]
        self.cursor -= 1
        self._redisplay()

    def _delete_word(self):
        if self.cursor == 0:
            return
        pos = self.cursor
        while pos > 0 and self.buffer[pos-1] == " ":
            pos -= 1
        while pos > 0 and self.buffer[pos-1] != " ":
            pos -= 1
        self.buffer = self.buffer[:pos] + self.buffer[self.cursor:]
        self.cursor = pos
        self._redisplay()

    def _insert_char(self, ch: str):
        self.buffer = self.buffer[:self.cursor] + ch + self.buffer[self.cursor:]
        self.cursor += 1
        self._redisplay()

    def _open_editor(self):
        """Open $EDITOR with current buffer. Replace buffer on exit."""
        editor = os.environ.get("EDITOR", "vim")
        try:
            subprocess.run([editor, "--version"], capture_output=True)
        except FileNotFoundError:
            return
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        )
        try:
            tmp.write(self.buffer)
            tmp.close()
            self.stop()
            _restore_terminal()
            sys.stdout.write("\r\n")
            sys.stdout.flush()
            subprocess.run([editor, tmp.name])
            with open(tmp.name, "r", encoding="utf-8") as f:
                content = f.read()
            self.buffer = content
            self.cursor = len(content)
            _enter_raw_mode()
            self.active = True
            self._redisplay()
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    # ── Display ──

    def _redisplay(self):
        prompt = self._prompt
        line = prompt + self.buffer
        try:
            cols = os.get_terminal_size().columns
        except OSError:
            cols = 80
        cols = max(cols, 1)

        # ① 剥离 ANSI 后计算真实可见宽度
        total_vis = _visible_width(line)
        new_rows = max(1, (total_vis - 1) // cols + 1) if total_vis > 0 else 1

        # ② 回到内容第一行：上移光标所在行数（而非总行数-1）
        sys.stdout.write("\r")
        if self._cursor_row > 0:
            sys.stdout.write(f"\033[{self._cursor_row}A")
        sys.stdout.write("\033[J")
        sys.stdout.write(line)

        # ③ 更新光标相对行（ANSI 剥离后计算）
        self._cursor_row = _visible_width(prompt + self.buffer[:self.cursor]) // cols
        self._prev_rows = new_rows

        # ④ 光标定位（ANSI 误差在差运算中抵消，逻辑不变）
        prompt_w = _buf_visible_width(prompt)
        cursor_w = prompt_w + _buf_visible_width(self.buffer, self.cursor)
        total_w = prompt_w + _buf_visible_width(self.buffer)
        if cursor_w < total_w:
            sys.stdout.write(f"\033[{total_w - cursor_w}D")
        sys.stdout.flush()
    # ── History ──

    def load_history(self):
        try:
            with open(_get_history_file(), "r", encoding="utf-8") as f:
                self._history = [line.rstrip("\n") for line in f if line.strip()]
        except FileNotFoundError:
            self._history = []
        except UnicodeDecodeError:
            try:
                with open(_get_history_file(), "r", encoding="latin-1") as f:
                    raw = f.read()
                raw_bytes = raw.encode("latin-1")
                cleaned = raw_bytes.decode("utf-8", errors="replace")
                self._history = [l.rstrip("\n") for l in cleaned.split("\n") if l.strip()]
                try:
                    with open(_get_history_file(), "w", encoding="utf-8") as f:
                        f.write(cleaned)
                except OSError:
                    pass
            except OSError:
                self._history = []

    def save_history(self):
        os.makedirs(os.path.dirname(_get_history_file()), exist_ok=True)
        tmp = _get_history_file() + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                for line in self._history:
                    f.write(line + "\n")
            os.replace(tmp, _get_history_file())
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass

    def _add_history(self, line: str):
        if line and (not self._history or line != self._history[-1]):
            self._history.append(line)

    def _history_up(self):
        if not self._history:
            return
        if self._hist_idx == -1:
            self._hist_pending = self.buffer
            self._hist_idx = len(self._history) - 1
        elif self._hist_idx > 0:
            self._hist_idx -= 1
        self.buffer = self._history[self._hist_idx]
        self.cursor = len(self.buffer)
        self._redisplay()

    def _history_down(self):
        if self._hist_idx == -1:
            return
        self._hist_idx += 1
        if self._hist_idx >= len(self._history):
            self._hist_idx = -1
            self.buffer = self._hist_pending or ""
        else:
            self.buffer = self._history[self._hist_idx]
        self.cursor = len(self.buffer)
        self._redisplay()


# ── Terminal raw mode setup ──

def _enter_raw_mode():
    global _restore_term
    fd = sys.stdin.fileno()
    _restore_term = termios.tcgetattr(fd)
    new = termios.tcgetattr(fd)
    new[tty.IFLAG] &= ~(termios.BRKINT | termios.ICRNL | termios.INPCK | termios.ISTRIP | termios.IXON)
    new[tty.CFLAG] &= ~(termios.CSIZE | termios.PARENB)
    new[tty.CFLAG] |= termios.CS8
    new[tty.LFLAG] &= ~(termios.ECHO | termios.ICANON | termios.IEXTEN | termios.ISIG)
    new[tty.CC][termios.VMIN] = 1
    new[tty.CC][termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSADRAIN, new)

def _restore_terminal():
    global _restore_term
    if _restore_term is not None:
        try:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _restore_term)
        except Exception:
            pass
        _restore_term = None


def _record_repl_command(manager, cmd_text, result, *, kind="slash", exit_code=None):
    """repl 端命令历史落库（对齐 web 端 _record_web_cmd_history）。

    连接解析：取 focus agent 的 db（manager.focus.db）；无 focus 或写失败时
    静默降级为日志（观察者只读连接 / 数据库锁竞争不阻断交互）。
    """
    try:
        agent = manager.focus
        if agent is None:
            return
        from codes.history import add_command
        add_command(agent.db, cmd_text, result, kind=kind, exit_code=exit_code)
    except Exception:
        logger.warning(f"记录 command 历史失败: {cmd_text!r}", exc_info=True)


def _repl_cmd_context(manager, reader) -> CommandContext:
    """构造 REPL 侧命令上下文（注入 stdin 交互能力给 codes.commands）。"""
    ctx = CommandContext()

    def _confirm(prompt: str) -> bool:
        """stdin 交互确认（y/N）。"""
        try:
            ans = _ask(f"  ❓ {prompt} (y/N): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return ans in ("y", "yes")

    def _switch_hook(name: str) -> None:
        """切换会话后：展示 rounds + 刷新 prompt（T7: session 名 + 占用状态）。"""
        _print_rounds(manager)
        if reader.active:
            reader.replace_prompt(_build_prompt_str(manager))

    def _pick_session(matches, current: str, name: str):
        """stdin 多选（/session 模糊匹配多个时）。"""
        print(f"\n  🔍 Multiple sessions match '{name}':")
        for i, s in enumerate(matches, 1):
            marker = " ← current" if s == current else ""
            print(f"    {i}. {s}{marker}")
        try:
            choice = _ask(f"  💬 Enter number (1-{len(matches)}), or press Enter: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if choice.isdigit():
            ix = int(choice) - 1
            if 0 <= ix < len(matches):
                return matches[ix]
        return None

    ctx.confirm_handler = _confirm
    ctx.save_history = reader.save_history
    ctx.get_session = lambda: manager.focus
    ctx.switch_session_hook = _switch_hook
    ctx.pick_session = _pick_session
    ctx.format_session_line = (
        lambda si, mark: f"  {mark} {si.session}  [{si.status}]{_phase_tag(si)}{_lock_tag(si.session)}"
    )
    ctx.format_other_session = lambda s: f"    ○ {s}{_lock_tag(s)}"
    ctx.help_extra = (
        "Shortcuts:\n"
        "  !<command>        Execute as bash command (shows exit code)\n"
        "  @<path>           Inline file content into prompt\n"
        "\n"
        "Multi-line:\n"
        "  \\ at line end     Continue on next line (drop the \\)\n"
        "  TAB               Toggle mode at any time (input preserved)\n"
        "  Ctrl+N / Ctrl+P  Cycle through running sessions (input preserved)"
    )
    return ctx



# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Main entry
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def run_repl(args):
    global _tab_pressed, _session_cycle_dir
    _session_cycle_dir = 0  # 复位 session 循环切换标志

    # REPL 模式下 stderr 日志只显示 ERROR+，避免日志混入终端
    set_stderr_level("ERROR")
    session = args.session


    if args.resume:
        if not list_sessions():
            print("No sessions to resume.", file=sys.stderr)
            sys.exit(1)
        session = session or AgentManager.resolve_session()
        resume_flag = True
    elif not session:
        # 统一解析策略（manager.resolve_session：最新 session > 新建时间戳）
        session = AgentManager.resolve_session()
        resume_flag = session in list_sessions()
    else:
        resume_flag = False

    logger.info(f"REPL 启动: session={session}, resume={resume_flag}, mode={'prompt' if args.prompt else 'interactive'}")

    # ── Initialize Manager + start agent thread ──
    manager = AgentManager()
    global _agent_ref
    _agent_ref = manager
    logger.info(f"REPL main(): workdir={args.workdir}")
    mgr_start_ok = manager.start_agent(session)
    if not mgr_start_ok:
        print(f"  \u274c Failed to start agent for session '{session}'", file=sys.stderr)
        sys.exit(1)
    manager.switch_focus(session)

    # Cache for prompt building
    manager._focus_mode = "plan"
    manager._focus_observing = False
    manager._focus_holder_info = None

    # ── Resume if requested ──
    if resume_flag:
        manager.send_command("resume")
        info = manager.get_focus_info(timeout=3.0)
        if info:
            if info.get("is_observing"):
                holder = info.get("holder_info")
                holder_name = holder.get("holder", "?") if isinstance(holder, dict) else "?"
                print(f"  ⚠️ Session {Style.bold(session)} 被 {holder_name} 占用（观察者只读），未恢复")
            else:
                msg_count = info.get("msg_count", 0)
                print(f"  ➡️ Auto-resumed session: {Style.bold(session)} ({msg_count} messages)")
            manager._focus_mode = info.get("mode", "plan")
            manager._focus_observing = info.get("is_observing", False)
            manager._focus_holder_info = info.get("holder_info", None)
            if not info.get("is_observing"):
                rounds = manager.get_focus_latest_rounds(n=3, timeout=2.0)
                if rounds:
                    print(rounds)
        else:
            print(f"  \u27a1\ufe0f Session: {Style.bold(session)}")

    if args.prompt:
        manager.send_input(args.prompt)
        # Read all output
        while True:
            event = manager.read_output(timeout=1.0)
            if event is None:
                break
            t = event.get("type", "")
            if t == "_turn_end":
                break
            rendered = _handle_event(event, manager)
            if rendered:
                print(rendered, end="", flush=True)
        print()
        return

    print("  XKAgent — Code LLM Agent (multi-session)")
    print("  /help for commands  |  \\ to continue line  |  TAB toggle mode  |  Ctrl+N/P switch session")
    print()

        # ── 首次启动引导：无任何会话且未配置 provider.config 时给出可操作提示 ──
    try:
        from codes import provider_config
        _first_run = not list_sessions() and provider_config.find_config_file() is None
    except Exception:
        _first_run = False
    if _first_run:
        print("  ── 快速开始 ──")
        print("  📌 首次使用：请先配置 provider.config（.xkagent/provider.config 或项目根 provider.config）")
        print("     · /model           查看/切换模型")
        print("     · /help            查看全部命令")
        print("     · !<命令>          直接执行 shell")
        print()

# ── Non-tty mode: simple line-by-line input ──
    if not sys.stdin.isatty():
        for line in sys.stdin:
            line = line.strip()
            if line.startswith("!"):
                cmd = line[1:].strip()
                if not cmd:
                    continue
                r = subprocess.run(
                    cmd, shell=True,
                    capture_output=True, text=True, errors="replace",
                )
                if r.stdout:
                    sys.stdout.write(r.stdout)
                if r.stderr:
                    sys.stderr.write(r.stderr)
                print(f"  \u2514 exit: {r.returncode}")
            elif line.lower() in {"exit", "quit", "q"}:
                break
            else:
                if line.strip().lower() == "/compact":
                    line = COMPACT_MARKER + COMPACT_PROMPT
                manager.send_input(line)
                while True:
                    event = manager.read_output(timeout=1.0)
                    if event is None:
                        break
                    if event.get("type") == "_turn_end":
                        break
                    rendered = _handle_event(event, manager)
                    if rendered:
                        print(rendered, end="", flush=True)
        return

    # ── Raw terminal mode ──
    _enter_raw_mode()
    atexit.register(_restore_terminal)
    reader = _RawReader()
    reader.load_history()

    try:
        reader.start(_build_prompt_str(manager))
        while True:
            # Read any pending output from focused agent
            _drain_output(manager, timeout=0 if reader.active else 0.1)

            # ── Auto-restart crashed agent ──
            if manager.focus:
                crashed = [s for s in manager.list_sessions()
                           if s.session == manager.focus and s.status == "crashed"]
                if crashed:
                    print(f"\r\n  \u26a0\ufe0f Agent crashed. Restarting...", flush=True)
                    logger.warning(f"Agent 崩溃后重启: {manager.focus}")
                    manager.switch_focus(manager.focus)
                    print(f"  \u2705 Agent restarted.", flush=True)
                    if reader.active:
                        reader.stop()

            # ── Handle Tab (swap prompt without losing input) ──
            if _tab_pressed and reader.active:
                _tab_pressed = False
                current_mode = getattr(manager, '_focus_mode', 'plan')
                new_mode = MODE_CYCLE.get(current_mode, 'plan')
                if manager.send_command("set_mode", {"mode": new_mode}):
                    manager._focus_mode = new_mode
                else:
                    # 命令未送达（无聚焦 agent / agent 未运行）：回读真实 mode，防 UI 假象
                    info = manager.get_focus_info(timeout=2.0)
                    if info:
                        manager._focus_mode = info.get("mode", manager._focus_mode)
                    print("  ⚠️ 模式切换失败：无聚焦 agent 或 agent 未运行", flush=True)
                reader.replace_prompt(_build_prompt_str(manager))

            # ── Handle Ctrl+N / Ctrl+P (cycle sessions) ──
            if _session_cycle_dir and reader.active:
                direction, _session_cycle_dir = _session_cycle_dir, 0  # 消费标志，防连发
                if _cycle_session(manager, direction):
                    reader.replace_prompt(_build_prompt_str(manager))

            # ── Start reader if idle ──
            if not reader.active:
                reader.start(_build_prompt_str(manager))

            # ── Read one byte at a time (non-blocking) ──
            try:
                readable, _, _ = select.select([sys.stdin], [], [], 0.2)
                if readable:
                    byte = os.read(sys.stdin.fileno(), 1)
                    if not byte:
                        break
                    reader.feed_byte(byte)
            except EOFError:
                print("\r\n")
                break
            except KeyboardInterrupt:
                print("\r\n^C")
                logger.info(f"[DIAG] Ctrl+C pressed at {__import__('time').time():.3f}")
                # ── 向 agent 线程转发中断：完全停止当前 turn（T7）──
                if manager.focus:
                    manager.request_interrupt(manager.focus)
                    print("  \u23f9\ufe0f 正在停止 agent...", end="", flush=True)
                    _drain_until_turn_end(manager, total_timeout=3.0)  # 排空残留事件
                    print("\r" + " " * 40 + "\r  \u2705 已停止", flush=True)
                if reader.active:
                    reader.stop()
                    reader.start(_build_prompt_str(manager))
                continue

            # ── Process complete input when available ──
            line = reader.collect()
            if line is None:
                continue

            line = line.strip()
            if not line:
                continue

            # ── bare "exit" / "quit" ──
            if line.strip().lower() in {"exit", "quit", "q"}:
                break

            # ── /compact: 走普通消息通路（run_stream 流式压缩，替代旧同步命令）──
            if line.strip().lower() == "/compact":
                line = COMPACT_MARKER + COMPACT_PROMPT

            # ── !command: bash shortcut ──
            if line.startswith("!"):
                cmd = line[1:].strip()
                if not cmd:
                    continue
                _bang_parts = []
                try:
                    r = subprocess.run(
                        cmd, shell=True,
                        capture_output=True, text=True, errors="replace",
                    )
                    if r.stdout:
                        sys.stdout.write(r.stdout)
                        _bang_parts.append(r.stdout.rstrip())
                    if r.stderr:
                        sys.stderr.write(r.stderr)
                        _bang_parts.append("[stderr]\n" + r.stderr.rstrip())
                    print(f"  \u2514 exit: {r.returncode}")
                    _bang_parts.append(f"\u2514 exit: {r.returncode}")
                    _record_repl_command(manager, line, "\n".join(_bang_parts),
                                         kind="bang", exit_code=r.returncode)
                except KeyboardInterrupt:
                    print("  Interrupted")
                except Exception as e:
                    print(f"  Error: {e}", file=sys.stderr)
                    _record_repl_command(manager, line, f"Error: {e}", kind="bang", exit_code=None)

            # ── /command: built-in commands ──
            elif line.startswith("/"):
                # ── /command: 统一命令注册表（codes.commands.dispatch）──
                ctx = _repl_cmd_context(manager, reader)
                text = dispatch(manager, line, ctx=ctx)
                if text:
                    print(text)
                if ctx.exit_requested:
                    break
                _record_repl_command(manager, line, text or "", kind="slash")

            # ── LLM agent chat ──
            else:
                try:
                    logger.info(f"用户输入: {line!r:.100}")
                    manager.send_input(line)
                    turn_active = True
                    while turn_active:
                        # ── Ctrl+C 检测：非阻塞轮询 stdin（修复 while turn_active 盲区）──
                        # 原实现主线程阻塞在 read_output()，不读 stdin → Ctrl+C 的 0x03
                        # 滞留在缓冲区，中断延迟到 agent 下一个 _check_esc 才生效。
                        # 这里每轮循环先查一次 stdin，发现 0x03 立即转发中断。
                        try:
                            readable, _, _ = select.select([sys.stdin], [], [], 0)
                            if readable:
                                byte = os.read(sys.stdin.fileno(), 1)
                                if byte == b'\x03':
                                    print("\r\n^C")
                                    logger.info(f"[DIAG] Ctrl+C pressed (turn_active) at {__import__('time').time():.3f}")
                                    if manager.focus:
                                        manager.request_interrupt(manager.focus)
                                    _drain_until_turn_end(manager)
                                    break
                                elif byte in (b'\x0e', b'\x10'):
                                    # Ctrl+N / Ctrl+P — turn 期间切换 session（不中断后台 turn）
                                    # 只切换 focus，不 request_interrupt：旧 agent 继续跑，
                                    # 事件留在其队列，切回时可见完整输出。
                                    direction = 1 if byte == b'\x0e' else -1
                                    print("\r\n", end="", flush=True)
                                    if _cycle_session(manager, direction):
                                        break  # 切换成功 → 退出 turn_active，主循环自动刷新 prompt
                        except (BlockingIOError, OSError, ValueError, EOFError):
                            pass

                        event = manager.read_output(timeout=0.1)
                        if event is None:
                            continue
                        t = event.get("type", "")
                        if t == "_turn_end":
                            turn_active = False
                            break
                        rendered = _handle_event(event, manager)
                        if rendered:
                            print(rendered, end="", flush=True)
                    print()
                except KeyboardInterrupt:
                    print("\n  Interrupted")
                    # ── 修复: 捕获 Ctrl+C 时转发中断（原实现缺失）──
                    if manager.focus:
                        manager.request_interrupt(manager.focus)
                    _drain_until_turn_end(manager)
                except Exception as e:
                    print(f"\n  Error: {e}")

    finally:
        reader.save_history()
        # 2026-08-13: /exit 退出 REPL 时兜底清理本进程持有的 mkdir 锁，
        # 避免 daemon agent 线程随进程退出导致 lockdir 残留。
        try:
            from codes.lock import cleanup_all as _cleanup_all
            _n = _cleanup_all()
            if _n:
                logger.info(f"REPL 退出: 兜底清理 {_n} 个残留锁")
        except Exception:
            pass



def _handle_event(event: dict, manager) -> str | None:
    """Handle a single agent event: update cache + return rendered text.
    
    Returns:
        Rendered string for printing, or None if internal-only event.
    """
    t = event.get("type", "")
    
    # ── blocked: 观察者模式拒绝输入提示 (T7) ──
    if t == "blocked":
        reason = event.get("reason", "session 被占用")
        print(f"  ⚠️ [blocked] {reason}", flush=True)
        return None

    # ── _cmd_result: update manager cache ──
    if t == "_cmd_result":
        cmd = event.get("cmd")
        data = event.get("data")
        if cmd == "get_info" and isinstance(data, dict):
            manager._focus_mode = data.get("mode", manager._focus_mode)
            manager._focus_observing = data.get("is_observing", False)
            manager._focus_holder_info = data.get("holder_info", None)  # T6 补: 与 _lock_status 分支字段对齐
            manager._focus_skill_select = data.get("skill_select_enabled", manager._focus_skill_select)
        elif cmd == "set_mode":
            if data:
                manager._focus_mode = data
            elif event.get("ok") is False:
                # 观察者拒绝写命令（无 data）：回读真实 mode，防止缓存保留错误状态
                info = manager.get_focus_info(timeout=2.0)
                if info:
                    manager._focus_mode = info.get("mode", manager._focus_mode)
        elif cmd == "set_skill_select" and isinstance(data, bool):
            manager._focus_skill_select = data
        return None  # 不渲染
    
    # ── _lock_status: update lock state cache ──
    if t == "_lock_status":
        manager._focus_observing = event.get("is_observing", False)
        manager._focus_holder_info = event.get("holder_info", None)
        return None
    
    # ── _sync_update: print sync messages ──
    if t == "_sync_update":
        msgs = event.get("messages", [])
        try:
            for m in msgs:
                role = m.get("role", "?")
                content = m.get("content") or ""   # F1: 防御 content=None
                if role == "user":
                    parsed = parse_user_prefix(content)
                    if parsed:
                        content = parsed["body"]
                prefix = "🧑" if role == "user" else "🤖" if role == "assistant" else "🔧"
                print(f"  📥 {prefix} [sync][{role}] {content[:500]}", flush=True)
        except Exception as e:
            logger.warning(f"_sync_update 渲染异常: {e}")
        return None
    
    # ── _turn_end / done: 不渲染 ──
    if t in ("_turn_end", "done"):
        return None
    
    # ── tool_progress: repl 不渲染工具进度（方案2 仅 web 实时展示）──
    if t == "tool_progress":
        return None
    
    # ── other events: render ──
    return _render_event(event)


def _drain_output(manager, timeout=0.1) -> bool:
    """Drain any pending output events (non-blocking), update cache, render.

    Returns True if any events were consumed (caller may want to refresh prompt).
    """
    had_any = False
    while True:
        event = manager.read_output(timeout=timeout)
        if event is None:
            break
        had_any = True
        rendered = _handle_event(event, manager)
        if rendered:
            print(rendered, end="", flush=True)
    return had_any


def _drain_until_turn_end(manager, total_timeout=3.0):
    """Drain output queue until _turn_end is received, discarding events.

    Used after Ctrl+C during a turn to prevent stale events from leaking
    into the next user interaction.
    """
    deadline = time.time() + total_timeout
    while time.time() < deadline:
        try:
            event = manager.read_output(timeout=0.5)
        except Exception:
            break
        if event is None:
            break
        if isinstance(event, dict) and event.get("type") == "_turn_end":
            break


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  Event renderer — converts agent stream events to ANSI display
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

MAX_OUTPUT_CHARS = 800
MAX_SHOW_LINES_HEAD = 8
MAX_SHOW_LINES_TAIL = 4

from codes.llm import (MODEL_PRICING, get_model_pricing, estimate_cost)

def _get_model_pricing(model_name):
    """兼容旧调用方，统一委托 llm.py。"""
    return get_model_pricing(model_name)

def _estimate_cost(model, prompt_tokens, completion_tokens):
    """兼容旧调用方，统一委托 llm.py。"""
    return estimate_cost(model, prompt_tokens, completion_tokens)

def _format_ratio(a, b):
    if a == 0 and b == 0:
        return "0:0"
    if b == 0:
        return f"{a}:0"
    return f"1:{b/a:.1f}" if a > 0 else f"0:{b}"


def _truncate_output(text, max_chars=MAX_OUTPUT_CHARS):
    """Truncate long output, preserving head and tail."""
    lines = text.split("\n")
    if len(lines) > MAX_SHOW_LINES_HEAD + MAX_SHOW_LINES_TAIL:
        hidden = len(lines) - MAX_SHOW_LINES_HEAD - MAX_SHOW_LINES_TAIL
        ellipsis = "\u2026"
        warn_text = Style.warn(f"{ellipsis} truncated {hidden} lines {ellipsis}")
        truncated = lines[:MAX_SHOW_LINES_HEAD] + [warn_text] + lines[-MAX_SHOW_LINES_TAIL:]
        text = "\n".join(truncated)
    if len(text) <= max_chars:
        return text
    half = max_chars // 2
    ellipsis = "\u2026"
    warn_text = Style.warn(f"{ellipsis} truncated {len(text) - max_chars} chars {ellipsis}")
    return text[:half] + "\n" + warn_text + "\n" + text[-half:]



# ── thinking 区域状态管理 ──
# agent 事件序列中 clear_thinking 仅在首个 text chunk 时触发（agent.py L1882），
# 若模型思考后直接进入 tool_call / done（无 text），thinking 区域会残留不闭合。
# _thinking_active 标志 + _close_thinking_prefix() 用于防御该边界，
# 保证「思考开始 / 思考结束」标识在任意事件序列下都成对出现。

# 思考区域分隔标识：纯文本虚横线，无 ANSI/无颜色，用于与正文前后隔离
_THINK_SEP = "\u2500" * 10
_THINK_EQ = "=" * 14
_thinking_active = False


def _close_thinking_prefix() -> str:
    """若 thinking 区域活跃，返回思考结束标识并闭合状态（供 tool_call/done 前置调用）。"""
    global _thinking_active
    if not _thinking_active:
        return ""
    _thinking_active = False
    return "\n" + Style.ok(_THINK_EQ + " [结束] " + _THINK_EQ) + "\n"

def _render_event(event: dict) -> str:
    """Render a structured event from agent.run_stream() into ANSI-display string."""

    global _thinking_active
    t = event.get("type", "")

    if t == "thinking":

        _thinking_active = True

        return "\n" + Style.ok(_THINK_EQ + " [思考] " + _THINK_EQ) + "\n"

    elif t == "text":
        return event.get("data", "")

    elif t == "thinking_content":
        return event.get("data", "")

    elif t == "tool_call":
        name = event.get("name", "?")
        args = event.get("args", {})
        idx = event.get("index", 0)
        total = event.get("total", 1)
        step_str = f"({idx+1}/{total})" if total > 1 else ""
        args_preview = ", ".join(
            f"{k}={repr(v)[:80] + chr(0x2026) if len(repr(v)) > 80 else repr(v)}"
            for k, v in args.items()
        )
        mode_tags = {
            "plan": " 📐[plan]",
            "build": " 🔧[build]",
            "build-unsafe": " 🔥[build-unsafe]",
        }
        mode = event.get("mode", "plan")
        mode_tag = mode_tags.get(mode, "")

        return _close_thinking_prefix() + "\n" + "  " + Style.ICON_TOOL + " " + Style.bold("Tool Call") + " " + step_str + ":" + Style.warn(mode_tag) + " " + Style.cmd(name) + "(" + Style.warn(args_preview) + ")" + "\n"

    elif t == "tool_result":
        exit_code = event.get("exit_code", -1)
        elapsed = event.get("elapsed", 0)
        stdout = event.get("stdout", "")
        stderr = event.get("stderr", "")
        has_error = bool(event.get("error")) or exit_code != 0
        status = Style.fail(chr(10060)) if has_error else Style.ok(chr(9989))
        time_str = Style.muted(f"({elapsed:.2f}s)")
        exit_str = Style.muted(f"exit: {exit_code}")
        parts = [status + "  " + time_str + "  " + exit_str]
        err_text = event.get("error", "")
        if stdout:
            parts.append(_truncate_output(stdout))
        if stderr:
            parts.append("[stderr]\n" + stderr)
        if err_text:
            parts.append("[error] " + err_text)
        return "  " + "\n".join(parts) + "\n"

    elif t == "turn_end_by_tool":
        # 回合由 summary/exit 等工具结束：终端展示结束原因/结论（key）
        name = event.get("name", "?")
        key = event.get("key", "")
        note = f"回合由 {name} 工具结束"
        if key:
            note += f"：{key}"
        return _close_thinking_prefix() + "\n  " + Style.ok("🔚 " + note) + "\n"

    elif t == "stats":
        pt = event.get("prompt_tokens", 0)
        ct = event.get("completion_tokens", 0)
        total = pt + ct
        ratio = _format_ratio(pt, ct)
        model = event.get("model", "")
        cost = _estimate_cost(model, pt, ct)
        up = chr(8593)
        down = chr(8595)
        return "\n" + "  " + Style.ICON_STATS + "  " + Style.stats(f"{up}{pt:>6,}") + " / " + Style.stats(f"{down}{ct:>6,}") + "  " + Style.stats(f"{total:,} total") + "  " + Style.stats(f"ratio {ratio}") + "  " + Style.stats(f"${cost:.6f}") + "\n"

    elif t == "permission":
        pdata = event.get("data", "")
        if pdata:
            return "  " + Style.ICON_INFO + " " + Style.muted(pdata) + "\n"
        return _close_thinking_prefix()

    elif t == "suggested_skills":
        skills = event.get("skills", [])
        if skills:
            return "  " + Style.ICON_INFO + " " + Style.bold("建议技能: ") + Style.cmd(", ".join(str(s) for s in skills)) + "\n"

        return _close_thinking_prefix()

    elif t == "recommended_info":
        items = event.get("items", [])
        if items:
            parts = [f"  {Style.ICON_INFO} {Style.bold('推荐信息:')} {len(items)} 条"]
            for it in items:
                parts.append(f"    [{it.get('scope','')}] {it.get('path','')} | {it.get('snippet','')}")
            return "\n".join(parts) + "\n"
        return _close_thinking_prefix()

    elif t == "skill_selected":
        name = event.get("name", "")
        reason = event.get("reason") or ""
        suffix = f"（{reason}）" if reason else ""
        return "  " + "🎯" + " " + Style.bold("技能选择: ") + Style.cmd(name) + suffix + "\n"

    elif t == "skill_req":
        return "  " + Style.ICON_INFO + " " + Style.info("Skill loaded: ") + Style.muted(event.get("name", "")) + "\n"

    elif t == "no_skill":
        return "  " + "🎯" + " " + Style.bold("技能选择: 无") + "\n"

    elif t == "error":
        err_msg = event.get("data", "")
        hint = friendly_error_hint(err_msg)
        extra = "\n  " + Style.muted(hint) if hint else ""
        return "\n" + "  " + Style.ICON_FAIL + " " + Style.fail("Error: " + err_msg) + extra + "\n"

    elif t == "clear_thinking":

        _thinking_active = False

        return "\n" + Style.ok(_THINK_EQ + " [结束] " + _THINK_EQ) + "\n\n"

    elif t == "done":

        return _close_thinking_prefix()

    return ""

# REPL 统一由 codes.main 调度；本模块不再保留失效的独立入口。
