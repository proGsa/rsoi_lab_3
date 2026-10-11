import threading
import time
from enum import Enum
import time
import httpx
from fastapi import HTTPException


class CircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    HALF_OPEN = "HALF_OPEN"


class CircuitBreaker:
    def __init__(self, failure_threshold: int = 3, recovery_timeout: float = 10.0):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout

        self.failure_count = 0
        self.state = CircuitState.CLOSED
        self.opened_at = None

    def can_execute(self):
        if self.state == CircuitState.CLOSED:
            return True

        if self.state == CircuitState.OPEN:
            if time.monotonic() - self.opened_at >= self.recovery_timeout:
                self.state = CircuitState.HALF_OPEN
                print("Circuit state: HALF_OPEN")
                return True

            print("Circuit state: OPEN -> fallback")
            return False

        # HALF_OPEN
        return True
    
    def record_success(self):
        self.failure_count = 0
        self.state = CircuitState.CLOSED
        self.opened_at = None
        print("Circuit state: CLOSED")

    def record_failure(self):
        self.failure_count += 1
        print(
            f"Circuit failure: "
            f"{self.failure_count}/{self.failure_threshold}"
        )

        if self.failure_count >= self.failure_threshold:
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