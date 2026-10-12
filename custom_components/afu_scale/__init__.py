"""AFU 体脂秤集成入口"""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import restore_state
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONF_DEBUG,
    TUNABLES,
    DOMAIN,
    PENDING_KEY,
    SERVICE_ASSIGN,
    SERVICE_INJECT,
    STORAGE_VERSION,
)
from .coordinator import AfuScaleCoordinator
from .sensor import SENSOR_DEFS
from .users import get_users

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]

ASSIGN_SCHEMA = vol.Schema(
    {
        vol.Required("user"): cv.string,
        vol.Optional(CONF_ADDRESS): cv.string,
    }
)
INJECT_SCHEMA = vol.Schema(
    {
        vol.Required("packet"): cv.string,
        vol.Optional(CONF_ADDRESS): cv.string,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    address = entry.data[CONF_ADDRESS].upper()
    coordinator = AfuScaleCoordinator(
        hass,
        entry.entry_id,
        address,
        get_users(entry),
        debug=bool(entry.options.get(CONF_DEBUG, False)),
        settings={k: entry.options[k] for k in TUNABLES if k in entry.options},
    )
    await coordinator.async_load()
    _async_remove_deleted_users(hass, entry, address, {u.id for u in coordinator.users})
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # 用户/调试选项变化后整体重载，实体按新的用户列表重建
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    _async_register_services(hass)
    coordinator.start()
    # 刚通过"新用户发现"建了用户：把触发发现的那次访客读数归给他
    if pending := hass.data.get(PENDING_KEY, {}).pop(entry.entry_id, None):
        ts = dt_util.parse_datetime(pending["timestamp"]) or dt_util.utcnow()
        coordinator.adopt_measurement(
            pending["user_id"], pending["weight"], pending["impedance"], ts
        )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = hass.data[DOMAIN].pop(entry.entry_id)
    await coordinator.stop()
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not hass.data[DOMAIN]:
        hass.services.async_remove(DOMAIN, SERVICE_ASSIGN)
        hass.services.async_remove(DOMAIN, SERVICE_INJECT)
    return ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """删除集成时清掉它留下的数据，重新添加后从零开始。

    - 参考体重存储文件（按条目 id 命名，不删会一直留在磁盘上）
    - restore_state 里这个条目各实体的最后状态：HA 会把已删除实体的状态保留 7 天，
      重新添加后实体 id 相同，传感器会把旧值"复活"（比如还显示上次的归属和体重）。
      此时实体已卸载、但注册表还没清理，所以还能按条目找到它们。
    """
    await Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}").async_remove()
    last_states = restore_state.async_get(hass).last_states
    for ent in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id):
        last_states.pop(ent.entity_id, None)


def _async_remove_deleted_users(
    hass: HomeAssistant, entry: ConfigEntry, address: str, user_ids: set[str]
) -> None:
    """用户被删除后，清掉他的实体和子设备，免得一直留着"不可用"。"""
    # 设备级实体的 unique_id 后缀（其余都是 <用户id>_<key>）
    flat = {*SENSOR_DEFS, "measuring", "charging"}
    prefix = f"{DOMAIN}_{address}_"
    ent_reg = er.async_get(hass)
    for ent in er.async_entries_for_config_entry(ent_reg, entry.entry_id):
        uid = ent.unique_id
        if not uid.startswith(prefix):
            continue
        rest = uid[len(prefix):]
        if rest in flat:
            continue
        if rest.split("_", 1)[0] not in user_ids:
            ent_reg.async_remove(ent.entity_id)
    dev_reg = dr.async_get(hass)
    for dev in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
        for domain, ident in dev.identifiers:
            if domain != DOMAIN or ident == address or not ident.startswith(address + "_"):
                continue
            if ident[len(address) + 1:] not in user_ids:
                dev_reg.async_remove_device(dev.id)


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


def _coordinators(hass: HomeAssistant, address: str | None) -> list[AfuScaleCoordinator]:
    coords: list[AfuScaleCoordinator] = list(hass.data.get(DOMAIN, {}).values())
    if address:
        coords = [c for c in coords if c.address == address.strip().upper()]
    if not coords:
        raise ServiceValidationError("没有找到匹配的体脂秤")
    return coords


def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_ASSIGN):
        return

    async def _assign(call: ServiceCall) -> None:
        coords = _coordinators(hass, call.data.get(CONF_ADDRESS))
        # 有多台秤时，只在"有这个用户且有待归属测量"的那台上执行
        for c in coords:
            if c.last_measurement is not None and any(
                u.name == call.data["user"] or u.id == call.data["user"]
                for u in c.users
            ):
                c.assign_last_measurement(call.data["user"])
                return
        raise ServiceValidationError("没有可归属的测量，或没有这个用户")

    async def _inject(call: ServiceCall) -> None:
        coords = _coordinators(hass, call.data.get(CONF_ADDRESS))
        try:
            data = bytes.fromhex(call.data["packet"].replace(" ", ""))
        except ValueError as err:
            raise ServiceValidationError("packet 不是有效的十六进制") from err
        coords[0].inject_packet(data)

    hass.services.async_register(DOMAIN, SERVICE_ASSIGN, _assign, schema=ASSIGN_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_INJECT, _inject, schema=INJECT_SCHEMA)
