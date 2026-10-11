"""matcher.py 的离线单元测试（不需要 Home Assistant）。

运行: python3 -I -m unittest discover -s tests -v   （在 ha-afu-test 目录下）
"""

import importlib.util
import pathlib
import unittest

_p = (
    pathlib.Path(__file__).resolve().parent.parent
    / "custom_components/afu_scale/matcher.py"
)
_spec = importlib.util.spec_from_file_location("afu_matcher", _p)
m = importlib.util.module_from_spec(_spec)
import sys

sys.modules["afu_matcher"] = m  # dataclass 需要模块在 sys.modules 里
_spec.loader.exec_module(m)

DAY = 86400.0
NOW = 1_800_000_000.0


def user(uid, tol=None):
    return m.UserProfile(uid, uid, 175, "male", 30, tolerance_kg=tol)


def ref(w, imp=None, ts=NOW):
    return m.Reference(
        weights=(w,), impedances=(imp,) if imp is not None else (), last_ts=ts
    )


class MatchTests(unittest.TestCase):
    def test_no_users_is_guest(self):
        r = m.match(70, 500, [], {}, NOW)
        self.assertIsNone(r.user_id)
        self.assertEqual(r.confidence, m.GUEST)

    def test_single_user_far_off_is_guest(self):
        # 只有一个用户时，陌生人也不能算成他
        r = m.match(120, 500, [user("a")], {"a": ref(60)}, NOW)
        self.assertIsNone(r.user_id)
        self.assertEqual(r.confidence, m.GUEST)

    def test_single_user_in_range_matches(self):
        r = m.match(61.0, 500, [user("a")], {"a": ref(60)}, NOW)
        self.assertEqual((r.user_id, r.confidence), ("a", m.HIGH))

    def test_user_without_reference_never_matches(self):
        # 没有参考体重就没法比较：访客，由发现卡片决定归给谁
        for refs in ({}, {"a": m.Reference()}):
            r = m.match(70, 500, [user("a")], refs, NOW)
            self.assertIsNone(r.user_id)
            self.assertEqual(r.confidence, m.GUEST)

    def test_single_user_with_initial_weight_rejects_stranger_immediately(self):
        refs = {"a": m.seed_reference(70, NOW)}
        self.assertEqual(m.match(70.4, 500, [user("a")], refs, NOW).user_id, "a")
        self.assertIsNone(m.match(55, 600, [user("a")], refs, NOW).user_id)

    def test_unique_hit(self):
        us = [user("a"), user("b")]
        refs = {"a": ref(60), "b": ref(80)}
        r = m.match(61.2, 500, us, refs, NOW)
        self.assertEqual((r.user_id, r.confidence), ("a", m.HIGH))
        r = m.match(79.0, 500, us, refs, NOW)
        self.assertEqual(r.user_id, "b")

    def test_nobody_in_range_is_guest(self):
        us = [user("a"), user("b")]
        refs = {"a": ref(60), "b": ref(80)}
        r = m.match(70, 500, us, refs, NOW)
        self.assertIsNone(r.user_id)
        self.assertEqual(r.confidence, m.GUEST)

    def test_tolerance_is_relative_for_heavy_people(self):
        # 100 kg 的 3% = 3 kg，大于绝对下限 2 kg
        us = [user("a"), user("b")]
        refs = {"a": ref(100), "b": ref(50)}
        self.assertEqual(m.match(102.9, 500, us, refs, NOW).user_id, "a")
        self.assertIsNone(m.match(103.5, 500, us, refs, NOW).user_id)

    def test_custom_tolerance_overrides_default(self):
        us = [user("a", tol=0.5), user("b")]
        refs = {"a": ref(60), "b": ref(80)}
        self.assertIsNone(m.match(61.0, 500, us, refs, NOW).user_id)

    def test_tolerance_widens_with_time_and_is_capped(self):
        u, r = user("a"), ref(60, ts=NOW)
        base = m.tolerance_kg(u, r, NOW)
        self.assertAlmostEqual(base, 2.0)
        self.assertAlmostEqual(m.tolerance_kg(u, r, NOW + 10 * DAY), 3.0)
        # 封顶：再久也只加 3 kg
        self.assertAlmostEqual(m.tolerance_kg(u, r, NOW + 400 * DAY), 5.0)

    def test_drift_lets_old_reference_still_match(self):
        us = [user("a"), user("b")]
        refs = {"a": ref(60, ts=NOW - 30 * DAY), "b": ref(90)}
        # 30 天没称，容差 2+3=5，63.5 仍算 a
        self.assertEqual(m.match(63.5, 500, us, refs, NOW).user_id, "a")
        # 刚称过的话 63.5 就不算
        refs = {"a": ref(60, ts=NOW), "b": ref(90)}
        self.assertIsNone(m.match(63.5, 500, us, refs, NOW).user_id)

    def test_overlap_resolved_by_impedance(self):
        us = [user("a"), user("b")]
        refs = {"a": ref(70, 480), "b": ref(71, 620)}
        r = m.match(70.6, 625, us, refs, NOW)  # 体重两人都命中，阻抗像 b
        self.assertEqual((r.user_id, r.confidence), ("b", m.MEDIUM))
        r = m.match(70.6, 470, us, refs, NOW)
        self.assertEqual((r.user_id, r.confidence), ("a", m.MEDIUM))

    def test_overlap_impedance_too_close_falls_back_to_nearest_weight(self):
        us = [user("a"), user("b")]
        refs = {"a": ref(70, 500), "b": ref(71, 510)}
        r = m.match(70.8, 505, us, refs, NOW)  # 阻抗差不到 margin
        self.assertEqual((r.user_id, r.confidence), ("b", m.LOW))

    def test_overlap_without_impedance_reference_is_low(self):
        us = [user("a"), user("b")]
        refs = {"a": ref(70), "b": ref(71)}
        r = m.match(70.2, 500, us, refs, NOW)
        self.assertEqual((r.user_id, r.confidence), ("a", m.LOW))

    def test_overlap_tie_is_deterministic(self):
        us = [user("b"), user("a")]
        refs = {"a": ref(70), "b": ref(72)}
        r = m.match(71.0, 500, us, refs, NOW)  # 与两人等距
        self.assertEqual((r.user_id, r.confidence), ("a", m.LOW))

    def test_user_without_reference_is_skipped(self):
        us = [user("a"), user("b")]
        refs = {"a": ref(60)}  # b 还没有任何参考
        self.assertEqual(m.match(60.5, 500, us, refs, NOW).user_id, "a")
        self.assertIsNone(m.match(80, 500, us, refs, NOW).user_id)

    def test_match_does_not_mutate_state(self):
        us = [user("a"), user("b")]
        refs = {"a": ref(60), "b": ref(80)}
        before = dict(refs)
        m.match(60.5, 500, us, refs, NOW)
        self.assertEqual(refs, before)


class ReferenceTests(unittest.TestCase):
    def test_update_keeps_only_recent_history(self):
        r = None
        for i in range(8):
            r = m.update_reference(r, 60 + i, 500 + i, NOW + i)
        self.assertEqual(len(r.weights), m.REF_HISTORY)
        self.assertEqual(r.weights[-1], 67)
        self.assertEqual(r.last_ts, NOW + 7)

    def test_reference_is_median_so_outlier_is_tolerated(self):
        r = m.Reference(weights=(60, 60.4, 59.8, 75, 60.2), last_ts=NOW)
        self.assertAlmostEqual(r.weight, 60.2)

    def test_update_does_not_mutate_original(self):
        r0 = ref(60)
        r1 = m.update_reference(r0, 61, 500, NOW + 1)
        self.assertEqual(r0.weights, (60,))
        self.assertEqual(r1.weights, (60, 61))

    def test_zero_impedance_not_recorded(self):
        r = m.update_reference(ref(60, 500), 61, 0, NOW + 1)
        self.assertEqual(r.impedances, (500,))

    def test_roundtrip_dict(self):
        r = m.update_reference(ref(60, 500), 61, 510, NOW + 1)
        self.assertEqual(m.Reference.from_dict(r.to_dict()), r)

    def test_seed(self):
        r = m.seed_reference(65, NOW)
        self.assertEqual((r.weight, r.impedance), (65, None))


class ProfileTests(unittest.TestCase):
    def test_from_dict_handles_optional_fields(self):
        p = m.UserProfile.from_dict(
            {"id": "x", "name": "小明", "height_cm": 180, "sex": "female", "age": 20}
        )
        self.assertIsNone(p.tolerance_kg)
        self.assertIsNone(p.initial_weight)
        self.assertFalse(p.male)
        p = m.UserProfile.from_dict(
            {"id": "x", "name": "n", "height_cm": 170, "sex": "male", "age": 9,
             "tolerance_kg": 1.5, "initial_weight": 30}
        )
        self.assertEqual((p.tolerance_kg, p.initial_weight, p.male), (1.5, 30.0, True))


if __name__ == "__main__":
    unittest.main()
