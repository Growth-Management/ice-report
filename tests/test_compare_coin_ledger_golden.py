"""Targeted tests for scripts/compare_coin_ledger_golden.py's exact-key
2026-08 comic-type allowlist (legacy_golden_only_comic_type_2026_08).

This is a narrow allowlist, not a general "種別 differs -> accepted" rule:
it must only ever accept the exact two 2026-08 work names, never a different
work, never a different target_month, and any third mismatching key must
still surface as unexpected.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import compare_coin_ledger_golden as cmp  # noqa: E402

LEGACY_KEYS = sorted(cmp.LEGACY_GOLDEN_ONLY_COMIC_TYPE_2026_08_KEYS)


def _summary_sheet_result(mismatched_keys: list[str], *, extra_columns: dict | None = None) -> dict:
    columns = {
        "種別": {
            "mismatch_count": len(mismatched_keys),
            "value_types": {"generated": {"empty": len(mismatched_keys)}, "golden": {"str": len(mismatched_keys)}},
            "_mismatched_keys": list(mismatched_keys),
        }
    }
    if extra_columns:
        columns.update(extra_columns)
    return {
        "headers_equal": True,
        "only_in_generated": 0,
        "only_in_golden": 0,
        "columns": columns,
        "row_order_equal": True,
    }


class LegacyGoldenOnlyComicType2026_08Tests(unittest.TestCase):
    def test_case_a_exact_2026_08_works_are_accepted(self):
        result = _summary_sheet_result(LEGACY_KEYS)
        accepted, unexpected = cmp._classify_keyed(
            "app/サマリ", result, product=False, order_groups={}, target_month="2026-08"
        )
        self.assertEqual(unexpected, [])
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["accepted_reason_code"], "legacy_golden_only_comic_type_2026_08")
        self.assertEqual(accepted[0]["actual_diff_count"], 2)

    def test_case_b_a_third_key_in_2026_08_stays_unexpected(self):
        result = _summary_sheet_result(LEGACY_KEYS + ["キン肉マン"])
        accepted, unexpected = cmp._classify_keyed(
            "app/サマリ", result, product=False, order_groups={}, target_month="2026-08"
        )
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["accepted_reason_code"], "legacy_golden_only_comic_type_2026_08")
        self.assertEqual(accepted[0]["actual_diff_count"], 2)
        self.assertEqual(len(unexpected), 1)
        self.assertNotIn("accepted_reason_code", unexpected[0])
        self.assertEqual(unexpected[0]["actual_diff_count"], 1)

    def test_case_b_a_completely_different_work_alone_is_unexpected(self):
        result = _summary_sheet_result(["キン肉マン"])
        accepted, unexpected = cmp._classify_keyed(
            "app/サマリ", result, product=False, order_groups={}, target_month="2026-08"
        )
        self.assertEqual(accepted, [])
        self.assertEqual(len(unexpected), 1)
        self.assertEqual(unexpected[0]["actual_diff_count"], 1)

    def test_case_c_same_two_works_different_target_month_is_unexpected(self):
        result = _summary_sheet_result(LEGACY_KEYS)
        accepted, unexpected = cmp._classify_keyed(
            "app/サマリ", result, product=False, order_groups={}, target_month="2026-09"
        )
        self.assertEqual(accepted, [])
        self.assertEqual(len(unexpected), 1)
        self.assertEqual(unexpected[0]["actual_diff_count"], 2)

    def test_case_c_same_two_works_no_target_month_given_is_unexpected(self):
        result = _summary_sheet_result(LEGACY_KEYS)
        accepted, unexpected = cmp._classify_keyed("app/サマリ", result, product=False, order_groups={})
        self.assertEqual(accepted, [])
        self.assertEqual(len(unexpected), 1)
        self.assertEqual(unexpected[0]["actual_diff_count"], 2)

    def test_case_d_matching_type_set_is_clean_pass(self):
        result = _summary_sheet_result([])
        accepted, unexpected = cmp._classify_keyed(
            "app/サマリ", result, product=False, order_groups={}, target_month="2026-08"
        )
        self.assertEqual(accepted, [])
        self.assertEqual(unexpected, [])

    def test_allowlist_not_applied_outside_app_summary_area(self):
        # Same exact keys/mismatch, but a different area (e.g. a detail
        # sheet's own '種別' column, or a different area string) must not
        # accidentally match the allowlist -- it is scoped to app/サマリ only.
        result = _summary_sheet_result(LEGACY_KEYS)
        accepted, unexpected = cmp._classify_keyed(
            "app/有料話消費コイン（Apple）", result, product=False, order_groups={}, target_month="2026-08"
        )
        self.assertEqual(accepted, [])
        self.assertEqual(len(unexpected), 1)
        self.assertEqual(unexpected[0]["actual_diff_count"], 2)


if __name__ == "__main__":
    unittest.main()
