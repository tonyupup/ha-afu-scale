"""多用户匹配（纯 Python，不依赖 Home Assistant，便于离线测试）。

思路：每个用户保留最近几次稳定读数作为"参考"。新读数的体重落在某个用户参考体重的
容差范围内，就认为是这个人。

  - 容差默认 max(2 kg, 3%)，并随距上次测量的天数变宽（人会慢慢变胖变瘦、孩子会长个），封顶 3 kg
  - 体重同时命中多人时，用阻抗做第二特征（不同人的阻抗通常差别明显），要比次优近出一截才算分得清
  - 阻抗也分不出：取体重最近的，并标记低置信度
  - 没人命中：当访客，不更新任何人的参考，避免污染。没配置任何用户、或用户还没有
    参考体重时也是访客——由"发现"卡片让用户决定新建用户，还是归给现有用户
  - 单个用户也一样按范围判断，陌生人不会被算成他

阻抗受脚湿、站姿影响，区分度需要真实数据验证，所以只在体重打平时当兜底。
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Any

REF_HISTORY = 5  # 每人保留最近几次读数
TOLERANCE_ABS_KG = 2.0
TOLERANCE_REL = 0.03
DRIFT_KG_PER_DAY = 0.1
DRIFT_MAX_KG = 3.0
IMPEDANCE_MARGIN = 30.0  # 阻抗要比次优近这么多（Ω），才算"分得清"

HIGH = "high"
MEDIUM = "medium"
LOW = "low"
GUEST = "guest"


@dataclass(frozen=True)
class UserProfile:
    id: str
    name: str
    height_cm: float
    sex: str
    age: int
    tolerance_kg: float | None = None  # None = 自动
    initial_weight: float | None = None

    @property
    def male(self) -> bool:
        return self.sex == "male"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> UserProfile:
        return cls(
            id=str(d["id"]),
            name=str(d["name"]),
            height_cm=float(d["height_cm"]),
            sex=str(d["sex"]),
            age=int(d["age"]),
            tolerance_kg=(float(d["tolerance_kg"]) if d.get("tolerance_kg") else None),
            initial_weight=(
                float(d["initial_weight"]) if d.get("initial_weight") else None
            ),
        )


@dataclass(frozen=True)
class Reference:
    """一个用户最近几次稳定读数。"""

    weights: tuple[float, ...] = ()
    impedances: tuple[float, ...] = ()
    last_ts: float | None = None  # epoch 秒

    @property
    def weight(self) -> float | None:
        return median(self.weights) if self.weights else None

    @property
    def impedance(self) -> float | None:
        return median(self.impedances) if self.impedances else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "weights": list(self.weights),
            "impedances": list(self.impedances),
            "last_ts": self.last_ts,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Reference:
        return cls(
            weights=tuple(float(x) for x in d.get("weights", [])),
            impedances=tuple(float(x) for x in d.get("impedances", [])),
            last_ts=d.get("last_ts"),
        )


@dataclass(frozen=True)
class MatchResult:
    user_id: str | None  # None = 访客
    confidence: str
    reason: str = ""


def seed_reference(initial_weight: float, now: float) -> Reference:
    """用户创建时填了当前体重，作为匹配的初始参考。"""
    return Reference(weights=(initial_weight,), impedances=(), last_ts=now)


def tolerance_kg(profile: UserProfile, ref: Reference, now: float) -> float:
    """当前对这个用户的体重容差：基础容差 + 随时间变宽的漂移量。"""
    rw = ref.weight or 0.0
    base = profile.tolerance_kg or max(TOLERANCE_ABS_KG, TOLERANCE_REL * rw)
    days = max(0.0, (now - ref.last_ts) / 86400.0) if ref.last_ts else 0.0
    return base + min(DRIFT_KG_PER_DAY * days, DRIFT_MAX_KG)


def match(
    weight: float,
    impedance: float,
    users: list[UserProfile],
    refs: dict[str, Reference],
    now: float,
) -> MatchResult:
    """判断这次读数是谁的。不修改任何状态。"""
    if not users:
        return MatchResult(None, GUEST, "没有配置用户")

    # (用户, 参考, 体重差)
    cands: list[tuple[UserProfile, Reference, float]] = []
    for u in users:
        ref = refs.get(u.id)
        if ref is None or ref.weight is None:
            continue  # 还没有参考体重的用户无法按体重匹配
        diff = abs(weight - ref.weight)
        if diff <= tolerance_kg(u, ref, now):
            cands.append((u, ref, diff))

    if not cands:
        return MatchResult(None, GUEST, "体重不在任何人的范围内")
    if len(cands) == 1:
        return MatchResult(cands[0][0].id, HIGH, "体重唯一命中")

    # 体重命中多人：用阻抗打破平局（要求所有候选都有阻抗参考）
    if impedance > 0 and all(ref.impedance is not None for _, ref, _ in cands):
        scored = sorted(
            ((abs(impedance - ref.impedance), diff, u.id) for u, ref, diff in cands)
        )
        if scored[1][0] - scored[0][0] >= IMPEDANCE_MARGIN:
            return MatchResult(scored[0][2], MEDIUM, "体重命中多人，阻抗分出")

    # 分不出：取体重最近的（并列时按 id 稳定排序）
    best = min(cands, key=lambda c: (c[2], c[0].id))
    return MatchResult(best[0].id, LOW, "体重命中多人且阻抗分不出，取体重最近")


def update_reference(
    ref: Reference | None, weight: float, impedance: float, now: float
) -> Reference:
    """把一次读数记入参考，返回新的 Reference（只保留最近 REF_HISTORY 次）。"""
    ref = ref or Reference()
    weights = (*ref.weights, weight)[-REF_HISTORY:]
    imps = (*ref.impedances, impedance)[-REF_HISTORY:] if impedance > 0 else ref.impedances
    return Reference(weights=weights, impedances=imps, last_ts=now)
