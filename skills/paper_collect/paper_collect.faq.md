# FAQ / 边界条件

## Q1: 子 agent 找不到项目文件？
子 agent 的 workdir 可能与主管不同。解决：把数据切块写入 /tmp（如 /tmp/abs_chunk_XX.json），
子 agent 从 /tmp 读取，产出也写 /tmp（plan 模式 /tmp 可写）。

## Q2: 翻译 agent 超时（LLM stream timeout / agent timed out）？
常见。处理：
- 检查 /tmp/abs_zh_XX.json 是否部分落盘（超时前可能已写部分）
- 读 chunk 找缺失 arxiv_id，写 /tmp/abs_miss_XX.json，单独派发补译
- 补译 prompt 要求「读取已有文件合并写回」

## Q3: arXiv API 返回 ID 带版本号（2602.07022v1）？
必须归一化：re.sub(r'v\d+$', '', aid)，否则与 index.json 的 arxiv_id 不匹配。

## Q4: 免费翻译 API 可用吗？
不可靠。MyMemory 首次成功后续超时；LibreTranslate 403。一律用 agent 子任务翻译。

## Q5: 无 arXiv ID 的论文怎么办？
无法抓摘要/链接，仅保留标题，清单中标注「无摘要，仅标题」。

## Q6: 子 agent 数据 >3500B 必须分片？
是。callagent message ≤3500B，大数据分片回信；主管确认后明确「停止重发」防乒乓。
