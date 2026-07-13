#!/usr/bin/env python3
"""Supervise the primary ajib bot and isolated hosted reseller workers."""

import os
import signal
import subprocess
import sys
import time


os.environ["AJIB_BOT_ROLE"] = "supervisor"


BOT_DIR = os.getenv("AJIB_BOT_DIR", os.path.dirname(os.path.abspath(__file__)))
if BOT_DIR not in sys.path:
    sys.path.insert(0, BOT_DIR)

from utils.atomic_store import read_json
from utils.hosted_bots import MAX_ACTIVE_BOTS, get_token, list_bots, set_bot_runtime_status


RESELLERS_FILE = os.path.join(BOT_DIR, "resellers.json")
PYTHON = sys.executable
STOPPING = False


class Worker:
    def __init__(self, key, command, env, hosted=False):
        self.key = key
        self.command = command
        self.env = env
        self.hosted = hosted
        self.process = None
        self.failures = 0
        self.next_start = 0.0

    def start(self):
        if STOPPING or time.monotonic() < self.next_start:
            return
        self.process = subprocess.Popen(self.command, cwd=BOT_DIR, env=self.env)
        if self.hosted:
            set_bot_runtime_status(self.key, "active")

    def poll(self):
        if self.process is None:
            self.start()
            return
        return_code = self.process.poll()
        if return_code is None:
            return
        self.process = None
        self.failures += 1
        delay = min(60, 2 ** min(self.failures, 6))
        self.next_start = time.monotonic() + delay
        if self.hosted:
            set_bot_runtime_status(self.key, "error", f"Worker exited with status {return_code}; retry in {delay}s")

    def stop(self):
        if not self.process or self.process.poll() is not None:
            self.process = None
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.process = None


def _eligible_hosted_bots():
    registry = list_bots()
    resellers = read_json(RESELLERS_FILE, {})
    eligible = []
    for reseller_id, record in sorted(registry.items()):
        reseller = resellers.get(str(reseller_id), {}) if isinstance(resellers, dict) else {}
        if not record.get("enabled", True) or record.get("status") == "disconnected":
            continue
        if reseller.get("status") not in {"approved", "suspended"}:
            set_bot_runtime_status(reseller_id, "blocked", "Reseller is not approved or suspended")
            continue
        token = get_token(reseller_id)
        if not token:
            set_bot_runtime_status(reseller_id, "error", "Hosted bot token is missing")
            continue
        eligible.append((reseller_id, record, token))
    return eligible[:MAX_ACTIVE_BOTS]


def _hosted_worker(reseller_id, record, token):
    env = dict(os.environ)
    env.update({
        "AJIB_BOT_ROLE": "hosted",
        "AJIB_BOT_DIR": BOT_DIR,
        "AJIB_HOSTED_RESELLER_ID": str(reseller_id),
        "AJIB_HOSTED_BOT_TOKEN": token,
        "AJIB_HOSTED_BOT_ID": str(record.get("bot_id", "")),
        "AJIB_HOSTED_BOT_USERNAME": str(record.get("username", "")),
    })
    return Worker(str(reseller_id), [PYTHON, os.path.join(BOT_DIR, "hosted_worker.py")], env, hosted=True)


def _stop_signal(_signum, _frame):
    global STOPPING
    STOPPING = True


def main():
    signal.signal(signal.SIGTERM, _stop_signal)
    signal.signal(signal.SIGINT, _stop_signal)
    main_env = dict(os.environ)
    main_env["AJIB_BOT_ROLE"] = "main"
    workers = {
        "__main__": Worker("__main__", [PYTHON, os.path.join(BOT_DIR, "tbot.py")], main_env)
    }

    while not STOPPING:
        desired = {str(item[0]): item for item in _eligible_hosted_bots()}
        for key in list(workers):
            if key == "__main__":
                continue
            if key not in desired:
                workers.pop(key).stop()
        for key, (reseller_id, record, token) in desired.items():
            fingerprint = record.get("token_fingerprint")
            existing = workers.get(key)
            if existing and existing.env.get("AJIB_HOSTED_TOKEN_FINGERPRINT") != fingerprint:
                existing.stop()
                workers.pop(key, None)
                existing = None
            if not existing:
                worker = _hosted_worker(reseller_id, record, token)
                worker.env["AJIB_HOSTED_TOKEN_FINGERPRINT"] = str(fingerprint or "")
                workers[key] = worker
        for worker in list(workers.values()):
            worker.poll()
        time.sleep(3)

    for worker in list(workers.values()):
        worker.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
