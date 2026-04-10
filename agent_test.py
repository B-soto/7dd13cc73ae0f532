import asyncio
import websockets
from pprint import pprint
import json


NEON_WS = "wss://neonhealth.software/agent-puzzle/challenge"

async def ping():
    async with websockets.connect(NEON_WS) as ws:
        print("Connected!")
        msg = await ws.recv()
        pprint(json.loads(msg))

asyncio.run(ping())
