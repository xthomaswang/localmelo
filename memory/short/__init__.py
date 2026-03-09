from __future__ import annotations

from collections import deque

from ...schema import Message


class ShortTerm:
    def __init__(self, max_len: int) -> None:
        self._buf: deque[Message] = deque(maxlen=max_len)

    def append(self, msg: Message) -> None:
        self._buf.append(msg)

    def get_window(self) -> list[Message]:
        return list(self._buf)

    def clear(self) -> None:
        self._buf.clear()
