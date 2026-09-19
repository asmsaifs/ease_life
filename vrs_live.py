"""Ease Life live video via the VRS h5player WebSocket (FLV over WS).

Protocol reverse-engineered in evidence/E-008:

  wss://<vrs>/h5player/live   (Origin: https://www.ehomeease.com)

  send TEXT : __reqJSONStr=<urlencoded base64(AES-256-CBC(json))>
              key = b"viWebsdkCrypto" zero-padded to 32 bytes, iv = 16 zero bytes, PKCS7
  send BIN  : 0x03 + urlencode({"time": <ms>, "cmd": 3})   (periodic time sync)
  recv BIN  : first byte = cmd
              0x00 -> remaining bytes are raw FLV stream data
              0x04 -> urlencoded JSON control response

The `token` is the same CAS v4 token used for the REST API.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import string
import time
import urllib.parse

import aiohttp
from cryptography.hazmat.primitives import padding as sympad
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_LOGGER = logging.getLogger(__name__)

PRODUCT_KEY = "45c8bd2a-e70"
SDK_SECRET = b"viWebsdkCrypto".ljust(32, b"\x00")[:32]
IV = b"\x00" * 16
CMD_FLV = 0x00
CMD_REQ_MESSAGE = 0x03
CMD_RESP_MESSAGE = 0x04


class TalkSendError(Exception):
    """Talk-back frame could not be sent on the live session."""


def _aes_encrypt(text: str) -> str:
    enc = Cipher(algorithms.AES(SDK_SECRET), modes.CBC(IV)).encryptor()
    pad = sympad.PKCS7(128).padder()
    data = pad.update(text.encode()) + pad.finalize()
    return base64.b64encode(enc.update(data) + enc.finalize()).decode()


def build_req(
    token: str,
    device_id: str,
    product_key: str = PRODUCT_KEY,
    relay_server: str = "",
    channel: str = "720p",
    has_audio: bool = False,
) -> str:
    client_id = "WEBCLIENT_H5_" + "".join(
        random.choice(string.ascii_lowercase + string.digits) for _ in range(19)
    )
    params = {
        "requestTime": str(int(time.time() * 1000)),
        "productKey": product_key,
        "deviceId": device_id,
        "channelNo": "",
        "token": token,
        "hasAudio": "true" if has_audio else "false",
        "region": "",
        "isPermanentStorage": "false",
        "channel": channel,
        "deviceName": "",
        "clientId": client_id,
        "shareId": "",
        "relayServer": relay_server,
        "isSDCardPlayback": "false",
        "preConnect": "false",
        "releaseVersion": "",
        "noAAC": "1",
    }
    blob = _aes_encrypt(json.dumps(params, separators=(",", ":")))
    return "__reqJSONStr=" + urllib.parse.quote(blob, safe="")


class VrsLiveClient:
    """Streams FLV bytes from the VRS h5player WebSocket."""

    def __init__(
        self,
        vrs_host: str,
        token: str,
        device_id: str,
        product_key: str = PRODUCT_KEY,
        relay_server: str = "",
        has_audio: bool = False,
    ) -> None:
        self.vrs_host = vrs_host
        self.token = token
        self.device_id = device_id
        self.product_key = product_key
        self.relay_server = relay_server
        self.has_audio = has_audio
        self._ws = None

    async def async_send_talk(self, payload: bytes) -> None:
        """Write one talk-back frame on this live session.

        Raises TalkSendError if the session is gone (read loop owns
        reconnects; concurrent send/receive on one aiohttp WS is safe).
        """
        ws = self._ws
        if ws is None or ws.closed:
            raise TalkSendError("live session not active")
        try:
            await ws.send_bytes(payload)
        except Exception as err:  # noqa: BLE001
            raise TalkSendError(f"talk send failed: {err}") from err

    async def stream(self, should_stop=None):
        url = f"wss://{self.vrs_host}/h5player/live"
        headers = {"Origin": "https://www.ehomeease.com", "User-Agent": "Mozilla/5.0"}
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.ws_connect(url, headers=headers, heartbeat=20) as ws:
                self._ws = ws
                try:
                    await ws.send_str(
                        build_req(
                            self.token,
                            self.device_id,
                            self.product_key,
                            self.relay_server,
                            has_audio=self.has_audio,
                        )
                    )
                    _LOGGER.debug("ease_life: VRS live connected for %s", self.device_id)
                    await self._sync(ws, 0)
                    while True:
                        if should_stop is not None and should_stop():
                            return
                        try:
                            msg = await asyncio.wait_for(ws.receive(), timeout=1.0)
                        except asyncio.TimeoutError:
                            msg = None
                        if msg is not None:
                            if msg.type == aiohttp.WSMsgType.BINARY and msg.data:
                                cmd = msg.data[0]
                                body = msg.data[1:]
                                if cmd == CMD_FLV:
                                    yield body
                                elif cmd == CMD_RESP_MESSAGE:
                                    _LOGGER.debug(
                                        "ease_life: VRS resp %s",
                                        urllib.parse.unquote(
                                            body.decode("utf-8", "replace")
                                        )[:160],
                                    )
                            elif msg.type in (
                                aiohttp.WSMsgType.CLOSED,
                                aiohttp.WSMsgType.CLOSING,
                                aiohttp.WSMsgType.CLOSE,
                                aiohttp.WSMsgType.ERROR,
                            ):
                                _LOGGER.warning(
                                    "ease_life: VRS live closed for %s (%s)",
                                    self.device_id,
                                    msg.type,
                                )
                                return
                finally:
                    self._ws = None
        return

    @staticmethod
    async def _sync(ws, elapsed_ms: int) -> None:
        payload = urllib.parse.quote(
            json.dumps({"time": elapsed_ms, "cmd": 3})
        )
        await ws.send_bytes(bytes([CMD_REQ_MESSAGE]) + payload.encode())
