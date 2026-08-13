# 边界条件与错误处理

> 📎 **加载条件**：仅当遇到以下边缘/错误情况时加载。
> 使用 `pythonrt 读取 skills/makeskill/makeskill.faq.md`

## 常见问题

### Q1: 技能目录已存在

```
情景: 新建模式，但 skills/<name>/ 已存在
处理: 询问用户是否覆盖
  - 覆盖 → 先备份原 skill.md 为 skill.md.bak，再写入新文件
  - 跳过 → 终止当前操作
```

### Q2: session 名称与其他文件冲突

```
情景: 技能名包含非法字符（大写字母、空格、中文等）
处理: 只允许 [a-z0-9_]，提示用户修改
```

### Q3: DB 文件无法读取

```
情景: 结构化模式下读取目标 skill.md 时发现文件损坏
处理: 尝试读 .bak 备份，如果都损坏则报告错误并终止
```

### Q4: 生成的脚本语法错误

```
情景: Phase 6 验证时发现 pythonrt 返回 SyntaxError
处理: 定位到错误行，修复后再验证，循环直到通过
```

### Q5: 结构化拆分后导航树不一致

```
情景: 导航树中引用的子文件与实际生成的不匹配
处理: 重新生成导航树，确保每个子文件在导航表中有一行对应
```

### Q6: makeskill 自身在 plan 模式下运行，无法写入文件

```
情景: 用户在 plan 模式下调用 makeskill 新建技能
处理: 
  1. 按正常流程完成 Phase 1-4（纯对话，无需写文件）
  2. Phase 5 时检测到当前模式为 plan
  3. 输出 skill.md 和脚本的完整内容预览（纯文本）
  4. 提示用户: "如需实际写入文件，请切换至 build 模式"
  5. 用户切换后，重新执行 Phase 5 写入

注意: 即使在 plan 模式，Makeskill 仍可为「目标运行模式=build」的技能做规划，
     只是不能实际写入文件。
```

### Q7: 用户选择了多模式，但不知道适配细节

```
情景: Phase 1 选了 plan + build 多模式，但 Phase 4 填写模式适配细节时卡住
处理: 
  1. 引导用户思考: "脚本在 plan 下存在 /tmp/，在 build 下存在 skills/，这个差异需要什么额外处理？"
  2. 提供默认降级策略: "plan 只读输出预览，build 正常写文件"
  3. 如果用户不确定，建议简化为单模式
```

### Q8: 生成的 skill 在 plan 模式下无法 import 脚本

```
情景: 目标模式为 plan，生成的 skill.md 要求 from skills.<name> import func
     但 plan 模式下 skills/ 目录是只读的，脚本可能不存在
处理:
  生成的 skill.md 应为 plan 模式使用 importlib 动态加载:
  
  ```python
  import importlib.util
  spec = importlib.util.spec_from_file_location("cond", "/tmp/<name>/<script>.py")
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  ```
```

### Q9: compatible_modes 只出现在元数据中，但系统如何识别？

```
情景: 生成的 skill.md 中有 compatible_modes 字段，但用户不知道有什么用
说明:
  - compatible_modes 是 frontmatter 的元数据字段
  - 供其他技能/系统/框架读取，判断该技能在什么模式下可用
  - 格式: 
    compatible_modes:
      - plan
      - build
  - 系统将来可能基于此字段做模式路由或兼容性检查
```
