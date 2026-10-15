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
