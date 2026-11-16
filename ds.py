"""Custom data structure: a singly linked list used as a FIFO queue.

Why not just collections.deque? Because the waitlist needs things deque can't
do well:
  * O(1) enqueue (tail pointer) and O(1) dequeue (head pointer)
  * O(1) lookup "is this person already waiting?" (hash map -> node)
  * Removing someone from the MIDDLE when they cancel their waitlist spot
  * Reporting a person's position in line

The queue also keeps a short history of operations so the visualizer page
can show what the structure is doing.
"""
from collections import deque
from itertools import count
import time

_op_ids = count(1)


class Node:
    __slots__ = ("data", "next")

    def __init__(self, data):
        self.data = data
        self.next = None


class LinkedQueue:
    def __init__(self):
        self.head = None
        self.tail = None
        self._index = {}  # key -> Node   (hash map for O(1) membership)
        self._size = 0
        self.history = deque(maxlen=25)  # recent operations, newest last

    def _log(self, op, key, note):
        self.history.append({"id": next(_op_ids), "op": op, "key": key, "note": note})

    def __len__(self):
        return self._size

    def __contains__(self, key):
        return key in self._index

    def enqueue(self, key, data=None):
        """Add to the back of the line. O(1)."""
        if key in self._index:
            raise ValueError(f"{key} is already in the queue")
        node = Node((key, data))
        if self.tail is None:
            self.head = self.tail = node
        else:
            self.tail.next = node
            self.tail = node
        self._index[key] = node
        self._size += 1
        self._log("enqueue", key, "tail.next = new node; tail = new node  (O(1))")

    def dequeue(self):
        """Remove and return (key, data) from the front. O(1)."""
        if self.head is None:
            return None
        node = self.head
        self.head = node.next
        if self.head is None:
            self.tail = None
        key, data = node.data
        del self._index[key]
        self._size -= 1
        self._log("dequeue", key, "head = head.next  (O(1))")
        return key, data

    def peek(self):
        return self.head.data if self.head else None

    def remove(self, key):
        """Remove someone from anywhere in the line. O(n) pointer walk."""
        if key not in self._index:
            return False
        prev, cur, steps = None, self.head, 0
        while cur:
            steps += 1
            if cur.data[0] == key:
                if prev:
                    prev.next = cur.next
                else:
                    self.head = cur.next
                if cur is self.tail:
                    self.tail = prev
                del self._index[key]
                self._size -= 1
                self._log("remove", key,
                          f"walked {steps} node(s), prev.next = node.next  (O(n))")
                return True
            prev, cur = cur, cur.next
        return False

    def position(self, key):
        """1-based place in line, or None."""
        pos, cur = 1, self.head
        while cur:
            if cur.data[0] == key:
                return pos
            pos += 1
            cur = cur.next
        return None

    def to_list(self):
        out, cur = [], self.head
        while cur:
            out.append(cur.data)
            cur = cur.next
        return out

    def snapshot(self):
        """Describe the structure (nodes, pointers, hash map) for the visualizer."""
        nodes, cur = [], self.head
        while cur:
            nodes.append({"key": cur.data[0], "name": cur.data[1],
                          "next": cur.next.data[0] if cur.next else None})
            cur = cur.next
        return {"head": self.head.data[0] if self.head else None,
                "tail": self.tail.data[0] if self.tail else None,
                "size": self._size,
                "nodes": nodes,
                "hash_map": sorted(self._index.keys()),
                "history": list(self.history)}


class RateLimiter:
    """Sliding-window limiter: a deque of timestamps per key.

    Old hits are popped from the left in O(1) each, so a key never costs
    more than `limit` entries. Used to stop password guessing on /api/login.
    """

    def __init__(self, limit, window_seconds):
        self.limit, self.window = limit, window_seconds
        self.hits = {}  # key -> deque[timestamps]

    def _live(self, key, now):
        dq = self.hits.setdefault(key, deque())
        while dq and now - dq[0] > self.window:
            dq.popleft()
        return dq

    def allow(self, key, now=None):
        return len(self._live(key, now if now is not None else time.time())) < self.limit

    def record(self, key, now=None):
        self._live(key, now if now is not None else time.time()).append(
            now if now is not None else time.time())

    def clear(self, key):
        self.hits.pop(key, None)
