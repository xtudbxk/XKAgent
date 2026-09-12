---
name: pythonrt_utils
version: 1.1.0
description: pythonrt 沙箱库安全判定与 hacking 引入指南
category: workflow
compatible_modes:
  - plan
  - build
  - build-unsafe
triggers:
  - pythonrt 库扩展
  - 沙箱库判定
  - 硬盘IO防控
  - register_extension
  - hacking 引入
  - dulwich 接入
  - 库导入被拒
requires: {}
author: system
open_source: true
---

## 概要

pythonrt_utils 教 LLM 两件核心事（对应两条主路径）：

```
① 库安全判定（audit）  —— 判断"某个库引入 pythonrt 沙箱是否会破坏硬盘IO防控"
② hacking 引入（hack） —— 通过 register_extension 扩展通道把指定库接入沙箱
```

本质：**先判得准，再引得对**。判定错了 → 引错库 = 逃逸面；判定对了但引入方式错 → 库不可用或沙箱崩溃。

---

## 🔀 与 preflight / pythonrt_prompt 的分工（2026-08-30）

pythonrt 知识体系分四层（总索引：项目根 `pythonrt_docs/ARCHITECTURE.md`）：

| 层 | 载体 | 职责 |
|----|------|------|
| L1 常驻注入 | 实例根 `pythonrt_prompt.txt`（agent.py 动态注入） | LLM 编写规范：骨架/硬规则/模板/失败归因 |
| L1 调用层兜底 | `codes/pythonrt_preflight.py`（tools.py 自动调用） | LLM 笔误自动修复：F1 补 import / F2 shutil+pathlib stub / F3 open 编码 / F4 删除改 .trash |
| L2 按需技能 | **本技能** | **新第三方库接入沙箱**：audit 安全判定 + hacking register_extension 五接入点 |
| L3 人类文档 | 项目根 `pythonrt_docs/` | usage_guide.md（诊断档案）+ ARCHITECTURE.md（总索引） |

判断规则：报"第三方模块被拒"时——shutil/pathlib 已被 preflight stub 兜底不会报；其余库三条出路顺序不变：stdlib 重实现 → build-unsafe → 本技能 audit+hacking。preflight 的 stub 是内置特例，任意新库仍走本技能路径。

---

## 🌳 树状文档结构

```
skills/pythonrt_utils/
├── skill.md                        ← 本文件（主入口 + 导航）
├── pythonrt_utils.overview.md      ← 1. 沙箱总览（威胁模型 + 五层防护 + 扩展通道）
├── pythonrt_utils.audit.md         ← 2. 库安全判定（需求1：如何判定不影响硬盘IO）
│   ├── 判定标准（路径级 vs fd级 / C扩展 vs 纯py / 危险依赖）
│   ├── 判定流程（6 步）
│   ├── 边界案例
│   └── 辅助脚本 audit_lib.py
├── pythonrt_utils.hacking.md       ← 3. hacking 引入（需求2：如何接入指定库）
│   ├── 五种接入点（黑名单/注册表/ImportGate/开关/setup/worker）
│   ├── setup 函数模式
│   ├── 依赖链打通（ModuleNotFoundError / stub / waitpid 恢复）
│   └── 辅助脚本 gen_extension.py
├── pythonrt_utils.examples.md      ← 4. 实战示例（sqlite3 / dulwich 全流程）
├── audit_lib.py                    ← 工具脚本：库安全审计
├── gen_extension.py                ← 工具脚本：生成接入骨架
└── example_dulwich.py              ← 示例脚本：dulwich 接入演示
```

---

## 🧭 Mode Pre-Check

进入正式流程前，先检测当前运行模式（决定脚本加载方式与写盘能力）。

| 检测方式 | 说明 |
|---------|------|
| 系统消息前缀「模式: xxx」 | 最可靠（plan / build / build-unsafe） |
| 尝试写项目目录 | plan 拒绝 / build 可写 / unsafe 无限制 |

| 检测结果 | 行为策略 |
|---------|---------|
| 🔵 **plan** | 脚本从 `/tmp/pythonrt_utils/` 用 importlib 动态加载；只读判定，不写项目目录 |
| 🟢 **build** | 脚本 `from skills.pythonrt_utils.xxx import ...`；可写项目目录 |
| 🔥 **build-unsafe** | 同上；audit_lib.py 可扫描 site-packages（workdir 外） |

---

## 📖 文件加载指令

| 时机 | 指令 | 说明 |
|------|------|------|
| 加载技能后 | 无需加载 | 常驻内容在 skill.md |
| 进入路径 1（判定） | `pythonrt 读取 skills/pythonrt_utils/pythonrt_utils.audit.md` | 库安全判定完整流程 |
| 进入路径 2（引入） | `pythonrt 读取 skills/pythonrt_utils/pythonrt_utils.hacking.md` | hacking 接入完整流程 |
| 需总览/背景 | `pythonrt 读取 skills/pythonrt_utils/pythonrt_utils.overview.md` | 威胁模型与防护架构 |
| 需实战参考 | `pythonrt 读取 skills/pythonrt_utils/pythonrt_utils.examples.md` | sqlite3 / dulwich 案例 |
| 需跑审计脚本 | `pythonrt` 调 `audit_lib.py` | 见 audit.md 用法 |
| 需生成接入骨架 | `pythonrt` 调 `gen_extension.py` | 见 hacking.md 用法 |

---

## 🛤️ 两条主路径（快速导航）

```
用户问："库 X 能不能引入沙箱？"
   │
   ▼
路径1: 安全判定（audit）
   │  读取 pythonrt_utils.audit.md
   │  audit_lib.py 扫描 X 源码 → 危险特征报告
   │  判定结论: 安全放行 / 需 stub / 需入口 patch / 拒绝
   ▼
路径2: hacking 引入（hack）
   │  读取 pythonrt_utils.hacking.md
   │  按五种接入点生成代码（gen_extension.py 辅助）
   │  修改 codes/sandbox.py + codes/tools.py 透传
   │  实测验证（examples.md 的验证清单）
   ▼
输出: 修改清单 + 验证结果 + 安全论证
```

---

## 📋 硬规则

1. **先判定后引入** —— 未经 audit 判定的库不得直接注册进沙箱
2. **注册与 setup 分离** —— 模块级注册（遍历前完成），setup 内**不得**增删 `_EXTENSIONS`（会触发 RuntimeError）
3. **ImportGate 抛错语义** —— 第三方拒绝抛 `ModuleNotFoundError`（兼容 try/except 回退），危险黑名单抛 `ImportError`（不可静默绕过）
4. **subprocess 只 stub 不删 import** —— 保留 Popen 类（泛型注解依赖），替换 `__init__` 与 call/run 等入口
5. **os 手术只放宽必要项** —— open/fdopen/read/write/getpid/getuid 恢复需逐个论证"无逃逸面"
6. **辅助脚本可独立运行** —— 提供 `if __name__ == "__main__"` 入口
7. **函数返回字符串** —— 复杂数据 JSON 序列化
8. **不改业务文件** —— 只动 `codes/sandbox.py` / `codes/tools.py` / `system_prompt.txt`，其他文件先问
