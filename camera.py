import logging

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from . import ptz_client

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        EaseLifeCamera(coordinator, device)
        for device in (coordinator.data or [])
    ]
    async_add_entities(entities, update_before_add=True)


class EaseLifeCamera(CoordinatorEntity, Camera):
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, coordinator, device):
        CoordinatorEntity.__init__(self, coordinator)
        Camera.__init__(self)
        self._device_id = device.get("deviceId")
        self._attr_unique_id = self._device_id
        self._attr_name = device.get("deviceName") or device.get("deviceTitle") or self._device_id
        self._attr_model = device.get("cameraModelId")
        self._attr_brand = "Ease Life"
        self._last_device = device
        self._attr_is_on = device.get("onlineStatus") == "available"

    @property
    def available(self):
        return self.coordinator.last_update_success

    def _current_device(self):
        for d in self.coordinator.data or []:
            if d.get("deviceId") == self._device_id:
                return d
        return self._last_device

    @property
    def extra_state_attributes(self):
        d = self._current_device() or {}
        caps = ptz_client.ptz_capabilities(d.get("comment") or "")
        return {
            "serial_number": d.get("serialNumber"),
            "online_status": d.get("onlineStatus"),
            "region": d.get("nowRegion"),
            "cloud_did": d.get("cloudDid"),
            "ptz_supported": caps["pan"] or caps["tilt"],
        }

    async def stream_source(self):
        proxy = getattr(self.coordinator, "flv_proxy", None)
        if proxy is None:
            return None
        return proxy.url_for(self._device_id)

    async def async_camera_image(self, width=None, height=None):
        try:
            return await self.coordinator.async_thumbnail(
                self._device_id, width=width or 320
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("thumbnail fetch failed for %s: %s", self._device_id, err)
            return None
