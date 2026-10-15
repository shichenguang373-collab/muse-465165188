# 微信桥（WeChat Bridge）完整方案文档

> 给另一个 Muse 看的复刻指南。按这份文档从头到尾做，就能让微信和 Muse 连上。
> 来源：2026-10-15/16 实战调通，端到端可用。
> 配套代码包：`wechat-bridge-code.tar.gz`（与本文档同目录）
> 包含桥核心源码（`wechat_bridge/`：serve/ilink/cli/media/store/crypto）、
> worker 脚本（watch/send）、worker_prompt.md、TROUBLESHOOTING.md。
> 解压即用：`tar -xzf wechat-bridge-code.tar.gz`，然后 `pip install -e src/wechat-bridge` 建虚拟环境。
> 注意：`state/`（含登录凭据 session.json）不在包里，首次绑定扫码后自动生成。


---

## 1. 技术路线（为什么是这个方案）

- 走的是 **iLink 协议**（腾讯官方 GitHub org 的 openclaw-weixin 插件协议），建的是**独立 Bot 账号**，不是接管个人微信号。
- 微信《软件许可及服务协议》明禁非腾讯第三方客户端自动化，**主号不要冒险**，用小号扫码建机器人。
- 结论：一个微信小号 + 一个 Bot 机器人 + 本机常驻服务 + 看门狗 cron，微信里跟机器人聊天，Muse 在后台回。

---

## 2. 组件架构

```
微信用户 ◄──► iLink 服务端 ◄──► serve（长轮询收消息 / 0.3s 发送线程）
                                          │
                          inbox/<id>.json ▼        ▲ outbox/pending/<id>.json
                          常驻 worker（watch 600s → 合并回复 → worker_send.py）
                                          │
                    看门狗 cron（每 5 分钟：保活 serve / 保活 worker / 登录态 / 兜底回复）
```

- **serve**：`python3 -m wechat_bridge serve`，常驻进程。两件事：①长轮询 iLink 拉新消息，写成 `inbox/<message_id>.json`；②独立发送线程每 0.3 秒扫 `outbox/pending/`，调用 sendmessage 发出。
- **worker**：常驻 agent（由主 agent 直接拉起，见第 7 节教训）。循环：`worker_watch.py --timeout 600` 阻塞等新消息 → 把同一批消息合并成一条回复 → `worker_send.py` 幂等写入 `outbox/pending`，serve 的发送线程 0.3 秒内发出。
- **看门狗**：cron `wechat-bridge-reply`，每 5 分钟一轮，全程静默，无异常不打扰用户。

代码位置：`~/workspace/wechat-bridge/`（`src/wechat-bridge/` 是桥的上游源码，`.venv` 是独立虚拟环境）。

---

## 3. 首次绑定（扫码）

```bash
cd ~/workspace/wechat-bridge
WECHAT_BRIDGE_DIR=~/workspace/wechat-bridge .venv/bin/python -m wechat_bridge login
# 生成 state/qrcode.png（仓库里也有份 qrcode.png），用微信小号扫码
WECHAT_BRIDGE_DIR=~/workspace/wechat-bridge .venv/bin/python -m wechat_bridge wait-login
```

登录凭据落在 `state/session.json`（权限 0600）。**血泪教训：重新扫码 ≠ 刷新旧机器人，而是建一个全新机器人**——旧 token 会被顶掉失效（-14），旧 serve 必须停掉、换新会话重启。用户微信里会出现两个机器人聊天窗口，务必让用户确认在和**新**机器人聊，并给新机器人改设备备注名区分。**旧机器人一旦退役，绝不重启/复活。**

---

## 4. 拉起 serve（常驻收发）

```bash
WECHAT_BRIDGE_DIR=/home/hatch/workspace/wechat-bridge nohup /home/hatch/workspace/wechat-bridge/.venv/bin/python -m wechat_bridge serve >> /home/hatch/workspace/wechat-bridge/state/serve.log 2>&1 &
```

**停 serve 必须用**（单独一条命令执行，不要在同一命令里再写明文 pattern，否则会把执行命令的 shell 自己杀掉）：

```bash
pkill -f "[w]echat_bridge serve"
```

---

## 5. 常驻 worker（自动回复大脑）

worker 提示词全文在 `~/workspace/wechat-bridge/worker_prompt.md`，看门狗重启和初次启动用**同一份**，直接取全文 spawn。核心循环：

1. 启动第一件事：`date +%s > state/worker.heartbeat` 写心跳；之后每 60 秒（每次循环迭代）更新一次。
2. `python3 worker_watch.py --timeout 600`：阻塞直到有新消息，返回 `{"watch_ts", "messages": [{"id","text","from_user_id","context_token","bot_id"}]}`。超时（退出码 1）立刻重跑，不要用更长的超时。
3. 同一批 messages 合并成一条回复（200 字以内，温暖直接中文）：
   - 简单消息：直接生成回复，不调其他工具，越快越好。
   - 重复问题（文字和之前某条相同）：**按本条消息的 id**去 `state/serve.log` 查有没有对应的「已发送回复」，确认上一条真的送达了才能当重复处理；不要拿别的消息 id 的记录当证据。拿不准就正常回一条，**不要静默跳过**。
   - 需要查资料/执行操作：先回一句"收到，我记下了，查到就发你"，不要卡住；超过约 90 秒没搞定也先回这句。
4. 发送（一次调用，幂等不重发）：
   ```bash
   python3 worker_send.py '<最新一条id>' '<from_user_id>' '<最新一条context_token>' '<合并回复>' '<其余id逗号分隔>'
   ```
   `watch_ts` 和 `worker.log` 时间戳用于统计生成耗时，不要改这两个脚本的输出格式。
5. 回到第 2 步，**一直运行，不要退出**。

实测延迟：outbox 排队约 1.2–1.8 秒；简单回复端到端约 4.5–22.6 秒。不要承诺秒回。

---

## 6. 看门狗 cron（每 5 分钟一轮）

cron 名：`wechat-bridge-reply`，工作目录 `~/workspace/wechat-bridge`。四件事：

1. **serve 保活**：`pgrep -f "wechat_bridge serve"` 有进程则跳过；无则按第 4 节拉起，等 5 秒确认。
   **1b. 连通性检查**（防"进程活着但连不上"）：`tail -n 5 state/serve.log` 若全是 `getupdates 出错`（出口代理凭证约 2 小时轮换一次，过期会导致 403），则 kill 旧进程（kill 前确认 `outbox/pending` 为空，避免打断正在发送的回复），等 3 秒确认已死再重启；重启后等 15 秒确认日志不再报错。
2. **worker 保活**：三信号判定，满足任一即判死：A. inbox 有超 5 分钟未处理的消息；B. `state/worker.heartbeat` mtime 超 5 分钟没更新；C. `pgrep` 找不到 `worker_watch` 进程（等 90 秒复查，排除轮次间隙）。**心跳新鲜但进程不存在 = 心跳欺骗，直接判死**。判死后用 `subagent.list` 确认没有正在启动的 worker，避免重复，然后 `subagent.spawn` 拉新 worker（message 取 `worker_prompt.md` 全文）。
3. **登录态**：`state/NEEDS_RELOGIN` 存在且 `state/notified_relogin` 不存在 → 在聊天里通知用户"微信桥登录过期了，跟我说一声，我发新的二维码重新绑定"，创建空文件 `state/notified_relogin`。
4. **兜底回复**（最后手段）：`inbox/*.json` 中 message_id 不在 `processed.json` 且超 15 分钟未处理 → 本轮直接合并成一条回复（200 字以内），用 `worker_send.py` 写入 `outbox/pending`。如果 worker 心跳正常但消息卡住超 15 分钟，先重启 worker 再兜底。

---

## 7. 血泪教训（必须看）

1. **看门狗 spawn 的 worker 活不过父 run 超时**：看门狗 cron run 自身有执行超时（默认 3600 秒），它直接 spawn 的常驻 worker 是其子 agent，会随父 run 超时一起被杀——而心跳文件还留着新鲜时间戳，导致下一轮误判"worker 存活"、消息无人回。**要长寿的 worker 让主 agent 直接拉起**（活到下次虚拟机替换或自身报错）；看门狗只做检测 + 补拉。`wechat-bridge-reply` 的 `timeout_secs` 已设为 86400 秒。
2. **重复消息不要直接当网络抖动跳过**（见第 5 节第 3 条）。
3. **补发流程**：`processed.json` 里已有 ID 时 `worker_send.py` 会直接跳过。要补发，先把该 ID 从 `processed.json` 移除，再调 `worker_send.py`，用 inbox 里**最新一条**的 context_token 发（用户发新消息会刷新凭证，旧 token 可能过期）。
4. **403 连刷**：出口代理拦截（非登录问题，无需重新扫码），重启 serve 恢复；看门狗 1b 连通性检查 5 分钟内自愈。
5. **"微信没回"先看日志**：先看 `serve.log` 有没有"收到消息"，再下结论——可能是用户在旧机器人窗口看（退役机器人窗口没删就会这样）。
6. 平台部署更新会直接换掉整台虚拟机，进程全灭，只有 home 目录和平台托管的定时任务能恢复——**不要指望 systemd**，保活唯一靠 cron 看门狗。

---

## 8. 已知限制（跟用户说清楚）

- 一对一私聊；用户须先发一条消息才有 context_token（机器人不能主动开口）。
- 连续约 10 条不回会被限流。
- Bot 头像/名字改不了，用户可以在微信里给 Bot 改设备备注名。
- 微信语音消息以转写文本进入 inbox，同批合并处理。
- 客人机器人：`bots/<bot_id>/` 独立目录，`bots/registry.json` 里 `approved=true` 才能用；发送时前面加 `WECHAT_BOT_ID=<客人id>`。客人只能动自己的沙盒，绝不碰老板的文件、不透露老板任何信息、不叫"老板"；客人之间互相隔离；新机器人绑定/批准/停用只听老板的。

---

## 9. 安全红线

- **绝不**把 `state/session.json` 的内容打印到任何输出、日志或聊天记录中，显示时必须脱敏。凭据只存本机（0600），不进记忆、不进聊天。
- 退役的旧机器人未经用户同意**不得重启/复活**；重新扫码前先跟用户说清楚"会建一个新机器人"。

---

## 10. 排障速查

| 现象 | 原因 | 处理 |
|---|---|---|
| `state/` 下有 NEEDS_RELOGIN | 登录凭据失效 | 重新 login 扫码（= 建新机器人） |
| serve 进程没了 | 进程挂了 | 按第 4 节拉起 |
| 日志反复 `Tunnel connection failed: 403` | 出口代理凭证过期 | 重启 serve；看门狗 1b 会自动处理 |
| 日志有"已发送回复"但微信没显示 | 回复凭证过期或被限流 | 让用户在微信里先发一条新消息刷新凭证 |
| 进程在、无报错、无新日志 | 长轮询卡住 | 重启 serve |

详细排障见 `TROUBLESHOOTING.md`。

---

## 代码与指令位置（代码都在仓库里，本文档只讲方案）

| 内容 | 文件 |
|---|---|
| 常驻 worker 提示词（看门狗重启与初次启动共用同一份） | `worker_prompt.md` |
| 看门狗 cron 指令全文 | `WATCHDOG.md` |
| worker 眼睛（等新消息） | `worker_watch.py` |
| worker 手（幂等发送） | `worker_send.py` |
| 桥核心（serve 收发 / ilink 协议 / cli / 媒体 / 存储） | `wechat_bridge/` |
| 排障手册 | `TROUBLESHOOTING.md` |
| 给另一个 Muse 的一键话术 | `PROMPT-for-other-muse.md` |
