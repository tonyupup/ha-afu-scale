"""AFU 体脂秤传感器实体"""

from __future__ import annotations

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONFIDENCE_OPTIONS, DOMAIN, USER_SENSOR_KEYS
from .coordinator import AfuScaleCoordinator
from .matcher import UserProfile

SENSOR_DEFS: dict[str, dict] = {
    "weight": {
        "name": "体重",
        "unit": "kg",
        "device_class": SensorDeviceClass.WEIGHT,
        "state_class": SensorStateClass.MEASUREMENT,
        "precision": 2,
    },
    "impedance": {
        "name": "电阻抗",
        "unit": "Ω",
        "state_class": SensorStateClass.MEASUREMENT,
        "precision": 0,
    },
    "stable": {
        "name": "称重稳定",
        "unit": None,
        "state_class": SensorStateClass.MEASUREMENT,
        "precision": 0,
    },
    "bmi": {
        "name": "BMI",
        "state_class": SensorStateClass.MEASUREMENT,
        "precision": 1,
    },
    "body_fat": {
        "name": "体脂率",
        "unit": "%",
        "state_class": SensorStateClass.MEASUREMENT,
        "precision": 1,
    },
    "water": {
        "name": "水分率",
        "unit": "%",
        "state_class": SensorStateClass.MEASUREMENT,
        "precision": 1,
    },
    "muscle": {
        "name": "肌肉量",
        "unit": "kg",
        "device_class": SensorDeviceClass.WEIGHT,
        "state_class": SensorStateClass.MEASUREMENT,
        "precision": 1,
    },
    "protein": {
        "name": "蛋白质率",
        "unit": "%",
        "state_class": SensorStateClass.MEASUREMENT,
        "precision": 1,
    },
    "bone": {
        "name": "骨量",
        "unit": "kg",
        "device_class": SensorDeviceClass.WEIGHT,
        "state_class": SensorStateClass.MEASUREMENT,
        "precision": 2,
    },
    "battery": {
        "name": "电量",
        "unit": PERCENTAGE,
        "device_class": SensorDeviceClass.BATTERY,
        "state_class": SensorStateClass.MEASUREMENT,
        "entity_category": EntityCategory.DIAGNOSTIC,
        "precision": 0,
    },
    "timestamp": {
        "name": "最近测量时间",
        "device_class": SensorDeviceClass.TIMESTAMP,
        "entity_category": EntityCategory.DIAGNOSTIC,
    },
    # 最近一次测量归给了谁（用户名 / 访客）
    "last_user": {
        "name": "最近测量归属",
        "icon": "mdi:account-question",
    },
    # 归属的置信度，见 matcher.py
    "confidence": {
        "name": "归属置信度",
        "device_class": SensorDeviceClass.ENUM,
        "options": CONFIDENCE_OPTIONS,
        "entity_category": EntityCategory.DIAGNOSTIC,
    },
}


def _scale_device(coordinator: AfuScaleCoordinator) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, coordinator.address)},
        name="AFU 体脂秤",
        manufacturer="沃莱科技",
        model="AFU-WL-TZ-A1",
    )


class AfuSensor(RestoreSensor):
    """AFU 体脂秤传感器基类（设备级："最近一次测量"）。重启后恢复上次的值。"""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: AfuScaleCoordinator, key: str) -> None:
        self._coordinator = coordinator
        self._key = key
        self._def = SENSOR_DEFS[key]
        self._attr_unique_id = f"{DOMAIN}_{coordinator.address}_{key}"
        self._attr_name = self._def["name"]
        self._attr_native_unit_of_measurement = self._def.get("unit")
        if self._def.get("device_class"):
            self._attr_device_class = self._def["device_class"]
        if self._def.get("state_class"):
            self._attr_state_class = self._def["state_class"]
        if self._def.get("entity_category"):
            self._attr_entity_category = self._def["entity_category"]
        if self._def.get("options"):
            self._attr_options = list(self._def["options"])
        if self._def.get("icon"):
            self._attr_icon = self._def["icon"]
        if "precision" in self._def:
            self._attr_suggested_display_precision = self._def["precision"]

    @property
    def device_info(self) -> DeviceInfo:
        return _scale_device(self._coordinator)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        last = await self.async_get_last_sensor_data()
        if last is not None and last.native_value is not None:
            self._attr_native_value = last.native_value

    @callback
    def async_update_state(self, value) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()


class AfuUserSensor(AfuSensor):
    """某个用户自己的传感器，挂在该用户的子设备下（子设备归属于秤）。"""

    def __init__(
        self, coordinator: AfuScaleCoordinator, user: UserProfile, key: str
    ) -> None:
        super().__init__(coordinator, key)
        self._user = user
        self._attr_unique_id = f"{DOMAIN}_{coordinator.address}_{user.id}_{key}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, f"{self._coordinator.address}_{self._user.id}")},
            name=f"AFU 体脂秤 {self._user.name}",
            manufacturer="沃莱科技",
            model="AFU-WL-TZ-A1",
            via_device_id=self._coordinator.scale_device_id,
        )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: AfuScaleCoordinator = hass.data[DOMAIN][entry.entry_id]

    # 先建好"秤"这个设备，用户子设备用它的 id 做 via_device_id
    scale = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, **_scale_device(coordinator)
    )
    coordinator.scale_device_id = scale.id

    device_entities =[AfuSensor(coordinator, key) for key in SENSOR_DEFS]
    for entity in device_entities:
        coordinator.register_entity(entity._key, entity)

    user_entities: list[AfuUserSensor] = []
    for user in coordinator.users:
        for key in USER_SENSOR_KEYS:
            entity = AfuUserSensor(coordinator, user, key)
            coordinator.register_user_entity(user.id, key, entity)
            user_entities.append(entity)

    async_add_entities([*device_entities, *user_entities])
