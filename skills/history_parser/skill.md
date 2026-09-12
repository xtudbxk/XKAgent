---
name: history_parser
version: 1.1.0
description: 从 session 历史提取消息，多模式查询输出
category: tool
compatible_modes:
  - plan
  - build
  - build-unsafe
triggers:
  - 历史提取
  - 消息查询
  - session
  - history_parser
  - 提取会话
  - 查看历史
  - 对话记录
  - 提取消息
requires: {}
author: system
open_source: true
---

## 概述

history_parser 是一个 **tool 类型技能**，提供从 `historys/` 目录下的 SQLite 数据库中提取历史消息的能力。每个 session 为一个 `.db` 文件，单表 `messages` 存储所有对话记录。

## 数据源结构

| 项目 | 详情 |
|------|------|
| 数据库目录 | `historys/` |
| 数据库格式 | SQLite，每 session 一个文件 |
| 文件命名 | `{session_name}.db` |
| 表名 | `messages` |
| 字段 | `id`, `role`, `content`, `extras`(JSON), `turn`, `created_at` |
| role 取值 | `user` / `assistant` / `tool` / `compact` / `git` |

## 工具清单

| 脚本 | 职责 |
|------|------|
| `session_scanner.py` | Session 发现与概览 |
| `message_extractor.py` | 核心提取引擎（7 种模式） |
| `extras_parser.py` | extras JSON 字段解析 |
| `wal_reader.py` | WAL 安全连接（自动合并 WAL + 只读降级） |

## WAL 安全读取说明

WASM 沙箱中 SQLite 数据库因 WAL 模式（`.db-wal`/`.db-shm`）无法直接连接。
`wal_reader.py` 提供三层降级策略：

| 策略 | 说明 |
|------|------|
| 1️⃣ 直接连接 | 正常环境直接 `sqlite3.connect()` |
| 2️⃣ 改版本标记 + deserialize | 只读 `.db` 文件到内存，绕过 WAL 锁 |
| 3️⃣ 合并 WAL 帧 + 2️⃣ | 先合并 `.db-wal` 帧到 `.db` 字节，再加载到内存 |

其他三个脚本（`session_scanner.py`、`message_extractor.py`、`extras_parser.py`）
均已改用 `wal_reader.safe_get_conn`，无需手动处理。

## 文件引用

> [.modes.md] 7 种提取模式详解（含参数、代码示例）
> [.examples.md] 典型工作流 + 使用示例 + 注意事项
