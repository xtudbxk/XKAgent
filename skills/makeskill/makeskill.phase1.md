## Phase 1: 需求澄清

### ⚠️ 流程说明

1. 逐条问用户以下问题，确认后再进入下一项
2. 每条问题的答案会影响后续多个 Phase 的决策

---

### 0. 复杂度分级（仅新建模式必答）

| 级别 | 判断标准 | 后续路径 |
|------|---------|---------|
| 🪶 轻量 | 单一流程、步骤 ≤ 8、无脚本、单模式 | 跳 Phase 3 详细脚本化分析与 Phase 7 |
| 🏋️ 完整 | 多流程、需脚本化、多模式兼容 | 走完整 7 阶段 |

### 1. 新建、规范化还是结构化？

| 选项 | 后续操作 |
|------|---------|
| **新建** — 从零创建一个技能 | ✅ 进入 Phase 2 前，先加载模式详解：`pythonrt 读取 skills/makeskill/makeskill.modes.md` |
| **规范化** — 已有技能目录，需修复格式/补充 frontmatter | ✅ 直接进入 Phase 2 |
| **结构化** — 现有 skill.md 按访问模式拆分，按需加载 | ✅ 直接跳转 Phase 7：`pythonrt 读取 skills/makeskill/makeskill.phase7.md` |

### 2. 如果是新建 — 技能名称是什么？
- 只允许小写字母、数字、下划线
- 名称即目录名，如 `web_search`、`code_review`

### 3. 如果是规范化 — 目标技能名是什么？
- 必须是 `skills/` 下已存在的目录

### 4. 如果是结构化 — 目标技能名是什么？
- 必须是 `skills/` 下已存在的目录
- 技能目录内需有 `skill.md` 文件

### 5. 一句话描述（description 字段，30 字以内）

### 6. 目标运行模式是什么？（新建/规范化时必填，结构化可选）

这个技能预期在哪些模式下运行？影响 Phase 2-7 的所有决策。

各模式的真实能力边界：

| 模式 | 项目目录 | `/tmp` | pythonrt | 用户 !xxx / 无限制 |
|------|---------|--------|------------|----------------|
| 🔵 **plan** | ❌ 只读 | ✅ 可写临时文件 | ✅ 主执行途径 | ❌ 无 |
| 🟢 **build** | ✅ 可读写 | ✅ 可写 | ✅ | ✅ bash沙箱（受限） |
| 🔥 **build-unsafe** | ✅ 可读写 | ✅ 可写 | ✅ 无限制 | ✅ pythonrt 无限制（或用户 !xxx） |

> 📌 **执行统一性**：LLM 工具侧所有脚本/代码一律通过 **pythonrt** 调用执行
> （沙箱双路径：plan/build 用 importlib 动态加载，build-unsafe 直接 import，见 rules_detail 规则 13），不依赖 bash/系统命令；bash 仅限用户 `!xxx` 或 build-unsafe 宿主侧手动使用。

> ⚠️ **影响范围**：这个答案贯穿所有阶段：
>   - **Phase 2**：模式影响 workflow / tool 的选择倾向
>   - **Phase 3**：模式影响脚本化方案（plan 下脚本存 `/tmp/`）
>   - **Phase 4**：元数据中需记录 `compatible_modes`
>   - **Phase 5**：单模式 → 简洁版；多模式 → 生成 Mode Pre-Check
>   - **Phase 7**：模式影响子文件的参考内容

#### 选择方式

| 选择 | 含义 | 生成策略 |
|------|------|---------|
| **单选**（如仅 build） | 只在一个模式下运行 | 简洁版 skill.md，无模式分支 |
| **多选**（如 plan + build） | 跨模式运行 | 生成 Mode Pre-Check 阶段，各模式自适应 |

---

### 7. 落盘位置（新建/规范化时必填）

新技能（或规范化后的技能）放到哪个技能库？

| 选择 | 落盘路径 | 可见范围 | 写入方式 |
|------|---------|---------|---------|
| **A. 用户级** | `.xkagent/skills/<name>/` | 仅当前项目会话 | `.xkagent` 全模式只读 → 宿主 `!cmd` 复制 |
| **B. 系统级** | `xkagent_v0902/skills/<name>/` | 全局所有会话 | 挂载 ro/rw 后 pythonrt 直接写 |

> ⚠️ **禁止**默认落盘到工作目录 `skills/`（不在 `_skill_dirs`，不被技能系统索引，`searchskill` 搜不到）。
> 记录选择为 `skill_location`，贯穿 Phase 5 落盘与 Phase 6 验证；若为 B 系统级，还须确认并记录开源属性 `open_source`（见 §7.1）。

#### 7.1 开源属性（仅落盘位置 = B 系统级时必答）

代码目录 `xkagent_v0902/skills/` 会随 XKAgent 公开仓库发布，因此系统级技能必须区分开源属性：

| 选择 | 含义 | 标注（写入 skill.md frontmatter） |
|------|------|----------------------------------|
| **可开源** | 可随公开仓库发布 | `open_source: true` |
| **不可开源** | 含内部信息，发布前须从公开提交中排除 | `open_source: false` + 建议 `open_source_note: <原因>` |

> 📌 拿不准时按「不可开源」处理更安全（后续可再改）；规范化已有技能时：已标注则确认/更新，缺失则补问。
> 记录为 `open_source`，由 Phase 5 写入 frontmatter，Phase 6 校验（缺失 → FAIL）。

#### 影响范围

- **Phase 5**：按落盘位置分支执行（用户级 → 宿主 `!cmd` 复制；系统级 → 挂载直写 + 写入 `open_source` 标注）
- **Phase 6**：落盘后用 `searchskill` 验证新技能名可检索；系统级技能校验 `open_source` 存在（缺失 → FAIL）

---


### 输出格式

```
🎯 makeskill — Phase 1/6: 需求澄清

📝 需求总结：
  操作类型:    新建
  技能名:      code_review
  描述:        对代码进行自动审查并输出质量报告
  主类型:      [待确认 — 下一步]
  目标模式:    build（单选）
  --- 或 ---
  目标模式:    plan + build（多模式兼容）
  落盘位置:    [A 用户级 .xkagent/skills | B 系统级 xkagent_v0902/skills]
  开源属性:    [可开源 open_source:true | 不可开源 open_source:false]（仅 B 系统级必填）

以上是否正确？如有补充请说明。
```

用户确认后，根据选择的模式执行后续阶段。

> 🔀 **分支提示**：如果用户选了「新建」，请先执行 `pythonrt 读取 skills/makeskill/makeskill.modes.md` 加载新建模式详解，再进入 Phase 2。
