"""成本分析领域服务：管理管线资源消耗快照、费用趋势预测和同比分析。

上游：resource_pool 产出资源使用 → cost_analysis 计算费用；
      backfill_tracker 提供有效分区写入量 → 本服务摊分费用期快照
消费：告警策略和容量规划面板消费成本数据；预算人员下载成本核对文件

分区费用分摊的金额一律取自 cost_period 绑定的成本快照，分摊只改变「金额落在
哪个分区」，不改变合计；已发出文件留版本，重算后旧文件仍可按版本号取回。
"""
from __future__ import annotations

import csv
import io
from datetime import date, datetime
from typing import Any

from app.engine import cost_allocation
from app.services.backfill_tracker import BackfillTrackerService
from app.services.base import BaseModuleService
from app.store import store

REVISION_MODULE = "cost_revision"
PERIOD_MODULE = "cost_period"
_receipts = BackfillTrackerService()


class CostAnalysisService(BaseModuleService):
    MODULE = "cost_analysis"
    ENTITY = "成本快照"
    OPERATION_TERM = "存储"
    LIST_FIELDS = ["快照日期", "管线标识", "计算时长", "扫描数据量", "计算费用", "存储费用", "同比变化"]
    REQUIRED_FIELDS = ["快照日期", "管线标识", "计算时长"]
    STATUS_ORDER = ["估算", "已确认", "已结算"]
    ACTION_RULES = {"确认费用": "估算", "完成结算": "已确认"}
    NEGATIVE_ACTIONS = []

    def query_by_pipeline(self, pipeline_name: str) -> list[dict[str, Any]]:
        """按管线标识查询成本快照。

        从 store 中筛选指定管线的费用记录，
        返回成本数据供趋势分析消费。
        """
        result: list[dict[str, Any]] = []
        for row in store.rows(self.MODULE):
            if pipeline_name.casefold() in str(row.get("管线标识", "")).casefold():
                result.append(dict(row))
        return result

    def aggregate_pipeline_cost(self, pipeline_name: str) -> dict[str, Any]:
        """汇总管线的历史成本。

        统计指定管线所有快照的计算费用和存储费用总和，
        计算日均和月均费用供预算规划。
        """
        snapshots = self.query_by_pipeline(pipeline_name)
        total_compute = 0.0
        total_storage = 0.0
        total_scan = 0.0

        for snap in snapshots:
            try:
                total_compute += float(snap.get("计算费用", "0"))
                total_storage += float(snap.get("存储费用", "0"))
                total_scan += float(snap.get("扫描数据量", "0"))
            except ValueError:
                pass

        count = max(len(snapshots), 1)
        return {
            "pipeline": pipeline_name,
            "snapshot_count": len(snapshots),
            "total_compute_cost": round(total_compute, 2),
            "total_storage_cost": round(total_storage, 2),
            "total_cost": round(total_compute + total_storage, 2),
            "avg_compute_per_snapshot": round(total_compute / count, 2),
            "avg_storage_per_snapshot": round(total_storage / count, 2),
            "total_scan_data_gb": round(total_scan, 1),
            "snapshot_at": datetime.now().isoformat(),
        }

    def detect_cost_anomalies(self, threshold_factor: float = 2.0) -> list[dict[str, Any]]:
        """检测费用异常波动。

        对比各管线最近两个快照的费用变化，
        当环比增长超过阈值因子时标记为异常。
        """
        rows = store.rows(self.MODULE)
        pipeline_groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            pipeline = str(row.get("管线标识", ""))
            pipeline_groups.setdefault(pipeline, []).append(row)

        anomalies: list[dict[str, Any]] = []
        for pipeline, snaps in pipeline_groups.items():
            if len(snaps) < 2:
                continue
            sorted_snaps = sorted(
                snaps,
                key=lambda s: str(s.get("快照日期", "")),
                reverse=True,
            )
            try:
                latest_cost = float(sorted_snaps[0].get("计算费用", "0")) + \
                              float(sorted_snaps[0].get("存储费用", "0"))
                prev_cost = float(sorted_snaps[1].get("计算费用", "0")) + \
                            float(sorted_snaps[1].get("存储费用", "0"))
            except ValueError:
                continue

            if prev_cost > 0 and latest_cost / prev_cost >= threshold_factor:
                anomalies.append({
                    "pipeline": pipeline,
                    "latest_cost": latest_cost,
                    "previous_cost": prev_cost,
                    "ratio": round(latest_cost / max(prev_cost, 0.01), 2),
                    "change_pct": round((latest_cost - prev_cost) / max(prev_cost, 0.01) * 100, 1),
                    "level": "严重" if latest_cost / max(prev_cost, 0.01) >= 3 else "关注",
                })

        return anomalies

    def forecast_monthly_cost(self) -> dict[str, Any]:
        """预测月度总费用。

        基于所有管线最近快照的日平均费用推估月度成本，
        产出预测数据供预算审批消费。
        """
        rows = store.rows(self.MODULE)
        total_daily = 0.0
        pipeline_count = 0

        for row in rows:
            try:
                total_daily += float(row.get("计算费用", "0"))
                total_daily += float(row.get("存储费用", "0"))
                pipeline_count += 1
            except ValueError:
                pass

        avg_daily = total_daily / max(pipeline_count, 1)
        forecast_monthly = avg_daily * 30

        return {
            "total_pipeline_count": pipeline_count,
            "avg_daily_cost_per_pipeline": round(avg_daily, 2),
            "total_daily_observed": round(total_daily, 2),
            "forecast_monthly_cost": round(forecast_monthly, 2),
            "forecast_quarterly_cost": round(forecast_monthly * 3, 2),
            "forecast_annual_cost": round(forecast_monthly * 12, 2),
            "snapshot_at": datetime.now().isoformat(),
        }

    # ---- 费用期与分区费用核对 ----

    def list_periods(self) -> list[dict[str, Any]]:
        """列出费用期账期及其对账汇总（预算核对首页）。"""
        rows: list[dict[str, Any]] = []
        receipts = _receipts.effective_writes()
        for period_row in store.rows(PERIOD_MODULE):
            period = str(period_row.get("费用期", ""))
            summary = cost_allocation.period_summary(period, receipts)
            if summary is None:
                continue
            summary["note"] = str(period_row.get("备注", ""))
            rows.append(summary)
        rows.sort(key=lambda item: item["period"])
        return rows

    def period_reconciliation(
        self, period: str, as_of: date | None = None,
    ) -> dict[str, Any] | None:
        """按费用期输出分区费用表：逐行分摊 + 暂未分摊额 + 排除清单。"""
        receipts = _receipts.effective_writes(as_of=as_of)
        return cost_allocation.reconcile(period, receipts, as_of=as_of)

    def allocation_for_task(
        self, task_name: str, as_of: date | None = None,
    ) -> dict[str, Any]:
        """按回填任务看分区费用（只取该任务的回执）。"""
        receipts = _receipts.effective_writes(as_of=as_of, task_name=task_name)
        periods = sorted({
            str(item.get("归属费用期", ""))
            for item in receipts if item.get("归属费用期")
        })
        results = []
        for period in periods:
            result = cost_allocation.reconcile(period, receipts, as_of=as_of)
            if result is not None:
                result["period"] = period
                results.append(result)
        return {
            "task": task_name,
            "as_of": as_of.isoformat() if as_of else None,
            "periods": results,
            "total_confirmed_write_gb": round(sum(
                float(item.get("写入量GB") or 0)
                for item in receipts if item.get("计入")), 3),
        }

    # ---- 成本核对文件（CSV 导出）----

    def reconciliation_csv(
        self,
        period: str,
        *,
        as_of: date | None = None,
        version: int | None = None,
    ) -> tuple[str, str] | None:
        """把某费用期的成本核对文件渲染成 CSV。

        文件包含四段，预算人员可逐笔核对：
          1) 已确认费用明细：每行关联回该批次已确认吞吐回执
          2) 暂未分摊金额：预期写入量未交齐的部分
          3) 排除回执：失败尝试、待补交、跨月补缴、导出时点未到达
          4) 费用合计：必须等于快照金额
        指定 version 时从已发出的修订记录里取回原文件，保证旧文件和修订依据可查。
        """
        if version is not None:
            record = self._find_revision(period, version)
            if record is None:
                return None
            return str(record.get("文件名", "")), str(record.get("内容", ""))

        result = self.period_reconciliation(period, as_of=as_of)
        if result is None:
            return None
        snapshot = result["snapshot"]
        totals = result["totals"]
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        buffer.write("\ufeff")

        writer.writerow(["成本核对文件", "费用期", period, "快照", snapshot["id"],
                         snapshot["label"], "快照日期", snapshot["date"],
                         "截止日", result["cutoff_date"],
                         "导出时点", result["as_of"] or "实时"])

        writer.writerow([])
        writer.writerow(["[已确认费用明细]"])
        writer.writerow(["分区日期", "目标管线", "回填任务", "追踪ID", "关联批次",
                         "回执编号", "有效写入量GB", "占比%", "计算费用", "存储费用",
                         "费用小计", "快照ID"])
        for line in result["lines"]:
            writer.writerow([
                line["分区日期"], line["目标管线"], line["回填任务"], line["追踪ID"],
                line["关联批次"], "|".join(str(no) for no in line["回执编号"]),
                f"{line['有效写入量GB']:.3f}", f"{line['占比']:.2f}",
                f"{line['计算费用']:.2f}", f"{line['存储费用']:.2f}",
                f"{line['费用小计']:.2f}", snapshot["id"],
            ])
        writer.writerow([
            "小计", "", "", "", "", "", f"{result['confirmed_write_gb']:.3f}", "",
            f"{totals['allocated_compute']:.2f}", f"{totals['allocated_storage']:.2f}",
            f"{totals['allocated_total']:.2f}", snapshot["id"],
        ])

        writer.writerow([])
        writer.writerow(["[暂未分摊金额]"])
        writer.writerow(["项目", "写入量GB", "计算费用", "存储费用", "金额小计", "说明"])
        writer.writerow([
            "未交出有效写入量",
            f"{max(result['denominator_gb'] - result['confirmed_write_gb'], 0):.3f}",
            f"{totals['unallocated_compute']:.2f}",
            f"{totals['unallocated_storage']:.2f}",
            f"{totals['unallocated_total']:.2f}",
            f"预期写入量{result['expected_write_gb']:.3f}GB 与已确认量差额部分",
        ])

        writer.writerow([])
        writer.writerow(["[排除回执]"])
        writer.writerow(["回执编号", "回填任务", "追踪ID", "分区日期", "状态",
                         "写入量GB", "费用期", "归属费用期", "排除原因"])
        for item in result["excluded"]:
            writer.writerow([
                item["回执编号"], item["回填任务"], item["追踪ID"], item["分区日期"],
                item["状态"], f"{item['写入量GB']:.3f}", item["费用期"],
                item["归属费用期"], item["排除原因"],
            ])

        writer.writerow([])
        writer.writerow(["[费用合计]"])
        writer.writerow(["费用项", "金额", "说明"])
        writer.writerow(["计算费用", f"{totals['snapshot_compute']:.2f}",
                        f"快照 {snapshot['id']}"])
        writer.writerow(["存储费用", f"{totals['snapshot_storage']:.2f}",
                        f"快照 {snapshot['id']}"])
        writer.writerow(["费用合计", f"{totals['snapshot_total']:.2f}", "快照金额"])
        writer.writerow(["已分摊", f"{totals['allocated_total']:.2f}", "已确认费用"])
        writer.writerow(["暂未分摊", f"{totals['unallocated_total']:.2f}", "待核金额"])
        writer.writerow(["核对结论", "一致" if totals["reconciled"] else "不一致",
                         "已分摊+暂未分摊=费用合计"])

        filename = f"cost_reconciliation_{period}_{snapshot['id']}.csv"
        return filename, buffer.getvalue()

    def issue_reconciliation(
        self,
        period: str,
        *,
        as_of: date | None = None,
        reason: str = "",
        trigger: str = "手动导出",
    ) -> dict[str, Any] | None:
        """生成并登记一版成本核对文件。

        有效吞吐变更后重新导出会得到新版本，旧版本仍保留文件和修订依据。
        """
        rendered = self.reconciliation_csv(period, as_of=as_of)
        if rendered is None:
            return None
        filename, content = rendered
        result = self.period_reconciliation(period, as_of=as_of) or {}
        rows = store.rows(REVISION_MODULE)
        versions = [int(row.get("版本号", 0)) for row in rows
                    if str(row.get("费用期", "")) == period]
        version = max(versions, default=0) + 1
        previous = self._latest_revision(period)
        record = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "status": "已发出",
            "pending": False,
            "abnormal": False,
            "费用期": period,
            "版本号": str(version),
            "文件名": f"cost_reconciliation_{period}_v{version}.csv",
            "快照ID": str(result.get("snapshot", {}).get("id", "")),
            "导出时点": result.get("as_of") or "",
            "触发原因": trigger,
            "修订依据": reason or (
                "首次导出" if previous is None else "有效吞吐或回执归属变更后重算"),
            "上一版本": str(previous.get("版本号", "")) if previous else "",
            "变更摘要": self._change_summary(previous, result),
            "确认写入量GB": f"{float(result.get('confirmed_write_gb', 0)):.3f}",
            "已分摊金额": f"{result.get('totals', {}).get('allocated_total', 0):.2f}",
            "暂未分摊金额": f"{result.get('totals', {}).get('unallocated_total', 0):.2f}",
            "快照金额": f"{result.get('totals', {}).get('snapshot_total', 0):.2f}",
            "核对结论": "一致" if result.get("totals", {}).get("reconciled") else "不一致",
            "生成时间": datetime.now().isoformat(timespec="seconds"),
            "内容": content,
            "文件大小": str(len(content)),
        }
        rows.append(record)
        return {
            key: value for key, value in record.items() if key != "内容"
        } | {"download_name": filename}

    def list_revisions(self, period: str | None = None) -> list[dict[str, Any]]:
        """列出已发出的成本核对文件版本（不含正文）。"""
        rows = store.rows(REVISION_MODULE)
        result = [
            {key: value for key, value in row.items() if key != "内容"}
            for row in rows
            if period is None or str(row.get("费用期", "")) == period
        ]
        result.sort(key=lambda item: (
            str(item.get("费用期", "")), int(item.get("版本号", 0))))
        return result

    def recalculate_after_throughput_change(
        self, periods: list[str], reason: str = "",
    ) -> list[dict[str, Any]]:
        """有效吞吐变更后重算受影响费用期。

        只对「已经发出过核对文件」的费用期自动登记新版本：
        旧文件按版本号保留，新版本带修订依据和变更摘要。
        """
        issued: list[dict[str, Any]] = []
        for period in sorted({p for p in periods if p}):
            if self._latest_revision(period) is None:
                continue
            record = self.issue_reconciliation(
                period,
                reason=reason or "有效吞吐变更后重算",
                trigger="有效吞吐变更",
            )
            if record is not None:
                issued.append(record)
        return issued

    def _change_summary(
        self, previous: dict[str, Any] | None, result: dict[str, Any],
    ) -> str:
        """对比上一版，给出可读的修订依据。"""
        if previous is None:
            return "首次导出，无历史版本"
        changes: list[str] = []
        previous_write = float(previous.get("确认写入量GB") or 0)
        current_write = float(result.get("confirmed_write_gb") or 0)
        if abs(current_write - previous_write) > 1e-9:
            changes.append(
                f"有效写入量 {previous_write:.3f}GB → {current_write:.3f}GB")
        previous_alloc = float(previous.get("已分摊金额") or 0)
        current_alloc = float(result.get("totals", {}).get("allocated_total", 0))
        if abs(current_alloc - previous_alloc) > 0.004:
            changes.append(
                f"已分摊 {previous_alloc:.2f} → {current_alloc:.2f}")
        previous_pending = float(previous.get("暂未分摊金额") or 0)
        current_pending = float(result.get("totals", {}).get("unallocated_total", 0))
        if abs(current_pending - previous_pending) > 0.004:
            changes.append(
                f"暂未分摊 {previous_pending:.2f} → {current_pending:.2f}")
        return "；".join(changes) if changes else "金额与口径均无变化（按同一时点重出）"

    def _latest_revision(self, period: str) -> dict[str, Any] | None:
        rows = [
            row for row in store.rows(REVISION_MODULE)
            if str(row.get("费用期", "")) == period
        ]
        if not rows:
            return None
        return max(rows, key=lambda row: int(row.get("版本号", 0)))

    def _find_revision(self, period: str, version: int) -> dict[str, Any] | None:
        return next(
            (row for row in store.rows(REVISION_MODULE)
             if str(row.get("费用期", "")) == period
             and int(row.get("版本号", 0)) == version),
            None,
        )

    def revision_csv(self, period: str, version: int) -> tuple[str, str] | None:
        """取回原样发出的历史文件（连同文件名）。"""
        record = self._find_revision(period, version)
        if record is None:
            return None
        filename = str(record.get("文件名", f"cost_reconciliation_{period}_v{version}.csv"))
        return filename, str(record.get("内容", ""))

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, msg = super().run_action(entry_id, action)
        if entry:
            entry["last_operated_at"] = datetime.now().isoformat()
        return entry, msg