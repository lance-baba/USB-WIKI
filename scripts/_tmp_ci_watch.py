#!/usr/bin/env python3
"""临时：轮询 GitHub Actions 作业状态，全部结束后打印汇总（scripts/_tmp_* 由 --clean 清理）。"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

RUN = "35221843032"
URL = f"https://api.github.com/repos/lance-baba/USB-WIKI/actions/runs/{RUN}/jobs?per_page=10"
DEADLINE = time.time() + 40 * 60


def fetch() -> dict:
    req = urllib.request.Request(URL, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "usb-wiki-ci-watch",
        "Cache-Control": "no-cache",
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    last = ""
    while time.time() < DEADLINE:
        try:
            data = fetch()
        except Exception as exc:
            print(f"[watch] fetch 失败：{exc}", flush=True)
            time.sleep(20)
            continue
        jobs = data.get("jobs", [])
        line = " | ".join(
            f"{j['name'].replace('windows-latest', 'win').replace('ubuntu-latest', 'ubu')}"
            f".{j['name'].split('Python ')[-1] if 'Python ' in j['name'] else 'P'}"
            f"={j['status']}/{j['conclusion']}" for j in jobs)
        if line != last:
            print(f"[watch] {time.strftime('%H:%M:%S')} {line}", flush=True)
            last = line
        if jobs and all(j["status"] == "completed" for j in jobs):
            print("\n=== 最终结果 ===", flush=True)
            bad = 0
            for j in sorted(jobs, key=lambda x: x["name"]):
                ok = j["conclusion"] == "success"
                bad += 0 if ok else 1
                print(f"  {'PASS' if ok else 'FAIL'}  {j['name']}  ({j['conclusion']})",
                      flush=True)
            print(f"总计 {len(jobs)} 个作业，失败 {bad} 个", flush=True)
            return 0 if bad == 0 else 1
        time.sleep(20)
    print("[watch] 超时未完成", flush=True)
    return 2


if __name__ == "__main__":
    sys.exit(main())
