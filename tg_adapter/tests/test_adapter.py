"""Tests ohne echte Telegram-Verbindung (Fake-Client)."""
import asyncio
import datetime as dt
from types import SimpleNamespace

import pytest
from aiohttp.test_utils import TestClient, TestServer
from telethon.tl import functions, types

from tg_adapter import adapter as adapter_mod
from tg_adapter.adapter import AdapterError, TelegramAdapter, UnknownUserError, split_text
from tg_adapter.api import make_app
from tg_adapter.config import Config
from tg_adapter.state import State

ADMIN = 1000
NOW = dt.datetime(2026, 10, 6, 12, 0, tzinfo=dt.timezone.utc)


class FakeClient:
    def __init__(self, known=(42, 43, ADMIN)):
        self.known = set(known)
        self.log = []  # chronologisch: ("typing", uid) / ("send", uid, text) / ("rpc", req)

    async def get_input_entity(self, uid):
        if uid not in self.known:
            raise ValueError("unknown")
        return types.InputPeerUser(uid, 0)

    async def send_message(self, peer, text):
        await asyncio.sleep(0)
        self.log.append(("send", peer.user_id, text))

    async def __call__(self, req):
        if isinstance(req, functions.messages.SetTypingRequest):
            self.log.append(("typing", req.peer.user_id))
        else:
            self.log.append(("rpc", req))
        await asyncio.sleep(0)

    def is_connected(self):
        return True


class ListSink:
    def __init__(self):
        self.events = []

    async def emit(self, e):
        self.events.append(e)

    async def close(self):
        pass


@pytest.fixture
def sleeps(monkeypatch):
    rec = []
    real_sleep = asyncio.sleep

    async def fake_sleep(s):
        if s:  # sleep(0) des Fake-Clients nicht mitzählen
            rec.append(s)
        await real_sleep(0)

    monkeypatch.setattr(adapter_mod.asyncio, "sleep", fake_sleep)
    return rec


@pytest.fixture
def ad(tmp_path):
    cfg = Config(api_id=1, api_hash="x", bot_token="t", admin_ids=frozenset({ADMIN}), max_delay=30)
    return TelegramAdapter(
        cfg, client=FakeClient(), state=State(str(tmp_path / "s.json")),
        inbound_sink=ListSink(), payment_sink=ListSink(),
    )


def msg_event(user_id, text, replies=None):
    replies = replies if replies is not None else []

    async def reply(t):
        replies.append(t)

    return SimpleNamespace(
        sender_id=user_id, message=SimpleNamespace(message=text, date=NOW), reply=reply
    ), replies


# ---------------------------------------------------------------- Outbound
async def test_typing_before_each_chunk_and_delay_order(ad, sleeps):
    n = await ad.send_message_chunks(42, ["Hi", "Wie gehts?"], [1.5, 2.0])
    assert n == 2
    assert ad.client.log == [
        ("typing", 42), ("send", 42, "Hi"),
        ("typing", 42), ("send", 42, "Wie gehts?"),
    ]
    assert sleeps == [1.5, 2.0]


async def test_long_delay_refreshes_typing(ad, sleeps):
    await ad.send_message_chunks(42, ["lang"], [9])
    assert [e[0] for e in ad.client.log] == ["typing", "typing", "typing", "send"]
    assert sleeps == [4.0, 4.0, 1.0]


async def test_zero_delay_still_sets_typing(ad, sleeps):
    await ad.send_message_chunks(42, ["x"], [0])
    assert ad.client.log == [("typing", 42), ("send", 42, "x")]
    assert sleeps == []


async def test_delay_is_capped(ad, sleeps):
    await ad.send_message_chunks(42, ["x"], [500])
    assert sum(sleeps) == 30


async def test_long_chunk_split(ad, sleeps):
    n = await ad.send_message_chunks(42, ["a " * 3000], [0])
    sends = [e for e in ad.client.log if e[0] == "send"]
    assert n == len(sends) == 2
    assert all(len(s[2]) <= 4096 for s in sends)


@pytest.mark.parametrize("chunks,delays", [
    (["a", "b"], [1]), ([], []), (["a"], [-1]), ([""], [0]), (["a"], ["1"]),
])
async def test_validation(ad, chunks, delays):
    with pytest.raises(AdapterError):
        await ad.send_message_chunks(42, chunks, delays)


async def test_unknown_user(ad):
    with pytest.raises(UnknownUserError):
        await ad.send_message_chunks(999, ["x"], [0])


async def test_same_user_never_interleaves(ad, sleeps):
    await asyncio.gather(
        ad.send_message_chunks(42, ["A1", "A2", "A3"], [1, 1, 1]),
        ad.send_message_chunks(42, ["B1", "B2"], [1, 1]),
    )
    texts = [e[2] for e in ad.client.log if e[0] == "send"]
    assert texts in (["A1", "A2", "A3", "B1", "B2"], ["B1", "B2", "A1", "A2", "A3"])


def test_split_text_prefers_newlines():
    parts = split_text("x" * 10 + "\n" + "y" * 10, limit=15)
    assert parts == ["x" * 10, "y" * 10]


# ---------------------------------------------------------------- Inbound + Admin
async def test_inbound_schema(ad):
    ev, _ = msg_event(42, "  Hallo  ")
    await ad.on_message(ev)
    assert ad.inbound.events == [{"user_id": 42, "text": "Hallo", "timestamp": int(NOW.timestamp())}]


async def test_empty_message_ignored(ad):
    ev, _ = msg_event(42, None)
    await ad.on_message(ev)
    assert ad.inbound.events == []


async def test_pause_resume_flow(ad, tmp_path):
    ev, replies = msg_event(ADMIN, "/pause 42")
    await ad.on_message(ev)
    assert "pausiert" in replies[-1]
    await ad.on_message(msg_event(42, "hallo?")[0])
    await ad.on_message(msg_event(43, "ich darf")[0])
    assert [e["user_id"] for e in ad.inbound.events] == [43]

    # Zustand überlebt Neustart
    assert State(str(tmp_path / "s.json")).is_paused(42)

    await ad.on_message(msg_event(ADMIN, "/paused")[0])
    ev, replies = msg_event(ADMIN, "/resume@MeinBot 42")
    await ad.on_message(ev)
    assert "wieder aktiv" in replies[-1]
    await ad.on_message(msg_event(42, "wieder da")[0])
    assert ad.inbound.events[-1]["text"] == "wieder da"


async def test_admin_command_without_id(ad):
    ev, replies = msg_event(ADMIN, "/pause")
    await ad.on_message(ev)
    assert replies == ["Benutzung: /pause <user_id>"]
    assert ad.inbound.events == []


async def test_non_admin_pause_is_normal_text(ad):
    await ad.on_message(msg_event(42, "/pause 43")[0])
    assert not ad.state.is_paused(43)
    assert ad.inbound.events[0]["text"] == "/pause 43"


async def test_admin_normal_text_forwarded(ad):
    await ad.on_message(msg_event(ADMIN, "/start")[0])
    assert ad.inbound.events[0]["text"] == "/start"


# ---------------------------------------------------------------- Stars
def precheckout(currency="XTR", amount=50):
    return types.UpdateBotPrecheckoutQuery(
        query_id=7, user_id=42, payload=b"pkg_basic", currency=currency, total_amount=amount
    )


async def test_precheckout_accepts_stars(ad):
    await ad.on_raw(precheckout())
    req = ad.client.log[-1][1]
    assert isinstance(req, functions.messages.SetBotPrecheckoutResultsRequest)
    assert req.query_id == 7 and req.success and req.error is None


@pytest.mark.parametrize("cur,amt", [("EUR", 50), ("XTR", 0)])
async def test_precheckout_rejects(ad, cur, amt):
    await ad.on_raw(precheckout(cur, amt))
    req = ad.client.log[-1][1]
    assert not req.success and req.error


def payment_update(charge="ch_1"):
    action = types.MessageActionPaymentSentMe(
        currency="XTR", total_amount=50, payload=b"pkg_basic",
        charge=types.PaymentCharge(id=charge, provider_charge_id=""),
    )
    msg = types.MessageService(id=1, peer_id=types.PeerUser(42), date=NOW, action=action)
    return types.UpdateNewMessage(message=msg, pts=1, pts_count=1)


async def test_successful_payment_emitted_once(ad):
    ad.state.pause(42)  # Zahlungen kommen trotz Pause durch
    await ad.on_raw(payment_update())
    await ad.on_raw(payment_update())  # Duplikat
    assert ad.payments.events == [{
        "event": "successful_payment", "user_id": 42, "currency": "XTR", "total_amount": 50,
        "payload": "pkg_basic", "charge_id": "ch_1", "provider_charge_id": "",
        "timestamp": int(NOW.timestamp()),
    }]
    assert ad.inbound.events == []


async def test_invoice_request(ad):
    await ad.send_stars_invoice(42, "Paket", "100 Nachrichten", "pkg_basic", 50)
    req = ad.client.log[-1][1]
    assert isinstance(req, functions.messages.SendMediaRequest)
    assert req.media.invoice.currency == "XTR"
    assert req.media.invoice.prices[0].amount == 50
    assert req.media.payload == b"pkg_basic"


async def test_invoice_validation(ad):
    with pytest.raises(AdapterError):
        await ad.send_stars_invoice(42, "x" * 40, "d", "p", 5)


# ---------------------------------------------------------------- HTTP-API
@pytest.fixture
async def http(ad):
    client = TestClient(TestServer(make_app(ad, token="geheim")))
    await client.start_server()
    yield client
    await client.close()


AUTH = {"Authorization": "Bearer geheim"}


async def test_api_auth(http):
    r = await http.post("/send", json={})
    assert r.status == 401
    assert (await http.get("/health")).status == 200


async def test_api_send_wait(http, ad, sleeps):
    r = await http.post("/send?wait=1", headers=AUTH,
                        json={"user_id": 42, "text_chunks": ["a", "b"], "delays": [0.1, 0.2]})
    assert r.status == 200 and (await r.json())["messages_sent"] == 2


async def test_api_send_background(http, ad, sleeps):
    r = await http.post("/send", headers=AUTH,
                        json={"user_id": 42, "text_chunks": ["a"], "delays": [0]})
    assert r.status == 202
    for _ in range(20):
        await asyncio.sleep(0)
    assert ("send", 42, "a") in ad.client.log


async def test_api_errors(http):
    r = await http.post("/send", headers=AUTH,
                        json={"user_id": 42, "text_chunks": ["a"], "delays": []})
    assert r.status == 400
    r = await http.post("/send?wait=1", headers=AUTH,
                        json={"user_id": 999, "text_chunks": ["a"], "delays": [0]})
    assert r.status == 404
    r = await http.post("/send", headers=AUTH, data="kein json")
    assert r.status == 400
