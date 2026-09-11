---
name: playwright_web
version: 2.0.0
description: 用 Playwright 无头浏览器自动化抓取网页与交互
category: workflow
triggers:
  - playwright
  - 网页抓取
  - 浏览器自动化
  - web 抓取
  - headless
  - 网页交互
compatible_modes:
  - build-unsafe
requires:
  pip:
    - playwright
author: system
---

# Playwright 网页自动化流程（v2：一次性任务 + CDP 常驻守护）

> ⚠️ **适用范围**：仅 build-unsafe 模式可执行（Playwright 需 pip 安装 + 下载 Chromium + 运行浏览器进程）。
> 在 plan / build 模式下：先提示用户切换到 build-unsafe，再继续本流程。

## 概述

用 Playwright（Python, headless Chromium）在后台自动处理网页：抓取内容、执行交互（点击/填写/提交）、截图、提取结构化数据、管理登录态，并把产物落盘供后续分析。适合「后台批量处理网页」场景（爬取、表单自动化、页面验证、数据提取）。

## v2 常驻守护（pw_daemon.py）—— 跨 turn 持久化同一浏览器实例

v1 每任务新启浏览器、结束即关；v2 通过 CDP 常驻 Chromium（`start_new_session` 脱离
pythonrt 进程树），多个 LLM turn 间复用同一实例：**页面、JS 内存态、登录态全保留**。

### 工作流（多 turn 协作）

| 时机 | 操作 |
|------|------|
| 首次 turn | `d.start()`（幂等）→ `d.open_page(url)` 开页面 → 本调用结束（浏览器存活） |
| 后续 turn | `d.status()` 确认 → `d.list_pages()` 按 url 找回页面 → `d.act(page, actions)` 继续操作 |
| 收尾 | `d.stop()`（SIGTERM 优雅退出；profile 保留 → 重启后登录态不丢） |

```python
from skills.playwright_web import pw_daemon as d

d.start()                                   # -> {"running": true, "pid": ..., "port": 9222}
d.open_page("https://example.com")          # 页面跨 turn 存活
# ---- 新的 pythonrt 调用（全新进程）----
d.status()                                  # pid 与上次一致
d.list_pages()                              # -> [{"url": "https://example.com", ...}]
d.act("example.com", '[{"action":"extract","selector":"h1"},{"action":"screenshot","name":"s"}]')

# turn 内直接操控页面（connect 返回 (playwright, browser)；browser.close() 仅断连不杀浏览器）
p, browser = d.connect()
try:
    print(browser.contexts[0].pages[-1].title())
finally:
    browser.close(); p.stop()

# 宕机/需重置时重启（profile 保留 → 登录态不丢）
d.restart()
d.stop()                                    # 收尾
```

### 关键语义
- `connect_over_cdp` 的 `browser.close()` **只断开本连接，不杀浏览器**——turn 内用完直接 close
- `d.act()` 复用 v1 动作语义（goto/click/fill/select/press/wait/screenshot/extract/eval/save_state）
- 页面定位：`page=None` 最后活动页 / `int` 全局序号 / `str` url 子串
- **重启后动态页面不保留**（页面不持久）；登录态/cookies/localStorage 经 profile 持久
- 状态目录 `/tmp/pw_daemon/`：`daemon.json`（pid/port）、`profile/`（user_data_dir）、`chrome.log`、`out/<ts>/`（产物）
- ⚠️ `/tmp` 不随容器持久化：容器重启后需重新 `start()`（如需跨重启保登录态，`start(profile_dir=<持久路径>)`）
- ⚠️ 常驻实例占 ~300MB 内存；`status()` 随时健康检查，宕机后 `start()` 起新实例

## 前置准备（一次性）

```bash
# 用户执行（或 build-unsafe 下 pythonrt subprocess）
pip install playwright
playwright install chromium        # 下载浏览器（约 170MB）
# Linux 如缺系统库: playwright install-deps chromium  (root 权限)
```

## 执行流程

### Phase 1: 环境检查

用 pythonrt 运行环境探测脚本，确认就绪状态：

```python
# build-unsafe 下直接 import（技能仅 build-unsafe 可用，无沙箱限制）
from skills.playwright_web.env_check import check_env
print(check_env())   # -> {"ready": true|false, "checks": [...], "fix": [...]}
```

- 检查项：Python 版本（≥3.10）、playwright 是否安装、Chromium 是否就位（executable path / ms-playwright 缓存）
- 返回 JSON（`ready: true` 或 `false` + 修复指引）
- 未就绪 → 按指引安装后重跑；用户环境无 pip/无网络 → 停止并告知

### Phase 2: 任务解析（LLM 做，不脚本化）

从用户输入解析出任务参数：

| 参数 | 说明 | 默认 |
|------|------|------|
| urls | 目标 URL（单个或多个） | 必填 |
| actions | 动作序列（可选）：`goto/click/fill/select/press/wait/screenshot/extract/eval/save_state` | 无（纯抓取） |
| output_dir | 产物目录 | `_playwright_out/` |
| storage_state | 登录态文件路径（复用已保存的 cookies/localStorage） | 无 |
| timeout | 每页超时（秒） | 30 |
| retry | 失败重试次数 | 2 |
| headless | 是否无头 | true（后台场景保持 true） |

动作序列格式示例（actions JSON）：

```json
[
  {"action": "goto", "url": "https://example.com/login"},
  {"action": "fill", "selector": "#user", "value": "demo"},
  {"action": "fill", "selector": "#pass", "value": "***"},
  {"action": "click", "selector": "button[type=submit]"},
  {"action": "wait", "ms": 2000},
  {"action": "extract", "selector": ".dashboard"},
  {"action": "screenshot", "name": "dashboard"}
]
```

### Phase 3: 执行（脚本化）

```python
from skills.playwright_web.web_runner import run_fetch, run_actions, save_storage_state

# 纯抓取（单/多 URL）
print(run_fetch(urls=['https://a.com', 'https://b.com'], output_dir='_playwright_out'))

# 交互任务
print(run_actions(url='https://example.com', actions_json='[...]', output_dir='_playwright_out'))

# 登录态：先运行保存（人工或 actions 登录后），后续任务复用
print(save_storage_state(output_dir='_playwright_out'))
# 复用时传 storage_state='_playwright_out/auth.json'
```

产物结构（每个任务一个时间戳目录）：

```
_playwright_out/<ts>/
  ├─ summary.json      # 每 URL/动作的状态、耗时、产物清单、错误（JSON 摘要）
  ├─ page.html         # 渲染后 DOM（每 URL 一个, 或 <name>.html）
  ├─ page.txt          # inner_text 提取（每 URL 一个）
  └─ <name>.png        # 截图（可选）
```

### Phase 4: 结果解读（LLM 做）

- 读取 `summary.json` + `page.txt` 给 LLM 做语义分析（提取/总结/对比）
- 需要看页面视觉 → 读 `*.png`（需切多模态模型或子 agent 识别）
- 结果直接落盘，不回灌浏览器

### Phase 5: 收尾

- 汇报摘要（成功/失败 URL、产物路径、耗时）
- 失败项按 summary.json 的 error 字段归因（超时/选择器未找到/验证码），重试或告知用户
- 成功后无需关闭浏览器（脚本内已自动关闭）

## 边界与禁忌

1. **登录/验证码**：Playwright 自动化被部分网站识别；登录页/CAPTCHA 需人肉介入 → 用 `save_storage_state` 保存人工登录后的态，或提示用户手动完成
2. **反爬**：headless 默认 UA 易被识别 → 可设置真实 UA/视口；需要代理/stealth 时参考 browserless/browser-use 云方案（本技能不含）
3. **资源**：每次执行启动独立 Chromium（约 300MB 内存），批量任务用单进程串行（本脚本默认串行）；不要并发开过多浏览器；v2 常驻实例常驻期同样占 ~300MB，用完 `stop()` 回收
4. **不要**：执行未知来源的 actions JSON（eval 动作可执行页面内 JS）；用本技能做绕过验证码/违规抓取
5. **非 build-unsafe**：脚本 import playwright 会失败 → 一切都先切模式

## 脚本清单

| 脚本 | 职责 | 依赖 |
|------|------|------|
| env_check.py | 环境探测（Python/playwright/Chromium） | playwright（需 build-unsafe） |
| web_runner.py | 抓取/交互/截图/登录态/重试/落盘 | playwright（需 build-unsafe） |
| pw_daemon.py | v2 CDP 常驻守护（跨 turn 持久化实例） | playwright（需 build-unsafe） |
