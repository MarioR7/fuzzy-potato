from __future__ import annotations

import time
from collections import deque
from typing import Iterator, Set

from onchain.helius_volume_ws import HeliusVolumeWS


class HeliusDiscoveryWS:
    """
    Discovers new active mints using Helius WS activity.
    """

    def __init__(self):
        self.ws = HeliusVolumeWS()
        self._seen: Set[str] = set()
        self._queue = deque()

    def start(self):
        self.ws.start()

    def stop(self):
        self.ws.stop()

    def on_new_activity(self, mint: str):
        if mint in self._seen:
            return
        self._seen.add(mint)
        self._queue.append(mint)

    def iter_mints(self) -> Iterator[str]:
        """
        Generator yielding newly discovered active mints.
        """
        while True:
            if self._queue:
                yield self._queue.popleft()
            else:
                time.sleep(0.2)
