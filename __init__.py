import asyncio
import io
import logging
from datetime import timedelta

import requests
import voluptuous as vol
from homeassistant.components import tts
from homeassistant.components.tts.const import DATA_TTS_MANAGER
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import ptz_client, talk, v4_client, vrs_live
from .const import (
    CONF_DEVICE_ID,
    CONF_EMAIL,
    CONF_ENABLE_AUDIO,
    CONF_PASSWORD,
    CONF_PROXY_HOST,
    CONF_PROXY_TOKEN,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .flv_proxy import FlvProxy

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["camera"]

SERVICE_PTZ = "ptz"
SERVICE_SPEAK = "speak"

# SAFETY (2026-09-17, E-011): uplink audio wedges this camera's audio pipeline
# for ALL clients until a physical reboot (confirmed: mobile app talk also
# broke after HA sent audio).  Root cause unknown (missing start handshake?
# aborted utterance without STOP?).  The speak service stays registered so
# automations keep resolving, but refuses to transmit until the handshake is
# understood.  PTZ and listen (downlink) are unaffected.
SPEAK_UPLINK_ENABLED = False

PTZ_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_id,
        vol.Required("direction"): vol.In(["left", "right", "up", "down", "home"]),
        vol.Optional("steps", default=1): vol.All(int, vol.Range(min=1, max=10)),
    }
)
SPEAK_SCHEMA = vol.Schema(
    {
        vol.Required("entity_id"): cv.entity_id,
        vol.Required("message"): str,
        vol.Optional("engine"): str,
        vol.Optional("language"): str,
    }
)


class EaseLifeCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry):
        self.entry = entry
        self._session = requests.Session()
        self._token = None
        self._refresh = None
        interval = entry.options.get(
            CONF_SCAN_INTERVAL, entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval),
        )

    def _blocking_login(self):
        data = v4_client.login(
            self.entry.data[CONF_EMAIL],
            self.entry.data[CONF_PASSWORD],
            device_id=self.entry.data.get(CONF_DEVICE_ID) or None,
            session=self._session,
        )
        self._token = data["token"]
        self._refresh = data.get("refreshToken")

    def _blocking_refresh(self):
        if not self._refresh:
            self._blocking_login()
            return
        data = v4_client.refresh_token(self._refresh, session=self._session)
        self._token = data.get("token", self._token)
        self._refresh = data.get("refreshToken", self._refresh)

    def _with_retry(self, call):
        """Run a REST call, recovering the token or a stale keep-alive connection."""
        try:
            return call(self._session)
        except v4_client.EaseLifeAuthError:
            self._blocking_refresh()
            return call(self._session)
        except requests.exceptions.RequestException:
            # The cloud closes idle keep-alive sockets; replace the pooled session.
            self._session.close()
            self._session = requests.Session()
            return call(self._session)

    def _device_list(self):
        if not self._token:
            self._blocking_login()
        return self._with_retry(
            lambda session: v4_client.device_list(self._token, session=session)
        )

    async def _async_update_data(self):
        def work():
            return self._device_list()

        try:
            return await self.hass.async_add_executor_job(work)
        except Exception as err:
            raise UpdateFailed(str(err)) from err

    async def async_live_params(self, device_id):
        """Return VRS WebSocket connection params for a device (token kept fresh)."""

        def work():
            if not self._token:
                self._blocking_login()
            device = next(
                (d for d in (self.data or []) if d.get("deviceId") == device_id), None
            )
            if device is None:
                device = next(
                    (d for d in self._device_list() if d.get("deviceId") == device_id),
                    None,
                )
            region = (device or {}).get("region") or ""
            return {
                "vrs_host": region.split("/")[0],
                "token": self._token,
                "product_key": v4_client.PRODUCT_KEY,
                "relay_server": "",
            }

        return await self.hass.async_add_executor_job(work)

    async def async_thumbnail(self, device_id, width=320):
        def work():
            devices = self.data or []
            device = next((d for d in devices if d.get("deviceId") == device_id), None)
            if device is None:
                device = next(
                    (d for d in self._device_list() if d.get("deviceId") == device_id),
                    None,
                )
            if device is None:
                raise UpdateFailed("device %s not found" % device_id)
            return self._with_retry(
                lambda session: v4_client.thumbnail_bytes(
                    device, width=width, session=session
                )
            )

        return await self.hass.async_add_executor_job(work)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    coordinator = EaseLifeCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    keep_audio = bool(entry.options.get(CONF_ENABLE_AUDIO, False))
    proxy = FlvProxy(
        hass, coordinator.async_live_params, keep_audio=keep_audio,
        host=entry.options.get(CONF_PROXY_HOST, "127.0.0.1") or "127.0.0.1",
        proxy_token=entry.options.get(CONF_PROXY_TOKEN, "") or "",
    )
    await proxy.start()
    coordinator.flv_proxy = proxy
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    _async_register_services(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    return True


def _resolve_device(hass: HomeAssistant, entity_id: str):
    """Map a camera entity_id to (coordinator, device_id)."""
    ent_reg = er.async_get(hass)
    ent = ent_reg.async_get(entity_id)
    if ent is None or ent.platform != DOMAIN:
        raise HomeAssistantError(f"{entity_id} is not an Ease Life camera")
    coordinator = hass.data.get(DOMAIN, {}).get(ent.config_entry_id)
    if coordinator is None:
        raise HomeAssistantError("Ease Life integration not loaded")
    return coordinator, ent.unique_id


def _device_comment(coordinator, device_id: str) -> str:
    for d in coordinator.data or []:
        if d.get("deviceId") == device_id:
            return d.get("comment") or ""
    return ""


def _async_register_services(hass: HomeAssistant) -> None:
    if hass.data.get(f"{DOMAIN}_services"):
        return
    hass.data[f"{DOMAIN}_services"] = True

    async def async_handle_ptz(call: ServiceCall) -> None:
        coordinator, device_id = _resolve_device(hass, call.data["entity_id"])
        direction = call.data["direction"]
        steps = call.data["steps"]
        caps = ptz_client.ptz_capabilities(
            _device_comment(coordinator, device_id)
        )
        need = {"left": "pan", "right": "pan", "up": "tilt",
                "down": "tilt", "home": None}[direction]
        if need is not None and not caps.get(need):
            raise HomeAssistantError(
                f"camera does not support PTZ {direction}")
        params = await coordinator.async_live_params(device_id)
        value = ptz_client.PTZ_DIRECTIONS[direction]
        for i in range(steps):
            try:
                await ptz_client.async_ptz_move(
                    params["token"], device_id,
                    params.get("product_key", v4_client.PRODUCT_KEY), value)
            except ptz_client.PtzError as err:
                raise HomeAssistantError(f"PTZ move failed: {err}") from err
            if i < steps - 1:
                await asyncio.sleep(0.4)

    async def async_handle_speak(call: ServiceCall) -> None:
        if not SPEAK_UPLINK_ENABLED:
            raise HomeAssistantError(
                "ease_life.speak is temporarily disabled: sending audio can "
                "wedge the camera's audio pipeline until a physical reboot "
                "(see evidence/E-011). PTZ and listen are unaffected.")
        coordinator, device_id = _resolve_device(hass, call.data["entity_id"])
        message = call.data["message"]
        engine = tts.async_resolve_engine(hass, call.data.get("engine"))
        if engine is None:
            raise HomeAssistantError(
                "no text-to-speech engine available")
        manager = hass.data[DATA_TTS_MANAGER]
        stream = manager.async_create_result_stream(
            engine=engine, language=call.data.get("language"), options=None)
        stream.async_set_message(message)
        chunks = [chunk async for chunk in stream.async_stream_result()]
        pcm = await hass.async_add_executor_job(
            _transcode_to_pcm16_8k, stream.extension, b"".join(chunks))
        if not pcm:
            raise HomeAssistantError("TTS produced no audio")
        params = await coordinator.async_live_params(device_id)
        sender = None
        proxy = getattr(coordinator, "flv_proxy", None)
        if proxy is not None:
            # Prefer the already-running live upstream: the VRS server does
            # not tolerate a second concurrent live session well (it goes
            # quiet and is then closed, killing a dedicated talk session).
            sender = proxy.talk_sender(device_id)
        try:
            if sender is not None:
                await talk.async_stream_pcm16(sender, pcm)
                await sender(talk.talk_stop_frame())
            else:
                await talk.async_speak_pcm16(hass, params, device_id, pcm)
        except (talk.TalkError, vrs_live.TalkSendError) as err:
            raise HomeAssistantError(f"speak failed: {err}") from err

    hass.services.async_register(DOMAIN, SERVICE_PTZ, async_handle_ptz,
                                 schema=PTZ_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_SPEAK, async_handle_speak,
                                 schema=SPEAK_SCHEMA)


def _transcode_to_pcm16_8k(extension: str, data: bytes) -> bytes:
    """Transcode rendered TTS audio to s16le mono 8 kHz PCM (blocking)."""
    import av  # noqa: PLC0415  (lazy: only needed for speak)

    out = bytearray()
    with av.open(io.BytesIO(data)) as container:
        audio = next(
            (s for s in container.streams if s.type == "audio"), None)
        if audio is None:
            raise HomeAssistantError(f"TTS audio has no audio stream (.{extension})")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=8000)
        for frame in container.decode(audio):
            for resampled in resampler.resample(frame):
                out += bytes(resampled.planes[0])
    # Drop a partial tail so the speaker gets whole 20 ms frames.
    tail = len(out) % 320
    if tail:
        del out[len(out) - tail:]
    return bytes(out)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    proxy = getattr(coordinator, "flv_proxy", None)
    if proxy is not None:
        await proxy.stop()
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry):
    await hass.config_entries.async_reload(entry.entry_id)
