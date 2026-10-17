import threading
import time
from enum import Enum
import time
import httpx
from fastapi import HTTPException
from collections import deque

class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 3, recovery_timeout: float = 10.0, buffer_size: int = 5,):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout

        self.results = deque(maxlen=buffer_size)
        # self.failure_count = 0
        self.state = CircuitState.CLOSED
        self.opened_at = None
        self.lock = threading.Lock()

    def can_execute(self):
        with self.lock:
            if self.state == CircuitState.CLOSED:
                return True

            if self.state == CircuitState.OPEN:
                if time.monotonic() - self.opened_at >= self.recovery_timeout:
                    self.state = CircuitState.HALF_OPEN
                    self.probe_in_progress = True
                    print("Circuit state: HALF_OPEN")
                    return True

                print("Circuit state: OPEN -> fallback")
                return False

            if not self.probe_in_progress:
                self.probe_in_progress = True
                return True

            return False
    
    def record_success(self):
        with self.lock:
            if self.state == CircuitState.HALF_OPEN:
                self.results.clear()
                self.state = CircuitState.CLOSED
                self.opened_at = None
                self.probe_in_progress = False
                print("Circuit state: CLOSED")
                return

            if self.state == CircuitState.CLOSED:
                self.results.append(True)

    def record_failure(self):
        with self.lock:
            if self.state == CircuitState.HALF_OPEN:
                self.state = CircuitState.OPEN
                self.opened_at = time.monotonic()
                self.probe_in_progress = False
                print("Circuit state: OPEN")
                return

            if self.state != CircuitState.CLOSED:
                return

            self.results.append(False)

            failures = self.results.count(False)

            print(
                f"Circuit failures in buffer: "
                f"{failures}/{len(self.results)}"
            )

            if failures >= self.failure_threshold:
                self.state = CircuitState.OPEN
                self.opened_at = time.monotonic()
                print("Circuit state: OPEN")

def retry_request_until_success(method: str, url: str, headers: dict | None = None, json: dict | None = None, timeout: float = 5.0, retry_time: float = 10.0,):
    start_time = time.time()

    while time.time() - start_time < retry_time:
        try:
            response = httpx.request(
                method=method,
                url=url,
                headers=headers,
                json=json,
                timeout=timeout,
            )

            if response.status_code in (200, 204):
                return response

        except httpx.RequestError:
            pass

        time.sleep(1)

    raise HTTPException(status_code=500, detail="Upstream service is unavailable")