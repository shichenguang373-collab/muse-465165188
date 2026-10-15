# 看门狗 cron 指令（wechat-bridge-reply，每 5 分钟一轮）

```
微信桥看门狗（serve + 常驻 worker 保活 + 兜底回复）。每 5 分钟一轮，全程静默，无异常不打扰用户。

工作目录 /home/hatch/workspace/wechat-bridge

1. serve 保活：pgrep -f "wechat_bridge serve" 有进程则跳过；无进程则拉起：
   WECHAT_BRIDGE_DIR=/home/hatch/workspace/wechat-bridge nohup /home/hatch/workspace/wechat-bridge/.venv/bin/python -m wechat_bridge serve >> /home/hatch/workspace/wechat-bridge/state/serve.log 2>&1 &
   等 5 秒确认进程存在。
   1b. serve 连通性检查（防"进程活着但连不上"）：tail -n 5 state/serve.log 若全是 "getupdates 出错"（出口代理凭证过期会导致 403），则 pkill -f "wechat_bridge serve" 杀掉旧进程，等 3 秒确认已死，再按上面命令重启。重启后等 15 秒，确认日志不再报 getupdates 出错。注意：kill 前确认 outbox/pending 为空，避免打断正在发送的回复。

2. worker 保活：检查 state/worker.heartbeat 的 mtime。
   - 5 分钟内更新过 → worker 存活，跳过。
   - 超过 5 分钟或文件不存在 → worker 已死。先用 subagent.list 确认没有正在启动的 worker，避免重复；确认无后，用 subagent.spawn 启动新常驻 worker，message 内容直接取 ~/workspace/wechat-bridge/worker_prompt.md 全文。

3. 登录态：state/NEEDS_RELOGIN 存在且 state/notified_relogin 不存在 → 在聊天中通知用户"微信桥登录过期了，跟我说一声，我发新的二维码重新绑定"，创建空文件 state/notified_relogin。

4. 兜底回复（最后手段）：inbox/*.json 中有 message_id 不在 processed.json 且已超过 15 分钟未处理的，由本轮直接处理：合并成一条回复（200 字以内，温暖直接中文），用 worker_send.py 写入 outbox/pending（serve 发送线程会发出）：
   python3 /home/hatch/workspace/wechat-bridge/worker_send.py '<最新id>' '<from_user_id>' '<最新context_token>' '<回复>' '<其余id逗号分隔>'
   inbox 记录里有 from_user_id 和 context_token。如果 worker 心跳正常但消息卡住超 15 分钟，先按第 2 步重启 worker 再兜底。

5. 安全红线：绝不把 state/session.json 的内容打印到任何输出、日志或聊天记录中，显示时必须脱敏。
```
