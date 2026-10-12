"""AFU 体脂秤集成常量"""

DOMAIN = "afu_scale"

CONF_HEIGHT_CM = "height_cm"
CONF_SEX = "sex"
CONF_AGE = "age"

DEFAULT_HEIGHT_CM = 175
DEFAULT_SEX = "male"
DEFAULT_AGE = 29

# GATT 服务 / 特征
SERVICE_UUID = "0000ffb0-0000-1000-8000-00805f9b34fb"
NOTIFY_CHAR_UUID = "0000ffb2-0000-1000-8000-00805f9b34fb"
WRITE_CHAR_UUID = "0000ffb1-0000-1000-8000-00805f9b34fb"

# 报文魔数 / 稳定标志
PACKET_MAGIC = 0xAC
STABLE_FLAG = 0x80

# 握手包（某些固件需要写入后才推送完整数据）
HANDSHAKE = bytes([0xFD, 0x37, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x37])

# 连接参数
# 秤只在被唤醒后的几十秒内广播（可连接），所以连接尝试要短、失败要快，
# 失败后不做定时重连，等下一次广播再触发。
CONNECT_TIMEOUT = 10
CONNECT_ATTEMPTS = 2
LIVE_MIN_DELTA_KG = 0.05  # 实时体重至少变化这么多才写实体
LIVE_MIN_INTERVAL = 0.5  # 秒：实时体重写实体的最小间隔
RESULT_WAIT = 45  # 秒：定格后等结果帧（带阻抗）的最长时间，实测 8~25 秒才到
IBEACON_MARK = bytes.fromhex("4c000215")  # Apple iBeacon：秤醒着时的广播
PLAIN_ADV_PREFIX = bytes.fromhex("020106")  # 空闲时的普通可连接广播（flags 开头）
AWAKE_HOLD = 15  # 秒：见到醒着的广播后，这么久内都当作醒着
POLL_INTERVAL = 1  # 秒：轮询间隔
POLL_ADV_MAX_AGE = 10  # 秒：最近广播多新才算
RETRY_COOLDOWN = 5  # 会话结束后，多久内忽略新的广播（秒）
NO_DATA_IDLE_TIMEOUT = 30  # 连上后一个通知都没收到，多久主动断开（秒）
QUIET_MAX = 75  # 空闲断开后最长静默多久（秒）；实测秤无连接后 41~74 秒内睡着
SESSION_IDLE_TIMEOUT = 60  # 连上后无任何通知多久主动断开，释放连接槽（秒）
MEASURING_IDLE_TIMEOUT = 15  # 无有效数据多久把"测量中"置 False（秒）

# ---- 多用户 ----------------------------------------------------------
CONF_USERS = "users"  # options 里的用户列表
CONF_USER_ID = "id"
CONF_INITIAL_WEIGHT = "initial_weight"  # 可选：新用户的当前体重，用作匹配的初始参考
CONF_TOLERANCE_KG = "tolerance_kg"  # 可选：覆盖默认的体重匹配容差
CONF_DEBUG = "debug"  # options：开启后才允许 afu_scale.inject_packet
MAX_USERS = 8

# 同一次会话里，体重与上次提交相差多少 kg 以上才视为"换了个人"，允许再次提交
RESET_DELTA_KG_DEFAULT = 3.0

# 可在选项里调的时间参数：键 -> (默认值, 最小, 最大, 步长)；默认值取自上面的常量
CONF_QUIET_MAX = "quiet_max"
CONF_NO_DATA_TIMEOUT = "no_data_timeout"
CONF_SESSION_IDLE_TIMEOUT = "session_idle_timeout"
CONF_RESULT_WAIT = "result_wait"
CONF_RESET_DELTA_KG = "reset_delta_kg"
TUNABLES = {
    CONF_QUIET_MAX: (QUIET_MAX, 0, 300, 1),
    CONF_NO_DATA_TIMEOUT: (NO_DATA_IDLE_TIMEOUT, 5, 300, 1),
    CONF_SESSION_IDLE_TIMEOUT: (SESSION_IDLE_TIMEOUT, 10, 600, 1),
    CONF_RESULT_WAIT: (RESULT_WAIT, 10, 120, 1),
    CONF_RESET_DELTA_KG: (RESET_DELTA_KG_DEFAULT, 0.5, 30, 0.5),
}


# 发现流程创建新用户后，把那次访客读数交给新协调器采纳（用户列表变更会触发重载）
PENDING_KEY = f"{DOMAIN}_pending"

EVENT_MEASUREMENT = "afu_scale_measurement"
SERVICE_ASSIGN = "assign_last_measurement"
SERVICE_INJECT = "inject_packet"

STORAGE_VERSION = 1

# 每个用户各自一份的传感器（设备级"最近一次测量"那套另有 battery/stable 等）
USER_SENSOR_KEYS = (
    "weight",
    "impedance",
    "bmi",
    "body_fat",
    "water",
    "muscle",
    "protein",
    "bone",
    "timestamp",
)

# 匹配置信度（confidence 传感器的取值）
CONFIDENCE_HIGH = "high"  # 体重只命中一个人，或只配置了一个用户
CONFIDENCE_MEDIUM = "medium"  # 体重命中多人，靠阻抗分出
CONFIDENCE_LOW = "low"  # 体重命中多人且阻抗分不出，取体重最近的
CONFIDENCE_GUEST = "guest"  # 没人命中，当访客
CONFIDENCE_MANUAL = "manual"  # 通过 assign_last_measurement 手动归属
CONFIDENCE_OPTIONS = [
    CONFIDENCE_HIGH,
    CONFIDENCE_MEDIUM,
    CONFIDENCE_LOW,
    CONFIDENCE_GUEST,
    CONFIDENCE_MANUAL,
]
