import httpx
import requests

from armreset.http import (
    CONNECT_TIMEOUT_S,
    READ_TIMEOUT_S,
    PoliteSession,
    Throttle,
    polite_client,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


class RecordingThrottle(Throttle):
    def __init__(self) -> None:
        super().__init__(5)
        self.events: list[str] = []

    def wait(self) -> None:
        self.events.append("wait")

    def mark(self) -> None:
        self.events.append("mark")


def test_throttle_keeps_the_interval_between_requests() -> None:
    clock = FakeClock()
    throttle = Throttle(5, clock=clock, sleep=clock.sleep)
    throttle.wait()
    assert clock.slept == []  # first request goes straight out
    clock.now += 1
    throttle.wait()
    assert clock.slept == [4.0]
    throttle.mark()
    clock.now += 5
    throttle.wait()
    assert clock.slept == [4.0]  # already waited long enough
    clock.now += 60  # a long download...
    throttle.mark()  # ...counts from when it finished
    clock.now += 2
    throttle.wait()
    assert clock.slept == [4.0, 3.0]


class CannedAdapter(requests.adapters.BaseAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[requests.PreparedRequest, dict]] = []

    def send(self, request, **kwargs):  # type: ignore[override]
        self.calls.append((request, kwargs))
        response = requests.Response()
        response.status_code = 200
        response._content = b"ok"
        response.request = request
        response.url = request.url
        return response

    def close(self) -> None:
        pass


def test_polite_session_throttles_and_sets_user_agent_and_timeout() -> None:
    throttle = RecordingThrottle()
    session = PoliteSession(throttle, "arm-reset-research/test")
    adapter = CannedAdapter()
    session.mount("https://", adapter)
    session.get("https://example.test/a")
    session.post("https://example.test/b", data={"x": "1"})
    assert throttle.events == ["wait", "mark", "wait", "mark"]
    request, kwargs = adapter.calls[0]
    assert request.headers["User-Agent"] == "arm-reset-research/test"
    assert kwargs["timeout"] == (CONNECT_TIMEOUT_S, READ_TIMEOUT_S)


def test_polite_client_throttles_every_hop_including_redirects() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "https://example.test/final"})
        return httpx.Response(200, text="done")

    throttle = RecordingThrottle()
    with polite_client(throttle, "ua/1", transport=httpx.MockTransport(handler)) as client:
        response = client.get("https://example.test/start")
    assert response.text == "done"
    assert throttle.events == ["wait", "mark", "wait", "mark"]
    assert response.request.headers["User-Agent"] == "ua/1"
