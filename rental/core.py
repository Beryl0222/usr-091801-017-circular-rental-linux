"""事件内核：内容寻址、规范序列化与只增事件存储。

同一事实重复投递会得到相同的事件 id，从而天然幂等；
所有重放都按 (发生时间, 事件 id) 的规范顺序折叠，保证任意
到达次序都能收敛到同一本账。
"""

from __future__ import annotations

import bisect
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone


def canonical_json(value) -> str:
    """确定性 JSON：键排序、无冗余空白，用于哈希与账本摘要。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(value) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def parse_instant(value: str) -> datetime:
    """解析 ISO 8601 时间，必须携带时区，统一归一到 UTC。"""
    if not isinstance(value, str):
        raise ValueError("时间必须是 ISO 8601 字符串")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    instant = datetime.fromisoformat(text)
    if instant.tzinfo is None:
        raise ValueError("时间必须携带时区")
    return instant.astimezone(timezone.utc)


def format_instant(instant: datetime) -> str:
    return instant.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def now_iso() -> str:
    return format_instant(datetime.now(timezone.utc))


@dataclass(frozen=True)
class Event:
    """内容寻址的不可变事件。"""

    id: str
    type: str
    occurred_at: str
    payload: dict

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "occurred_at": self.occurred_at,
            "payload": self.payload,
        }


def make_event(event_type: str, occurred_at: str, payload: dict) -> Event:
    """构造事件：时间先归一化，再对 (类型, 时间, 负载) 取内容哈希作为 id。"""
    at = format_instant(parse_instant(occurred_at))
    event_id = content_hash({"type": event_type, "occurred_at": at, "payload": payload})
    return Event(event_id, event_type, at, payload)


def _sort_key(event: Event):
    return (parse_instant(event.occurred_at), event.id)


class EventStore:
    """只增不改的事件存储：按 id 去重，按规范顺序排序。"""

    def __init__(self):
        self._events: dict[str, Event] = {}
        self._entries: list = []  # 有序 (sort_key, event)

    def append(self, event: Event) -> bool:
        """追加事件；重复事件返回 False 且状态不变。"""
        if event.id in self._events:
            return False
        self._events[event.id] = event
        bisect.insort(self._entries, (_sort_key(event), event))
        return True

    def contains(self, event_id: str) -> bool:
        return event_id in self._events

    def __len__(self) -> int:
        return len(self._events)

    def is_tail(self, event: Event) -> bool:
        """事件是否位于规范顺序末尾（可增量折叠而不必整体重放）。"""
        return bool(self._entries) and self._entries[-1][1].id == event.id

    def canonical(self) -> list[Event]:
        return [event for _, event in self._entries]

    def up_to(self, at: str) -> list[Event]:
        limit = parse_instant(at)
        return [event for _, event in self._entries if parse_instant(event.occurred_at) <= limit]
