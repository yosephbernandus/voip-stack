"""
WebSocket signaling server for VoIP call setup and teardown.

Each JSON message type maps one to one onto a SIP concept:
  "register" -> SIP REGISTER    (I am here, this is where you can reach me)
  "invite"   -> SIP INVITE      (I want to call you, here is my SDP offer)
  "answer"   -> SIP 200 OK      (I accept, here is my SDP answer)
  "ice"      -> WebRTC only     (ICE candidate for NAT traversal)
  "bye"      -> SIP BYE         (I am hanging up)
  "reject"   -> SIP 603 Decline (I do not want to answer)

Transport is the websockets library. A browser cannot open a raw TCP or UDP
socket, and WebSocket is plumbing rather than VoIP, so the protocol work
happens in the SIP, SDP, and RTP modules instead.
"""

from __future__ import annotations

import asyncio
import http
import json
import logging
import os
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import websockets
from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from voip.sdp import log_sdp

logger = logging.getLogger(__name__)

# Cloudflare TURN: short-lived relay credentials, minted per registration.
# The API token stays server-side; browsers only ever see the ephemeral
# username/credential pair, which expires after _TURN_CREDENTIAL_TTL seconds.
# This mirrors the production "TURN REST API" pattern instead of exposing
# a static credential on a public endpoint.
_TURN_MINT_URL = (
    "https://rtc.live.cloudflare.com/v1/turn/keys/{key_id}"
    "/credentials/generate-ice-servers"
)
_TURN_CREDENTIAL_TTL = 3600  # long enough to cover a demo session


def _mint_turn_credentials() -> list | None:
    """
    Generate ephemeral TURN credentials via the Cloudflare Realtime API.

    Returns the RTCIceServer list, or None when TURN is not configured.
    Runs blocking urllib I/O, so call it via asyncio.to_thread().
    """
    key_id = os.environ.get("TURN_KEY_ID", "")
    api_token = os.environ.get("TURN_API_TOKEN", "")
    if not (key_id and api_token):
        return None

    request = urllib.request.Request(
        _TURN_MINT_URL.format(key_id=key_id),
        data=json.dumps({"ttl": _TURN_CREDENTIAL_TTL}).encode(),
        headers={
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
            # Cloudflare's WAF rejects urllib's default "Python-urllib/x.y"
            # user agent with 403; any custom value passes.
            "User-Agent": "pyvoip-signaling/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        payload = json.loads(response.read())

    ice_servers = payload.get("iceServers")
    if isinstance(ice_servers, dict):
        ice_servers = [ice_servers]
    return ice_servers


def _static_ice_servers() -> list | None:
    """Fallback: a static RTCIceServer list from the ICE_SERVERS env var."""
    raw = os.environ.get("ICE_SERVERS", "")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("ICE_SERVERS is not valid JSON; ignoring")
        return None


class _IgnoreBrowserPreconnect(logging.Filter):
    """
    Browsers open speculative TCP connections (preconnect) and close them
    without sending a request. The websockets library logs each one as an
    ERROR-level "opening handshake failed" with an EOFError traceback.
    Harmless, so drop them to keep the console readable.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        while exc is not None:
            if isinstance(exc, EOFError):
                return False
            exc = exc.__cause__
        return True


@dataclass
class CallDialog:
    """
    Tracks the state of an active call.

    Maps to a SIP dialog (RFC 3261 §12): a peer-to-peer relationship
    identified by Call-ID + From-tag + To-tag. We simplify to just call_id.

    States mirror the SIP call flow:
      "ringing": INVITE sent, waiting for 200 OK
      "active":  200 OK received, media flowing
      "ended":   BYE sent or connection dropped
    """

    call_id: str
    caller: str
    callee: str
    state: str  # "ringing", "active", "ended"
    started_at: float = field(default_factory=time.time)


class SignalingServer:
    """
    WebSocket signaling server that routes JSON call setup/teardown messages
    between browser clients.

    The server does no media handling. It is purely a rendezvous point for
    exchanging SDP offers/answers and ICE candidates. Once the WebRTC
    peer-to-peer connection is established, media flows directly between
    browsers (no server involvement).
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8080,
        static_dir: str | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._static_dir = static_dir

        # username → websocket connection
        self._connections: dict[str, ServerConnection] = {}
        # call_id → CallDialog
        self._dialogs: dict[str, CallDialog] = {}

        # Exposed after start() so tests can discover the actual port (when port=0)
        self.port: int = port

        # Signalled once the server socket is bound and accepting connections
        self._ready: asyncio.Event = asyncio.Event()

        self._server: websockets.asyncio.server.Server | None = None

    async def start(self) -> None:
        """
        Start the WebSocket server and serve forever.

        Binds the server socket, sets self.port to the actual port (useful
        when port=0 is passed for ephemeral port allocation in tests), fires
        self._ready, then blocks until cancelled.
        """
        websockets_logger = logging.getLogger("websockets.server")
        websockets_logger.addFilter(_IgnoreBrowserPreconnect())

        async with serve(
            self._handle_connection,
            self._host,
            self._port,
            process_request=self._process_request if self._static_dir else None,
            logger=websockets_logger,
        ) as server:
            self._server = server
            # Resolve the actual port, which matters when port=0 was requested
            self.port = server.sockets[0].getsockname()[1]
            self._ready.set()
            logger.info("[SIGNALING] server listening on %s:%d", self._host, self.port)
            try:
                await server.serve_forever()
            except asyncio.CancelledError:
                # Ctrl-C: asyncio.run() cancels us once and then waits.
                # Close the listener and await it here, so websockets'
                # internal _close task finishes before the loop shuts down
                # (otherwise: "Task was destroyed but it is pending!").
                server.close()
                await server.wait_closed()
                raise

    async def _process_request(
        self, connection: ServerConnection, request: Request
    ) -> Response | None:
        """
        Intercept HTTP GET requests to serve static files before WebSocket upgrade.

        Returns a Response for static file requests, or None to continue with
        the normal WebSocket handshake.
        """
        # Upgrade requests carry a Connection: Upgrade header, so let them through
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return None

        # Plain-HTTP request: serve the client page, or a clean 404. Returning
        # None here would make websockets attempt a WS handshake on a normal
        # HTTP request (e.g. /favicon.ico) and log a scary InvalidUpgrade error.
        path = request.path.split("?")[0]
        if path not in ("/", "/index.html"):
            return Response(
                status_code=http.HTTPStatus.NOT_FOUND,
                reason_phrase="Not Found",
                headers=Headers([("Content-Type", "text/plain")]),
                body=b"not found",
            )

        assert self._static_dir is not None
        static_file = Path(self._static_dir) / "index.html"
        if not static_file.exists():
            return Response(
                status_code=http.HTTPStatus.NOT_FOUND,
                reason_phrase="Not Found",
                headers=Headers([("Content-Type", "text/plain")]),
                body=b"index.html not found",
            )

        body = static_file.read_bytes()
        return Response(
            status_code=http.HTTPStatus.OK,
            reason_phrase="OK",
            headers=Headers([
                ("Content-Type", "text/html; charset=utf-8"),
                ("Content-Length", str(len(body))),
            ]),
            body=body,
        )

    async def _handle_connection(self, websocket: ServerConnection) -> None:
        """
        Message processing loop for a single WebSocket connection.

        Reads JSON messages and dispatches by type. On disconnect (clean or
        abrupt), cleans up the user's registration and notifies active call
        partners with a synthetic BYE.
        """
        username: str | None = None
        try:
            async for raw in websocket:
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    await self._send_error(websocket, 400, "invalid JSON")
                    continue

                msg_type = msg.get("type")

                if msg_type == "register":
                    new_username = msg.get("username", "").strip()
                    if not new_username:
                        await self._send_error(websocket, 400, "username required")
                        continue
                    username = new_username
                    await self._handle_register(username, websocket)

                elif username is None:
                    await self._send_error(websocket, 401, "not registered")

                elif msg_type == "invite":
                    await self._handle_invite(username, msg)
                elif msg_type == "answer":
                    await self._handle_answer(username, msg)
                elif msg_type == "ice":
                    await self._handle_ice(username, msg)
                elif msg_type == "bye":
                    await self._handle_bye(username, msg)
                elif msg_type == "reject":
                    await self._handle_reject(username, msg)
                else:
                    await self._send_error(
                        websocket, 400, f"unknown message type: {msg_type!r}"
                    )

        except websockets.ConnectionClosed:
            # Normal path: the remote end closed the connection
            pass
        finally:
            if username:
                del self._connections[username]
                logger.info("[SIGNALING] %s disconnected", username)

                # Broadcast user_offline to remaining users
                await self._broadcast(
                    exclude=username,
                    message={"type": "user_offline", "username": username},
                )

                # Send synthetic BYE to partners of active calls
                ended_calls = self._cleanup_dialogs(username)
                for other_user, call_id, _reason in ended_calls:
                    if other_user in self._connections:
                        await self._connections[other_user].send(
                            json.dumps({
                                "type": "bye",
                                "username": username,
                                "call_id": call_id,
                            })
                        )

    async def _handle_register(
        self, username: str, websocket: ServerConnection
    ) -> None:
        """
        Process a register message.

        Equivalent to SIP REGISTER (RFC 3261 §10): the user announces their
        presence and obtains a list of other reachable users. Broadcasts
        user_online to all currently registered peers.
        """
        # Store connection (re-registration replaces previous entry)
        self._connections[username] = websocket

        others = [u for u in self._connections if u != username]
        logger.info(
            "[SIGNALING] %s registered (%d other user%s online)",
            username,
            len(others),
            "s" if len(others) != 1 else "",
        )

        # ICE servers for this session. Mint ephemeral TURN credentials from
        # the relay provider, otherwise fall back to a static list, otherwise
        # omit them and let the client use its default STUN. These are
        # delivered inside the registered reply, on the client's own
        # connection, never on a public endpoint.
        ice_servers = None
        try:
            ice_servers = await asyncio.to_thread(_mint_turn_credentials)
            if ice_servers:
                logger.info(
                    "[SIGNALING] minted TURN credentials for %s (ttl=%ds)",
                    username, _TURN_CREDENTIAL_TTL,
                )
        except Exception as exc:
            logger.warning("TURN credential mint failed: %s", exc)
        if ice_servers is None:
            ice_servers = _static_ice_servers()

        reply: dict = {"type": "registered", "users": others}
        if ice_servers:
            reply["ice_servers"] = ice_servers

        # Confirm registration with list of other online users
        await websocket.send(json.dumps(reply))

        # Broadcast the newcomer to everyone else
        await self._broadcast(
            exclude=username,
            message={"type": "user_online", "username": username},
        )

    async def _handle_invite(self, caller: str, msg: dict) -> None:
        """
        Forward an INVITE (SDP offer) to the target user.

        Equivalent to SIP INVITE (RFC 3261 §13): the caller initiates a
        session by sending their SDP offer to the callee. If the target is
        not registered, returns 404 Not Found (mirrors SIP §21.4.5).
        """
        target = msg.get("target", "")
        call_id = msg.get("call_id", "")
        sdp = msg.get("sdp", "")

        if target not in self._connections:
            await self._send_error(
                self._connections[caller], 404, f"user {target!r} not found"
            )
            return

        # Create a dialog to track this call's state
        if call_id:
            self._dialogs[call_id] = CallDialog(
                call_id=call_id,
                caller=caller,
                callee=target,
                state="ringing",
            )

        logger.info(
            "[SIGNALING] %s → INVITE → %s (call_id: %s)", caller, target, call_id
        )

        # Log the SDP offer for educational output
        if sdp:
            log_sdp(f"OFFER from {caller} to {target}", sdp)

        await self._connections[target].send(
            json.dumps({
                "type": "invite",
                "username": caller,
                "sdp": sdp,
                "call_id": call_id,
            })
        )

    async def _handle_answer(self, answerer: str, msg: dict) -> None:
        """
        Forward an SDP answer back to the caller.

        Completes the offer/answer exchange (RFC 3264). After both sides have
        exchanged SDP, the WebRTC peer-to-peer connection can be established
        and media flows directly between browsers.
        """
        target = msg.get("target", "")
        call_id = msg.get("call_id", "")
        sdp = msg.get("sdp", "")

        if target not in self._connections:
            return

        # Transition dialog to active state
        if call_id and call_id in self._dialogs:
            self._dialogs[call_id].state = "active"

        logger.info(
            "[SIGNALING] %s → ANSWER → %s (call_id: %s)", answerer, target, call_id
        )

        if sdp:
            log_sdp(f"ANSWER from {answerer} to {target}", sdp)

        await self._connections[target].send(
            json.dumps({
                "type": "answer",
                "username": answerer,
                "sdp": sdp,
                "call_id": call_id,
            })
        )

    async def _handle_ice(self, sender: str, msg: dict) -> None:
        """
        Relay an ICE candidate to the peer.

        ICE (Interactive Connectivity Establishment, RFC 8445) discovers the
        best network path between two browsers. Candidates are trickled one
        by one as the browser discovers them. This is faster than waiting
        for all candidates before starting connectivity checks.
        """
        target = msg.get("target", "")
        candidate = msg.get("candidate")

        logger.info("[ICE] %s → %s: %s", sender, target, candidate)

        if target not in self._connections:
            return

        await self._connections[target].send(
            json.dumps({
                "type": "ice",
                "username": sender,
                "candidate": candidate,
            })
        )

    async def _handle_bye(self, sender: str, msg: dict) -> None:
        """
        Terminate a call.

        Equivalent to SIP BYE (RFC 3261 §15): ends an established or pending
        dialog. Updates dialog state to "ended" and forwards to the peer.
        """
        target = msg.get("target", "")
        call_id = msg.get("call_id", "")

        # Remove the dialog entirely: if we only marked it "ended", the
        # disconnect cleanup would still find it and send a second,
        # synthetic BYE for a call that already ended normally.
        if call_id:
            self._dialogs.pop(call_id, None)

        logger.info(
            "[SIGNALING] %s → BYE → %s (call_id: %s)", sender, target, call_id
        )

        if target not in self._connections:
            return

        await self._connections[target].send(
            json.dumps({
                "type": "bye",
                "username": sender,
                "call_id": call_id,
            })
        )

    async def _handle_reject(self, sender: str, msg: dict) -> None:
        """
        Decline an incoming call.

        Equivalent to SIP 603 Decline (RFC 3261 §21.6.6): the callee does not
        wish to answer. Ends the dialog and notifies the caller.
        """
        target = msg.get("target", "")
        call_id = msg.get("call_id", "")

        # Same as BYE: drop the dialog so disconnect cleanup cannot
        # resurrect a call that was already declined.
        if call_id:
            self._dialogs.pop(call_id, None)

        logger.info(
            "[SIGNALING] %s → REJECT → %s (call_id: %s)", sender, target, call_id
        )

        if target not in self._connections:
            return

        await self._connections[target].send(
            json.dumps({
                "type": "rejected",
                "username": sender,
                "call_id": call_id,
            })
        )

    async def _send_error(
        self, websocket: ServerConnection, code: int, message: str
    ) -> None:
        """Send an error message to a connection."""
        await websocket.send(
            json.dumps({"type": "error", "code": code, "message": message})
        )

    async def _broadcast(self, exclude: str, message: dict) -> None:
        """Send a message to all registered users except the excluded one."""
        payload = json.dumps(message)
        for username, websocket in list(self._connections.items()):
            if username != exclude:
                try:
                    await websocket.send(payload)
                except websockets.ConnectionClosed:
                    pass

    def _cleanup_dialogs(self, username: str) -> list[tuple[str, str, str]]:
        """
        Remove all dialogs involving the disconnected user.

        Returns a list of (other_user, call_id, reason) tuples so the caller
        can send BYE messages to active call partners.
        """
        ended: list[tuple[str, str, str]] = []
        to_delete: list[str] = []

        for call_id, dialog in self._dialogs.items():
            if dialog.caller == username or dialog.callee == username:
                dialog.state = "ended"
                other = dialog.callee if dialog.caller == username else dialog.caller
                ended.append((other, call_id, "disconnected"))
                to_delete.append(call_id)

        for call_id in to_delete:
            del self._dialogs[call_id]

        return ended


if __name__ == "__main__":
    import argparse

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    parser = argparse.ArgumentParser(description="VoIP signaling server")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--static-dir", default=None)
    args = parser.parse_args()

    server = SignalingServer(
        host=args.host, port=args.port, static_dir=args.static_dir
    )
    asyncio.run(server.start())
