"""PTZ control via the wsrelay device-control WebSocket.

Mirrors the official web player (resources_common_websocket_socket.js):

  open  wss://wsrelay.blurams.com:50843   (Origin https://www.ehomeease.com)
  send  {"type":1,"token":...,"deviceId":"WEBCLIENT_WEBSOCKET<ms>",
         "channelName":"websocket","userName":"","productKey":...}
  ping  {"type":7,"cmdId":N} every ~10 s (server answers type 7; reply type 8)
  ptz   {"type":3,"cameraId":...,"cmdId":N,
         "msg":{"msgSession":764963713,"msgSequence":0,"msgTimeStamp":ms,
                "msgCategory":"camera",
                "msgContent":{"request":1793,"requestParams":{"value":V},
                              "subRequest":5}}}
  value: 1=left 2=right 3=up 4=down, 0=back to original position.
  Camera answers type 134 with msgContent.responseRequest=1793,
  responseSubRequest=5 and response=0 on success.

Move direction support comes from the FEATURE bitmask in the device comment
field: pan=0x10, tilt=0x20, zoom=0x40 (this camera: pan+tilt, no zoom).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import aiohttp

_LOGGER = logging.getLogger(__name__)

WSRELAY_URL = "wss://wsrelay.blurams.com:50843"
ORIGIN = "https://www.ehomeease.com"

CMD_SET_DEVICE_CONFIG = 1793
SUBREQ_LENS_PAN = 5
MSG_SESSION = 764963713

PTZ_DIRECTIONS = {"left": 1, "right": 2, "up": 3, "down": 4, "home": 0}

FLAG_LENS_PAN = 0x10
FLAG_LENS_TILT = 0x20
FLAG_LENS_ZOOM = 0x40


class PtzError(Exception):
    """PTZ command failed."""


def feature_bits(comment: str) -> int:
    """Parse the FEATURE bitmask out of a device comment string."""
    import re

    m = re.search(r"'FEATURE'\s*:\s*(\d+)", comment or "")
    return int(m.group(1)) if m else 0


def ptz_capabilities(comment: str) -> dict[str, bool]:
    """Return pan/tilt/zoom support for a device comment string."""
    bits = feature_bits(comment)
    return {
        "pan": bool(bits & FLAG_LENS_PAN),
        "tilt": bool(bits & FLAG_LENS_TILT),
        "zoom": bool(bits & FLAG_LENS_ZOOM),
    }


async def async_ptz_move(
    token: str,
    device_id: str,
    product_key: str,
    value: int,
    timeout: float = 20.0,
) -> None:
    """Send one PTZ step and wait for the camera acknowledgement."""
    cmd_id = 0

    async def send(ws, obj):
        nonlocal cmd_id
        if "cmdId" not in obj:
            cmd_id += 1
            obj["cmdId"] = cmd_id
        await ws.send_str(json.dumps(obj))
        return obj["cmdId"]

    session_timeout = aiohttp.ClientTimeout(total=None, sock_connect=15)
    try:
        async with aiohttp.ClientSession(timeout=session_timeout) as session:
            async with session.ws_connect(
                WSRELAY_URL, headers={"Origin": ORIGIN}
            ) as ws:
                await send(
                    ws,
                    {"type": 1, "token": token,
                     "deviceId": f"WEBCLIENT_WEBSOCKET{int(time.time()*1000)}",
                     "channelName": "websocket", "userName": "",
                     "productKey": product_key},
                )
                ptz_cmd = await send(
                    ws,
                    {"type": 3, "cameraId": device_id,
                     "msg": {"msgSession": MSG_SESSION, "msgSequence": 0,
                             "msgTimeStamp": int(time.time() * 1000),
                             "msgCategory": "camera",
                             "msgContent": {"request": CMD_SET_DEVICE_CONFIG,
                                            "requestParams": {"value": value},
                                            "subRequest": SUBREQ_LENS_PAN}}},
                )
                deadline = time.monotonic() + timeout
                last_ping = 0.0
                async for msg in ws:
                    now = time.monotonic()
                    if now - last_ping > 9:
                        last_ping = now
                        await send(ws, {"type": 7})
                    if now > deadline:
                        raise PtzError("timed out waiting for PTZ ack")
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    try:
                        data = json.loads(msg.data)
                    except ValueError:
                        continue
                    mtype = data.get("type")
                    if mtype == 100 and data.get("result") not in (0, None):
                        raise PtzError(
                            f"control channel rejected token (result={data.get('result')})"
                        )
                    if mtype == 7:  # server ping -> pong
                        await send(ws, {"type": 8})
                    if mtype in (133, 134):
                        raw = data.get("msg")
                        try:
                            content = (json.loads(raw) if isinstance(raw, str)
                                       else raw).get("msgContent", {})
                        except (ValueError, AttributeError):
                            content = {}
                        if (content.get("responseRequest") == CMD_SET_DEVICE_CONFIG
                                and content.get("responseSubRequest") == SUBREQ_LENS_PAN
                                and data.get("cmdId") == ptz_cmd):
                            if content.get("response") == 0:
                                _LOGGER.debug(
                                    "ease_life: PTZ ack ok for %s value=%s",
                                    device_id, value)
                                if data.get("cmdId"):
                                    try:
                                        await ws.send_str(json.dumps(
                                            {"type": 4, "cameraId": device_id,
                                             "msg": "0", "cmdId": data["cmdId"]}))
                                    except OSError:
                                        pass
                                return
                            raise PtzError(
                                f"camera refused PTZ move (response={content.get('response')})"
                            )
                raise PtzError("control channel closed before PTZ ack")
    except PtzError:
        raise
    except Exception as err:  # noqa: BLE001
        raise PtzError(f"PTZ transport failed: {err}") from err
