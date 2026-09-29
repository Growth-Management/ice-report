"""Builds the --order-groups JSON for scripts/compare_coin_ledger_golden.py.

The generated/Golden detail sheets have no name_kana column (only 作品名),
so the sort-key group a コンテンツID_Raise belongs to (for validating that a
row-order diff is confined to name_kana ties, see docs/jumpplus-coin-ledger-report.md
「明細のGolden行順」) has to come from the same BigQuery content source the
report itself reads. Read-only: one SELECT on the content table for the
given target month, no writes anywhere. The same prefixed_id -> name_kana
mapping applies to every App/WEB detail sheet (content_id is disjoint per
app_id/purchase_type, but not reused across sheets), so one JSON section per
sheet name is emitted, all pointing at the same underlying mapping.

Usage:
  python scripts/build_coin_ledger_order_groups.py --target-month 2026-08 \
    --output out/order_groups.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from google.cloud import bigquery

import jumpplus_coin_ledger_report as report
import jumpplus_coin_ledger_workbooks as workbooks

APP_DETAIL_SHEETS = (
    "有料話消費コイン（Apple）",
    "有料話消費コイン（Google）",
    "有料巻消費コイン（Apple）",
    "有料巻消費コイン（Google）",
)
WEB_DETAIL_SHEETS = ("有料話消費ポイント", "有料巻消費ポイント")


def build(*, project_id: str, target_month) -> dict:
    client = bigquery.Client(project=project_id)
    content = report.fetch_content_rows(client=client, target_month=target_month)
    kana_by_prefixed: dict[str, str] = {}
    for row in content:
        pid = row.get("prefixed_id")
        if pid:
            kana_by_prefixed[str(pid)] = workbooks._kana(row)
    return {
        "app": {sheet: kana_by_prefixed for sheet in APP_DETAIL_SHEETS},
        "web": {sheet: kana_by_prefixed for sheet in WEB_DETAIL_SHEETS},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--target-month", required=True, help="YYYY-MM or YYYY-MM-DD")
    parser.add_argument("--project-id", default="jumpplus-4a5f4")
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    text = args.target_month if len(args.target_month) > 7 else f"{args.target_month}-01"
    target_month = datetime.strptime(text, "%Y-%m-%d").date()

    groups = build(project_id=args.project_id, target_month=target_month)
    Path(args.output).write_text(json.dumps(groups, ensure_ascii=False), encoding="utf-8")
    print(f"written: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
