"""
Integration tests for voip.server, the WebSocket signaling server.

Tests use websockets.connect() as a client against a real server running on
an ephemeral port. Because pytest-asyncio is not available, each test is a
plain synchronous function that wraps an async coroutine with asyncio.run().

Helper pattern:
    def test_something():
        asyncio.run(_test_something())

    async def _test_something():
        async with server_fixture() as srv:
            ...
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import AsyncIterator

import pytest
import websockets
from websockets.asyncio.client import connect

from voip.server import SignalingServer


# ---------------------------------------------------------------------------
# Server fixture
# ---------------------------------------------------------------------------

@asynccontextmanager
async def server_fixture() -> AsyncIterator[SignalingServer]:
    """
    Async context manager that starts a SignalingServer on an ephemeral port,
    yields it, then cancels it and waits for cleanup.

    Usage:
        async with server_fixture() as srv:
            url = f"ws://127.0.0.1:{srv.port}"
            ...
    """
    srv = SignalingServer(host="127.0.0.1", port=0)
    task = asyncio.create_task(srv.start())
    # Wait until the server is accepting connections
    await asyncio.wait_for(srv._ready.wait(), timeout=5.0)
    try:
        yield srv
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def recv_json(ws) -> dict:
    """Receive one JSON message from a websocket."""
    raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
    return json.loads(raw)


async def send_json(ws, msg: dict) -> None:
    """Send a dict as a JSON message."""
    await ws.send(json.dumps(msg))


async def register(ws, username: str) -> dict:
    """Register a user and return the 'registered' response."""
    await send_json(ws, {"type": "register", "username": username})
    return await recv_json(ws)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_register_flow():
    """Client sends register, receives 'registered' response with empty users list."""

    async def _run():
        async with server_fixture() as srv:
            url = f"ws://127.0.0.1:{srv.port}"
            async with connect(url) as ws:
                response = await register(ws, "alice")
                assert response["type"] == "registered"
                assert response["users"] == []

    asyncio.run(_run())


def test_user_online_broadcast():
    """Second client registering triggers 'user_online' on first client."""

    async def _run():
        async with server_fixture() as srv:
            url = f"ws://127.0.0.1:{srv.port}"
            async with connect(url) as alice_ws, connect(url) as bob_ws:
                # Alice registers first
                resp_alice = await register(alice_ws, "alice")
                assert resp_alice["type"] == "registered"

                # Bob registers, alice should receive user_online
                resp_bob = await register(bob_ws, "bob")
                assert resp_bob["type"] == "registered"
                assert "alice" in resp_bob["users"]

                # Alice gets notified of bob
                notification = await recv_json(alice_ws)
                assert notification["type"] == "user_online"
                assert notification["username"] == "bob"

    asyncio.run(_run())


def test_invite_routing():
    """Alice invites bob; bob receives invite with sdp and call_id."""

    async def _run():
        async with server_fixture() as srv:
            url = f"ws://127.0.0.1:{srv.port}"
            async with connect(url) as alice_ws, connect(url) as bob_ws:
                await register(alice_ws, "alice")
                await register(bob_ws, "bob")
                # alice gets user_online for bob
                await recv_json(alice_ws)

                await send_json(alice_ws, {
                    "type": "invite",
                    "target": "bob",
                    "sdp": "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\nm=audio 9 UDP/TLS/RTP/SAVP 0\r\n",
                    "call_id": "call-abc-123",
                })

                invite = await recv_json(bob_ws)
                assert invite["type"] == "invite"
                assert invite["username"] == "alice"
                assert invite["call_id"] == "call-abc-123"
                assert "v=0" in invite["sdp"]

    asyncio.run(_run())


def test_answer_routing():
    """Bob sends answer; alice receives it."""

    async def _run():
        async with server_fixture() as srv:
            url = f"ws://127.0.0.1:{srv.port}"
            async with connect(url) as alice_ws, connect(url) as bob_ws:
                await register(alice_ws, "alice")
                await register(bob_ws, "bob")
                await recv_json(alice_ws)  # user_online for bob

                # alice invites bob
                await send_json(alice_ws, {
                    "type": "invite",
                    "target": "bob",
                    "sdp": "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\nm=audio 9 UDP/TLS/RTP/SAVP 0\r\n",
                    "call_id": "call-xyz",
                })
                await recv_json(bob_ws)  # invite forwarded to bob

                # bob answers
                await send_json(bob_ws, {
                    "type": "answer",
                    "target": "alice",
                    "sdp": "v=0\r\no=- 2 1 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\nm=audio 9 UDP/TLS/RTP/SAVP 0\r\n",
                    "call_id": "call-xyz",
                })

                answer = await recv_json(alice_ws)
                assert answer["type"] == "answer"
                assert answer["username"] == "bob"
                assert answer["call_id"] == "call-xyz"
                assert "v=0" in answer["sdp"]

    asyncio.run(_run())


def test_ice_relay():
    """ICE candidate from alice reaches bob."""

    async def _run():
        async with server_fixture() as srv:
            url = f"ws://127.0.0.1:{srv.port}"
            async with connect(url) as alice_ws, connect(url) as bob_ws:
                await register(alice_ws, "alice")
                await register(bob_ws, "bob")
                await recv_json(alice_ws)  # user_online for bob

                candidate_data = {
                    "candidate": "candidate:1 1 UDP 2122 192.168.1.5 54321 typ host",
                    "sdpMid": "0",
                    "sdpMLineIndex": 0,
                }
                await send_json(alice_ws, {
                    "type": "ice",
                    "target": "bob",
                    "candidate": candidate_data,
                })

                ice_msg = await recv_json(bob_ws)
                assert ice_msg["type"] == "ice"
                assert ice_msg["username"] == "alice"
                assert ice_msg["candidate"] == candidate_data

    asyncio.run(_run())


def test_bye_routing():
    """Alice sends bye; bob receives it."""

    async def _run():
        async with server_fixture() as srv:
            url = f"ws://127.0.0.1:{srv.port}"
            async with connect(url) as alice_ws, connect(url) as bob_ws:
                await register(alice_ws, "alice")
                await register(bob_ws, "bob")
                await recv_json(alice_ws)  # user_online for bob

                await send_json(alice_ws, {
                    "type": "bye",
                    "target": "bob",
                    "call_id": "call-bye-test",
                })

                bye_msg = await recv_json(bob_ws)
                assert bye_msg["type"] == "bye"
                assert bye_msg["username"] == "alice"
                assert bye_msg["call_id"] == "call-bye-test"

    asyncio.run(_run())


def test_reject_routing():
    """Bob rejects; alice receives 'rejected'."""

    async def _run():
        async with server_fixture() as srv:
            url = f"ws://127.0.0.1:{srv.port}"
            async with connect(url) as alice_ws, connect(url) as bob_ws:
                await register(alice_ws, "alice")
                await register(bob_ws, "bob")
                await recv_json(alice_ws)  # user_online for bob

                # alice invites
                await send_json(alice_ws, {
                    "type": "invite",
                    "target": "bob",
                    "sdp": "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\nm=audio 9 UDP/TLS/RTP/SAVP 0\r\n",
                    "call_id": "call-reject",
                })
                await recv_json(bob_ws)  # invite

                # bob rejects
                await send_json(bob_ws, {
                    "type": "reject",
                    "target": "alice",
                    "call_id": "call-reject",
                })

                rejected = await recv_json(alice_ws)
                assert rejected["type"] == "rejected"
                assert rejected["username"] == "bob"
                assert rejected["call_id"] == "call-reject"

    asyncio.run(_run())


def test_invite_unknown_user():
    """Inviting a user who is not registered returns error 404."""

    async def _run():
        async with server_fixture() as srv:
            url = f"ws://127.0.0.1:{srv.port}"
            async with connect(url) as alice_ws:
                await register(alice_ws, "alice")

                await send_json(alice_ws, {
                    "type": "invite",
                    "target": "ghost",
                    "sdp": "",
                    "call_id": "call-ghost",
                })

                error = await recv_json(alice_ws)
                assert error["type"] == "error"
                assert error["code"] == 404
                assert "ghost" in error["message"]

    asyncio.run(_run())


def test_not_registered_returns_401():
    """Sending invite before register returns error 401."""

    async def _run():
        async with server_fixture() as srv:
            url = f"ws://127.0.0.1:{srv.port}"
            async with connect(url) as ws:
                await send_json(ws, {
                    "type": "invite",
                    "target": "bob",
                    "sdp": "",
                    "call_id": "x",
                })
                error = await recv_json(ws)
                assert error["type"] == "error"
                assert error["code"] == 401

    asyncio.run(_run())


def test_invalid_json_returns_400():
    """Sending non-JSON returns error 400."""

    async def _run():
        async with server_fixture() as srv:
            url = f"ws://127.0.0.1:{srv.port}"
            async with connect(url) as ws:
                await ws.send("this is not json{{{")
                error = await recv_json(ws)
                assert error["type"] == "error"
                assert error["code"] == 400
                assert "JSON" in error["message"] or "json" in error["message"].lower()

    asyncio.run(_run())


def test_disconnection_cleanup():
    """Alice disconnects; bob receives 'user_offline'."""

    async def _run():
        async with server_fixture() as srv:
            url = f"ws://127.0.0.1:{srv.port}"
            # Open both connections, then close alice explicitly
            alice_ws = await connect(url).__aenter__()
            bob_ws = await connect(url).__aenter__()
            try:
                await register(alice_ws, "alice")
                await register(bob_ws, "bob")
                await recv_json(alice_ws)  # user_online for bob

                # Close only alice's connection
                await alice_ws.close()

                # Bob should receive user_offline
                offline = await asyncio.wait_for(bob_ws.recv(), timeout=5.0)
                msg = json.loads(offline)
                assert msg["type"] == "user_offline"
                assert msg["username"] == "alice"
            finally:
                await bob_ws.close()

    asyncio.run(_run())


def test_call_ended_on_disconnect():
    """Alice disconnects mid-call; bob receives 'bye'."""

    async def _run():
        async with server_fixture() as srv:
            url = f"ws://127.0.0.1:{srv.port}"
            async with connect(url) as bob_ws:
                async with connect(url) as alice_ws:
                    await register(alice_ws, "alice")
                    await register(bob_ws, "bob")
                    await recv_json(alice_ws)  # user_online for bob

                    # Establish a call: alice invites bob
                    await send_json(alice_ws, {
                        "type": "invite",
                        "target": "bob",
                        "sdp": "v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\nm=audio 9 UDP/TLS/RTP/SAVP 0\r\n",
                        "call_id": "active-call",
                    })
                    invite_fwd = await recv_json(bob_ws)
                    assert invite_fwd["type"] == "invite"

                    # bob answers
                    await send_json(bob_ws, {
                        "type": "answer",
                        "target": "alice",
                        "sdp": "v=0\r\no=- 2 1 IN IP4 127.0.0.1\r\ns=-\r\nt=0 0\r\nm=audio 9 UDP/TLS/RTP/SAVP 0\r\n",
                        "call_id": "active-call",
                    })
                    await recv_json(alice_ws)  # answer forwarded

                    # alice disconnects (exits inner context)

                # bob should receive user_offline then/or bye
                # The server sends bye before or at same time as user_offline
                messages: list[dict] = []
                for _ in range(2):
                    try:
                        raw = await asyncio.wait_for(bob_ws.recv(), timeout=3.0)
                        messages.append(json.loads(raw))
                    except asyncio.TimeoutError:
                        break

                types = {m["type"] for m in messages}
                # Must get at least a bye (call ended) and user_offline
                assert "bye" in types, f"Expected 'bye' in {types}"
                assert "user_offline" in types, f"Expected 'user_offline' in {types}"

                bye_msg = next(m for m in messages if m["type"] == "bye")
                assert bye_msg["username"] == "alice"
                assert bye_msg["call_id"] == "active-call"

    asyncio.run(_run())


def test_unknown_message_type():
    """Sending an unknown message type returns error 400."""

    async def _run():
        async with server_fixture() as srv:
            url = f"ws://127.0.0.1:{srv.port}"
            async with connect(url) as ws:
                await register(ws, "alice")

                await send_json(ws, {"type": "ping_unknown"})
                error = await recv_json(ws)
                assert error["type"] == "error"
                assert error["code"] == 400
                assert "ping_unknown" in error["message"]

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Robustness: malformed messages and duplicate registration
# ---------------------------------------------------------------------------

def test_non_object_json_rejected():
    """A JSON array is valid JSON but not a message, so it is rejected with 400
    rather than crashing the connection handler."""

    async def _run():
        async with server_fixture() as srv:
            url = f"ws://127.0.0.1:{srv.port}"
            async with connect(url) as ws:
                await ws.send(json.dumps([1, 2, 3]))
                response = await recv_json(ws)
                assert response["type"] == "error"
                assert response["code"] == 400
                # The connection stays usable after the error.
                registered = await register(ws, "alice")
                assert registered["type"] == "registered"

    asyncio.run(_run())


def test_duplicate_username_rejected():
    """A name held by a live connection cannot be taken over by another."""

    async def _run():
        async with server_fixture() as srv:
            url = f"ws://127.0.0.1:{srv.port}"
            async with connect(url) as first_ws, connect(url) as second_ws:
                assert (await register(first_ws, "alice"))["type"] == "registered"

                await send_json(second_ws, {"type": "register", "username": "alice"})
                response = await recv_json(second_ws)
                assert response["type"] == "error"
                assert response["code"] == 409

    asyncio.run(_run())


def test_original_connection_survives_duplicate_attempt():
    """When a duplicate registration is refused, the original connection keeps
    its name and can still place calls."""

    async def _run():
        async with server_fixture() as srv:
            url = f"ws://127.0.0.1:{srv.port}"
            async with connect(url) as first_ws, connect(url) as second_ws:
                await register(first_ws, "alice")
                await send_json(second_ws, {"type": "register", "username": "alice"})
                await recv_json(second_ws)  # 409 error

                # Register a real second user and confirm alice is still online.
                resp = await register(second_ws, "bob")
                assert "alice" in resp["users"]

    asyncio.run(_run())
