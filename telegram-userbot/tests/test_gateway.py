"""Tests mit einem Fake-Telegram-Client (kein Netz nötig).

Ausführen:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gateway import Config, PauseList, SendJob, TelegramGateway, UnknownUser, UserPaused  # noqa: E402
from gateway.http_bridge import build_app  # noqa: E402


class FakeClient:
    def __init__(self, known=(42,)):
        self.log: list[tuple] = []
        self.known = set(known)
        self.handlers: list = []
        self.dialogs_loaded = False

    def add_event_handler(self, callback, event):
        self.handlers.append((callback, event))

    def action(self, entity, action, **kwargs):
        client = self
        if action == "cancel":
            async def cancel():
                client.log.append(("cancel", entity))
            return cancel()

        class TypingCM:
            async def __aenter__(self):
                client.log.append(("typing", entity, time.monotonic(), kwargs))

            async def __aexit__(self, *exc):
                return False

        return TypingCM()

    async def send_message(self, entity, text, **kwargs):
        self.log.append(("send", entity, text, time.monotonic(), kwargs))

    async def send_file(self, entity, path, **kwargs):
        self.log.append(("file", entity, path, kwargs))

    async def get_input_entity(self, user_id):
        if user_id in self.known:
            return f"peer{user_id}"
        raise ValueError("unknown")

    async def get_dialogs(self):
        self.dialogs_loaded = True

    async def send_read_acknowledge(self, entity):
        self.log.append(("read", entity))

    def is_connected(self):
        return True

    def kinds(self):
        return [entry[0] for entry in self.log]


class FakeEvent:
    def __init__(self, sender, text, *, chat_id=None):
        self._sender = sender
        self.raw_text = text
        self.sender_id = sender.id if sender else None
        self.chat_id = chat_id if chat_id is not None else self.sender_id
        self.date = datetime(2026, 10, 6, 16, 0, tzinfo=timezone.utc)
        self.is_private = True
        self.deleted = False

    async def get_sender(self):
        return self._sender

    async def delete(self):
        self.deleted = True


def user(uid=42, **flags):
    base = dict(id=uid, username="tester", first_name="Max", bot=False, is_self=False,
                deleted=False, support=False)
    base.update(flags)
    return SimpleNamespace(**base)


def make(cfg_overrides=None, known=(42,), pause_path=None):
    cfg = Config(api_id=1, api_hash="x", api_token="secret", pause_file=None,
                 **(cfg_overrides or {}))
    client = FakeClient(known)
    received = []

    async def handler(msg):
        received.append(msg)

    gw = TelegramGateway(client, cfg, PauseList(pause_path), handler)
    gw.register()
    return gw, client, received, cfg


class SendJobValidation(unittest.TestCase):
    def test_valid(self):
        job = SendJob.from_dict({"user_id": 1, "chunks": ["a", "b"], "delays": [1, 2.5]})
        self.assertEqual(job.delays, (1.0, 2.5))

    def test_invalid(self):
        bad = [
            {"user_id": True, "chunks": ["a"], "delays": [1]},
            {"user_id": "1", "chunks": ["a"], "delays": [1]},
            {"user_id": 1, "chunks": ["a", "b"], "delays": [1]},
            {"user_id": 1, "chunks": [], "delays": []},
            {"user_id": 1, "chunks": ["  "], "delays": [1]},
            {"user_id": 1, "chunks": ["a"], "delays": [-1]},
            {"user_id": 1, "chunks": ["a"], "delays": [float("nan")]},
            {"user_id": 1, "chunks": ["a"], "delays": [999]},
            {"user_id": 1, "chunks": ["x" * 5000], "delays": [1]},
            [1, 2, 3],
        ]
        for data in bad:
            with self.subTest(data=str(data)[:60]):
                with self.assertRaises(ValueError):
                    SendJob.from_dict(data, max_delay=120)


class Outbound(unittest.IsolatedAsyncioTestCase):
    async def test_typing_then_send_with_delays_in_order(self):
        gw, client, _, _ = make()
        job = SendJob.from_dict({"user_id": 42, "chunks": ["hahaha okay", "was hast du danach gemacht?"],
                                 "delays": [0.2, 0.3]})
        sent = await gw.send_natural_chunks(job)
        self.assertEqual(sent, 2)
        self.assertEqual(client.kinds(), ["read", "typing", "send", "typing", "send"])

        t1, s1, t2, s2 = [e for e in client.log if e[0] in ("typing", "send")]
        self.assertGreaterEqual(s1[3] - t1[2], 0.19)
        self.assertGreaterEqual(s2[3] - t2[2], 0.29)
        self.assertEqual([s1[2], s2[2]], ["hahaha okay", "was hast du danach gemacht?"])
        self.assertIsNone(s1[4]["parse_mode"])
        self.assertFalse(t1[3]["auto_cancel"])

    async def test_jobs_for_same_user_do_not_interleave(self):
        gw, client, _, _ = make()
        a = gw.submit(SendJob(42, ("a1", "a2"), (0.1, 0.1)))
        b = gw.submit(SendJob(42, ("b1",), (0.0,)))
        await asyncio.gather(a, b)
        texts = [e[2] for e in client.log if e[0] == "send"]
        self.assertEqual(texts, ["a1", "a2", "b1"])

    async def test_unknown_user_loads_dialogs_then_fails(self):
        gw, client, _, _ = make(known=())
        with self.assertRaises(UnknownUser):
            await gw.send_natural_chunks(SendJob(7, ("hi",), (0,)))
        self.assertTrue(client.dialogs_loaded)


class AdminCommands(unittest.IsolatedAsyncioTestCase):
    async def test_pause_cancels_running_job_and_blocks(self):
        gw, client, received, _ = make()
        task = gw.submit(SendJob(42, ("eins", "zwei"), (0.05, 1.0)))
        await asyncio.sleep(0.2)  # erster Chunk raus, zweiter tippt

        cmd = FakeEvent(user(999, is_self=True), "/pause", chat_id=42)
        self.assertTrue(TelegramGateway._is_command(cmd))
        await gw._on_command(cmd)
        await asyncio.gather(task, return_exceptions=True)

        self.assertTrue(task.cancelled())
        self.assertTrue(cmd.deleted)
        self.assertEqual([e[2] for e in client.log if e[0] == "send"], ["eins"])
        self.assertIn("cancel", client.kinds())

        with self.assertRaises(UserPaused):
            gw.submit(SendJob(42, ("x",), (0,)))

        await gw._on_incoming(FakeEvent(user(42), "hallo?"))
        await asyncio.sleep(0.05)
        self.assertEqual(received, [])

        await gw._on_command(FakeEvent(user(999, is_self=True), "/RESUME", chat_id=42))
        await gw._on_incoming(FakeEvent(user(42), "jetzt aber"))
        await asyncio.sleep(0.05)
        self.assertEqual([m.message_text for m in received], ["jetzt aber"])

    def test_command_pattern(self):
        for text, expected in [("/pause", True), (" /resume ", True), ("/pause bitte", False),
                               ("pause", False), ("/pauses", False)]:
            self.assertEqual(TelegramGateway._is_command(FakeEvent(user(), text)), expected, text)


class Inbound(unittest.IsolatedAsyncioTestCase):
    async def test_event_fields(self):
        gw, _, received, _ = make()
        await gw._on_incoming(FakeEvent(user(42), "schau mal https://example.com/a_b"))
        await asyncio.sleep(0.01)
        msg = received[0]
        self.assertEqual(msg.to_dict(), {
            "user_id": 42, "username": "tester", "first_name": "Max",
            "message_text": "schau mal https://example.com/a_b",
            "timestamp": int(datetime(2026, 10, 6, 16, 0, tzinfo=timezone.utc).timestamp()),
        })

    async def test_filtered(self):
        gw, _, received, _ = make()
        for ev in [
            FakeEvent(user(42), ""),
            FakeEvent(user(42), "   "),
            FakeEvent(user(43, bot=True), "bot"),
            FakeEvent(user(777000), "Login code: 12345"),
            FakeEvent(user(44, deleted=True), "geist"),
            FakeEvent(user(45, support=True), "support"),
        ]:
            await gw._on_incoming(ev)
        await asyncio.sleep(0.01)
        self.assertEqual(received, [])


class PauseListPersistence(unittest.TestCase):
    def test_roundtrip_and_corrupt_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "paused.json"
            pl = PauseList(str(path))
            self.assertTrue(pl.pause(5))
            self.assertFalse(pl.pause(5))
            pl.pause(3)
            self.assertEqual(PauseList(str(path)).snapshot(), [3, 5])
            pl.resume(5)
            self.assertEqual(json.loads(path.read_text()), {"paused": [3]})
            path.write_text("{kaputt")
            with self.assertRaises(RuntimeError):
                PauseList(str(path))


class Http(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.gw, self.client, _, cfg = make()
        self.http = TestClient(TestServer(build_app(self.gw, cfg, self.client)))
        await self.http.start_server()
        self.auth = {"Authorization": "Bearer secret"}

    async def asyncTearDown(self):
        await self.http.close()

    async def test_auth(self):
        r = await self.http.post("/send", json={})
        self.assertEqual(r.status, 401)
        r = await self.http.post("/send", json={}, headers={"Authorization": "Bearer falsch"})
        self.assertEqual(r.status, 401)
        r = await self.http.get("/health")
        self.assertEqual(r.status, 200)

    async def test_send_flow(self):
        body = {"user_id": 42, "chunks": ["a", "b"], "delays": [0.01, 0.01]}
        r = await self.http.post("/send?wait=1", json=body, headers=self.auth)
        self.assertEqual((r.status, await r.json()), (200, {"status": "sent", "sent": 2}))

        r = await self.http.post("/send", json=body, headers=self.auth)
        self.assertEqual(r.status, 202)

        r = await self.http.post("/send", json={**body, "delays": [1]}, headers=self.auth)
        self.assertEqual(r.status, 400)

        r = await self.http.post("/send?wait=1", json={**body, "user_id": 8}, headers=self.auth)
        self.assertEqual(r.status, 404)

        self.gw.pauses.pause(42)
        r = await self.http.post("/send", json=body, headers=self.auth)
        self.assertEqual(r.status, 409)
        r = await self.http.get("/paused", headers=self.auth)
        self.assertEqual(await r.json(), {"paused": [42]})

    async def test_wait_reports_cancel_on_pause(self):
        body = {"user_id": 42, "chunks": ["a", "b"], "delays": [0.01, 2]}
        req = asyncio.ensure_future(self.http.post("/send?wait=1", json=body, headers=self.auth))
        await asyncio.sleep(0.2)
        await self.gw._on_command(FakeEvent(user(1, is_self=True), "/pause", chat_id=42))
        r = await req
        self.assertEqual(r.status, 409)
        self.assertEqual((await r.json())["status"], "cancelled")


class Files(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.content = root / "content"
        self.content.mkdir()
        (self.content / "guide.pdf").write_bytes(b"%PDF")
        (root / "geheim.txt").write_text("x")
        self.gw, self.client, _, cfg = make({"content_dir": str(self.content)})
        self.http = TestClient(TestServer(build_app(self.gw, cfg, self.client)))
        await self.http.start_server()
        self.auth = {"Authorization": "Bearer secret"}

    async def asyncTearDown(self):
        await self.http.close()
        self.tmp.cleanup()

    async def test_send_file(self):
        r = await self.http.post("/send_file", json={"user_id": 42, "file": "guide.pdf", "caption": "Hi"},
                                 headers=self.auth)
        self.assertEqual(r.status, 200)
        self.assertEqual(self.client.log[-1][:3], ("file", "peer42", str(self.content / "guide.pdf")))
        self.assertEqual(self.client.log[-1][3]["caption"], "Hi")

    async def test_send_file_errors(self):
        for name in ("../geheim.txt", "/etc/passwd", "fehlt.pdf", "", None):
            r = await self.http.post("/send_file", json={"user_id": 42, "file": name}, headers=self.auth)
            self.assertEqual(r.status, 400, name)
        r = await self.http.post("/send_file", json={"user_id": 8, "file": "guide.pdf"}, headers=self.auth)
        self.assertEqual(r.status, 404)
        self.gw.pauses.pause(42)
        r = await self.http.post("/send_file", json={"user_id": 42, "file": "guide.pdf"}, headers=self.auth)
        self.assertEqual(r.status, 409)
        r = await self.http.post("/send_file", json={"user_id": 42, "file": "guide.pdf"})
        self.assertEqual(r.status, 401)
        self.assertFalse([e for e in self.client.log if e[0] == "file"])


if __name__ == "__main__":
    unittest.main()
