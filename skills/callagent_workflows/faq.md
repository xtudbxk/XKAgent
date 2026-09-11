# callagent 常见问题（FAQ）

| Q | A |
|---|---|
| 收件人不存在会怎样？ | 名字合法 → **自动创建**（start_agent 只校验名）；默认 plan 模式+无挂载 |
| 一封邮件多个收件人？ | `to=[...]` 广播：单 msg_id、per-to 状态；**禁 need_reply=true** |
| 为什么同会话邮件排队慢？ | 串行闸门（in_flight）+ _turn_active 跳过；要并行必须多会话/广播 |
| need_reply 什么时候用？ | 单对单要回执时 true（注入回信指引）；通知型/广播用 false |
| 投递失败怎么办？ | failed（retry 1-2）自动重投（0.2s 冷却）；dead=重试耗尽，人工查 mail.jsonl |
| 长任务服务重启会丢吗？ | 不会。send/delivered 行在 mail.jsonl，重启后两阶段投递恢复 |
| 心跳多久一次？ | < IDLE_RECLAIM_SECONDS(300s)，否则被空闲回收 |
| 消息有多长限制？ | message ≤3500 字节；超长拆多封或改用文件/数据附引 |
| 状态如何审计？ | mail.jsonl 状态行（send/delivered/done/failed/dead + to + turn_id + 时间戳）；链回溯用 reply_to/chain |
| compact 会删历史吗？ | 邮件条目>5000 自动压实：已完全终态的邮件剔除、有在途/待投递收件人的保留 → 长期审计需定期另存档 |
| provider 怎么写？ | `my-provider/gpt-5.4:max`；`:effort` 自动切分透传（本次覆盖>会话配置） |
| 会话名从哪拿？ | 当前会话名=用户消息元信息「当前会话」字段；目标名查 session_registry.json |
| 回信发给谁？ | 默认 reply_to 回**发信人**；多跳任务必须显式写"回信 to=XX" |
| 自调度会死循环吗？ | 会（无防护）→ 循环信必须带 [第 N/M 次]+显式终止条件+need_reply=false |
