# /// script
# requires-python = ">=3.11"
# dependencies = ["pandas==2.3.3", "matplotlib==3.10.8", "requests==2.32.5", "jinja2==3.1.6"]
# ///
"""Run with: uv run tests/test_analysis.py"""
import csv
import io
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build


class AnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = build.RAW.read_bytes()
        cls.wide, cls.info = build.parse_csv(cls.raw)
        cls.result = build.aggregate(cls.wide)

    def mutate(self, operation):
        rows = list(csv.reader(io.StringIO(self.raw.decode("cp932"))))
        header = next(i for i, row in enumerate(rows) if row and row[0] == "cat01_code")
        operation(rows, header)
        text = io.StringIO()
        csv.writer(text).writerows(rows)
        return text.getvalue().encode("cp932")

    def test_small_capital_is_ratio_of_sums(self):
        for row in self.result[self.result.capital_size == "small"].itertuples():
            source = self.wide[(self.wide.fiscal_year == row.fiscal_year) & (self.wide.sector_code == row.sector_code) & self.wide.capital_code.isin(["19", "16"])]
            expected = 100 * sum(source.ordinary_profit_million_yen) / sum(source.sales_million_yen)
            self.assertAlmostEqual(row.ordinary_margin_pct, expected, places=12)

    def test_latest_matches_mof_release(self):
        # 独立照合: 財務省 r7.pdf p.5「経常利益の推移」（億円・四捨五入）。
        # 原データ更新で期間外になった場合は、当該年度のチェックのみ除外する。
        reference = {"104": 1248313, "108": 433260, "144": 815053}
        for sector, expected in reference.items():
            actual = self.wide[(self.wide.fiscal_year == 2025) & (self.wide.sector_code == sector) & (self.wide.capital_code == "26")]
            if not actual.empty:
                self.assertEqual(round(actual.iloc[0].ordinary_profit_million_yen / 100), expected)

    def test_missing_value_is_rejected(self):
        raw = self.mutate(lambda rows, h: rows[h + 1].__setitem__(9, "*"))
        with self.assertRaises(ValueError):
            build.parse_csv(raw)

    def test_duplicate_or_incomplete_data_is_rejected(self):
        raw = self.mutate(lambda rows, h: rows.__setitem__(h + 2, rows[h + 1].copy()))
        with self.assertRaisesRegex(ValueError, "重複"):
            build.parse_csv(raw)

    def test_zero_sales_is_rejected(self):
        raw = self.mutate(lambda rows, h: rows[h + 1].__setitem__(9, "0"))
        with self.assertRaisesRegex(ValueError, "売上高"):
            build.parse_csv(raw)

    def test_reported_ratio_mismatch_is_rejected(self):
        def corrupt(rows, h):
            row = next(row for row in rows[h + 1:] if row[0] == "127")
            row[9] = "99.9"
        with self.assertRaisesRegex(ValueError, "照合不一致"):
            build.parse_csv(self.mutate(corrupt))


if __name__ == "__main__":
    unittest.main()
