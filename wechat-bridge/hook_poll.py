#!/usr/bin/env python3
"""微信桥快速回复轮询核心：扫 inbox -> 认领 -> 决定 wake/silent。

输出两行：第一行 wake|silent，第二行 payload JSON（wake 时）或 reason（silent 时）。
环境变量 HATCH_HOOK_DRY_RUN=1 时只观测，不写认领。
"""
import json
import os
import sys
import time

BRIDGE = "/home/hatch/workspace/wechat-bridge"
INBOX = os.path.join(BRIDGE, "inbox")
STATE = os.path.join(BRIDGE, "state")
DRY = os.environ.get("HATCH_HOOK_DRY_RUN") == "1"
CLAIM_TTL_SECS = 900  # 认领超过这么久未处理视为失联（由 cron 接管）
CLAIM_TTL_NO_DUTY_SECS = 120  # 无 worker 值班时，认领只保护这么久：
# worker 若在值班 watch 里认领后异常退出（无心跳），hook 能在 2 分钟后重新唤醒重试，
# 而不是等 900 秒过期。冷启动（约 105 秒）仍在保护窗口内，不会重复唤醒。
DUTY = os.path.join(STATE, "duty.heartbeat")
DUTY_FRESH_SECS = 120


def load_list(p):
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


def main():
    now = time.time()
    processed = load_list(os.path.join(STATE, "processed.json"))
    claimed = load_dict(os.path.join(STATE, "claimed.json"))

    duty_fresh = (os.path.exists(DUTY)
                  and now - os.path.getmtime(DUTY) < DUTY_FRESH_SECS)
    claim_ttl = CLAIM_TTL_SECS if duty_fresh else CLAIM_TTL_NO_DUTY_SECS
    fresh_claimed = {k for k, v in claimed.items()
                     if now - float(v) < claim_ttl}
    files = {}
    try:
        for f in os.listdir(INBOX):
            if f.endswith(".json"):
                files[os.path.splitext(f)[0]] = os.path.join(INBOX, f)
    except FileNotFoundError:
        pass

    new_ids = sorted(set(files) - processed - fresh_claimed)
    new_msgs = []
    for mid in new_ids:
        try:
            m = json.load(open(files[mid]))
            new_msgs.append({"id": mid, "text": m.get("text", "")})
        except Exception:
            new_msgs.append({"id": mid, "text": ""})

    if DRY:
        print("silent")
        print("dry-run: new=%d" % len(new_msgs))
        return

    if new_ids:
        for mid in new_ids:
            claimed[mid] = now
        atomic_write(os.path.join(STATE, "claimed.json"), claimed)
        print("wake")
        print(json.dumps({"messages": new_msgs}, ensure_ascii=False))
    else:
        print("silent")
        print("no new wechat messages")


main()
