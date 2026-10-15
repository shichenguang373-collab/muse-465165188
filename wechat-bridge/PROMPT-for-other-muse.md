# 给另一个 Muse 的话术（直接复制发给他）

> 下面这段直接粘贴发给另一个 Muse，他照着做就能把微信桥跑起来。

---

你是 Muse，现在要帮我在你的环境里搭一套"微信桥"，让一个微信机器人能收发微信消息、自动回复我。完整方案文档和代码都在这个 GitHub 仓库里：

https://github.com/shichenguang373-collab/muse-465165188/tree/main/wechat-bridge

按以下步骤做：

1. **拉代码**：把上面仓库 `wechat-bridge/` 目录的内容弄到你本地（git clone 或下载 zip）。
   目录结构：`README.md`（完整方案文档，先读一遍）、`wechat_bridge/`（桥核心源码：serve 收发、ilink 协议、cli 命令行）、`worker_watch.py` / `worker_send.py` / `worker_prompt.md`（常驻回复 worker）、`TROUBLESHOOTING.md`（排障）。

2. **装环境**：`cd wechat-bridge`，`python3 -m venv .venv`，`.venv/bin/pip install -e .`。

3. **扫码绑定机器人**：
   `WECHAT_BRIDGE_DIR=$(pwd) .venv/bin/python -m wechat_bridge login` 会生成二维码，把二维码图片发给我，我用微信小号扫码；扫完你跑 `WECHAT_BRIDGE_DIR=$(pwd) .venv/bin/python -m wechat_bridge wait-login` 等确认。登录凭据落在 `state/session.json`。
   注意：每次重新扫码都是建一个**全新**机器人，旧机器人作废，不要复活旧的。

4. **拉起 serve（常驻收发服务）**：
   `WECHAT_BRIDGE_DIR=$(pwd) nohup .venv/bin/python -m wechat_bridge serve >> state/serve.log 2>&1 &`
   它负责长轮询收消息（写 `inbox/`）和 0.3 秒一次把 `outbox/pending/` 里的回复发出去。

5. **启动常驻回复 worker**：用 `worker_prompt.md` 的全文作为系统提示词，起一个常驻子 agent 让它一直运行。它的循环是：`python3 worker_watch.py --timeout 600` 阻塞等新消息 → 把同一批消息合并成一条 200 字以内的中文回复 → `python3 worker_send.py '<id>' '<from_user_id>' '<context_token>' '<回复>'` 写入 outbox，serve 会发出。

6. **建看门狗定时任务**：每 5 分钟跑一轮，全程静默。指令全文在 `README.md` 附录 B（serve 保活、serve 连通性检查、worker 保活、微信登录过期通知、15 分钟未处理消息的兜底回复）。

铁律（违反就白搭）：
- 绝不把 `state/session.json` 的内容输出到任何地方。
- 停 serve 必须单独执行 `pkill -f "[w]echat_bridge serve"`。
- 机器人不能主动开口，必须用户先发一条消息才有 context_token；收到重复文字的消息时，按**本条消息的 id**去 `state/serve.log` 查"已发送回复"确认上一条真的发出去了，拿不准就正常回一条，不要静默跳过。

做完后在微信里跟我说一声，我发消息验证。
