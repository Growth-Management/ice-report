import unittest
from decimal import Decimal

import ad_revenue_work_summaries as ws


class RoundYenTests(unittest.TestCase):
    def test_rounds_half_away_from_zero_like_excel(self):
        # Python's/Decimal's default rounding is half-to-even, which would
        # give 2 here -- Excel's ROUND() always rounds .5 up (away from
        # zero), giving 3.
        self.assertEqual(ws.round_yen(Decimal("2.5")), 3)
        self.assertEqual(ws.round_yen(Decimal("3.5")), 4)

    def test_matches_real_app2_2026_08_golden_master_numerator(self):
        # Real official APP_2 template's 広告単価(50%原資) formula numerator:
        # ROUND(9789547 * 0.5, 0) -- confirmed against the live Excel array
        # formula extracted from the official template.
        self.assertEqual(ws.round_yen(Decimal(9789547) * Decimal("0.5")), 4894774)


class UnitPriceTests(unittest.TestCase):
    def test_matches_real_app2_formula(self):
        # =IFERROR(SUM(ROUND(広告売上[広告売上]*広告単価[[#This Row],[割合]],0))
        #   /広告表示数[[#Totals],[広告表示数]],"-") from the real official
        # APP_2 template, ratio=1 for 全体広告単価.
        price = ws.unit_price(9789547, 59310824)
        self.assertEqual(price, Decimal(9789547) / Decimal(59310824))

    def test_ratio_50_percent_matches_golden_master(self):
        price_50 = ws.unit_price(9789547, 59310824, ratio=Decimal("0.5"))
        self.assertEqual(price_50, Decimal(4894774) / Decimal(59310824))

    def test_zero_views_is_zero_not_a_crash(self):
        self.assertEqual(ws.unit_price(100, 0), Decimal(0))


class BuildApp2WorkSummaryTests(unittest.TestCase):
    def test_groups_by_work_title_title_id_digital_title_name(self):
        rows = [
            {"作品名": "A", "タイトルID": 1, "デジタルタイトル名": "da", "広告売上": Decimal("10")},
            {"作品名": "A", "タイトルID": 1, "デジタルタイトル名": "da", "広告売上": Decimal("5")},
            {"作品名": "B", "タイトルID": 2, "デジタルタイトル名": "db", "広告売上": Decimal("30")},
        ]
        result = ws.build_app2_work_summary(rows)
        self.assertEqual(len(result), 2)
        by_name = {r["作品名"]: r for r in result}
        self.assertEqual(by_name["A"]["広告売上"], Decimal("15"))
        self.assertEqual(by_name["B"]["広告売上"], Decimal("30"))

    def test_sorted_descending_by_ad_revenue(self):
        rows = [
            {"作品名": "small", "タイトルID": 1, "デジタルタイトル名": "d1", "広告売上": Decimal("1")},
            {"作品名": "big", "タイトルID": 2, "デジタルタイトル名": "d2", "広告売上": Decimal("100")},
        ]
        result = ws.build_app2_work_summary(rows)
        self.assertEqual([r["作品名"] for r in result], ["big", "small"])

    def test_tiebreak_is_deterministic_when_values_equal(self):
        rows = [
            {"作品名": "zeta", "タイトルID": 1, "デジタルタイトル名": "d", "広告売上": Decimal("10")},
            {"作品名": "alpha", "タイトルID": 2, "デジタルタイトル名": "d", "広告売上": Decimal("10")},
        ]
        result = ws.build_app2_work_summary(rows)
        # equal 広告売上 -> ascending 作品名 tiebreak
        self.assertEqual([r["作品名"] for r in result], ["alpha", "zeta"])

    def test_none_title_id_does_not_crash_sort(self):
        rows = [
            {"作品名": "A", "タイトルID": None, "デジタルタイトル名": None, "広告売上": Decimal("1")},
            {"作品名": "B", "タイトルID": 2, "デジタルタイトル名": "d", "広告売上": Decimal("1")},
        ]
        result = ws.build_app2_work_summary(rows)
        self.assertEqual(len(result), 2)


class BuildApp2WorkSummary50Tests(unittest.TestCase):
    def test_groups_by_work_id_and_work_title(self):
        rows = [
            {"作品ID": 1, "作品名": "A", "広告売上_原資50": Decimal("4")},
            {"作品ID": 1, "作品名": "A", "広告売上_原資50": Decimal("6")},
            {"作品ID": 2, "作品名": "B", "広告売上_原資50": Decimal("2")},
        ]
        result = ws.build_app2_work_summary_50(rows)
        self.assertEqual(len(result), 2)
        by_id = {r["作品ID"]: r for r in result}
        self.assertEqual(by_id[1]["広告売上_原資50"], Decimal("10"))
        self.assertEqual(by_id[2]["広告売上_原資50"], Decimal("2"))

    def test_sorted_descending_by_value(self):
        rows = [
            {"作品ID": 1, "作品名": "small", "広告売上_原資50": Decimal("1")},
            {"作品ID": 2, "作品名": "big", "広告売上_原資50": Decimal("100")},
        ]
        result = ws.build_app2_work_summary_50(rows)
        self.assertEqual([r["作品ID"] for r in result], [2, 1])


class BuildWebWorkSummaryTests(unittest.TestCase):
    def test_same_shape_as_app2_work_summary(self):
        rows = [
            {"作品名": "A", "タイトルID": 1, "デジタルタイトル名": "da", "広告売上": Decimal("10")},
            {"作品名": "A", "タイトルID": 1, "デジタルタイトル名": "da", "広告売上": Decimal("5")},
        ]
        result = ws.build_web_work_summary(rows)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["広告売上"], Decimal("15"))


class BuildVideoRewardWorkSummaryTests(unittest.TestCase):
    def test_groups_by_work_title_and_computes_share_and_author_amount(self):
        rows = [
            {"作品名": "A", "コイン消費数": 300},
            {"作品名": "A", "コイン消費数": 200},
            {"作品名": "B", "コイン消費数": 500},
        ]
        result = ws.build_video_reward_work_summary(rows, revenue_yen=14017945)
        self.assertEqual(len(result), 2)
        by_name = {r["作品名"]: r for r in result}
        self.assertEqual(by_name["A"]["コイン消費数"], 500)
        self.assertEqual(by_name["B"]["コイン消費数"], 500)
        # total coin = 1000, author_amount = ROUND(14017945*0.15,0) = 2102692
        expected_author_amount = ws.round_yen(Decimal(14017945) * Decimal("0.15"))
        self.assertEqual(expected_author_amount, 2102692)
        self.assertEqual(by_name["A"]["コイン消費割合"], Decimal(500) / Decimal(1000))
        self.assertEqual(by_name["A"]["広告還元額"], Decimal(2102692) * (Decimal(500) / Decimal(1000)))

    def test_shares_sum_to_one(self):
        rows = [{"作品名": f"work-{i}", "コイン消費数": i} for i in range(1, 6)]
        result = ws.build_video_reward_work_summary(rows, revenue_yen=1000000)
        total_share = sum((r["コイン消費割合"] for r in result), Decimal(0))
        self.assertEqual(total_share, Decimal(1))

    def test_sorted_descending_by_coin_count(self):
        rows = [
            {"作品名": "small", "コイン消費数": 1},
            {"作品名": "big", "コイン消費数": 100},
        ]
        result = ws.build_video_reward_work_summary(rows, revenue_yen=1000)
        self.assertEqual([r["作品名"] for r in result], ["big", "small"])


if __name__ == "__main__":
    unittest.main()
