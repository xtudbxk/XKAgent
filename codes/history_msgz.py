"""history_msgz.py — msgz 存储层（纯标准库实现，零第三方依赖）

设计动机（2026-08-24）：
  sqlite 的 WAL 模式需要 -wal/-shm/-lock 多文件交互，在 CubeFS 满卷/
  扩容窗口期出现间歇性 disk I/O error（EIO），且故障期写操作同步失败。
  msgz 改为：单文件、内存主数据、压缩原子同步——写操作在内存完成（故障
  期零报错零丢失），落盘为普通 IO（tmp + os.replace 原子替换）。

数据格式：<history_dir>/<session>.msgz
  zlib.compress(json.dumps({version, next_id, messages, states}))

API 语义对齐 history.py：
  add_message / get_messages_since / get_messages / get_chat_messages
  set_state / get_state（token_state / agent_state / mount_state 等）
  compact / resume 支持
"""
import os as _os

MSGZ_SUFFIX = ".msgz"
SYNC_INTERVAL = 30.0      # 定时同步周期（秒）
SYNC_ROUNDS = 3           # 每次同步尝试轮数（存储故障时快速重试）
COMPRESS_LEVEL = 6


class MsgzStore:
    """单 session 存储：内存主数据 + 定时原子落盘。线程安全。"""

    def __init__(self, path, auto_sync=True):
        import threading
        self.path = path
        self.lock = threading.Lock()
        self.messages = []       # list[dict]: id/role/content/extras/turn/created_at
        self.states = {}         # dict[str, dict]
        self._next_id = 1
        self.dirty = False
        self.sync_errors = 0     # 连续同步失败计数（供诊断）
        self._dirty_seq = 0      # 写序号（sync 清 dirty 前比对，防覆盖窗口内新写）
        self._file_key = None    # 已加载文件的 (mtime_ns, size)，跨进程同步检测用
        self._load()
        if auto_sync:
            self._start_syncer()

    # ── 写路径（内存，永不因存储故障失败）──

    def add_message(self, role, content, extras=None, turn=0, created_at=None):
        import json, time
        with self.lock:
            msg = {
                "id": self._next_id,
                "role": role,
                "content": content,
                "extras": json.dumps(extras, ensure_ascii=False) if extras else None,
                "turn": turn,
                "created_at": created_at or time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            self.messages.append(msg)
            self._next_id += 1
            self.dirty = True
            self._dirty_seq += 1
            return msg["id"]

    def add_marker(self, role, content, extras=None):
        """compact/drop marker（id 与消息同序列）。"""
        import json, time
        with self.lock:
            msg = {
                "id": self._next_id,
                "role": role,
                "content": content,
                "extras": json.dumps(extras, ensure_ascii=False) if extras else None,
                "turn": 0,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            self.messages.append(msg)
            self._next_id += 1
            self.dirty = True
            self._dirty_seq += 1
            return msg["id"]

    def delete_all_messages(self):
        with self.lock:
            self.messages = []
            self._next_id = 1
            self.dirty = True
            self._dirty_seq += 1

    def set_state(self, name, data):
        with self.lock:
            self.states[name] = data
            self.dirty = True
            self._dirty_seq += 1

    def get_state(self, name):
        with self.lock:
            return self.states.get(name)

    # ── 读路径（内存 O(1)/O(n)）──

    def get_messages_since(self, after_id=None, with_id=False, limit=None,
                           fields=None, with_time=False, ignore_cutoff=False):
        """对齐 history.get_messages_since 语义。

        - after_id=None → 全量；>=0 → id > after_id（增量游标）
        - limit → 取最新 limit 条（升序返回）
        - compact/drop/clear marker 边界语义：ignore_cutoff=True 时返回完整历史
          （web 展示）；False 时在 marker 处截断并生成 synthetic summary。
        """
        with self.lock:
            msgs = self.messages
            last_id = 0
            out = []
            # compact/drop 处理（对齐 sqlite 版）
            marker = None
            for m in reversed(msgs):
                if m["role"] in ("compact", "drop", "clear"):
                    marker = m
                    break
            if marker and not ignore_cutoff:
                import json as _json
                ext = _json.loads(marker["extras"]) if marker["extras"] else {}
                cutoff_max_id = ext.get("cutoff_max_id", 0)
                if after_id is None or marker["id"] > after_id:
                    if marker["role"] == "compact":
                        summary = (marker["content"] or "").strip()
                        synthetic = "[对话历史已压缩。以下是完整上下文，请基于此继续当前任务：]\n\n" + summary + "\n"
                    elif marker["role"] == "clear":
                        synthetic = "[对话历史已清空。之前的消息已保留在存储中（断点标记），后续消息从此处开始。]\n"
                    else:
                        synthetic = "[对话历史已丢弃。之前的消息已标记为丢弃，后续消息从此处开始。]\n"
                    out.append({"role": marker["role"], "content": synthetic,
                                "extras": None, "turn": 0,
                                "created_at": marker["created_at"]})
                # 截断：cutoff 之前的消息不返回（after_id 过滤在下方独立执行取交集；
                # 2026-09-11 修复：原 or 分支在 after_id 有值时恒真 → 截断失效）
                msgs = [m for m in msgs if m["id"] > cutoff_max_id]
            else:
                msgs = list(msgs)
            if after_id is not None:
                msgs = [m for m in msgs if m["id"] > after_id]
            if limit is not None and limit > 0:
                msgs = msgs[-limit:]
            for m in msgs:
                d = self._expand_msg(m, with_id=with_id, with_time=with_time)
                d["extras"] = m["extras"]
                d["turn"] = m["turn"]
                if with_time:
                    d["created_at"] = m["created_at"]
                out.append(d)
                if m["id"] > last_id:
                    last_id = m["id"]
            return out, last_id

    def get_messages(self, fields=None):
        return self.get_messages_since(None, fields=fields)[0]

    def get_chat_messages(self):
        """agent LLM 上下文路径：排除 git/compact/drop/command/thinking；
        extras 的 tool_calls/tool_call_id 展开到顶层（LLM 请求必需）。

        2026-08-25 修复：应用 compact/drop/clear cutoff（对齐 get_messages_since）。
        最后一个 marker 之前的历史消息不返回，以 synthetic summary 替代，
        避免 agent 的 LLM 上下文在全量重载（resume/switch_session/_sync_from_db）
        后把压缩前历史重新塞回请求导致 token 超限。"""
        with self.lock:
            msgs = self.messages
            # ── compact/drop marker 截断（与 get_messages_since 语义对齐）──
            marker = None
            for m in reversed(msgs):
                if m["role"] in ("compact", "drop", "clear"):
                    marker = m
                    break
            if marker:
                import json as _json
                ext = _json.loads(marker["extras"]) if marker["extras"] else {}
                cutoff_max_id = ext.get("cutoff_max_id") or marker["id"]
                out = []
                # synthetic summary（role=user，对齐 _finalize_compact 内存行为）
                if marker["role"] == "compact":
                    summary = (marker["content"] or "").strip()
                    if summary:
                        out.append({"role": "user",
                                    "content": "[对话历史已压缩。以下是完整上下文，请基于此继续当前任务：]\n\n"
                                                + summary + "\n",
                                    "extras": None, "turn": 0})
                elif marker["role"] == "clear":
                    out.append({"role": "user",
                                "content": "[对话历史已清空。之前的消息已保留在存储中（断点标记），后续消息从此处开始。]\n",
                                "extras": None, "turn": 0})
                else:
                    out.append({"role": "user",
                                "content": "[对话历史已丢弃。之前的消息已标记为丢弃，后续消息从此处开始。]\n",
                                "extras": None, "turn": 0})
                for m in msgs:
                    if m["role"] in ("git", "compact", "drop", "clear", "command", "thinking"):
                        continue
                    if m["id"] <= cutoff_max_id:
                        continue
                    d = self._expand_msg(m)
                    d["extras"] = m["extras"]
                    d["turn"] = m["turn"]
                    out.append(d)
                return out
            out = []
            for m in msgs:
                if m["role"] in ("git", "compact", "drop", "clear", "command", "thinking"):
                    continue
                d = self._expand_msg(m)
                d["extras"] = m["extras"]
                d["turn"] = m["turn"]
                out.append(d)
            return out

    def last_message_id(self):
        with self.lock:
            return self.messages[-1]["id"] if self.messages else 0

    def reload_if_changed(self) -> bool:
        """文件被其他进程更新且本地无未落盘写时，重新加载内存数据。

        2026-08-25：msgz 是内存主数据 + 30s 落盘，跨进程场景下本进程
        内存不感知其他进程写入（sqlite 时代文件共享天然可见）。供
        agent._sync_from_db 调用——文件 mtime/size 变化则重新 _load。
        本地 dirty（未落盘）时跳过，避免覆盖本进程刚写入的数据。
        """
        import os as _os2
        with self.lock:
            if self.dirty:
                return False
            try:
                _st3 = _os2.stat(self.path)
                _key = (_st3.st_mtime_ns, _st3.st_size)
            except OSError:
                return False
            if _key == self._file_key:
                return False
            # 文件变化 → 重新加载（_load 内部更新 _file_key）
            self._load()
            return True

    def _expand_msg(self, m, with_id=False, with_time=False):
        """序列化消息 dict（对齐 sqlite 版：extras 的 tool_calls/tool_call_id/
        reasoning_content 展开到顶层，供 LLM 请求与 web 展示）。"""
        import json
        d = {"role": m["role"], "content": m["content"] or ""}
        if with_id:
            d["_id"] = m["id"]
        if with_time:
            d["created_at"] = m["created_at"]
        ex = {}
        if m.get("extras"):
            try:
                ex = json.loads(m["extras"]) if isinstance(m["extras"], str) else m["extras"]
            except Exception:
                ex = {}
        if m["role"] == "assistant":
            if "tool_calls" in ex:
                d["tool_calls"] = ex["tool_calls"]
            rc = ex.get("reasoning_content")
            if rc:
                d["reasoning_content"] = rc
        elif m["role"] == "tool" and "tool_call_id" in ex:
            d["tool_call_id"] = ex["tool_call_id"]
        return d

    def delete_killed_tool_messages(self, tid=None):
        """删除 role='tool' AND content='tool is killed by user' 的孤立标记
        （对齐 sqlite 版 _patch_orphaned_tool_calls 的 DELETE SQL）。"""
        with self.lock:
            keep = []
            removed = 0
            for m in self.messages:
                if m["role"] == "tool" and m["content"] == "tool is killed by user":
                    if tid is None:
                        removed += 1
                        continue
                    ex = m.get("extras") or ""
                    if tid in ex:
                        removed += 1
                        continue
                keep.append(m)
            if removed:
                self.messages = keep
                self.dirty = True
                self._dirty_seq += 1
        return removed

    def truncate_oversized_tool_messages(self, transform, max_chars=100_000):
        """修剪超大的 tool 消息（content 经 transform 转换），返回修剪条数。

        2026-09-11: 供 agent 上下文超限恢复（prune 优先策略）使用——本地修剪
        超大 tool 输出，不调用 LLM。修改标 dirty，由常规 30s 落盘/显式 sync 持久化。
        """
        with self.lock:
            n = 0
            for m in self.messages:
                if m.get("role") != "tool":
                    continue
                c = m.get("content")
                if not isinstance(c, str) or len(c) <= max_chars:
                    continue
                m["content"] = transform(c)
                n += 1
            if n:
                self.dirty = True
                self._dirty_seq += 1
            return n

    def max_visible_id(self):
        """MAX(id) 但排除 git/compact/drop/command（对齐 agent _sync_from_db 语义）。"""
        with self.lock:
            m = None
            for x in reversed(self.messages):
                if x["role"] not in ("git", "compact", "drop", "clear", "command"):
                    m = x
                    break
            return m["id"] if m else 0

    def count_visible(self, include_compact=False):
        """COUNT(*) 排除 git/compact/drop（对齐 web/commands 统计语义）。

        include_compact=True（web 全量分页 total 用）：compact marker 计入，
        与 get_messages_since(ignore_cutoff=True) 的返回集保持一致——
        否则 total < 实际返回条数，前端 has_more/"加载更早"计数错乱。
        """
        with self.lock:
            n = 0
            for x in self.messages:
                if x["role"] not in ("git", "compact", "drop", "clear"):
                    n += 1
                elif include_compact and x["role"] == "compact":
                    n += 1
        return n

    def count_user_msgs_after_last_compact(self):
        """最后 compact marker 之后的普通用户消息数（连续 compact 防护 gate 用）。

        返回 None = 无 compact marker（从未压缩过，允许 compact）。
        统计边界内 role='user' 的真实用户消息；若期间出现 clear/drop marker
        （上下文已重置，连续压缩风险不存在）→ 直接返回 1（允许）。
        """
        with self.lock:
            last_compact_id = None
            for m in self.messages:
                if m.get("role") == "compact":
                    last_compact_id = m.get("id")
            if last_compact_id is None:
                return None
            count = 0
            for m in self.messages:
                mid = m.get("id") or 0
                if mid <= last_compact_id:
                    continue
                role = m.get("role")
                if role in ("clear", "drop"):
                    return 1
                if role == "user":
                    count += 1
            return count

    def get_messages_before(self, before_id, limit=None, with_id=False,
                            with_time=False, ignore_cutoff=False):
        """before_id 之前的最新 limit 条（倒序分页，升序返回；web 历史滚动）。"""
        with self.lock:
            msgs = [m for m in self.messages if m["id"] < before_id]
            if limit is not None and limit > 0:
                msgs = msgs[-limit:]
            out = []
            for m in msgs:
                d = self._expand_msg(m, with_id=with_id, with_time=with_time)
                d["extras"] = m["extras"]
                d["turn"] = m["turn"]
                out.append(d)
            return out

    def get_command_history(self, limit=50):
        """最近 limit 条 role='command' 记录（升序）。"""
        with self.lock:
            rows = [m for m in self.messages if m["role"] == "command"]
            rows = rows[-limit:] if limit and limit > 0 else rows
            return list(rows)

    def close(self):
        """兼容 sqlite 连接语义：msgz 常驻内存，关闭时尽力落盘一次。"""
        try:
            self.sync()
        except Exception:
            pass

    def _dump(self):
        """诊断用：当前内存状态快照。"""
        with self.lock:
            return {
                "path": self.path,
                "messages": len(self.messages),
                "next_id": self._next_id,
                "states": list(self.states.keys()),
                "dirty": self.dirty,
                "sync_errors": self.sync_errors,
            }

    # ── 持久化（压缩 + 原子替换；故障期失败可重试，内存数据不丢）──

    def sync(self):
        """尝试落盘。成功返回 True；存储故障返回 False（dirty 保留）。"""
        import json, zlib, os
        with self.lock:
            if not self.dirty:
                return True
            data = {
                "version": 1,
                "next_id": self._next_id,
                "messages": self.messages,
                "states": self.states,
            }
            blob = zlib.compress(
                json.dumps(data, ensure_ascii=False).encode("utf-8"), COMPRESS_LEVEL)
            seq_snapshot = self._dirty_seq   # 快照写序号：清 dirty 前比对，防覆盖窗口内新写
            tmp = self.path + ".tmp"
        for _ in range(SYNC_ROUNDS):
            try:
                with open(tmp, "wb") as f:
                    f.write(blob)
                os.replace(tmp, self.path)
                with self.lock:
                    if self._dirty_seq == seq_snapshot:
                        self.dirty = False   # 仅当快照后无新写入时清除（防覆盖窗口内新写）
                    self.sync_errors = 0
                    try:
                        _st2 = _os.stat(self.path)
                        self._file_key = (_st2.st_mtime_ns, _st2.st_size)
                    except OSError:
                        pass
                return True
            except OSError:
                self.sync_errors += 1
        return False

    def _load(self):
        import json, zlib, time, os
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "rb") as f:
                data = json.loads(zlib.decompress(f.read()))
            self.messages = data.get("messages", [])
            self._next_id = data.get("next_id", 0)
            if not self._next_id:
                self._next_id = (self.messages[-1]["id"] + 1) if self.messages else 1
            self.states = data.get("states", {})
            try:
                _st = _os.stat(self.path)
                self._file_key = (_st.st_mtime_ns, _st.st_size)
            except OSError:
                self._file_key = None
        except Exception:
            # 损坏文件：备份后从空开始（不崩溃，数据由内存/迁移备份兜底）
            try:
                os.rename(self.path, self.path + ".corrupt_" + time.strftime("%Y%m%d_%H%M%S"))
            except OSError:
                pass
            self.messages = []
            self._next_id = 1
            self.states = {}
            self._file_key = None

    def _start_syncer(self):
        import threading, time
        def loop():
            while True:
                time.sleep(SYNC_INTERVAL)
                try:
                    self.sync()
                except Exception:
                    pass
        t = threading.Thread(target=loop, daemon=True)
        t.start()


class MsgzManager:
    """session → MsgzStore 注册表（进程内单例语义）。"""

    def __init__(self, history_dir):
        import threading
        self.history_dir = history_dir
        self.stores = {}
        self.lock = threading.Lock()

    def get(self, path):
        """按完整文件路径获取 store（2026-08-25 修复：支持跨目录）。

        原实现按 session 名 + 固定 history_dir 构造路径，忽略了
        _db_dir() 的 registry 回退——web 从非数据目录启动时会读错/
        读空 store（消息串台/丢失）。改为按 path 缓存，跨目录唯一。
        """
        with self.lock:
            s = self.stores.get(path)
            if s is None:
                s = MsgzStore(path)
                self.stores[path] = s
            return s

    def sync_all(self):
        ok = 0
        with self.lock:
            stores = list(self.stores.values())
        for s in stores:
            if s.sync():
                ok += 1
        return ok

    def close_all(self):
        """退出前全量落盘。"""
        with self.lock:
            stores = list(self.stores.values())
            self.stores.clear()
        for s in stores:
            s.sync()
