import logging
import os
from typing import Any

import httpx

from .circuit_breaker import CircuitBreaker
from .errors import ServiceUnavailable

logger = logging.getLogger("gateway.upstream")

LIBRARY = "library"
RESERVATION = "reservation"
RATING = "rating"

SERVICE_LABELS: dict[str, str] = {
    LIBRARY: "Library Service",
    RESERVATION: "Reservation Service",
    RATING: "Bonus Service",
}

SERVICE_URLS: dict[str, str] = {
    LIBRARY: os.getenv("LIBRARY_SERVICE_URL", "http://library-service:8060"),
    RESERVATION: os.getenv("RESERVATION_SERVICE_URL", "http://reservation-service:8070"),
    RATING: os.getenv("RATING_SERVICE_URL", "http://rating-service:8050"),
}

REQUEST_TIMEOUT = float(os.getenv("UPSTREAM_TIMEOUT", "3"))
FAILURE_THRESHOLD = int(os.getenv("CB_FAILURE_THRESHOLD", "3"))
RECOVERY_TIMEOUT = float(os.getenv("CB_RECOVERY_TIMEOUT", "10"))


class UpstreamClient:
    def __init__(self) -> None:
        self._clients: dict[str, httpx.Client] = {
            service: httpx.Client(base_url=url, timeout=REQUEST_TIMEOUT)
            for service, url in SERVICE_URLS.items()
        }
        self.breakers: dict[str, CircuitBreaker] = {
            service: CircuitBreaker(
                service=service,
                failure_threshold=FAILURE_THRESHOLD,
                recovery_timeout=RECOVERY_TIMEOUT,
            )
            for service in SERVICE_URLS
        }

    @staticmethod
    def label(service: str) -> str:
        return SERVICE_LABELS[service]

    def call(self, service: str, method: str, path: str, **kwargs) -> httpx.Response:
        breaker = self.breakers[service]

        if not breaker.acquire():
            raise ServiceUnavailable(self.label(service))

        client = self._clients[service]

        try:
            response = client.request(method, path, **kwargs)
        except httpx.RequestError as exc:
            breaker.record_failure()
            logger.warning(
                "%s unavailable: %s %s%s -> %s (%s)",
                self.label(service),
                method,
                client.base_url,
                path,
                type(exc).__name__,
                exc,
            )
            raise ServiceUnavailable(self.label(service)) from exc

        if response.status_code >= 500:
            breaker.record_failure()
            logger.warning(
                "%s replied %s on %s %s%s",
                self.label(service),
                response.status_code,
                method,
                client.base_url,
                path,
            )
            raise ServiceUnavailable(self.label(service))

        breaker.record_success()
        return response

    def try_call(self, service: str, method: str, path: str, **kwargs) -> httpx.Response | None:
        try:
            return self.call(service, method, path, **kwargs)
        except ServiceUnavailable:
            return None

    def get_json(self, service: str, path: str, **kwargs) -> Any:
        return self.call(service, "GET", path, **kwargs).json()

    def optional_get_json(self, service: str, path: str, **kwargs) -> Any | None:
        try:
            return self.get_json(service, path, **kwargs)
        except ServiceUnavailable:
            return None


upstream = UpstreamClient()
