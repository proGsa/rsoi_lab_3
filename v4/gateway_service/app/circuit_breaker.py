import threading
import time
from enum import Enum


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreaker:
    def __init__(self, service: str, failure_threshold: int = 3, recovery_timeout: float = 10.0, clock=time.monotonic):
        self.service = service
        self.failure_threshold = max(1, failure_threshold)
        self.recovery_timeout = recovery_timeout
        self._clock = clock
        self._lock = threading.Lock()
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._trial_in_flight = False

    @property
    def state(self) -> CircuitState:
        with self._lock:
            self._half_open_if_expired()
            return self._state

    def acquire(self) -> bool:
        with self._lock:
            self._half_open_if_expired()

            if self._state is CircuitState.OPEN:
                return False

            if self._state is CircuitState.HALF_OPEN:
                if self._trial_in_flight:
                    return False
                self._trial_in_flight = True

            return True

    def record_success(self) -> None:
        with self._lock:
            self._state = CircuitState.CLOSED
            self._failures = 0
            self._trial_in_flight = False

    def record_failure(self) -> None:
        with self._lock:
            if self._state is CircuitState.HALF_OPEN:
                self._trip()
                return

            self._failures += 1
            if self._failures >= self.failure_threshold:
                self._trip()

    def _half_open_if_expired(self) -> None:
        if self._state is not CircuitState.OPEN:
            return

        if (self._clock() - self._opened_at) >= self.recovery_timeout:
            self._state = CircuitState.HALF_OPEN
            self._trial_in_flight = False

    def _trip(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = self._clock()
        self._failures = self.failure_threshold
        self._trial_in_flight = False

    def __repr__(self) -> str: 
        return (
            f"CircuitBreaker({self.service!r}, state={self.state.value}, "
            f"failures={self._failures}/{self.failure_threshold})"
        )
