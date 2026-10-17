#!/usr/bin/env python3
"""Worker 单步回复：幂等检查 -> 发送 -> 落盘，一次 exec 做完。

用法: worker_reply.py <message_id> <reply_text>
"""
import fcntl
import json
import os
import subprocess
import sys

BRIDGE = "/home/hatch/workspace/wechat-bridge"
VENV_PY = os.path.join(BRIDGE, ".venv", "bin", "python")
STATE = os.path.join(BRIDGE, "state")
PROCESSED = os.path.join(STATE, "processed.json")
CLAIMED = os.path.join(STATE, "claimed.json")


def load_list(p):
    try:
        d = json.load(open(p))
        return d if isinstance(d, list) else []
    except Exception:
        return []


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
    mid, text = sys.argv[1], sys.argv[2]
    # 原子预留：上锁 -> 检查 -> 先记入 processed 占位 -> 解锁 -> 再发送。
    # 两个 worker 同时抢一条消息时，只有一个能占位成功，另一个直接跳过。
    open(PROCESSED, "a").close()
    with open(PROCESSED, "r+") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            lf.seek(0)
            processed = json.load(lf)
            if not isinstance(processed, list):
                processed = []
        except Exception:
            processed = []
        if mid in processed:
            print("already processed, skip send")
            return 0
        processed.append(mid)
        lf.seek(0)
        lf.truncate()
        json.dump(processed, lf, ensure_ascii=False)
    env = dict(os.environ, WECHAT_BRIDGE_DIR=BRIDGE)
    r = subprocess.run(
        [VENV_PY, "-m", "wechat_bridge", "send", "--text", text],
        env=env, capture_output=True, text=True, timeout=90)
    out = (r.stdout or "")[-300:]
    err = (r.stderr or "")[-300:]
    if out:
        print("send stdout:", out)
    if err:
        print("send stderr:", err)
    if r.returncode != 0 or "message_id" not in (r.stdout or ""):
        print("SEND FAILED, release reservation")
        release(mid)
        return 1
    claimed = load_dict(CLAIMED)
    claimed.pop(mid, None)
    atomic_write(CLAIMED, claimed)
    print("done")
    return 0


def release(mid):
    """发送失败时释放预留，避免这条消息永久被占位。"""
    open(PROCESSED, "a").close()
    with open(PROCESSED, "r+") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            lf.seek(0)
            processed = json.load(lf)
            if not isinstance(processed, list):
                processed = []
        except Exception:
            processed = []
        if mid in processed:
            processed.remove(mid)
        lf.seek(0)
        lf.truncate()
        json.dump(processed, lf, ensure_ascii=False)


sys.exit(main())
