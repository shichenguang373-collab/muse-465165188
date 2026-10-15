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
- 吃药提醒微信通道：整点轮次、每天上限 8 条；用户在微信里明确说"吃了"/"吃过了" → 往水印文件追加一行停掉当天提醒（只记水印，不做医疗判断）。
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

详细排障见 `~/workspace/wechat-bridge/TROUBLESHOOTING.md`。

---

# 附录：完整代码（直接复制可用）

## 附录 A：worker_prompt.md 全文（常驻 worker 提示词，看门狗重启与初次启动共用）

```markdown
# 微信常驻回复 worker 提示词（看门狗重启用，与初次启动完全一致）

你是 Muse，老板的微信自动回复常驻 worker。你会一直运行，不断处理微信消息。目标：快。


- 你是老板的 AI 助手 Muse，在微信上跟老板聊天。可以叫"老板"，但别每句话都加，自然一点；不直呼其名。
- 语气温暖直接，像朋友聊天，用中文，回复 200 字以内。
- 背景：2026-10-15 凌晨在调试微信自动回复速度，中间出过几次故障导致消息没及时回，刚换了新架构。如果老板问为什么慢，如实说今晚在调试、出过故障，现在修好了。

（一直运行，不要退出）
1. exec 运行：python3 /home/hatch/workspace/wechat-bridge/worker_watch.py --timeout 600
它阻塞直到有新消息，返回 {"watch_ts": 时间戳, "messages": [{"id","text","from_user_id","context_token","bot_id"}]}。
bot_id="main" 是老板的机器人，其他 bot_id（如 guest-01）是客人机器人。
退出 1 是超时（10 分钟无消息），回到本步继续。注意：用 600 秒超时（不要用更长的），超时后立刻重新运行。

【多用户身份与隔离规则】
- bot_id="main"：在跟老板聊天，一切照旧，全部权限。
- bot_id 是客人：对方是老板批准接入的客人。规则：
  1. 只能动 ~/workspace/wechat-users/<bot_id>/ 下的东西，这是他的数据沙盒；
     绝不读取老板的任何文件（~/workspace/goals、~/memory、wechat-bridge/state/session.json 等一律不碰）。
  2. 绝不向客人透露老板的任何信息（名字、行程、数据、说了什么）。
  3. 不要叫客人"老板"；自然称呼对方（不知道名字就别加称呼）。
  4. 客人之间互相隔离：绝不把 A 客人的事告诉 B。
  5. 客人要求"看老板的XX/替我动老板的东西"：拒绝，说"我没有这个权限"。
- 老板问起客人的事（他发了什么、数据如何）：可以如实说，老板是超管。
- 新机器人绑定/批准/停用：只听老板的指令，客人无权要求。
2. 把同一批 messages 合并成一条回复：
- 简单消息：直接生成回复，不要调用任何其他工具，越快越好。
- 重复问题（和之前某条文字相同）：先按本条消息的 id 去 state/serve.log 里查有没有对应的「已发送回复」，
  确认上一条真的送达了才能当重复处理；不要拿别的消息 id 的发送记录当证据（2026-10-15 曾查错 ID 把正经提问当重发跳过）。
  拿不准就正常回一条，不要静默跳过。
- 需要查资料/看图/执行操作：先回一句"收到，我记下了，查到就发你"，不要卡住。
- 如果某条处理超过约 90 秒还没搞定：先发"收到，我记下了"，继续下一轮。
- 吃药提醒的停止口令：如果老板在微信里明确说今天的药吃了（"吃了"/"吃过了"/"已服药"等），exec 往 ~/workspace/goals/goal/hidden_files/medicine_reminder.md 追加一行「YYYY-MM-DD HH:MM (wechat) — 用户在微信回复已服药，今日停止提醒」（日期时间用北京时间），然后回一句确认收到。注意：这只是记水印停提醒，不要替他做任何医疗判断。
3. exec 发送（一次调用，幂等不会重发）：
- 回老板（bot_id="main"，默认）：python3 /home/hatch/workspace/wechat-bridge/worker_send.py '<最新一条的id>' '<from_user_id>' '<最新一条的context_token>' '<合并后的回复>' '<其余id逗号分隔，无则留空>'
- 回客人（bot_id=<客人id>）：前面加 WECHAT_BOT_ID=<客人id>，如 WECHAT_BOT_ID=guest-01 python3 .../worker_send.py '...' ...（其余参数完全一样；未批准的 bot_id 会被拒绝写入）
回复落盘后 serve 的发送线程约 0.3 秒内发出。
4. 回到第 1 步。

绝不输出 state/session.json 的内容。

注意：watch 返回的 watch_ts 和 worker_send.py 记的 worker.log 时间戳用于统计生成耗时（两者相减），不要改动这两个脚本的输出格式。
```

## 附录 B：看门狗 cron 指令全文（cron 名 wechat-bridge-reply，每 5 分钟）

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

## 附录 C：worker_watch.py 全文（常驻 worker 的眼睛）

```python
#!/usr/bin/env python3
"""常驻 worker 的眼睛：阻塞直到任一机器人 inbox 出现未处理的新消息。

- 每 0.3 秒扫一次：主机器人 inbox + registry.json 里 approved=true 的客人机器人 inbox。
- 每次循环 touch state/worker.heartbeat（看门狗靠它判断 worker 是否存活）。
- 发现新消息：打印 JSON {"watch_ts":..., "messages":[{"id","text","from_user_id","context_token","bot_id"}]}，退出 0。
  bot_id="main" 是老板的机器人，其他是客人机器人（bots/<bot_id>/）。
- 超时（默认 600 秒）：打印 "timeout"，退出 1。

用法: worker_watch.py [--timeout 600] [--interval 0.3]
"""
import argparse
import json
import os
import sys
import time

BRIDGE = "/home/hatch/workspace/wechat-bridge"
REGISTRY = os.path.join(BRIDGE, "bots", "registry.json")
HEARTBEAT = os.path.join(BRIDGE, "state", "worker.heartbeat")


def load_json(p, default=None):
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def load_processed(p):
    d = load_json(p)
    return set(d if isinstance(d, list) else [])


def touch(p):
    try:
        open(p, "a").close()
        os.utime(p, None)
    except OSError:
        pass


def bridge_dirs():
    """返回 [(bot_id, base_dir)]：main + 已批准的客人机器人。"""
    dirs = [("main", BRIDGE)]
    reg = load_json(REGISTRY, {}) or {}
    for bot_id, info in reg.items():
        if not isinstance(info, dict) or not info.get("approved"):
            continue
        d = os.path.join(BRIDGE, "bots", bot_id)
        if os.path.isdir(os.path.join(d, "inbox")):
            dirs.append((bot_id, d))
    return dirs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=600)
    ap.add_argument("--interval", type=float, default=0.3)
    a = ap.parse_args()
    deadline = time.time() + a.timeout
    while time.time() < deadline:
        touch(HEARTBEAT)
        msgs = []
        for bot_id, base in bridge_dirs():
            inbox = os.path.join(base, "inbox")
            processed = load_processed(os.path.join(base, "state", "processed.json"))
            try:
                files = sorted(f for f in os.listdir(inbox) if f.endswith(".json"))
            except FileNotFoundError:
                continue
            for f in files:
                mid = os.path.splitext(f)[0]
                if mid in processed:
                    continue
                try:
                    m = json.load(open(os.path.join(inbox, f)))
                    msgs.append({
                        "id": mid,
                        "text": m.get("text", ""),
                        "from_user_id": m.get("from_user_id", ""),
                        "context_token": m.get("context_token", ""),
                        "bot_id": bot_id,
                    })
                except Exception:
                    pass
        if msgs:
            out = {"watch_ts": time.time(), "messages": msgs}
            print(json.dumps(out, ensure_ascii=False))
            return 0
        time.sleep(a.interval)
    print("timeout")
    return 1


sys.exit(main())
```

## 附录 D：worker_send.py 全文（常驻 worker 的手）

```python
#!/usr/bin/env python3
"""常驻 worker 的手：幂等地把回复写入某机器人 outbox/pending/<message_id>.json。

- 文件名用 message_id，保证幂等（重复调用只会覆盖同一文件）。
- 写完后把 message_id 记入该机器人 state/processed.json。
- 若 outbox/sent/ 里已存在同名文件（已发出），只补记 processed，不重写。
- serve 的独立发送线程会在 ~0.3 秒内发出 pending 里的任务。
- 发往哪个机器人由环境变量 WECHAT_BOT_ID 决定，默认 "main"（老板的机器人）；
  客人机器人用 bots/<bot_id>/ 目录。非 main 的 bot_id 必须在 bots/registry.json
  里 approved=true，否则拒绝写入（防误发）。

用法: worker_send.py <message_id> <to_user_id> <context_token> <text> [extra_ids]
  （text 放最后，避免空格截断问题；extra_ids 为逗号分隔的其他消息 id，
   合并回复时把同一批的其余 id 也记入 processed，避免下一轮重复处理）
"""
import fcntl
import json
import os
import sys
import time

BRIDGE = "/home/hatch/workspace/wechat-bridge"


def resolve_base():
    bot_id = os.environ.get("WECHAT_BOT_ID", "main").strip() or "main"
    if bot_id == "main":
        return bot_id, BRIDGE
    # 客人机器人：必须已批准
    try:
        with open(os.path.join(BRIDGE, "bots", "registry.json"), encoding="utf-8") as f:
            reg = json.load(f)
    except Exception:
        reg = {}
    info = reg.get(bot_id)
    if not isinstance(info, dict) or not info.get("approved"):
        print("拒绝：机器人 %s 未批准或不存在" % bot_id)
        sys.exit(3)
    base = os.path.join(BRIDGE, "bots", bot_id)
    if not os.path.isdir(os.path.join(base, "inbox")):
        print("拒绝：机器人目录 %s 不存在" % base)
        sys.exit(3)
    return bot_id, base


BOT_ID, BASE = resolve_base()
STATE = os.path.join(BASE, "state")
PENDING = os.path.join(BASE, "outbox", "pending")
SENT = os.path.join(BASE, "outbox", "sent")
PROCESSED = os.path.join(STATE, "processed.json")


def load_processed_locked():
    open(PROCESSED, "a").close()
    lf = open(PROCESSED, "r+")
    fcntl.flock(lf, fcntl.LOCK_EX)
    try:
        lf.seek(0)
        d = json.load(lf)
        processed = d if isinstance(d, list) else []
    except Exception:
        processed = []
    return lf, processed


def save_processed(lf, processed):
    lf.seek(0)
    lf.truncate()
    json.dump(processed, lf, ensure_ascii=False)
    lf.flush()


def main():
    if len(sys.argv) < 5:
        print("用法: worker_send.py <message_id> <to_user_id> <context_token> <text> [extra_ids]")
        return 2
    mid, to_user_id, context_token, text = sys.argv[1:5]
    extra_ids = sys.argv[5].split(",") if len(sys.argv) > 5 and sys.argv[5] else []
    t0 = time.time()

    lf, processed = load_processed_locked()
    try:
        if mid in processed:
            print("already processed, skip")
            return 0
        sent_path = os.path.join(SENT, "%s.json" % mid)
        pending_path = os.path.join(PENDING, "%s.json" % mid)
        if os.path.exists(sent_path):
            # 已发出过，只补记 processed
            for i in [mid] + extra_ids:
                if i and i not in processed:
                    processed.append(i)
            save_processed(lf, processed)
            print("already sent, marked processed")
            return 0
        os.makedirs(PENDING, exist_ok=True)
        job = {
            "to_user_id": to_user_id,
            "text": text,
            "context_token": context_token,
            "message_id": mid,
        }
        tmp = pending_path + ".tmp"
        json.dump(job, open(tmp, "w"), ensure_ascii=False)
        os.replace(tmp, pending_path)
        for i in [mid] + extra_ids:
            if i and i not in processed:
                processed.append(i)
        save_processed(lf, processed)
    finally:
        lf.close()
    dt = time.time() - t0
    # 记 worker.log：供统计生成耗时（watch 返回 -> 落盘）
    # 客人机器人的日志记在各自 state/worker.log（格式与主机器人完全一致）
    try:
        with open(os.path.join(STATE, "worker.log"), "a") as wlog:
            wlog.write("%s send %s queue_write=%.2fs\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), mid, dt))
    except OSError:
        pass
    print("queued %s in %.2fs (bot=%s, serve 发送线程约 0.3s 内发出)" % (mid, dt, BOT_ID))
    return 0


sys.exit(main())
```

## 附录 E：serve 拉起 / 停止命令

```bash
WECHAT_BRIDGE_DIR=/home/hatch/workspace/wechat-bridge nohup /home/hatch/workspace/wechat-bridge/.venv/bin/python -m wechat_bridge serve >> /home/hatch/workspace/wechat-bridge/state/serve.log 2>&1 &
# 停止（必须单独一条命令执行）：
pkill -f "[w]echat_bridge serve"
```
