"""HTTP for the government servers (PLAN.md §0): one request at a time, at least
``cdr.request_delay_s`` seconds apart, a descriptive User-Agent, and finite timeouts."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import httpx
import requests

log = logging.getLogger(__name__)

CONNECT_TIMEOUT_S = 60.0
READ_TIMEOUT_S = 900.0  # HMDA nationwide CSVs stream for minutes


class Throttle:
    """Keeps at least ``min_interval_s`` between one request finishing and the next starting."""

    def __init__(
        self,
        min_interval_s: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.min_interval_s = min_interval_s
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None

    def wait(self) -> None:
        """Call before a request: sleeps off whatever is left of the interval."""
        if self._last is not None:
            remaining = self.min_interval_s - (self._clock() - self._last)
            if remaining > 0:
                self._sleep(remaining)
        self._last = self._clock()

    def mark(self) -> None:
        """Call when a request (including a streamed body) has finished."""
        self._last = self._clock()


class PoliteSession(requests.Session):
    """A ``requests`` session that waits on a :class:`Throttle` before every request and
    always sends a timeout. Used to drive ``ffiec-data-collector``."""

    def __init__(
        self,
        throttle: Throttle,
        user_agent: str,
        timeout: tuple[float, float] = (CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
    ) -> None:
        super().__init__()
        self.throttle = throttle
        self.timeout = timeout
        self.headers["User-Agent"] = user_agent

    def request(self, method: str | bytes, url: str | bytes, *args: Any, **kwargs: Any):  # type: ignore[override]
        kwargs.setdefault("timeout", self.timeout)
        self.throttle.wait()
        log.debug("%s %s", method, url)
        try:
            return super().request(method, url, *args, **kwargs)
        finally:
            self.throttle.mark()


def polite_client(throttle: Throttle, user_agent: str, **kwargs: Any) -> httpx.Client:
    """An ``httpx`` client with the same rules. Redirect hops are throttled too. Callers that
    stream a body should call ``throttle.mark()`` once they have finished reading it."""

    def before(request: httpx.Request) -> None:
        throttle.wait()
        log.debug("%s %s", request.method, request.url)

    def after(response: httpx.Response) -> None:
        throttle.mark()

    kwargs.setdefault("timeout", httpx.Timeout(CONNECT_TIMEOUT_S, read=READ_TIMEOUT_S))
    kwargs.setdefault("follow_redirects", True)
    return httpx.Client(
        headers={"User-Agent": user_agent},
        event_hooks={"request": [before], "response": [after]},
        **kwargs,
    )
