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
