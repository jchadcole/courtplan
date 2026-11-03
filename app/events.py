"""In-process pub/sub for Server-Sent Events (live schedule updates and notifications)."""
import asyncio
import json


class Broker:
    def __init__(self):
        self.loop: asyncio.AbstractEventLoop | None = None
        self.subs: dict[int, set[asyncio.Queue]] = {}

    def subscribe(self, tid: int) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self.subs.setdefault(tid, set()).add(q)
        return q

    def unsubscribe(self, tid: int, q):
        self.subs.get(tid, set()).discard(q)

    def publish(self, tid: int, event: dict):
        data = json.dumps(event)
        loop = self.loop
        if loop is None:
            return

        def _put():
            for q in list(self.subs.get(tid, ())):
                if q.full():
                    continue
                q.put_nowait(data)
        try:
            loop.call_soon_threadsafe(_put)
        except RuntimeError:
            pass


broker = Broker()
