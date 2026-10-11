"""AFU 体脂秤 BLE 连接协调器。

秤大部分时间处于休眠（完全不广播），踩上去唤醒后才会在短时间内发出可连接广播，
用完自动关机。所以这里不做定时轮询/重连，而是由广播驱动：

  - 向 HA 蓝牙集成（含 ESP32 蓝牙代理）注册该 MAC 的广播回调
  - 收到广播（= 秤醒了）就立刻发起一次连接会话
  - 会话内：握手、订阅 0xFFB2 通知、解析 0xAC 报文（体重/稳定/阻抗/电量）
  - 秤断开 / 长时间无数据 / 连接失败都直接结束会话，等下一次广播再连

多用户：
  - 实时读数（体重/阻抗/稳定）每个包都更新；
  - 每次称重只在第一个"稳定且阻抗有效"的包上做一次最终判定（见 matcher.py），
    归属到某个用户，并更新该用户的参考体重和实体，触发 afu_scale_measurement 事件。

"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import TYPE_CHECKING, Any, Callable

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY
from homeassistant.helpers import discovery_flow

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher,
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import (
    CONFIDENCE_MANUAL,
    CONNECT_ATTEMPTS,
    CONNECT_TIMEOUT,
    DOMAIN,
    EVENT_MEASUREMENT,
    HANDSHAKE,
    MAX_USERS,
    MEASURING_IDLE_TIMEOUT,
    NOTIFY_CHAR_UUID,
    PACKET_MAGIC,
    RESET_DELTA_KG,
    LIVE_MIN_DELTA_KG,
    LIVE_MIN_INTERVAL,
    AWAKE_HOLD,
    IBEACON_MARK,
    PLAIN_ADV_PREFIX,
    NO_DATA_IDLE_TIMEOUT,
    POLL_ADV_MAX_AGE,
    QUIET_MAX,
    RESULT_WAIT,
    POLL_INTERVAL,
    RETRY_COOLDOWN,
    SESSION_IDLE_TIMEOUT,
    STABLE_FLAG,
    STORAGE_VERSION,
    USER_SENSOR_KEYS,
    WRITE_CHAR_UUID,
)
from .matcher import (
    MatchResult,
    Reference,
    UserProfile,
    match,
    seed_reference,
    update_reference,
)

if TYPE_CHECKING:
    from .sensor import AfuSensor

_LOGGER = logging.getLogger(__name__)

# 由用户资料（身高/年龄/性别）算出来的指标
BIA_KEYS = ("bmi", "body_fat", "water", "muscle", "protein", "bone")


def _weight_g(hi: int, mid: int, lo: int) -> int:
    return (hi - 0x68) * 65536 + mid * 256 + lo


def parse_packet(data: bytes):
    """解析 0xAC 体重帧（[6]==2），返回 (weight_kg, is_stable) 或 None。
      [2]  bit7 置位表示数值锁定稳定（实测：称重过程中 0x00，定格后 0x80）
      [3:6] 体重（克，高位带 -0x68 偏移）
    阻抗不在这种帧里（[8:10] 是常量）；见 parse_result。
    """
    if len(data) < 10 or data[0] != PACKET_MAGIC or data[6] != 0x02:
        return None
    raw_weight = _weight_g(data[3], data[4], data[5])
    if raw_weight <= 0:
        return None
    return raw_weight / 1000.0, bool(data[2] & STABLE_FLAG)


def parse_result(data: bytes):
    """解析定格后秤发的结果帧（[2]==0x02；体重帧的 [2] 只会是 0x00/0x80），返回 (weight_kg, impedance_ohm) 或 None。
      [4:6] 阻抗 Ω，0 表示没测到（穿袜子/电极没踩到）
      [10:13] 体重（同体重帧编码）
    [6:8] 含义未知（光脚 0x0192 左右，没测到时全 0），暂不使用；
    阻抗取 [4:6]（≈445）还是 [6:8]（≈402）未能确认，归属只用相对差，影响不大。
    """
    if len(data) < 13 or data[0] != PACKET_MAGIC or data[2] != 0x02:
        return None
    raw_weight = _weight_g(data[10], data[11], data[12])
    if raw_weight <= 0:
        return None
    return raw_weight / 1000.0, float((data[4] << 8) | data[5])


def parse_battery(data: bytes):
    """解析电量，返回 (percent, charging) 或 None。

    只认体重帧（[6]==2）的 [11]：最高位=充电中，低 7 位=百分比。
    实测 [6]==1 的帧里 [7] 不是电量（几分钟内 24%→18% 且一直"充电"），不使用。
    """
    if len(data) < 12 or data[0] != PACKET_MAGIC or data[6] != 0x02:
        return None
    raw = data[11]
    percent = raw & 0x7F
    if raw == 0xFF or percent == 0x7F or percent > 100:
        return None
    return percent, bool(raw & 0x80)


class AfuScaleCoordinator:
    """由广播驱动的体脂秤 BLE 会话管理，负责多用户归属并分发测量数据。"""

    def __init__(self, hass: HomeAssistant, entry_id: str, address: str,
                 users: list[UserProfile], debug: bool = False) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.address = address
        self.users = users
        self.debug = debug

        self._unsub_adv: Callable[[], None] | None = None
        self._session: asyncio.Task | None = None
        self._cooldown_until: float = 0.0
        self._unsub_poll: Callable[[], None] | None = None
        self._awake_at: float = 0.0  # 最近一次见到"醒着"广播的时间
        self._awake: bool | None = None  # None=还没判断出来
        self._raw_seen = False  # 扫描器有没有提供原始广播字节
        self._quiet_until: float = 0.0  # 空闲断开后静默到这个时间（让秤睡着）
        self._session_got_data = False
        self._shutdown = False
        self._last_activity: float = 0.0

        # 实体：entities 是设备级（"最近一次测量"），user_entities 是每个用户的
        self.entities: dict[str, AfuSensor] = {}
        self.user_entities: dict[tuple[str, str], AfuSensor] = {}
        self.scale_device_id: str | None = None  # 设备注册表里"秤"的 id，用户子设备引用它

        # 多用户状态
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}"
        )
        self.refs: dict[str, Reference] = {}
        self.last_measurement: dict[str, Any] | None = None
        self._committed_weight: float | None = None
        self._live_weight: float | None = None  # 最近一次写入实体的实时体重
        self._live_at: float = 0.0
        self._pending_weight: float | None = None  # 已定格、等结果帧的体重
        self._result_handle: asyncio.TimerHandle | None = None

        # 测量中状态：收到有效数据包置 True，无数据超时或断开后置 False
        self.measuring = False
        self._last_data_at: float = 0.0
        self._idle_handle: asyncio.TimerHandle | None = None
        self.measuring_entity = None
        self.charging_entity = None

    # ---- 持久化 ------------------------------------------------------

    async def async_load(self) -> None:
        """读取每个用户的参考体重；新用户若填了当前体重则用它做初始参考。"""
        data = await self._store.async_load() or {}
        ids = {u.id for u in self.users}
        self.refs = {
            uid: Reference.from_dict(d)
            for uid, d in data.get("refs", {}).items()
            if uid in ids
        }
        now = time.time()
        seeded = False
        for u in self.users:
            if u.id not in self.refs and u.initial_weight:
                self.refs[u.id] = seed_reference(u.initial_weight, now)
                seeded = True
        if seeded:
            self._schedule_save()

    def _dump(self) -> dict[str, Any]:
        return {"refs": {uid: ref.to_dict() for uid, ref in self.refs.items()}}

    def _schedule_save(self) -> None:
        self._store.async_delay_save(self._dump, 1.0)

    # ---- 实体相关 ----------------------------------------------------

    def _set_measuring(self, value: bool) -> None:
        if self.measuring == value:
            return
        self.measuring = value
        if self.measuring_entity is not None:
            self.measuring_entity.async_update_state(value)

    @callback
    def register_entity(self, key: str, entity: AfuSensor) -> None:
        self.entities[key] = entity

    @callback
    def register_user_entity(self, user_id: str, key: str, entity: AfuSensor) -> None:
        self.user_entities[(user_id, key)] = entity

    def user_by_id(self, user_id: str | None) -> UserProfile | None:
        return next((u for u in self.users if u.id == user_id), None)

    # ---- 生命周期 ----------------------------------------------------

    @callback
    def start(self) -> None:
        self._shutdown = False
        self._unsub_adv = bluetooth.async_register_callback(
            self.hass,
            self._on_advertisement,
            BluetoothCallbackMatcher(address=self.address, connectable=True),
            BluetoothScanningMode.ACTIVE,
        )
        # 秤醒着/睡着的广播内容完全一样，HA 只在内容变化时才回调，
        # 所以光靠回调会漏掉后续的唤醒；再按时间轮询"最近是否有广播"兜底
        self._unsub_poll = async_track_time_interval(
            self.hass, self._poll, timedelta(seconds=POLL_INTERVAL)
        )
        _LOGGER.debug("AFU Scale: 已注册 %s 的广播回调，等待秤唤醒", self.address)

    async def stop(self) -> None:
        self._shutdown = True
        if self._unsub_poll is not None:
            self._unsub_poll()
            self._unsub_poll = None
        if self._unsub_adv is not None:
            self._unsub_adv()
            self._unsub_adv = None
        if self._session is not None:
            self._session.cancel()
            try:
                await self._session
            except asyncio.CancelledError:
                pass
            self._session = None
        if self._result_handle is not None:
            self._result_handle.cancel()
            self._result_handle = None
        self._pending_weight = None
        self._end_measuring()
        await self._store.async_save(self._dump())

    # ---- 广播 → 会话 -------------------------------------------------

    @callback
    def _on_advertisement(
        self, service_info: BluetoothServiceInfoBleak, change: BluetoothChange
    ) -> None:
        """收到该秤的可连接广播。已有会话、在冷却期内或秤睡着则忽略。"""
        self._note_adv(service_info)
        self._maybe_connect(service_info)

    @callback
    def _poll(self, _now) -> None:
        info = bluetooth.async_last_service_info(
            self.hass, self.address, connectable=True
        )
        if info is None:
            return
        self._note_adv(info)
        if time.monotonic() - info.time <= POLL_ADV_MAX_AGE:
            self._maybe_connect(info)

    def _note_adv(self, info: BluetoothServiceInfoBleak) -> None:
        """从最新一包广播判断秤醒没醒（实测：有人踩上去/屏幕亮着时发 iBeacon 包，
        空闲时只发带 FFB0 服务的普通广播；扫描响应两种状态都有，不能用）。"""
        raw = info.raw
        if not raw:
            return
        self._raw_seen = True
        if IBEACON_MARK in raw:
            self._awake_at = time.monotonic()
            awake = True
        elif raw.startswith(PLAIN_ADV_PREFIX):
            self._awake_at = 0.0
            self._quiet_until = 0.0  # 秤已经睡了，之后再出现 iBeacon 就是真的被唤醒
            awake = False
        else:
            return  # 扫描响应：不改变判断
        if awake != self._awake:
            self._awake = awake
            _LOGGER.info(
                "AFU Scale: 秤%s (raw=%s)", "醒了" if awake else "睡着了", raw.hex()
            )

    def _is_awake(self) -> bool:
        if not self._raw_seen:
            return True  # 拿不到原始广播（如部分远程代理）：退回"随时连"
        return time.monotonic() - self._awake_at <= AWAKE_HOLD

    def _maybe_connect(self, service_info: BluetoothServiceInfoBleak) -> None:
        if self._shutdown or not self._is_awake():
            return
        if time.monotonic() < self._quiet_until:
            return
        if self._session is not None and not self._session.done():
            return
        if time.monotonic() < self._cooldown_until:
            return
        _LOGGER.debug(
            "AFU Scale: 收到广播 rssi=%s source=%s，发起连接",
            service_info.rssi, service_info.source,
        )
        self._session = self.hass.async_create_background_task(
            self._run_session(service_info.device),
            name=f"afu_scale session {self.address}",
        )

    async def _run_session(self, device: BLEDevice) -> None:
        """一次完整的连接会话；任何结束路径都会进入冷却，等下一次广播。"""
        disconnected = asyncio.Event()
        client: BleakClientWithServiceCache | None = None
        try:
            try:
                client = await establish_connection(
                    BleakClientWithServiceCache,
                    device,
                    self.address,
                    disconnected_callback=lambda _client: disconnected.set(),
                    max_attempts=CONNECT_ATTEMPTS,
                    ble_device_callback=lambda: bluetooth.async_ble_device_from_address(
                        self.hass, self.address, connectable=True
                    ) or device,
                    timeout=CONNECT_TIMEOUT,
                )
            except (BleakError, TimeoutError) as exc:
                # 秤可能已经又睡了，属于正常情况，不打 warning
                _LOGGER.debug("AFU Scale: 连接失败 %s: %s", self.address, exc)
                return
            # establish_connection 重试时，前一次失败的尝试也会触发断开回调，
            # 会让下面的循环误以为"秤已断开"而立刻收尾；连上之后重新判断
            disconnected.clear()
            if not client.is_connected:
                _LOGGER.debug("AFU Scale: 连接建立后又断开 %s", self.address)
                return
            _LOGGER.info("AFU Scale: 已连接 %s", self.address)

            self._last_activity = time.monotonic()
            self._session_got_data = False
            await self._subscribe(client)

            idle_end = False
            while not disconnected.is_set() and not self._shutdown:
                try:
                    await asyncio.wait_for(disconnected.wait(), timeout=5)
                except TimeoutError:
                    # 一个数据都没收到的会话多半是误唤醒，早点断开
                    limit = (
                        SESSION_IDLE_TIMEOUT
                        if self._session_got_data
                        else NO_DATA_IDLE_TIMEOUT
                    )
                    if time.monotonic() - self._last_activity > limit:
                        idle_end = True
                        break
            _LOGGER.info(
                "AFU Scale: 会话结束 %s（%s）",
                self.address,
                (
                    "空闲断开，" + ("收到过数据" if self._session_got_data else "没有数据")
                    if idle_end
                    else "秤主动断开或关闭"
                ),
            )
            # 我们保持连接时秤不会睡；空闲断开后静默，等它睡着（看到普通广播）再继续。
            # 拿不到原始广播就没法判断睡/醒，此时不静默，免得漏测
            if idle_end and self._raw_seen:
                self._quiet_until = time.monotonic() + QUIET_MAX
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("AFU Scale: 会话异常: %s", exc)
        finally:
            self._cooldown_until = time.monotonic() + RETRY_COOLDOWN
            self._end_measuring()
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:  # noqa: BLE001
                    pass

    async def _subscribe(self, client: BleakClientWithServiceCache) -> None:
        # 发送握手包（写 0xFFB1，无响应写），触发完整数据推送
        try:
            await client.write_gatt_char(WRITE_CHAR_UUID, HANDSHAKE, response=False)
            _LOGGER.debug("AFU Scale: 已发送握手包")
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("AFU Scale: 握手包发送失败（忽略）: %s", exc)

        await client.start_notify(NOTIFY_CHAR_UUID, self._on_notify)
        _LOGGER.debug("AFU Scale: 已订阅 0xFFB2 通知")

    # ---- 数据处理 ----------------------------------------------------

    def inject_packet(self, data: bytes) -> None:
        """调试：把一个原始报文当作秤发来的通知处理（需在选项里开启调试）。"""
        if not self.debug:
            raise ServiceValidationError("未开启调试：请在集成选项里打开\"调试\"")
        self._on_notify(None, data)

    def _on_notify(self, _characteristic, data: bytes) -> None:
        """bleak 通知回调（在事件循环中调用）。"""
        self._last_activity = time.monotonic()
        self._session_got_data = True
        raw = bytes(data)
        _LOGGER.debug("AFU Scale: RAW %s", raw.hex())

        # 电量与体重是否有效无关（人离开后的报文里电量仍然有效），先单独处理
        battery = parse_battery(raw)
        if battery is not None:
            percent, charging = battery
            _LOGGER.debug("AFU Scale: 电量 %d%% charging=%s", percent, charging)
            if (entity := self.entities.get("battery")) is not None:
                entity.async_update_state(percent)
            if self.charging_entity is not None:
                self.charging_entity.async_update_state(charging)

        if (result_frame := parse_result(raw)) is not None:
            # 定格后的结果帧：带体重和阻抗，用它提交。连接比称重慢时（常见），
            # 只会收到这种帧，没有"定格的体重帧"，所以它也要能独立提交；
            # 它会重复发，已提交过同一个体重就忽略
            weight_kg, impedance = result_frame
            if self._pending_weight is not None or self._committed_weight is None or (
                abs(weight_kg - self._committed_weight) > RESET_DELTA_KG
            ):
                self._commit(weight_kg, impedance)
            return
        parsed = parse_packet(raw)
        if parsed is None:
            _LOGGER.debug("AFU Scale: 忽略报文 %s", raw.hex())
            return
        weight_kg, is_stable = parsed
        _LOGGER.debug("AFU Scale: %.2fkg stable=%s", weight_kg, is_stable)
        self._set_measuring(True)
        self._last_data_at = time.monotonic()
        if self._idle_handle is not None:
            self._idle_handle.cancel()
        self._idle_handle = self.hass.loop.call_later(
            MEASURING_IDLE_TIMEOUT, self._idle_timeout
        )

        # 实时读数：只更新体重/稳定标志（阻抗要等结果帧，BIA 和"最近测量时间"都在提交时才更新）。
        # 体重每秒会来 5~10 帧，限流后再写，免得记录器被刷屏；定格值总是写
        mono = time.monotonic()
        if (
            self._live_weight is None
            or (is_stable and weight_kg != self._live_weight)
            or (
                abs(weight_kg - self._live_weight) >= LIVE_MIN_DELTA_KG
                and mono - self._live_at >= LIVE_MIN_INTERVAL
            )
        ):
            self._live_weight, self._live_at = weight_kg, mono
            if (entity := self.entities.get("weight")) is not None:
                entity.async_update_state(weight_kg)
        if (entity := self.entities.get("stable")) is not None:
            entity.async_update_state(1.0 if is_stable else 0.0)

        if not is_stable:
            return
        # 同一次称重只提交一次；体重和上次提交差很多，说明换人了，允许再提交
        if (
            self._committed_weight is not None
            and abs(weight_kg - self._committed_weight) > RESET_DELTA_KG
        ):
            self._committed_weight = None
        if self._committed_weight is not None or self._pending_weight is not None:
            return
        # 定格了，但阻抗在随后的结果帧里：先等它，等不到就按无阻抗提交
        self._pending_weight = weight_kg
        self._result_handle = self.hass.loop.call_later(
            RESULT_WAIT, self._commit, weight_kg, 0.0
        )

    def _commit(self, weight_kg: float, impedance: float) -> None:
        """提交一次最终读数：此时才确定归属、更新参考、发事件。"""
        if self._result_handle is not None:
            self._result_handle.cancel()
            self._result_handle = None
        self._pending_weight = None
        self._committed_weight = weight_kg
        now_dt = dt_util.utcnow()
        result = match(weight_kg, impedance, self.users, self.refs, now_dt.timestamp())
        self._record(result, weight_kg, impedance, now_dt)

    # ---- 归属 / 提交 -------------------------------------------------

    def _record(
        self,
        result: MatchResult,
        weight_kg: float,
        impedance: float,
        now_dt,
        *,
        confidence: str | None = None,
        reassigned: bool = False,
        reset_reference: bool = False,
    ) -> None:
        """把一次最终读数归属给 result.user_id（None=访客），更新实体/参考/事件。

        reset_reference=True 时丢掉该用户的历史参考，只以这次读数为准（体重变化很大时用）。
        """
        profile = self.user_by_id(result.user_id)
        confidence = confidence or result.confidence
        values: dict[str, Any] = {
            "weight": weight_kg,
            "impedance": impedance,
            "timestamp": now_dt,
        }
        prev_ref: dict[str, Any] | None = None
        prev_values: dict[str, Any] = {}

        if profile is not None:
            values.update(self._compute_bia(weight_kg, impedance, profile))
            old = self.refs.get(profile.id)
            prev_ref = old.to_dict() if old is not None else None
            prev_values = {
                key: ent.native_value
                for key in USER_SENSOR_KEYS
                if (ent := self.user_entities.get((profile.id, key))) is not None
            }
            self.refs[profile.id] = update_reference(
                None if reset_reference else old,
                weight_kg, impedance, now_dt.timestamp(),
            )
            for key in USER_SENSOR_KEYS:
                ent = self.user_entities.get((profile.id, key))
                if ent is not None and key in values:
                    ent.async_update_state(values[key])
            self._schedule_save()

        # 设备级"最近一次测量"：BIA 按归属用户重算（手动改归属时需要）
        for key in ("weight", "impedance", "timestamp", *BIA_KEYS):
            if key in values and (ent := self.entities.get(key)) is not None:
                ent.async_update_state(values[key])
        if (ent := self.entities.get("last_user")) is not None:
            ent.async_update_state(profile.name if profile else "访客")
        if (ent := self.entities.get("confidence")) is not None:
            ent.async_update_state(confidence)

        self.last_measurement = {
            "user_id": profile.id if profile else None,
            "weight": weight_kg,
            "impedance": impedance,
            "now_dt": now_dt,
            "prev_ref": prev_ref,
            "prev_values": prev_values,
        }
        _LOGGER.info(
            "AFU Scale: 测量 %.2fkg %.0fΩ → %s (%s, %s)",
            weight_kg, impedance, profile.name if profile else "访客",
            confidence, result.reason,
        )
        event = {
            "address": self.address,
            "user_id": profile.id if profile else None,
            "user": profile.name if profile else None,
            "guest": profile is None,
            "confidence": confidence,
            "reassigned": reassigned,
            "timestamp": now_dt.isoformat(),
            **{k: v for k, v in values.items() if k != "timestamp"},
        }
        self.hass.bus.async_fire(EVENT_MEASUREMENT, event)

        # 访客读数：在"设置 → 设备与服务"里弹出发现卡片，问要不要为他新建用户
        if profile is None and not reassigned:
            self._offer_new_user(weight_kg, impedance, now_dt)

    # ---- 新用户发现 --------------------------------------------------

    @property
    def discovery_unique_id(self) -> str:
        return f"{self.address}_new_user"

    @callback
    def _abort_discovery(self) -> None:
        """撤掉还没处理的"新用户"发现卡片。"""
        mgr = self.hass.config_entries.flow
        for flow in mgr.async_progress_by_handler(
            DOMAIN, match_context={"source": SOURCE_INTEGRATION_DISCOVERY}
        ):
            if flow["context"].get("unique_id") == self.discovery_unique_id:
                mgr.async_abort(flow["flow_id"])

    @callback
    def _offer_new_user(self, weight_kg: float, impedance: float, now_dt) -> None:
        """为这次访客读数发起发现流程。

        同一时间只保留一张卡片，新的访客读数会替换旧的。用户在卡片上点"忽略"，
        就不会再被打扰（可在"已忽略"里恢复）。
        """
        if len(self.users) >= MAX_USERS:
            return
        self._abort_discovery()
        discovery_flow.async_create_flow(
            self.hass,
            DOMAIN,
            {"source": SOURCE_INTEGRATION_DISCOVERY},
            {
                "entry_id": self.entry_id,
                "address": self.address,
                "weight": weight_kg,
                "impedance": impedance,
                "timestamp": now_dt.isoformat(),
            },
        )

    @callback
    def adopt_measurement(
        self,
        user_id: str,
        weight_kg: float,
        impedance: float,
        now_dt,
        *,
        rebase: bool = False,
    ) -> None:
        """发现卡片的结果：把那次访客读数归给某个用户。

        新建用户时 rebase=False（他本来就没有历史）；"归给现有用户"时 rebase=True，
        丢掉他旧的参考体重、以这次为准，这样体重变化很大后下次就能直接认出来。
        """
        if self.user_by_id(user_id) is None:
            return
        self._record(
            MatchResult(user_id, CONFIDENCE_MANUAL, "发现卡片采纳"),
            weight_kg, impedance, now_dt,
            confidence=CONFIDENCE_MANUAL,
            reset_reference=rebase,
        )

    def assign_last_measurement(self, user: str) -> None:
        """把最近一次测量改归属给某个用户（按名称或 id），并撤销之前的归属。"""
        lm = self.last_measurement
        if lm is None:
            raise ServiceValidationError("还没有可归属的测量")
        target = next((u for u in self.users if u.name == user or u.id == user), None)
        if target is None:
            raise ServiceValidationError(f"没有名为 {user} 的用户")
        if lm["user_id"] == target.id:
            return
        # 已经手动归属了，就不用再问"要不要新建用户"
        self._abort_discovery()

        # 撤销原归属：恢复该用户的参考体重和实体值
        old_id = lm["user_id"]
        if old_id is not None:
            if lm["prev_ref"] is None:
                self.refs.pop(old_id, None)
            else:
                self.refs[old_id] = Reference.from_dict(lm["prev_ref"])
            for key, val in lm["prev_values"].items():
                if (ent := self.user_entities.get((old_id, key))) is not None:
                    ent.async_update_state(val)

        self._record(
            MatchResult(target.id, CONFIDENCE_MANUAL, "手动归属"),
            lm["weight"], lm["impedance"], lm["now_dt"],
            confidence=CONFIDENCE_MANUAL, reassigned=True,
        )

    def _idle_timeout(self) -> None:
        """连续一段时间无新数据时，把"测量中"置 False。"""
        self._idle_handle = None
        if time.monotonic() - self._last_data_at >= MEASURING_IDLE_TIMEOUT:
            self._set_measuring(False)
            self._committed_weight = None

    def _end_measuring(self) -> None:
        """连接已断开，不会再有数据：立刻结束"测量中"。"""
        if self._idle_handle is not None:
            self._idle_handle.cancel()
            self._idle_handle = None
        self._set_measuring(False)
        self._committed_weight = None

    @staticmethod
    def _compute_bia(
        weight_kg: float, impedance: float, profile: UserProfile
    ) -> dict[str, float]:
        """BIA 身体指标计算（按用户资料）"""
        if profile.height_cm <= 0:
            return {}
        male, age = profile.male, profile.age
        height_m = profile.height_cm / 100.0
        bmi = weight_kg / (height_m * height_m)

        if impedance > 0:
            if male:
                fat = 0.18 * bmi + 0.012 * age + 0.018 * impedance - 3.2
            else:
                fat = 0.26 * bmi + 0.011 * age + 0.020 * impedance - 2.5
        else:
            sex_offset = -10.8 if male else 0.0
            fat = 1.20 * bmi + 0.23 * age + sex_offset - 5.4
        fat = max(5.0, min(55.0, fat))

        water = max(35.0, min(75.0, 69.7 - fat * 0.55))
        bone = max(1.5, min(5.5, weight_kg * (0.047 if male else 0.040)))
        protein = max(10.0, min(24.0, 16.0 + (water - 50.0) * 0.12))
        muscle = max(0.0, weight_kg * (1.0 - fat / 100.0) - bone)

        return {
            "bmi": bmi,
            "body_fat": fat,
            "water": water,
            "muscle": muscle,
            "protein": protein,
            "bone": bone,
        }
