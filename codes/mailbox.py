"""codes/mailbox.py — mail.jsonl 全局总线（append-only 邮件原语，多进程安全）

mail v2.3.1 定稿 §2/§4/§5 的文件层实现：
  - 路径：{project_root}/mail.jsonl（与 session_registry.json 同目录），XKAGENT_MAIL 环境变量覆盖；
  - 写：mail.lockdir 毫秒临界区（mkdir 原子互斥；残留 >MAIL_LOCK_STALE 用 rename 原子接管）；
  - 读：无锁 + 字节游标增量 + 撕裂尾行丢弃（半行等待补全，3 轮未补全视为死行跳过）；
  - 状态机：send → delivered → done｜failed(retry<MAX) → …→ dead｜rejected（按 id 取最后状态行定态）；
  - Mailbox 实例必须 per-进程独立（cursor/聚合内存态不跨进程共享——演练实证：cursor 共享会丢 send 行）；
  - 写锁不可重入：postman 轮内持锁用 append_locked，外部 callagent 用 append（自抢锁）。
"""
from __future__ import annotations

import json
import os
import random
import socket
import time

# ── 常量 ────────────────────────────────────────────────────────────

# mail.lockdir 残留判定（秒）：目录 mtime 超过此值视为持锁者已死，可 rename 接管
MAIL_LOCK_STALE = 5.0
# 撕裂半行等待轮数：持续 N 轮无补全且大小不变 → 视为死行跳过（推进游标）
PARTIAL_MAX_STREAK = 3
# send 行 body 上限（字节）：完整行 <4KB 保障
MAIL_BODY_MAX = 3500
# host 截断长度（id 前缀，防超长行）
HOST_MAX = 16
# 外部发信（callagent）抢锁重试：postman 轮内持锁可达轮内预算（秒级），
# 无等待抢锁会概率性返回"忙"丢信；短退避重试显著收敛（仍失败则返回 False）。
MAIL_APPEND_RETRY = 5
MAIL_APPEND_RETRY_SLEEP = 0.05


def mail_path() -> str:
    """mail.jsonl 路径：XKAGENT_MAIL 环境变量优先，否则与 session_registry.json 同目录。"""
    override = os.environ.get("XKAGENT_MAIL", "").strip()
    if override.lower() not in ("", "off", "0", "false"):
        return override
    from codes.session_registry import registry_path
    return os.path.join(os.path.dirname(str(registry_path())), "mail.jsonl")


def mail_lockdir_path() -> str:
    return mail_path() + ".lockdir"


def _rmtree(path: str) -> None:
    """纯 os 递归删除（沙箱兼容，best-effort；仿 lock.py 实现）。"""
    if not os.path.isdir(path):
        return
    for root, dirs, files in os.walk(path, topdown=False):
        for name in files:
            try:
                os.unlink(os.path.join(root, name))
            except OSError:
                pass
        for name in dirs:
            try:
                os.rmdir(os.path.join(root, name))
            except OSError:
                pass
    try:
        os.rmdir(path)
    except OSError:
        pass


def gen_mail_id() -> str:
    """生成消息 id：m_<host>_<ts>_<rand4>（host 截断；ts=毫秒；rand4=4 hex）。"""
    host = socket.gethostname()[:HOST_MAX]
    ts = int(time.time() * 1000)
    rand = "%04x" % random.randint(0, 0xFFFF)
    return f"m_{host}_{ts}_{rand}"


def split_mail_provider(raw: str | None) -> tuple[str | None, str | None]:
    """拆分 callagent provider 参数 → (provider, model|None)。

    - 空/None → (None, None)（不覆盖）；
    - 'my-provider' → ('my-provider', None)（model 跟随其 default_model）；
    - 'my-provider/gpt-5.4' → ('my-provider', 'gpt-5.4')（显式指定模型）。
    仅拆格式不校验存在性（校验在 tools.exec_callagent 层）。
    """
    if not raw:
        return None, None
    raw = str(raw).strip()
    if "/" in raw:
        p, _, m = raw.partition("/")
        return (p or None), (m or None)
    return raw, None


# 状态机合法转换（状态行判定；send 为每个 id 的起始行）
_VALID_NEXT = {
    "send": {"delivered", "failed", "rejected"},
    "delivered": {"done", "failed", "dead"},
    "failed": {"delivered", "dead", "rejected"},
    "done": set(),       # 终态
    "dead": set(),       # 终态
    "rejected": set(),   # 终态
}


class Mailbox:
    """mail.jsonl 文件层访问（per-进程独立实例）。

    线程安全约定：append 系列自抢 mail.lockdir 临界区（毫秒级）；
    scan_new / 聚合读取无锁（append-only + 撕裂兜底）。
    """

    def __init__(self, path: str | None = None):
        self.path = path or mail_path()
        self.lockdir = self.path + ".lockdir"
        self.cursor = 0                     # 字节游标（已消费位置）
        self.status: dict[str, dict] = {}   # id -> {"state", "send", "last", "max_retry"} 按 id 定态
        self._last_partial_size = -1        # 上轮半行大小时的文件 size（-1=无）
        self._partial_streak = 0            # 半行连续无补全轮数
        self.scan_new()                     # 首轮全量扫描：保证 send_rows 完整

    # ── 轮次 lease ──────────────────────────────────────────────────

    def acquire_lease(self) -> bool:
        """抢轮次锁（mkdir 原子互斥）。残留 >MAIL_LOCK_STALE 用 rename 原子接管。"""
        try:
            os.mkdir(self.lockdir)
            os.utime(self.lockdir)
            return True
        except FileExistsError:
            try:
                mtime = os.stat(self.lockdir).st_mtime
            except OSError:
                return False
            if time.time() - mtime <= MAIL_LOCK_STALE:
                return False
            # 残留接管：rename 原子移走（仅 rename 成功者可继续，并发者让位）
            stale = f"{self.lockdir}.stale.{int(time.time())}.{random.randint(0, 9999)}"
            try:
                os.rename(self.lockdir, stale)
                os.mkdir(self.lockdir)
                os.utime(self.lockdir)
                _rmtree(stale)   # 清理移走的残留（best-effort）
                return True
            except OSError:
                return False

    def release_lease(self) -> None:
        """释放轮次锁（仅删除空目录；非空说明有残迹，留给接管逻辑）。"""
        try:
            os.rmdir(self.lockdir)
        except OSError:
            pass

    # ── 写入（单条行，'\n' 结尾；正文单行 <4KB）──────────────────────

    def _write_line(self, line: dict) -> None:
        # 崩溃残迹防护（2026-09-11）：文件尾若为撕裂半行（非 \n 结尾），先补 \n 闭合，
        # 避免新行与残迹黏连成非法行（两端一并丢失）。
        payload = (json.dumps(line, ensure_ascii=False) + "\n").encode("utf-8")
        with open(self.path, "a+b") as f:
            f.seek(0, os.SEEK_END)
            if f.tell() > 0:
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    f.write(b"\n")
            f.write(payload)

    def append_locked(self, line: dict) -> None:
        """持锁写入（postman 轮内已 acquire_lease 时调用，lease 不可重入）。"""
        self._write_line(line)

    def append(self, line: dict) -> bool:
        """自抢锁写入（外部调用：callagent 发信等）。

        postman 轮内持锁（≤ 轮内预算）时抢锁瞬时失败 → 短退避重试
        （竞争窗口毫秒~秒级，5×50ms 后仍失败返回 False，由调用方报错）。
        """
        for _attempt in range(MAIL_APPEND_RETRY):
            if self.acquire_lease():
                try:
                    self._write_line(line)
                    return True
                finally:
                    self.release_lease()
            time.sleep(MAIL_APPEND_RETRY_SLEEP)
        return False

    def add_send(self, from_: str, to: str | list[str], body: str,
                 reply_to: str | None = None, delay_seconds: float = 0.0,
                 deliver_at: float | None = None, priority: int = 0,
                 provider: str | None = None,
                 need_reply: bool = False) -> tuple:
        """写入 send 行，返回 (ok, msg_id 或错误)。body 超长/非法返回错误。

        to：单收件人字符串，或收件人列表（列表=广播：一封信多收件人，
        各收件人独立投递/重试/状态跟踪；广播邮件不支持 need_reply=true）。
        deliver_at：绝对投递时间戳（优先）；否则 now+delay_seconds。
        priority：投递优先级（大者优先，仅影响 claimable 排序）。
        provider：收信方本次任务的 LLM provider（可选，如 'my-provider' 或
        'my-provider/gpt-5.4'；turn 级临时覆盖，不影响收信会话自身配置）。
        need_reply：是否期望对方回复（默认 false；随信封透传，agent 层据此
        决定是否注入 callagent 回信指引；广播邮件强制 false）。
        """
        # to 规范化：str → [str]；list → 去重保序
        if isinstance(to, str):
            to_val: str | list[str] = to
        elif isinstance(to, (list, tuple)):
            seen: list[str] = []
            for _t in to:
                if isinstance(_t, str) and _t.strip() and _t not in seen:
                    seen.append(_t)
            if not seen:
                return False, "to 列表为空"
            to_val = seen[0] if len(seen) == 1 else seen   # 单元素 → 单值（兼容路径）
        else:
            return False, "to 类型非法（应为字符串或字符串列表）"
        if isinstance(to_val, list) and need_reply:
            return False, "广播邮件不支持 need_reply=true（广播=通知型）"
        if not isinstance(body, str) or not body.strip():
            return False, "message 为空"
        if len(body) > MAIL_BODY_MAX:
            return False, f"message 超过 {MAIL_BODY_MAX} 字节上限"
        if reply_to is not None and (not isinstance(reply_to, str) or not reply_to.strip()):
            return False, "reply_to 非法"
        if not isinstance(need_reply, bool):
            return False, "need_reply 必须为 bool"
        mid = gen_mail_id()
        tdeliver = float(deliver_at) if deliver_at is not None else time.time()             + max(0.0, float(delay_seconds))
        line = {
            "type": "send",
            "id": mid,
            "from": from_,
            "to": to,
            "reply_to": reply_to or None,
            "deliver_at": tdeliver,
            "created_at": time.time(),
            "body": body,
            "priority": int(priority) if priority else 0,
            "provider": provider or None,
            "need_reply": bool(need_reply),
        }
        if not self.append(line):
            return False, "mail.lockdir 忙，请重试"
        self._apply_line(line)
        return True, mid

    def add_status(self, mid: str, type_: str, **fields) -> bool:
        """写入状态行（delivered/done/failed/dead/rejected；postman 轮内用 append_locked）。"""
        line = {"type": type_, "id": mid, "at": time.time(), **fields}
        self._write_line(line)
        self._apply_line(line)
        return True

    # ── 读取：增量扫描 + 聚合 ───────────────────────────────────────

    def scan_new(self) -> list[dict]:
        """增量读取新行（无锁）。撕裂尾行丢弃逻辑：
        尾部无 \\n → 暂不推进游标；连续 PARTIAL_MAX_STREAK 轮大小不变 → 视为死行：先消费"最后一个 \\n 之前"的完整部分，再跳过残余半行。
        文件被替换/截断（size < cursor）→ 游标归零全量重建。"""
        if not os.path.exists(self.path):
            return []
        size = os.path.getsize(self.path)
        if size < self.cursor:
            self.cursor = 0
            self.status.clear()
        if size == self.cursor:
            return []
        with open(self.path, "rb") as f:
            f.seek(self.cursor)
            data = f.read()
        if not data:
            return []
        # 尾部撕裂检测：半行暂不推进游标（等写方补全）；
        # 连续 PARTIAL_MAX_STREAK 轮文件大小不变 → 视为死行跳过（推进游标）
        if not data.endswith(b"\n"):
            if size == self._last_partial_size:
                self._partial_streak += 1
                if self._partial_streak >= PARTIAL_MAX_STREAK:
                    # 死行（连续 N 轮未补全）：先解析并应用"最后一个 \\n 之前"的
                    # 完整部分（防连坐丢信），残余半行按撕裂残迹跳过。
                    self._partial_streak = 0
                    self._last_partial_size = -1
                    last_nl = data.rfind(b"\n")
                    recs = self._consume_block(data[:last_nl + 1]) if last_nl != -1 else []
                    self.cursor = size
                    return recs
                return []
            # 首次发现半行即计为第 1 次未补全（配合 PARTIAL_MAX_STREAK=3：
            # 连续 3 轮大小不变 → 死行跳过）
            self._last_partial_size = size
            self._partial_streak = 1
            return []
        self._partial_streak = 0
        self._last_partial_size = -1
        # 消费到最后一个 \n 处（整块都含完整行——末尾无半行）
        self.cursor = size
        return self._consume_block(data)

    def _consume_block(self, data: bytes) -> list[dict]:
        """解析完整块（以 \n 结尾，或已裁出的完整部分）：畸形行跳过、逐行 apply。"""
        new_recs = []
        for raw in data.split(b"\n"):
            if not raw:
                continue
            try:
                rec = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                continue   # 畸形行：跳过（append-only 下必为撕裂残迹）
            if isinstance(rec, dict):
                new_recs.append(rec)
                self._apply_line(rec)
        return new_recs

    @staticmethod
    def _recipients(st: dict) -> list[str]:
        """send 行的收件人集合（str → [str]；list → 原样）。"""
        to = st["send"].get("to")
        if isinstance(to, list):
            return [t for t in to if isinstance(t, str)]
        return [to] if isinstance(to, str) else []

    @staticmethod
    def _per_to(st: dict, rcpt: str) -> dict | None:
        """指定收件人的 per-to 状态（无状态行记录时为 None）。"""
        pt = (st.get("per_to") or {}).get(rcpt)
        if pt is not None:
            return pt
        if not st.get("per_to") and not isinstance(st["send"].get("to"), list):
            return {"state": st["state"], "last": st["last"],
                    "max_retry": st.get("max_retry") or 0}   # 旧格式（状态行无 to）
        return {"state": "send", "last": None, "max_retry": 0}   # 广播未投递收件人

    def _apply_line(self, rec: dict) -> None:
        """按状态机更新聚合（非法转换忽略）。

        状态行带 "to"（per-收件人）→ 按收件人独立迁移 + 重算整体聚合；
        状态行无 "to"（旧格式/单收件人兼容）→ 整体迁移（旧逻辑）。
        """
        t = rec.get("type")
        mid = rec.get("id")
        if not isinstance(mid, str):
            return
        if t == "send":
            self.status[mid] = {"state": "send", "send": rec, "last": rec,
                                "per_to": {}}
            return
        if t not in _VALID_NEXT:
            return
        st = self.status.get(mid)
        if st is None:
            return   # 无 send 先行：丢弃（残行/重放）
        rcpt = rec.get("to")
        if isinstance(rcpt, str) and rcpt:
            # ── per-收件人迁移（广播 / 新单收件人行）──
            per_to: dict = st.setdefault("per_to", {})
            pt = per_to.get(rcpt) or {"state": "send", "last": None, "max_retry": 0}
            if pt["state"] not in _VALID_NEXT or t not in _VALID_NEXT[pt["state"]]:
                # 重建容错（2026-09-11）：compact 仅保留收件人"最新状态行"，
                # 重放时 done/dead 前缺 delivered 前置 → 允许 send→done/dead 直连重建。
                if not (pt["state"] == "send" and t in ("done", "dead")):
                    return   # 非法转换：忽略
            pt["state"] = t
            pt["last"] = rec
            if t == "failed":
                pt["max_retry"] = max(int(pt.get("max_retry") or 0), int(rec.get("retry") or 0))
            per_to[rcpt] = pt
            st["last"] = rec
            st["state"] = Mailbox._aggregate_state(st)
            return
        # ── 旧格式（状态行无 to）：整体迁移 ──
        if st["state"] not in _VALID_NEXT or t not in _VALID_NEXT[st["state"]]:
            # 重建容错（同 per-收件人分支）：允许 send→done/dead 直连重建
            if not (st["state"] == "send" and t in ("done", "dead")):
                return   # 非法转换：忽略
        st["state"] = t
        st["last"] = rec
        if t == "failed":
            # retry 累计（关键：重投后 last=delivered 无 retry 字段，
            # 必须从历史 failed 行累计，否则 retry 恒=1 → 永不 dead → 无限重投循环）
            st["max_retry"] = max(int(st.get("max_retry") or 0), int(rec.get("retry") or 0))

    @staticmethod
    def _aggregate_state(st: dict) -> str:
        """广播信整体聚合（单收件人不经此）：
        全 done → done；任一 dead/rejected 且其余全终态 → dead；
        否则（有未终态）按进展取 delivered > failed > send。
        """
        tos = Mailbox._recipients(st)
        if not tos:
            return st["state"]
        states = [(Mailbox._per_to(st, t)).get("state") for t in tos]
        if all(s == "done" for s in states):
            return "done"
        if any(s in ("dead", "rejected") for s in states) and \
                all(s in ("done", "dead", "rejected") for s in states):
            return "dead"
        if any(s == "delivered" for s in states):
            return "delivered"
        if any(s == "failed" for s in states):
            return "failed"
        return "send"

    # ── 聚合查询 ────────────────────────────────────────────────────

    def pending_sends(self, now: float | None = None) -> list[dict]:
        """已到期（deliver_at<=now）且无状态行的 send 行（=可认领候选）。

        返回每项为 send 行浅拷贝 + "_to"（本次投递目标收件人，广播时逐收件人）。
        """
        now = time.time() if now is None else now
        out = []
        for st in self.status.values():
            if isinstance(st["send"].get("to"), list):
                for rcpt in Mailbox._recipients(st):
                    if Mailbox._per_to(st, rcpt)["state"] == "send" and \
                            st["send"].get("deliver_at", 0) <= now:
                        out.append({**st["send"], "_to": rcpt})
            elif st["state"] == "send" and st["send"].get("deliver_at", 0) <= now:
                out.append({**st["send"], "_to": st["send"]["to"]})
        return out

    def claimable(self, now: float | None = None, max_retry: int = 2,
                   cooldown: float = 0.2) -> list[dict]:
        """可认领候选：到期的 send + 冷却期满且未耗尽的重投 failed（per-收件人）。

        返回每项为 send 行浅拷贝 + "_to"（本次投递目标收件人；广播信逐收件人候选，
        单收件人 "_to"=原 to）。cooldown：failed 后冷却期（秒）——防同轮
        failed→立即重投死循环；冷却检查必须在候选收集阶段放行（否则重投死锁）。
        """
        now = time.time() if now is None else now
        out = []
        for st in self.status.values():
            tos = Mailbox._recipients(st)
            for rcpt in tos:
                pt = Mailbox._per_to(st, rcpt)
                pstate = pt["state"]
                if pstate == "send" and st["send"].get("deliver_at", 0) <= now:
                    out.append({**st["send"], "_to": rcpt})
                elif (pstate == "failed"
                      and int(pt.get("max_retry") or 0) < max_retry
                      and now - (pt["last"] or {}).get("at", 0) >= cooldown):
                    out.append({**st["send"], "_to": rcpt})
        # 排序：priority 降序 → deliver_at 升序（先到期者先投）
        out.sort(key=lambda s: (-int(s.get("priority") or 0), s.get("deliver_at", 0)))
        return out

    def in_flight(self, to: str) -> bool:
        """该收件人是否存在未终态的在途信（该 to 的 delivered 未 fin）。"""
        for st in self.status.values():
            if to not in Mailbox._recipients(st):
                continue
            if Mailbox._per_to(st, to)["state"] == "delivered":
                return True
        return False

    def get(self, mid: str) -> dict | None:
        return self.status.get(mid)

    def delivered_items(self) -> list[dict]:
        """所有 delivered 态条目（per-收件人，含 send/last/to），供盯梢轮询。

        单收件人（旧格式状态行无 to）：按整体态识别。
        """
        out = []
        for st in self.status.values():
            for rcpt in Mailbox._recipients(st):
                pt = Mailbox._per_to(st, rcpt)
                if pt["state"] == "delivered":
                    out.append({"send": st["send"], "last": pt["last"], "to": rcpt})
        return out

    def compact(self, max_retry: int = 2) -> int:
        """压实归档：保留未终态邮件（send / delivered / failed）的 send 先行 + 状态行；
        广播信按收件人保留全部已有状态行（含 done/dead/rejected），防重建后
        终态收件人回退默认 send 被重复投递；整信全终态时整条剔除。
        持锁调用（写临界区内）。

        实现：全量重建聚合 → 输出保留行到 tmp → os.replace 原子替换
        → 其他进程旧游标 > 新 size 自动触发全量重建（scan_new 已支持）。
        返回保留行数。
        """
        probe = Mailbox(self.path)     # 独立实例全量重建（避免与本实例状态不同步）
        keep = []
        for st in probe.status.values():
            tos = Mailbox._recipients(st)
            broadcast = isinstance(st["send"].get("to"), list)
            any_open = False
            rows = [st["send"]]
            for rcpt in tos:
                pt = Mailbox._per_to(st, rcpt)
                ps = pt["state"]
                if ps in ("send", "delivered"):
                    any_open = True
                elif ps == "failed" and int(pt.get("max_retry") or 0) < max_retry:
                    any_open = True
                if pt.get("last") is not None:
                    # 保留全部已有状态行：delivered（turn_id 盯梢）/ failed（重投计数）
                    # / done|dead|rejected（防重建后回退默认 send 被重复投递）
                    rows.append(pt["last"])
            if broadcast:
                if any_open:
                    keep.extend(rows)
            else:
                # 单收件人（旧格式/单值 to）：整体终态才剔除；未终态保留
                # send 先行 + 状态行（重建要求 send 先行，否则状态行会被丢弃）
                if st["state"] == "send":
                    keep.append(st["send"])
                elif st["state"] in ("delivered", "failed"):
                    keep.append(st["send"])
                    if st.get("last") is not None:
                        keep.append(st["last"])
                # done/dead/rejected：终态，整条剔除
        tmp = self.path + ".compact.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for rec in keep:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        os.replace(tmp, self.path)
        # 本实例游标归零+重建（与新文件对齐；旧游标 > 新 size 也会触发）
        self.cursor = 0
        self.scan_new()
        return len(keep)

    def chain(self, mid: str) -> list[dict]:
        """回复链查询：从 mid 沿 reply_to 向上回溯（对话线），返回 [自旧至新]。"""
        out = []
        cur = mid
        seen = set()
        while cur and cur not in seen:
            seen.add(cur)
            st = self.status.get(cur)
            if st is None:
                break
            out.append(st["send"])
            cur = st["send"].get("reply_to")
        return list(reversed(out))
