"""Per-egress first-use-anchored cooldown scheduler."""

from dataclasses import dataclass
import time


@dataclass(frozen=True)
class Node:
    name: str
    exit_ip: str


class CooldownScheduler:
    """Reserve at most `quota` requests per exit IP per anchored window.

    The window begins with the first request. At `first_request + window`, the
    count resets even if the quota was not exhausted. Several proxy nodes may
    share one public exit IP, so their quota and first-use time are shared.
    A reservation is counted even when the request fails: the server may have
    received it before the client detected a transport error.
    """

    def __init__(self, nodes, quota=5, window=60.0, clock=None):
        if not nodes or quota < 1 or window <= 0:
            raise ValueError("nodes, quota and window must be positive")
        self.nodes = tuple(nodes)
        self.quota = quota
        self.window = float(window)
        self.clock = clock or time.monotonic
        self.window_start = {node.exit_ip: None for node in self.nodes}
        self.used = {node.exit_ip: 0 for node in self.nodes}
        self.blocked_until = {node.exit_ip: 0.0 for node in self.nodes}
        self.cursor = 0

    def _ready_at(self, exit_ip, now):
        start = self.window_start[exit_ip]
        if start is not None and now >= start + self.window:
            self.window_start[exit_ip] = None
            self.used[exit_ip] = 0
            start = None
        quota_ready = (start + self.window
                       if self.used[exit_ip] >= self.quota else now)
        return max(quota_ready, self.blocked_until[exit_ip])

    def reserve(self):
        """Return (node, delay); node is None when every exit is cooling down."""
        now = self.clock()
        soonest = float("inf")
        for offset in range(len(self.nodes)):
            position = (self.cursor + offset) % len(self.nodes)
            node = self.nodes[position]
            ready_at = self._ready_at(node.exit_ip, now)
            if ready_at <= now:
                if self.window_start[node.exit_ip] is None:
                    self.window_start[node.exit_ip] = now
                self.used[node.exit_ip] += 1
                self.cursor = (position + 1) % len(self.nodes)
                return node, 0.0
            soonest = min(soonest, ready_at)
        return None, max(0.0, soonest - now)

    def penalize(self, exit_ip, seconds):
        if exit_ip not in self.used:
            raise KeyError(exit_ip)
        self.blocked_until[exit_ip] = max(
            self.blocked_until[exit_ip], self.clock() + max(0.0, seconds))

