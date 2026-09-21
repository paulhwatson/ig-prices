from __future__ import annotations

import json

import pytest

from prices.ig import DEMO_BASE_URL, LIVE_BASE_URL, IGClient, IGError
from prices.settings import IGCredentials

CREDENTIALS = IGCredentials(username="u", password="p", api_key="k")


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if payload is None else json.dumps(payload)
        self.headers = headers or {}

    @property
    def ok(self):
        return 200 <= self.status_code < 300

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, login=None, gets=None):
        self._login = login or FakeResponse(headers={"CST": "c", "X-SECURITY-TOKEN": "t"})
        self._gets = list(gets or [])
        self.posts, self.requests = [], []

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append({"url": url, "json": json, "headers": headers})
        return self._login

    def get(self, url, params=None, headers=None, timeout=None):
        self.requests.append({"url": url, "params": params, "headers": headers})
        return self._gets.pop(0)


def market(epic, streamable=True, delay=0, name="X", status="TRADEABLE", type_="COMMODITIES"):
    return {
        "instrument": {"epic": epic, "streamingPricesAvailable": streamable,
                       "name": name, "type": type_},
        "snapshot": {"delayTime": delay, "marketStatus": status},
    }


def test_streamable_and_delayed_markets_are_distinguished():
    session = FakeSession(gets=[FakeResponse(payload={"marketDetails": [
        market("CC.D.LCO.USS.IP", streamable=True, delay=0),
        market("SI.D.FXAUS.DAILY.IP", streamable=False, delay=20, type_="SHARES"),
    ]})])

    statuses = IGClient(CREDENTIALS, session=session).market_status(
        ["CC.D.LCO.USS.IP", "SI.D.FXAUS.DAILY.IP"]
    )

    assert statuses["CC.D.LCO.USS.IP"].streamable is True
    assert statuses["SI.D.FXAUS.DAILY.IP"].streamable is False
    assert statuses["SI.D.FXAUS.DAILY.IP"].delay_minutes == 20


def test_login_happens_once_before_the_first_lookup():
    session = FakeSession(gets=[FakeResponse(payload={"marketDetails": []})] * 2)
    client = IGClient(CREDENTIALS, session=session)

    client.market_status(["A"])
    client.market_status(["B"])

    assert len(session.posts) == 1


def test_epics_are_batched_within_igs_limit():
    epics = [f"E{i}" for i in range(120)]
    session = FakeSession(gets=[FakeResponse(payload={"marketDetails": []})] * 3)

    IGClient(CREDENTIALS, session=session).market_status(epics)

    assert len(session.requests) == 3
    assert all(len(r["params"]["epics"].split(",")) <= 50 for r in session.requests)


def test_an_epic_ig_does_not_return_is_simply_absent():
    session = FakeSession(gets=[FakeResponse(payload={"marketDetails": [market("KNOWN")]})])

    statuses = IGClient(CREDENTIALS, session=session).market_status(["KNOWN", "RETIRED"])

    assert "KNOWN" in statuses
    assert "RETIRED" not in statuses


def test_a_failed_login_is_a_clear_error():
    session = FakeSession(login=FakeResponse(status_code=403, text="invalid credentials"))

    with pytest.raises(IGError, match="login failed"):
        IGClient(CREDENTIALS, session=session).market_status(["A"])


def test_a_login_without_tokens_is_an_error():
    session = FakeSession(login=FakeResponse(headers={}))

    with pytest.raises(IGError, match="missing"):
        IGClient(CREDENTIALS, session=session).market_status(["A"])


def test_a_failed_lookup_is_a_clear_error():
    session = FakeSession(gets=[FakeResponse(status_code=500, text="boom")])

    with pytest.raises(IGError, match="/markets failed"):
        IGClient(CREDENTIALS, session=session).market_status(["A"])


def test_an_unparseable_lookup_response_is_an_error():
    session = FakeSession(gets=[FakeResponse(status_code=200, text="not json")])

    with pytest.raises(IGError, match="unexpected IG"):
        IGClient(CREDENTIALS, session=session).market_status(["A"])


def test_the_auth_tokens_are_sent_on_lookups():
    session = FakeSession(gets=[FakeResponse(payload={"marketDetails": []})])

    IGClient(CREDENTIALS, session=session).market_status(["A"])

    headers = session.requests[0]["headers"]
    assert headers["CST"] == "c"
    assert headers["X-SECURITY-TOKEN"] == "t"
    assert headers["X-IG-API-KEY"] == "k"


def test_demo_credentials_use_the_demo_host():
    assert IGClient(CREDENTIALS, session=FakeSession())._base_url == DEMO_BASE_URL


def test_live_credentials_use_the_live_host():
    live = IGCredentials(username="u", password="p", api_key="k", acc_type="live")
    assert IGClient(live, session=FakeSession())._base_url == LIVE_BASE_URL
