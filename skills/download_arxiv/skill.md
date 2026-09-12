---
name: download_arxiv
version: 1.0.0
description: 从 arXiv 下载论文 TeX 源码/PDF 到本地目录
category: tool
compatible_modes:
  - plan
  - build
  - build-unsafe
triggers:
  - 下载arxiv
  - arxiv下载
  - 下载tex源码
  - download_arxiv
  - download arxiv
author: system
open_source: true
requires: {}
---

## 概述

从 arXiv 下载论文的 **TeX 源码**（默认）或 **PDF**（可选）到本地目录，并将 TeX 源码解压为可读的 `.tex` 文件结构，供论文阅读/讨论工作流（如 paper-discuss）使用。

- **默认下载**：TeX 源码（`https://export.arxiv.org/e-print/{id}`）
- **可选下载**：PDF（`--pdf`，`https://export.arxiv.org/pdf/{id}`）
- **默认落盘**：`<workdir>/docs/arxiv/`（可通过 `--output-dir` 覆盖）
- **落盘结构**（对齐 paper-discuss 约定）：

```
{output_dir}/{arxiv_id}/
  tex/
    {arxiv_id}.source      # 原始归档（tar.gz / gz / 单 tex）
    unpacked/              # 解压后的 .tex / figures / .sty ...
  {arxiv_id}.pdf           # 仅 --pdf 时
```

## 🧭 Mode Pre-Check

进入正式流程前，先检测当前运行模式（从系统消息「模式: xxx」判断）。

| 模式 | 脚本位置 | 落盘行为 |
|------|---------|---------|
| 🔵 **plan** | `/tmp/download_arxiv/download_arxiv.py`（或内联） | 提示用户下载到 `/tmp/`（不持久化），或仅输出预览/URL 列表 |
| 🟢 **build** | `skills/download_arxiv/download_arxiv.py` | 正常下载到 `<workdir>/docs/arxiv/` |
| 🔥 **build-unsafe** | `skills/download_arxiv/download_arxiv.py` | 同上，无任何限制 |

> ⚠️ **plan 模式提示**：plan 下项目目录只读，下载产物写入 `/tmp/`（重启不保留）。若需持久化，请用户切换 build / build-unsafe 后重新执行。

## 执行流程

### Phase 1: 解析 arXiv ID

从用户输入中提取 ID，支持以下形式：
- 纯 ID：`2406.02507`、`1904.06991`（可带版本 `2406.02507v2`）
- URL：`https://arxiv.org/abs/2406.02507`、`/pdf/...`、`/e-print/...`
- 非法格式（如 `abc`）→ 直接报错并回问用户

> 旧版 4 位 ID（如 `2406.0250`）同样合法，兼容。

### Phase 2: 确定目标目录

- 默认：`<workdir>/docs/arxiv/`（相对路径，解析到执行时 CWD）
- 用户可指定 `--output-dir /path/to/dir`
- 目录不存在则自动创建

### Phase 3: 下载 TeX 源码（默认）

调用 `download_arxiv(arxiv_id)`：
1. 下载 `https://export.arxiv.org/e-print/{id}` → `{out}/{id}/tex/{id}.source`
2. 自动解压到 `{out}/{id}/tex/unpacked/`：
   - `.tar.gz` / `.tar` → tarfile 解压（防路径穿越）
   - `.gz` 单文件 → gzip 解压为 `{id}.tex`
   - 纯 `.tex` → 直接复制
3. 失败自动重试 2 次（指数退避）

### Phase 4: 下载 PDF（可选，`--pdf`）

调用 `download_arxiv(arxiv_id, pdf=True)` → `{out}/{id}/{id}.pdf`

### Phase 5: 校验与摘要

返回 JSON 摘要：`status`（ok/partial）、各文件路径与大小、解压文件数、错误列表。
- `status: ok` → 向用户报告落盘位置
- `status: partial` → 报告已成功部分 + 失败原因（如 arXiv 无源码 → 建议 `--pdf`）

## 工具脚本

`skills/download_arxiv/download_arxiv.py` — 单脚本完成全部逻辑（stdlib only）。

### 沙箱兼容性说明

> ⚠️ **受限沙箱（plan / build）实测**：`urllib.request` / `tarfile` / `shutil` / `tempfile` 以及第三方 `requests` / `urllib3` / `certifi` 均被沙箱拦截（import 链触发 `[sandbox] 危险模块被禁: posix`），**不可用**。
> 本脚本因此采用**双路径设计**：
> - **受限沙箱**：自动 fallback 到 `http.client`（手动跟随 301 重定向）+ 纯 stdlib 手动 tar 解析器（支持 GNU 长文件名 / PAX / 目录 / 符号链接跳过 / 路径穿越防御）
> - **宿主环境 / build-unsafe**：优先使用标准 `urllib.request` + `tarfile`（原逻辑，功能更全）
> 两种路径的调用接口完全一致，LLM 无需感知差异。

### Python 调用

```python
from skills.download_arxiv.download_arxiv import download_arxiv
result = download_arxiv("2406.02507")                # 仅 TeX 源码
result = download_arxiv("https://arxiv.org/abs/2406.02507", pdf=True)  # + PDF
# 可选参数: output_dir="docs/arxiv", unpack=True/False, timeout=120
# 返回 JSON 字符串（LLM 消费）
```

### CLI 调用

```bash
python skills/download_arxiv/download_arxiv.py 2406.02507
python skills/download_arxiv/download_arxiv.py 2406.02507 --pdf --output-dir ~/papers
python skills/download_arxiv/download_arxiv.py 2406.02507 --no-unpack
```

### plan 模式脚本加载（脚本在 /tmp 时）

```python
import importlib.util, sys
spec = importlib.util.spec_from_file_location("dax", "/tmp/download_arxiv/download_arxiv.py")
mod = importlib.util.module_from_spec(spec); sys.modules["dax"] = mod
spec.loader.exec_module(mod)
result = mod.download_arxiv("2406.02507", output_dir="/tmp/arxivs")
```

## 边界与错误处理

| 场景 | 处理 |
|------|------|
| arXiv ID 非法 | 抛 ValueError，回问用户 |
| 网络失败 | 自动重试 2 次，仍失败报错 |
| 论文无 TeX 源码 | 报 partial，建议加 `--pdf` |
| 输出目录不可写 | 报错并提示切换 build-unsafe 或指定可写 `--output-dir` |
| tar 路径穿越 | 解压前过滤 `..` / 绝对路径成员 |
| 已有同 ID 目录 | 覆盖式写入（`.source` 重新下载），不删除旧 `unpacked` |

## 与 paper_lib 的对接（可选修复）

`scripts/paper_lib.py` 的 `download_tex()` 目前依赖一个未随仓库附带的外部脚本，恒返回 `False`。可将 `download_tex` 的下载逻辑改为调用本技能脚本（`python skills/download_arxiv/download_arxiv.py <id> --output-dir <paper_dir>`），从而复用同一套下载实现。
