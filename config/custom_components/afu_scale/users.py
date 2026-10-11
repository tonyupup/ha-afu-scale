"""用户资料的读取。

用户列表存在 config entry 的 options 里。刚装好时一个用户都没有，所有读数都是访客；
第一次称重会弹出"发现"卡片，由用户决定新建用户或归给现有用户。
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry

from .const import CONF_USERS
from .matcher import UserProfile


def get_user_dicts(entry: ConfigEntry) -> list[dict[str, Any]]:
    return [dict(u) for u in entry.options.get(CONF_USERS, [])]


def get_users(entry: ConfigEntry) -> list[UserProfile]:
    return [UserProfile.from_dict(d) for d in get_user_dicts(entry)]
