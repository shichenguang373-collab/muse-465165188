#!/usr/bin/env python3
"""值班盯梢：worker 回完消息后不退出，用这个脚本继续盯 inbox。

- 启动时若 duty.heartbeat 在 60 秒内被更新过，说明已有 worker 在值班，
  打印 "duty occupied" 并退出 2（调用方直接结束，不要重复值班）。
- 否则每 10 秒扫一次 inbox，每次循环都 touch duty.heartbeat（告诉 hook 别唤醒新的）。
- 发现新未处理消息：写入 claimed.json 认领，打印 JSON [{"id","text"}]，退出 0。
- 超时（默认 600 秒）无消息：打印 "timeout"，退出 1。

用法: watch_inbox.py [--timeout 600] [--interval 10]
"""
import argparse
import json
import os
import sys
import time

BRIDGE = "/home/hatch/workspace/wechat-bridge"
INBOX = os.path.join(BRIDGE, "inbox")
STATE = os.path.join(BRIDGE, "state")
DUTY = os.path.join(STATE, "duty.heartbeat")
DUTY_FRESH_SECS = 60
CLAIM_TTL = 900


def load_set(p):
    try:
        d = json.load(open(p))
        return set(d if isinstance(d, list) else [])
    except Exception:
        return set()


def load_dict(p):
    try:
        d = json.load(open(p))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def atomic_write(p, obj):
    tmp = p + ".tmp"
    json.dump(obj, open(tmp, "w"), ensure_ascii=False)
    os.replace(tmp, p)


def touch(p):
    open(p, "a").close()
    os.utime(p, None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--interval", type=int, default=10)
    a = ap.parse_args()

    if os.path.exists(DUTY) and time.time() - os.path.getmtime(DUTY) < DUTY_FRESH_SECS:
        print("duty occupied")
        return 2

    deadline = time.time() + a.timeout
    while time.time() < deadline:
        touch(DUTY)
        now = time.time()
        processed = load_set(os.path.join(STATE, "processed.json"))
        claimed = load_dict(os.path.join(STATE, "claimed.json"))
        fresh = {k for k, v in claimed.items() if now - float(v) < CLAIM_TTL}
        try:
            files = {os.path.splitext(f)[0]: os.path.join(INBOX, f)
                     for f in os.listdir(INBOX) if f.endswith(".json")}
        except FileNotFoundError:
            files = {}
        new_ids = sorted(set(files) - processed - fresh)
        if new_ids:
            msgs = []
            for mid in new_ids:
                try:
                    m = json.load(open(files[mid]))
                    msgs.append({"id": mid, "text": m.get("text", "")})
                except Exception:
                    msgs.append({"id": mid, "text": ""})
                claimed[mid] = now
            atomic_write(os.path.join(STATE, "claimed.json"), claimed)
            print(json.dumps(msgs, ensure_ascii=False))
            return 0
        time.sleep(a.interval)
    print("timeout")
    return 1


sys.exit(main())
