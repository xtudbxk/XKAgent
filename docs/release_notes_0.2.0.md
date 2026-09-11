# XKAgent 0.2.0 更新说明（简易版）

> 本版汇总自上次公开发布以来的主要变化；完整细节见 `.xkagent/docs/` 专题文档。

## 一、多 Agent 通信（新增）

- 新增全局邮件总线 `mail.jsonl`：append-only、字节游标、原子锁与状态机（send → delivered → done｜failed → dead｜rejected）；
- 新增轮次邮差 MailPostman：两阶段投递、`turn_id` 盯梢、孤儿兜底重投（重试上限 2）、线程自愈；
- 新增 `callagent` 工具：全异步跨会话邮件；支持延迟/定点投递、优先级、回复链、广播、收信方 provider 覆盖与回信指引；
- 配套 `/mail` 管理命令与三条标准流程技能（定时唤醒 / 信息交换 / 长程托管）。
- 文档：[11 · 多 Agent 通信](../.xkagent/docs/11-多Agent通信.md)；设计稿：`docs/mail_longtask_plan.md`。

## 二、存储层升级：SQLite → msgz 单文件（新增）

- 会话存储改为单文件 `<session>.msgz`（zlib 压缩 JSON）：内存主数据 + 30 秒定时同步 + 原子替换落盘；
- 动机：WAL 多文件在 CubeFS 等网络文件系统满卷/扩容窗口期易出现 EIO；msgz 写入先在内存完成，存储故障不阻塞对话，搬运/备份只需一个文件；
- 兼容：旧 `*.db` 文件保留为只读历史；`codes/msgz_migrate.py` 提供一次性迁移脚本。
- 文档：[03 · 基础设施](../.xkagent/docs/03-基础设施.md)。

## 三、Status Info（状态信息）升级与支持（新增）

- 新增**会话级 KV 状态板**：`addinfo` / `listinfo` / `rminfo` 工具与 `/addinfo` / `/listinfo` / `/rminfo` 命令双通道维护，存储于 `.xkagent/state/<session>.json`；
- **每回合随状态注入**：非空时以「状态信息:」块附加在用户消息头部；`/compact` 压缩后依然生效，是跨回合可靠的运行态记忆；
- 与 `summary` 分工明确：状态信息＝短小运行态 KV（进度 / 偏好 / 约定 / 教训），结论与长文本走检索式 `summary` 文档；
- 并发安全：同会话写入以 lockdir 互斥 + 唯一临时名 + 原子替换；删除为软删除（存储保留存档）。

## 四、内置技能扩充（新增）

- `paper_collect`：多 Agent 并行收集 arXiv 论文并生成中英双语 HTML 清单；
- `paper_discuss`：arXiv 论文定位、深读、对比与下载；
- `pdfreader`：PDF 轻量提取、图片抽取、概览生成；
- `playwright_web`：Playwright 无头浏览器自动化；
- `callagent_workflows`：callagent 三流程完整模板；
- 另有 `scheduled_task` 等。

## 五、其他改进（简述）

- Web：会话标题与置顶、文件在线预览页、文件路径解析增强、访问日志完善；
- 上下文工程：自动压缩默认开启（默认阈值 60 万 tokens）、工具输出截断（100KB + 尾 2KB）、超限自动修剪重试；
- 邮件修复：重投计数修正（防无限重投）、`mail_meta` 信封回信指引、`callagent` provider 指定；
- 工程与文档：README 与主题文档更新、`.gitignore` 整理、`tests/` 补充（mail / 空闲回收）。

## English (brief)

XKAgent 0.2.0 adds a multi-agent mail system (global `mail.jsonl` bus, MailPostman carrier, and the `callagent` tool), switches session storage to a single-file msgz format (zlib-compressed JSON, memory-first with atomic persistence; legacy SQLite files remain readable and can be migrated via `codes/msgz_migrate.py`), adds a session-level Status Info board (`addinfo` / `listinfo` / `rminfo`) injected on every turn and preserved across compaction, and expands built-in skills (`paper_collect`, `paper_discuss`, `pdfreader`, `playwright_web`, `callagent_workflows`, and more). Other changes include web session titles/pins and an online file preview page, context-engineering improvements (auto-compaction, tool-output truncation, overflow recovery), and mail fixes. See the docs for details.
