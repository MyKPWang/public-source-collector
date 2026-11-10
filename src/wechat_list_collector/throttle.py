from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from .errors import CooldownActive
from .store import Store


@dataclass(frozen=True)
class RequestGate:
    store: Store
    minimum_interval_seconds: int
    cooldown_seconds: int
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep

    def wait_for_turn(self) -> int:
        now = int(self.clock())
        cooldown_until = self.store.get_state_int("cooldown_until")
        if cooldown_until > now:
            raise CooldownActive(cooldown_until)
        last_request_at = self.store.get_state_int("last_request_at")
        wait_seconds = max(0, last_request_at + self.minimum_interval_seconds - now)
        if wait_seconds:
            self.sleep(wait_seconds)
        # Persist just before the outbound call. A run lease keeps local callers serialized.
        self.store.set_state_int("last_request_at", int(self.clock()))
        return wait_seconds

    def record_rate_limit(self) -> int:
        until = int(self.clock()) + self.cooldown_seconds
        self.store.set_state_int("cooldown_until", until)
        return until
