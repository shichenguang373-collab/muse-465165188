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
import fcntl
import json
import os
import sys
import time

BRIDGE = "/home/hatch/workspace/wechat-bridge"
REGISTRY = os.path.join(BRIDGE, "bots", "registry.json")
HEARTBEAT = os.path.join(BRIDGE, "state", "worker.heartbeat")
LOCKFILE = os.path.join(BRIDGE, "state", "worker.lock")


def launched_by_sleep_wrapper():
    """检测祖先进程链里有没有 `sleep N; ... worker_watch.py` 形式的包装启动。

    2026-10-20 事故根因：旧 wechat-bridge-reply 的僵尸 agent（schedule-owned，
    主 agent 的 send/close 都被平台以 outside scope 拒绝，关不掉）给自己设定了
    "备份 failover"角色，只会循环执行 `sleep 300; python3 worker_watch.py`，
    拿到 exit 2 就认定"主循环还活着"继续退让。主 worker 真死后它也永不升主，
    还靠 touch 心跳冒充存活、抢占 watch 周期。合法 worker 一律直接启动
    worker_watch.py（见 worker_prompt.md），从不用 sleep 前缀包装，所以凡是被
    sleep 包装启动的 watch 一律拒绝运行（退出码 3，不抢锁、不碰心跳），
    让僵尸的空转在脚本层失效。
    """
    pid = os.getppid()
    for _ in range(8):
        if pid <= 1:
            break
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().replace(b"\x00", b" ").decode("utf-8", "ignore")
        except OSError:
            break
        if "worker_watch" in cmd and "sleep" in cmd:
            return True
        try:
            with open(f"/proc/{pid}/stat", "rb") as f:
                # comm 可能含空格/括号，从最后一个 ')' 之后取字段，ppid 是其后第 2 个
                pid = int(f.read().rsplit(b")", 1)[1].split()[1])
        except Exception:
            break
    return False


def acquire_singleton_lock():
    """单实例锁：拿不到锁说明已有 worker 在跑，直接退出。

    2026-10-20 根治双 worker 重复发送：两个 agent（主 agent 直属 worker
    与旧 wechat-bridge-reply 的僵尸 agent）各跑一个 600s watch 循环，
    相位漂移导致反复出现两个 worker_watch 并发抢消息（10-20 当天已造成
    6 条重复发送）。flock 是内核态锁，持有者死亡（kill -9/虚拟机替换）
    时由 OS 自动释放，无 stale 锁问题。拿不到锁的进程退出码 2，
    调用方的 while 循环会 sleep 后重试，不会忙等抢占。
    """
    fd = os.open(LOCKFILE, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        print("another worker holds the lock, exiting", flush=True)
        return None
    return fd


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
    if launched_by_sleep_wrapper():
        print("refused: sleep-wrapped launch (retired failover loop), exiting", flush=True)
        return 3
    lock_fd = acquire_singleton_lock()
    if lock_fd is None:
        return 2
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
