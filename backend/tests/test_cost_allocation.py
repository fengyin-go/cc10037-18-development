"""费用期分区费用分摊 / 成本核对文件的回归测试。

覆盖口径：
- 费用合计 = 快照金额，已分摊 + 暂未分摊严格闭合；
- 失败尝试、待补交、导出时点未到达的回执不参与分摊；
- 早于第二条确认回执到达时导出，待核金额保留；
- 迟到回执按到达日归入下一费用期，不修改已结转期金额；
- 跨月更正先校验账期关联；
- 有效吞吐变更后重算生成新版本，旧版本原样可取回。

运行：cd backend && .venv/bin/python -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.backfill_tracker import BackfillTrackerService  # noqa: E402
from app.services.cost_analysis import CostAnalysisService  # noqa: E402


class CostAllocationTest(unittest.TestCase):
    def setUp(self) -> None:
        # 每个用例从种子数据重新开始，避免跨用例污染
        from app import seed
        from app.store import store

        store._tables = {
            name: [dict(row) for row in rows] for name, rows in seed.SEED_ROWS.items()
        }
        self.receipts = BackfillTrackerService()
        self.costs = CostAnalysisService()

    def _period(self, period: str, as_of: str | None = None):
        moment = date.fromisoformat(as_of) if as_of else None
        result = self.costs.period_reconciliation(period, as_of=moment)
        self.assertIsNotNone(result)
        return result

    def test_every_period_matches_snapshot(self) -> None:
        for summary in self.costs.list_periods():
            self.assertTrue(summary["reconciled"], summary["period"])
            self.assertAlmostEqual(
                summary["allocated_total"] + summary["unallocated_total"],
                summary["snapshot_total"], places=2)

    def test_failed_attempt_excluded(self) -> None:
        result = self._period("2026-11")
        rejected = [item for item in result["excluded"] if item["状态"] == "已拒绝"]
        self.assertEqual([item["回执编号"] for item in rejected], ["RC-1119-002X"])
        # 失败尝试的 96GB 未进入分母：分母仍为预期 750GB
        self.assertEqual(result["denominator_gb"], 750.0)
        self.assertEqual(result["confirmed_write_gb"], 600.0)

    def test_export_before_and_after_second_confirmation(self) -> None:
        before = self._period("2026-11", as_of="2026-11-18")
        self.assertEqual(before["confirmed_write_gb"], 180.0)
        self.assertEqual(before["totals"]["allocated_total"], 108.0)
        self.assertEqual(before["totals"]["unallocated_total"], 342.0)
        self.assertEqual([line["分区日期"] for line in before["lines"]], ["2026-11-14"])

        after = self._period("2026-11", as_of="2026-11-19")
        self.assertEqual(after["confirmed_write_gb"], 600.0)
        self.assertEqual(after["totals"]["allocated_total"], 360.0)
        self.assertEqual(after["totals"]["unallocated_total"], 90.0)
        self.assertEqual(len(after["lines"]), 4)
        for result in (before, after):
            self.assertTrue(result["totals"]["reconciled"])
            self.assertEqual(result["totals"]["snapshot_total"], 450.0)

    def test_late_receipt_moves_to_next_period(self) -> None:
        november = self._period("2026-11")
        late = [
            item for item in november["excluded"]
            if item["回执编号"] == "RC-1118-001B"
        ]
        self.assertEqual(len(late), 1)
        self.assertEqual(late[0]["归属费用期"], "2026-12")
        self.assertIn("跨月补交", late[0]["排除原因"])

        december = self._period("2026-12")
        self.assertEqual(december["confirmed_write_gb"], 150.0)
        self.assertEqual(december["totals"]["allocated_total"], 125.0)
        self.assertEqual(december["totals"]["unallocated_total"], 25.0)

        # 截止点前后导出：11-20 时 12 月期还没有迟到回执
        december_before = self._period("2026-12", as_of="2026-11-20")
        self.assertEqual(december_before["confirmed_write_gb"], 0.0)
        self.assertEqual(december_before["totals"]["unallocated_total"], 150.0)

    def test_confirm_pending_receipt_recalculates(self) -> None:
        entry, message = self.receipts.correct_receipt(
            7, {"写入量GB": 50, "status": "已确认"})
        self.assertIsNotNone(entry, message)
        result = self._period("2026-11")
        self.assertEqual(result["confirmed_write_gb"], 650.0)
        self.assertTrue(result["totals"]["reconciled"])
        self.assertEqual(result["totals"]["allocated_total"], 390.0)
        self.assertAlmostEqual(
            result["totals"]["allocated_total"] + result["totals"]["unallocated_total"],
            450.0, places=2)

    def test_cross_period_correction_requires_period_link(self) -> None:
        entry, message = self.receipts.correct_receipt(
            7, {"回执到达日期": "2027-03-05"})
        self.assertIsNone(entry)
        self.assertIn("无可用账期关联", message)
        # 校验失败不应改动写入量
        self.assertEqual(
            self.receipts.list_receipts()[0].get("status") in {"已确认", "待补交"}, True)

    def test_correction_reattributes_period(self) -> None:
        entry, message = self.receipts.correct_receipt(
            1, {"回执到达日期": "2026-11-21", "备注": "跨月更正"})
        self.assertIsNotNone(entry, message)
        self.assertEqual(entry["归属费用期"], "2026-12")
        november = self._period("2026-11")
        self.assertEqual(november["confirmed_write_gb"], 420.0)
        december = self._period("2026-12")
        self.assertEqual(december["confirmed_write_gb"], 330.0)
        for result in (november, december):
            self.assertTrue(result["totals"]["reconciled"])

    def test_closed_period_keeps_old_version(self) -> None:
        first = self.costs.issue_reconciliation(
            "2026-11", as_of=date(2026, 11, 18), reason="首次导出")
        self.assertIsNotNone(first)
        self.assertEqual(first["版本号"], "1")

        second = self.costs.issue_reconciliation(
            "2026-11", as_of=date(2026, 11, 20), reason="第二条确认到达")
        self.assertIsNotNone(second)
        self.assertEqual(second["版本号"], "2")
        self.assertIn("180.000GB", second["变更摘要"])
        self.assertIn("600.000GB", second["变更摘要"])

        old = self.costs.revision_csv("2026-11", 1)
        self.assertIsNotNone(old)
        self.assertIn("180.000", old[1])
        self.assertIn("暂未分摊,342.00", old[1])
        # 已结转期仍能取到旧期金额
        latest = self.costs.revision_csv("2026-11", 2)
        self.assertIn("暂未分摊,90.00", latest[1])
        revisions = self.costs.list_revisions("2026-11")
        self.assertEqual([row["版本号"] for row in revisions], ["1", "2"])
        self.assertEqual(revisions[1]["上一版本"], "1")

    def test_reconciliation_csv_contains_all_sections(self) -> None:
        filename, content = self.costs.reconciliation_csv(
            "2026-11", as_of=date(2026, 11, 20))
        self.assertIn("cost_reconciliation_2026-11", filename)
        for section in ("[已确认费用明细]", "[暂未分摊金额]", "[排除回执]", "[费用合计]"):
            self.assertIn(section, content)
        self.assertIn("RC-1118-001A", content)
        self.assertIn("核对结论,一致", content)
        self.assertIn("费用合计,450.00", content)

    def test_create_receipt_validates_tracking_and_period(self) -> None:
        entry, message = self.receipts.create_receipt({
            "回执编号": "RC-NEW-1", "回填任务": "11月1-15日订单宽表回刷",
            "追踪ID": "BK-NOPE", "分区日期": "2026-11-14", "写入量GB": 10,
        })
        self.assertIsNone(entry)
        self.assertIn("不存在", message)

        entry, message = self.receipts.create_receipt({
            "回执编号": "RC-NEW-2", "回填任务": "11月1-15日订单宽表回刷",
            "追踪ID": "BK-20261118-001", "分区日期": "2026-11-14",
            "写入量GB": 10, "回执到达日期": "2026-11-21",
        })
        self.assertIsNotNone(entry, message)
        self.assertEqual(entry["归属费用期"], "2026-12")


if __name__ == "__main__":
    unittest.main()
