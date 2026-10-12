"""AFU 体脂秤集成配置流"""

from __future__ import annotations

import uuid
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.const import CONF_ADDRESS, CONF_NAME
from homeassistant.core import callback
from homeassistant.util import dt as dt_util

from .const import (
    CONF_AGE,
    CONF_DEBUG,
    TUNABLES,
    CONF_HEIGHT_CM,
    CONF_INITIAL_WEIGHT,
    CONF_SEX,
    CONF_TOLERANCE_KG,
    CONF_USER_ID,
    CONF_USERS,
    DEFAULT_AGE,
    DEFAULT_HEIGHT_CM,
    DEFAULT_SEX,
    DOMAIN,
    MAX_USERS,
    PENDING_KEY,
)
from .users import get_user_dicts

# 个人参数（用于 BIA 计算）
PROFILE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_HEIGHT_CM, default=DEFAULT_HEIGHT_CM): vol.All(
            vol.Coerce(float), vol.Range(min=50, max=250)
        ),
        vol.Optional(CONF_SEX, default=DEFAULT_SEX): vol.In(["male", "female"]),
        vol.Optional(CONF_AGE, default=DEFAULT_AGE): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=120)
        ),
    }
)

# 手动输入 MAC 的兜底流程（用户资料不在这里填：第一次称重时会弹卡片）
DATA_SCHEMA = vol.Schema({vol.Required(CONF_ADDRESS): str})

# 选项里的用户表单
USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NAME): vol.All(str, vol.Length(min=1, max=20)),
        vol.Required(CONF_HEIGHT_CM, default=DEFAULT_HEIGHT_CM): vol.All(
            vol.Coerce(float), vol.Range(min=50, max=250)
        ),
        vol.Required(CONF_SEX, default=DEFAULT_SEX): vol.In(["male", "female"]),
        vol.Required(CONF_AGE, default=DEFAULT_AGE): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=120)
        ),
        vol.Optional(CONF_INITIAL_WEIGHT): vol.All(
            vol.Coerce(float), vol.Range(min=5, max=300)
        ),
        vol.Optional(CONF_TOLERANCE_KG): vol.All(
            vol.Coerce(float), vol.Range(min=0.5, max=20)
        ),
    }
)

# 发现卡片上的新用户表单（体重来自那次访客读数，不用再填）
NEW_USER_SCHEMA = vol.Schema(
    {vol.Required(CONF_NAME): vol.All(str, vol.Length(min=1, max=20))}
).extend(PROFILE_SCHEMA.schema)


class AfuScaleConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """AFU 体脂秤配置流"""

    VERSION = 1

    def __init__(self) -> None:
        self._discovery_info: BluetoothServiceInfoBleak | None = None
        self._guest: dict[str, Any] = {}

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> AfuScaleOptionsFlow:
        return AfuScaleOptionsFlow()

    # ---- 蓝牙自动发现（秤广播 AFU-WL-TZ-A1 时触发）------------------------

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> config_entries.ConfigFlowResult:
        address = discovery_info.address.upper()
        await self.async_set_unique_id(address)
        self._abort_if_unique_id_configured()
        self._discovery_info = discovery_info
        self.context["title_placeholders"] = {
            "name": discovery_info.name or "AFU 体脂秤",
            "address": address,
        }
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        assert self._discovery_info is not None
        address = self._discovery_info.address.upper()
        if user_input is not None:
            return self.async_create_entry(
                title=f"AFU 体脂秤 {address}", data={CONF_ADDRESS: address}
            )
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={
                "name": self._discovery_info.name or "AFU 体脂秤",
                "address": address,
            },
        )

    # ---- 访客读数发现（出现无法归属的读数时，由协调器发起）------------------

    async def async_step_integration_discovery(
        self, discovery_info: dict[str, Any]
    ) -> config_entries.ConfigFlowResult:
        entry = self.hass.config_entries.async_get_entry(discovery_info["entry_id"])
        if entry is None or entry.state is not config_entries.ConfigEntryState.LOADED:
            return self.async_abort(reason="entry_not_found")
        # 固定的 unique_id：同一时间只有一张卡片；用户点"忽略"后就不会再弹
        await self.async_set_unique_id(f"{discovery_info['address']}_new_user")
        self._abort_if_unique_id_configured()
        self._guest = discovery_info
        self.context["title_placeholders"] = {
            "name": f"新用户？{discovery_info['weight']:.1f} kg",
            "address": discovery_info["address"],
        }
        return await self.async_step_new_user_menu()

    async def async_step_new_user_menu(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """已有用户：让用户选"新建"还是"归给现有用户"；一个用户都没有就直接新建。"""
        entry = self.hass.config_entries.async_get_entry(self._guest["entry_id"])
        if entry is None:
            return self.async_abort(reason="entry_not_found")
        users = get_user_dicts(entry)
        if not users:
            return await self.async_step_new_user()
        options = ["assign_existing"]
        if len(users) < MAX_USERS:
            options.insert(0, "new_user")
        return self.async_show_menu(
            step_id="new_user_menu",
            menu_options=options,
            description_placeholders=self._guest_placeholders(),
        )

    def _guest_placeholders(self) -> dict[str, str]:
        return {
            "weight": f"{self._guest['weight']:.1f}",
            "impedance": f"{self._guest['impedance']:.0f}",
        }

    async def async_step_new_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        guest = self._guest
        entry = self.hass.config_entries.async_get_entry(guest["entry_id"])
        if entry is None:
            return self.async_abort(reason="entry_not_found")
        users = get_user_dicts(entry)
        errors: dict[str, str] = {}
        if user_input is not None:
            if any(u[CONF_NAME] == user_input[CONF_NAME] for u in users):
                errors[CONF_NAME] = "name_taken"
            elif len(users) >= MAX_USERS:
                return self.async_abort(reason="max_users")
            else:
                new = {CONF_USER_ID: uuid.uuid4().hex[:8], **user_input}
                # 用户列表变更会触发重载；把那次访客读数留给新协调器采纳
                self.hass.data.setdefault(PENDING_KEY, {})[entry.entry_id] = {
                    "user_id": new[CONF_USER_ID],
                    "weight": guest["weight"],
                    "impedance": guest["impedance"],
                    "timestamp": guest["timestamp"],
                }
                self.hass.config_entries.async_update_entry(
                    entry, options={**entry.options, CONF_USERS: [*users, new]}
                )
                return self.async_abort(
                    reason="user_added",
                    description_placeholders={"name": user_input[CONF_NAME]},
                )
        return self.async_show_form(
            step_id="new_user",
            data_schema=NEW_USER_SCHEMA,
            description_placeholders=self._guest_placeholders(),
            errors=errors,
        )

    async def async_step_assign_existing(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.ConfigFlowResult:
        """用这次读数更新现有用户：重置他的参考体重（体重变化大、或久未称重时用）。"""
        guest = self._guest
        entry = self.hass.config_entries.async_get_entry(guest["entry_id"])
        coordinator = self.hass.data.get(DOMAIN, {}).get(guest["entry_id"])
        if entry is None or coordinator is None:
            return self.async_abort(reason="entry_not_found")
        users = get_user_dicts(entry)
        if user_input is not None:
            user = next((u for u in users if u[CONF_USER_ID] == user_input[CONF_USER_ID]), None)
            if user is None:
                return self.async_abort(reason="entry_not_found")
            coordinator.adopt_measurement(
                user[CONF_USER_ID],
                guest["weight"],
                guest["impedance"],
                dt_util.parse_datetime(guest["timestamp"]) or dt_util.utcnow(),
                rebase=True,
            )
            return self.async_abort(
                reason="user_updated",
                description_placeholders={"name": user[CONF_NAME]},
            )
        return self.async_show_form(
            step_id="assign_existing",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USER_ID): vol.In(
                        {u[CONF_USER_ID]: u[CONF_NAME] for u in users}
                    )
                }
            ),
            description_placeholders=self._guest_placeholders(),
        )

    # ---- 手动输入 MAC -----------------------------------------------------

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            address = str(user_input[CONF_ADDRESS]).strip().upper()
            if not self._valid_mac(address):
                errors[CONF_ADDRESS] = "invalid_mac"
            else:
                await self.async_set_unique_id(address)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"AFU 体脂秤 {address}", data={CONF_ADDRESS: address}
                )

        return self.async_show_form(
            step_id="user",
            data_schema=DATA_SCHEMA,
            errors=errors,
        )

    @staticmethod
    def _valid_mac(address: str) -> bool:
        parts = address.split(":")
        return len(address) == 17 and len(parts) == 6 and all(
            len(p) == 2 for p in parts
        )


class AfuScaleOptionsFlow(config_entries.OptionsFlow):
    """多用户管理：增删改用户、开关调试。

    每个动作保存后即结束对话框（再次打开"配置"继续下一项）。
    保存会让集成重载，实体按新的用户列表重建。
    """

    def __init__(self) -> None:
        self._edit_id: str | None = None

    # -- helpers --

    def _users(self) -> list[dict[str, Any]]:
        return get_user_dicts(self.config_entry)

    def _save(self, users: list[dict[str, Any]], **extra: Any):
        options = {**self.config_entry.options, CONF_USERS: users, **extra}
        return self.async_create_entry(data=options)

    def _name_taken(self, name: str, ignore_id: str | None = None) -> bool:
        return any(
            u[CONF_NAME] == name and u[CONF_USER_ID] != ignore_id for u in self._users()
        )

    @staticmethod
    def _clean(user_input: dict[str, Any]) -> dict[str, Any]:
        """可选字段被清空时不要留着旧值。"""
        return {k: v for k, v in user_input.items() if v not in (None, "")}

    # -- steps --

    async def async_step_init(self, user_input: dict[str, Any] | None = None):
        options = ["add_user"]
        if self._users():
            options += ["edit_user", "remove_user"]
        options += ["timing", "debug"]
        return self.async_show_menu(step_id="init", menu_options=options)

    async def async_step_add_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        users = self._users()
        if len(users) >= MAX_USERS:
            return self.async_abort(reason="max_users")
        if user_input is not None:
            if self._name_taken(user_input[CONF_NAME]):
                errors[CONF_NAME] = "name_taken"
            else:
                new = {CONF_USER_ID: uuid.uuid4().hex[:8], **self._clean(user_input)}
                return self._save([*users, new])
        return self.async_show_form(
            step_id="add_user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_edit_user(self, user_input: dict[str, Any] | None = None):
        users = self._users()
        if user_input is not None:
            self._edit_id = user_input[CONF_USER_ID]
            return await self.async_step_edit_user_form()
        return self.async_show_form(
            step_id="edit_user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USER_ID): vol.In(
                        {u[CONF_USER_ID]: u[CONF_NAME] for u in users}
                    )
                }
            ),
        )

    async def async_step_edit_user_form(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        users = self._users()
        current = next((u for u in users if u[CONF_USER_ID] == self._edit_id), None)
        if current is None:
            return self.async_abort(reason="unknown_user")
        if user_input is not None:
            if self._name_taken(user_input[CONF_NAME], ignore_id=self._edit_id):
                errors[CONF_NAME] = "name_taken"
            else:
                updated = {CONF_USER_ID: self._edit_id, **self._clean(user_input)}
                return self._save(
                    [updated if u[CONF_USER_ID] == self._edit_id else u for u in users]
                )
        return self.async_show_form(
            step_id="edit_user_form",
            data_schema=self.add_suggested_values_to_schema(
                USER_SCHEMA, user_input or current
            ),
            errors=errors,
        )

    async def async_step_remove_user(self, user_input: dict[str, Any] | None = None):
        users = self._users()
        if not users:
            return self.async_abort(reason="unknown_user")
        if user_input is not None:
            return self._save(
                [u for u in users if u[CONF_USER_ID] != user_input[CONF_USER_ID]]
            )
        return self.async_show_form(
            step_id="remove_user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USER_ID): vol.In(
                        {u[CONF_USER_ID]: u[CONF_NAME] for u in users}
                    )
                }
            ),
        )

    async def async_step_timing(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self._save(self._users(), **user_input)
        opts = self.config_entry.options
        schema = {}
        for key, (default, lo, hi, step) in TUNABLES.items():
            kind = float if isinstance(default, float) else int
            schema[vol.Required(key, default=opts.get(key, default))] = vol.All(
                vol.Coerce(kind), vol.Range(min=lo, max=hi)
            )
        return self.async_show_form(step_id="timing", data_schema=vol.Schema(schema))

    async def async_step_debug(self, user_input: dict[str, Any] | None = None):
        if user_input is not None:
            return self._save(self._users(), **user_input)
        return self.async_show_form(
            step_id="debug",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_DEBUG,
                        default=bool(self.config_entry.options.get(CONF_DEBUG, False)),
                    ): bool
                }
            ),
        )
