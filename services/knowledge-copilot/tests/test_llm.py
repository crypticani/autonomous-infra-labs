import pytest
import requests

from errors import UpstreamError
from llm import OllamaProvider


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self.payload


@pytest.mark.parametrize(
    "error, status",
    [
        (requests.exceptions.Timeout("slow"), 504),
        (requests.exceptions.HTTPError("404 model not found"), 502),
        (requests.exceptions.ConnectionError("refused"), 503),
    ],
)
def test_transport_failures_carry_the_right_status(monkeypatch, error, status):
    def boom(*args, **kwargs):
        raise error

    monkeypatch.setattr(requests, "post", boom)
    with pytest.raises(UpstreamError) as caught:
        OllamaProvider().generate("system", "user")

    # HTTPError is a RequestException, so a model that was never pulled used to
    # be reported as "backend unreachable". It has to be caught first.
    assert caught.value.status == status


def test_an_empty_body_is_a_502(monkeypatch):
    monkeypatch.setattr(
        requests, "post", lambda *a, **k: FakeResponse({"response": " "})
    )
    with pytest.raises(UpstreamError) as caught:
        OllamaProvider().generate("system", "user")
    assert caught.value.status == 502


def test_temperature_is_sent_under_options(monkeypatch):
    sent = {}

    def capture(url, json, timeout):
        sent.update(json)
        return FakeResponse({"response": "  ok  "})

    monkeypatch.setattr(requests, "post", capture)
    assert OllamaProvider().generate("sys", "usr", temperature=0.4) == "ok"
    # Ollama ignores a top-level temperature key; it reads "options".
    assert sent["options"] == {"temperature": 0.4}
