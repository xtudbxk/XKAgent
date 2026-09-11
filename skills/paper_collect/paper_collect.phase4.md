# Phase 4：中文翻译（agent 分批）

## 目标
将标题 + 完整摘要翻译为简体中文。

## 流程
1. 切块：完整摘要按 50 篇/批写入 /tmp/abs_chunk_XX.json（[arxiv_id, {title, abstract, url}]）
2. 派发 agent 子任务（每批一个 agent）：
   - 读 /tmp/abs_chunk_XX.json → LLM 翻译 → 写 /tmp/abs_zh_XX.json
   - 关键：产出写 /tmp 文件（勿回传大 JSON，防超长/超时）
   - max_steps 15~25，timeout 900s
3. 超时补齐：agent 超时可能部分落盘，读已有文件找缺失 ID，单独派发补译
4. 合并：全部 abs_zh_XX.json → 合并 dict → arxiv_abstracts_zh.json

## 子 agent 任务书模板

```
你的任务：把一批 arXiv 论文的完整英文摘要翻译为简体中文，并写入 /tmp 文件。

数据源：读取 `/tmp/abs_chunk_XX.json`（JSON 数组，每项为 [arxiv_id, {"title": ..., "abstract": ..., "url": ...}]，共 50 项）。

翻译要求：
- 将每项的 abstract 翻译为专业、通顺的简体中文
- 保留学术术语；专有名词/模型名/方法名保留英文原词（GAN、VAE、diffusion 等）
- 不要省略，完整翻译整段摘要

执行步骤：
1. 用 pythonrt 读取 /tmp/abs_chunk_XX.json，打印全部 50 项的 arxiv_id 和 abstract
2. 你（LLM）逐篇翻译，产出 dict：{"<arxiv_id>": "<中文摘要>", ...}
3. 用 pythonrt 分 3~5 次把结果写入 /tmp/abs_zh_XX.json（每次写一部分；先读已有文件合并再写回，避免覆盖；最终包含全部 50 篇）
4. 最终回复只输出简短 JSON：{"status": "ok", "count": 50, "file": "/tmp/abs_zh_XX.json"}

注意：全部 50 篇必须覆盖；不要把大段内容贴进最终回复，写文件即可。
```

## 经验
- 免费翻译 API 不可靠（MyMemory 超时 / LibreTranslate 403），必须走 agent
- 子 agent 偶发 LLM stream timeout（120s idle）→ 重试或补译缺失
- 标题翻译可并入摘要翻译任务（title_zh 字段），或单独一批
