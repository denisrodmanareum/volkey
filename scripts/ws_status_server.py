#!/usr/bin/env python3
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import websockets

BASE = Path('/Users/riot91naver.com/Desktop/2026/volky-bot')
STATUS = BASE / 'dashboard' / 'data' / 'status.json'
HOST = '0.0.0.0'
PORT = 8765

clients: set = set()


async def handler(ws):
    clients.add(ws)
    try:
        if STATUS.exists():
            await ws.send(STATUS.read_text(encoding='utf-8'))
        async for _ in ws:
            pass
    finally:
        clients.discard(ws)


async def broadcaster():
    last = None
    while True:
        try:
            if STATUS.exists():
                cur = STATUS.read_text(encoding='utf-8')
                if cur != last:
                    last = cur
                    dead = []
                    for ws in list(clients):
                        try:
                            await ws.send(cur)
                        except Exception:
                            dead.append(ws)
                    for d in dead:
                        clients.discard(d)
        except Exception:
            pass
        await asyncio.sleep(1)


async def main():
    async with websockets.serve(handler, HOST, PORT, ping_interval=20, ping_timeout=20):
        await broadcaster()


if __name__ == '__main__':
    asyncio.run(main())
