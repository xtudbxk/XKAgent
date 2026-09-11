"""codes/session_info.py — session 级 KV 状态存储（status info）。

定位（2026-09-04）：与用户消息头部元信息区（sys_prefix）互补——
  · 系统字段（时间/系统模式/路径访问权限/建议技能/推荐信息…）：
    全局只读，系统每回合生成；
  · 状态信息字段：session 级可写，LLM（addinfo/listinfo/rminfo 工具）与
    用户（/addinfo /listinfo /rminfo 命令）共同维护，每回合以「状态信息:」
    块字段注入用户消息头部（格式同包装规范 v2：条目行缩进两空格）。

与 summary 的分工：状态信息=短小运行态 KV（进度/分支/计数/偏好），
结论/决策/长文本/跨会话信息走 summary（.xkagent/docs，检索式）。

存储：{project_root}/.xkagent/state/{session}.json。
pythonrt 沙箱对 .xkagent/ 只读，故读写必须经本模块（宿主进程）执行。
项目根推导：session_registry.registry_path() 同目录（与 mailbox.py 同模式）。
并发：同 session 写（add_info/remove_info/copy_state）以 lockdir 互斥
（{session}.json.lockdir：mkdir 原子 + 残留接管）串行化 load→改→写整段；
文件层 tmp 唯一名 + os.replace 原子替换。2026-09-07 修复：原 tmp 固定名
在并发写时 os.replace 报 ENOENT 丢更新（errors.log 两起）。
顺序：条目按首次写入序注入（dict 插入序，更新已有 key 不改变位置，新增排尾）。
删除：软删除（tombstone）——条目保留原值+deleted 标志，注入/计数只看活跃条目；
addinfo 同 key 即复活；存档可查 state 文件。
"""

import json
import os
import random
import re
import time

from codes.session_registry import registry_path

# key：短标识符；value：单行（换行折叠为空格）；上限防头部元信息区膨胀
KEY_RE = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_-]{0,31}$')
MAX_KEYS = 16
MAX_VALUE_LEN = 200
# lockdir 残留判定（秒）：state 写为毫秒级，mtime 超此值视为持锁者已死，可 rename 接管
STATE_LOCK_STALE = 10.0
# 锁自旋上限（秒）：持锁者均为毫秒级写，1s 内必让出
_LOCK_ACQUIRE_TIMEOUT = 1.0


def _state_dir():
    """状态目录：{project_root}/.xkagent/state（测试可 monkeypatch）。"""
    return os.path.join(os.path.dirname(str(registry_path())), '.xkagent', 'state')


def _state_path(session):
    if not re.match(r'^[a-zA-Z0-9_\-.]+$', session or ''):
        raise ValueError('illegal session name: %r' % (session,))
    return os.path.join(_state_dir(), session + '.json')

def _state_lockdir(session):
    """同 session state 文件的互斥锁目录（与 .json 同级，跨线程/跨进程可见）。"""
    return _state_path(session) + '.lockdir'


class _state_lock:
    """with _state_lock(session): 同 session 写互斥（mkdir 原子 + 残留接管）。

    保护 load→改→写整段（仅锁 save_state 不够——两个并发 add_info 各自 load
    旧快照仍会互相覆盖）。持锁均为毫秒级；残留 >STATE_LOCK_STALE 视为持锁者
    已死，rename 原子接管（mailbox.py 同模式）。2026-09-07 引入。
    """

    def __init__(self, session):
        self.lockdir = _state_lockdir(session)
        self._acquired = False

    def __enter__(self):
        os.makedirs(os.path.dirname(self.lockdir), exist_ok=True)
        deadline = time.time() + _LOCK_ACQUIRE_TIMEOUT
        while True:
            if self._try_acquire():
                self._acquired = True
                return self
            if time.time() >= deadline:
                raise RuntimeError('state lock acquire timeout: %s' % self.lockdir)
            time.sleep(0.01)

    def _try_acquire(self):
        try:
            os.mkdir(self.lockdir)
            os.utime(self.lockdir)
            return True
        except FileExistsError:
            try:
                mtime = os.stat(self.lockdir).st_mtime
            except OSError:
                return False
            if time.time() - mtime <= STATE_LOCK_STALE:
                return False
            stale = "%s.stale.%d.%04d" % (self.lockdir, int(time.time()),
                                          random.randint(0, 9999))
            try:
                os.rename(self.lockdir, stale)
                os.mkdir(self.lockdir)
                os.utime(self.lockdir)
            except OSError:
                return False
            try:  # 清理移走的残留（best-effort：空目录 rmdir，非空留给下次）
                os.rmdir(stale)
            except OSError:
                pass
            return True

    def __exit__(self, *exc):
        if self._acquired:
            try:
                os.rmdir(self.lockdir)
            except OSError:
                pass
            self._acquired = False
        return False



def load_state(session):
    path = _state_path(session)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}  # 损坏文件视为空（下次写入重建）


def save_state(session, state):
    """原子写 state 文件（tmp 唯一名 + os.replace）。

    并发互斥由调用方持 _state_lock 保证；save_state 自身不强制持锁
    （copy_state 二次加锁/测试可无锁调用）——唯一名 tmp 保证极端并发下
    不产生 ENOENT（后写覆盖先写，最多丢一次更新而非报错）。
    """
    path = _state_path(session)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = "%s.%d.%08x.tmp" % (path, os.getpid(), random.randint(0, 0xFFFFFFFF))
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            # 无 sort_keys：保持首次写入序（dict 插入序），更新已有 key 不改变位置
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        # 写盘中断/异常时清理残留 tmp（os.replace 成功后 tmp 已不存在，此处无操作）
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def add_info(session, key, value, by='llm'):
    """写入/更新（upsert）。返回用户可读结果文本。"""
    key = str(key or '').strip()
    if value is None:  # 显式 None 检查：0/False 等 falsy 值是合法状态值
        return '❌ value 不能为空（删除条目请用 rminfo）'
    if isinstance(value, bool):
        value = 'true' if value else 'false'  # JSON 布尔转可读小写
    value = str(value).strip()
    if not KEY_RE.match(key):
        return '❌ key 非法（需匹配 [a-zA-Z_][a-zA-Z0-9_-]{0,31}）: %r' % key
    if not value:
        return '❌ value 不能为空（删除条目请用 rminfo）'
    value = re.sub(r'\s+', ' ', value)  # 单行约束：多行内容应走 summary
    if len(value) > MAX_VALUE_LEN:
        return '❌ value 超长（>%d 字符）；长文本请用 summary' % MAX_VALUE_LEN
    # 锁内 load→改→写整段：防并发写者（多线程/多进程 addinfo）读-改-写覆盖丢更新
    with _state_lock(session):
        state = load_state(session)
        entry = state.get(key)
        existed = entry is not None and not entry.get('deleted')
        revived = entry is not None and bool(entry.get('deleted'))
        active_n = sum(1 for v in state.values() if not v.get('deleted'))
        if not existed and active_n >= MAX_KEYS:  # 上限只数活跃条目（tombstone 不占名额）
            return '❌ 状态条目已达上限 %d 条，请先用 rminfo 清理' % MAX_KEYS
        state[key] = {  # 整体重建：自然清除 deleted 标志（复活语义）；位置不变
            'value': value,
            'updated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'updated_by': by,
        }
        save_state(session, state)
    verb = '更新' if existed else '写入'
    tag = '（复活自删除存档）' if revived else ''
    return '✅ 已%s状态信息 [%s]: %s%s（下回合注入生效）' % (verb, key, value, tag)


def remove_info(session, key):
    """软删除：打 deleted 标志（保留原值供后续查看），状态板立即移除。"""
    key = str(key or '').strip()
    with _state_lock(session):
        state = load_state(session)
        entry = state.get(key)
        if entry is None or entry.get('deleted'):
            return '❌ 未找到 key: %r（listinfo 查看现有条目）' % key
        old_value = entry.get('value', '')
        entry['deleted'] = True
        entry['deleted_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
        save_state(session, state)
    return '✅ 已从状态板移除 [%s]（原值: %s；条目保留删除标志，可查 state 文件）' % (key, old_value)


def list_info(session):
    state = load_state(session)
    active = {k: v for k, v in state.items() if not v.get('deleted')}
    deleted = [k for k, v in state.items() if v.get('deleted')]
    if not active and not deleted:
        return '（空）当前 session 无状态信息（addinfo 写入）'
    lines = ['当前 session 状态信息 %d/%d 条:' % (len(active), MAX_KEYS)]
    for k, v in active.items():
        lines.append('  %s: %s  (%s %s)' % (
            k, v.get('value', ''), v.get('updated_by', '?'), v.get('updated_at', '')))
    if deleted:
        lines.append('  （另有 %d 条已删除存档: %s）' % (len(deleted), ', '.join(deleted)))
    return '\n'.join(lines)


def render_field(session):
    """渲染为 sys_prefix 的「状态信息:」块字段（条目行缩进两空格）。

    无条目返回 ''（不注入空字段）；调用方（agent.run_stream）直接
    嵌入 f-string，异常由调用方静默降级。
    """
    state = load_state(session)
    active = {k: v for k, v in state.items() if not v.get('deleted')}
    if not active:
        return ''
    lines = ['状态信息:']
    for k, v in active.items():
        lines.append('  %s: %s' % (k, v.get('value', '')))
    return '\n'.join(lines) + '\n'


def copy_state(src, dst):
    """复制整个状态板（fork 继承用，含软删除存档）；dst 已有状态时覆盖。返回条目数。

    双锁：src 读锁 + dst 写锁（先 src 后 dst，固定顺序防死锁；src==dst 短路）。
    """
    if src == dst:
        return len(load_state(src))
    with _state_lock(src):
        state = load_state(src)
        if state:
            with _state_lock(dst):
                save_state(dst, state)
    return len(state)
