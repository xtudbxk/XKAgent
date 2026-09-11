# 07 · Skills

[简体中文](07-技能系统.md) | English

---

A Skill is a set of working instructions kept outside the Agent core. It can describe domain knowledge, operating procedures, checklists, and stopping conditions, allowing XKAgent to learn new ways of working without changes to the main loop. A Skill is a prompt extension: it does not automatically register Python tools or bypass the current mode and path permissions.

## Where Skills Live

Each Skill occupies one directory with a fixed entry-point filename, `skill.md`. The loader examines only direct child directories of the Skill roots and searches them in this order:

```text
<workdir>/.xkagent/skills/<技能名>/skill.md   # 项目自定义，优先
<XKAgent代码根>/skills/<技能名>/skill.md      # 内置技能，兜底
```

The directory name is the Skill name. Placing a Skill with the same name in the project directory overrides the built-in version, while `/skills` still lists it only once. If the custom version is removed, the loader falls back to the built-in version. This supports project-specific customization without directly modifying the Skills bundled with XKAgent.

## Writing a Skill

`skill.md` consists of frontmatter followed by a body. The following is a minimal working example:

```markdown
---
name: release_check
version: 1.0.0
description: 发布前检查版本、测试与变更说明
category: workflow
triggers:
  - 发布检查
  - release
---

执行发布任务前：
1. 确认工作区状态。
2. 运行项目测试。
3. 检查版本号和变更说明。
4. 发现风险时先报告，不直接发布。
```

After saving it to `.xkagent/skills/release_check/skill.md`, run `/validate release_check` first. The body is best used for goals, steps, decision criteria, and risk notices. Rather than listing functions, explain what the Agent should do under specific conditions.

### Frontmatter Conventions

`/validate` checks four required fields:

- `name`: Must exactly match the directory name.
- `version`: Must use semantic versioning in the form `1.2.3`.
- `description`: Briefly explains the problem the Skill solves.
- `category`: Must be either `tool` or `workflow`.

`triggers` is an optional list of common Chinese or English trigger terms. The parser supports only a simple YAML subset, including `key: value`, indented lists, and one level of child keys. Do not rely on inline arrays, anchors, complex types, or deep nesting.

## How a Skill Enters a Turn

At the start of a normal message, the system locally matches against the Skill body and the frontmatter `description` and `triggers`, then places candidate Skills in Status (no extra model call). To follow a Skill workflow, the Agent loads the full text of a Skill with the `selectskill` tool; after the same version has been injected in full, later turns use a content anchor to reduce repeated token usage.

Common commands include:

- `/skills`, `/showskills`: List currently loadable Skills.
- `/validate [names]`: Validate all Skills or the named Skills.
- `/skill <name>`: Read a Skill and add it to the current Agent's active Skill list.

`/skill` and the in-turn `selectskill` path are independent. The current implementation does not treat `/skill` as "force this Skill to run on the next turn." To verify a Skill's behavior, send a real task and observe the candidate Skills and the final load result shown in the UI.

## When Changes Take Effect

Skills support hot loading and do not require a process restart. The body is read again on every load. Frontmatter is cached by file modification time and size, then reparsed automatically when the file changes. After creating or deleting a Skill directory, run `/skills` again to see the updated result.

Several implementation boundaries are worth noting:

- Loading a Skill does not automatically validate its format; run `/validate` after making changes.
- `__init__.py` is not imported, and code in the directory does not automatically become a tool.
- Skill loading and retrieval share the same directories (user-level `.xkagent/skills` first, built-in `skills` as fallback), keeping both sides consistent.
- Skills affect only prompts and workflows. Actual read and write capabilities remain governed by `plan`, `build`, `build-unsafe`, and mount configuration.

## Related Documentation

- [05 · Sandbox and Tool Execution](05-sandbox-and-tools.md): permission boundaries while a Skill is in use
- [06 · Agent Engine](06-agent-engine.md): Skill selection, injection, and turn execution
- [08 · Search and Memory](08-search-and-memory.md): candidate Skills and information retrieval
- [09 · Command System](09-commands.md): Skill-related commands
