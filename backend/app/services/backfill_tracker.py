"""回填追踪领域服务：管理数据回填进度、写入量统计和完成状态追踪。

上游：rerun_compensation 产生回刷任务 → backfill_tracker 追踪进度
消费：alert_policy 监视回填状态触发完成和失败告警；cost_analysis 读取
      「有效分区写入量」摊分费用期快照，回执作为逐笔核对的事实来源。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.engine import cost_allocation
from app.services.base import BaseModuleService
from app.store import store

RECEIPT_MODULE = "backfill_receipt"
RECEIPT_CONFIRMED = "已确认"
RECEIPT_REJECTED = "已拒绝"
RECEIPT_PENDING = "待补交"
RECEIPT_STATUS_ORDER = [RECEIPT_PENDING, RECEIPT_CONFIRMED, RECEIPT_REJECTED]


class BackfillTrackerService(BaseModuleService):
    MODULE = "backfill_tracker"
    ENTITY = "回填进度"
    OPERATION_TERM = "写入"
    LIST_FIELDS = ["追踪ID", "回填任务", "当前分区", "完成比例", "预估剩余", "数据写入量", "错误计数"]
    REQUIRED_FIELDS = ["追踪ID", "回填任务", "当前分区"]
    STATUS_ORDER = ["执行中", "已完成", "已暂停", "已失败"]
    ACTION_RULES = {"暂停回填": "执行中", "恢复回填": "已暂停", "标记完成": "执行中"}
    NEGATIVE_ACTIONS = []

    def update_progress(self, tracking_id: str, new_partition: str,
                        rows_written: int, errors: int = 0) -> dict[str, Any]:
        """更新回填进度。

        更新当前分区位置、数据写入量累计和错误计数，
        自动计算完成比例和预估剩余时间。
        """
        tracker = None
        for row in store.rows(self.MODULE):
            if row.get("追踪ID") == tracking_id:
                tracker = row
                break
        if tracker is None:
            return {"error": f"追踪记录 {tracking_id} 不存在"}

        previous_partition = tracker.get("当前分区", "")
        previous_written = int(tracker.get("数据写入量", "0"))
        previous_errors = int(tracker.get("错误计数", "0"))

        tracker["当前分区"] = new_partition
        tracker["数据写入量"] = str(previous_written + rows_written)
        tracker["错误计数"] = str(previous_errors + errors)

        try:
            current = int(new_partition)
            if len(new_partition) == 10:
                total_start_h = 2025010100
                total_end_h = 2025013123
            else:
                total_start_h = 0
                total_end_h = int(tracker.get("预估剩余", "100"))

            progress = min(round(current / max(total_end_h - total_start_h, 1) * 100, 1), 100)
            remaining = max(total_end_h - current, 0)
        except ValueError:
            progress = 0
            remaining = 100

        tracker["完成比例"] = str(progress)
        tracker["预估剩余"] = str(remaining)

        return {
            "tracking_id": tracking_id,
            "previous_partition": previous_partition,
            "current_partition": new_partition,
            "rows_written_this_batch": rows_written,
            "cumulative_rows": previous_written + rows_written,
            "cumulative_errors": previous_errors + errors,
            "progress_pct": progress,
            "remaining_partitions": remaining,
            "updated_at": datetime.now().isoformat(),
        }

    def query_by_task(self, task_name: str) -> list[dict[str, Any]]:
        """按回填任务查询追踪记录。

        从 store 中筛选指定任务的回填追踪数据，
        返回追踪列表供进度展示消费。
        """
        result: list[dict[str, Any]] = []
        for row in store.rows(self.MODULE):
            if task_name.casefold() in str(row.get("回填任务", "")).casefold():
                result.append(dict(row))
        return result

    def compute_throughput(self, task_name: str) -> dict[str, Any]:
        """计算回填吞吐量。

        统计指定任务所有追踪记录的写入量总和和错误率，
        产出吞吐量指标供容量规划消费。
        """
        trackers = self.query_by_task(task_name)
        total_rows = 0
        total_errors = 0
        completed = 0
        failed = 0

        for t in trackers:
            total_rows += int(t.get("数据写入量", "0"))
            total_errors += int(t.get("错误计数", "0"))
            status = t.get("status", "")
            if status == "已完成":
                completed += 1
            elif status == "已失败":
                failed += 1

        error_rate = round(total_errors / max(total_rows, 1) * 100, 2)
        return {
            "task": task_name,
            "total_trackers": len(trackers),
            "completed_trackers": completed,
            "failed_trackers": failed,
            "total_rows_written": total_rows,
            "total_errors": total_errors,
            "error_rate_pct": error_rate,
            "avg_rows_per_tracker": round(total_rows / max(len(trackers), 1), 0),
            "snapshot_at": datetime.now().isoformat(),
        }

    def check_completion_blockers(self, tracking_id: str) -> dict[str, Any]:
        """检查回填完成的阻塞因素。

        根据错误计数和当前状态判断回填是否被阻塞，
        产出阻塞分析供运维排障使用。
        """
        tracker = None
        for row in store.rows(self.MODULE):
            if row.get("追踪ID") == tracking_id:
                tracker = row
                break
        if tracker is None:
            return {"error": f"追踪记录 {tracking_id} 不存在"}

        errors = int(tracker.get("错误计数", "0"))
        status = tracker.get("status", "")
        progress = float(tracker.get("完成比例", "0"))
        written = int(tracker.get("数据写入量", "0"))

        blockers: list[str] = []
        if status == "已暂停":
            blockers.append("任务已被手动暂停")
        if errors > 100:
            blockers.append(f"错误次数过多({errors})")
        if progress > 80 and errors > 0 and written > 1000000:
            blockers.append("大数据量下的尾部错误风险")

        return {
            "tracking_id": tracking_id,
            "status": status,
            "progress_pct": progress,
            "errors": errors,
            "rows_written": written,
            "blocked": len(blockers) > 0,
            "blockers": blockers,
            "checked_at": datetime.now().isoformat(),
        }

    # ---- 回执 / 有效分区写入量：成本分摊的事实来源 ----

    def list_receipts(
        self,
        *,
        task_name: str | None = None,
        tracking_id: str | None = None,
        period: str | None = None,
    ) -> list[dict[str, Any]]:
        """按回填任务/追踪号/费用期列出回执。"""
        rows = store.rows(RECEIPT_MODULE)
        result: list[dict[str, Any]] = []
        for row in rows:
            if task_name and task_name.casefold() not in str(
                    row.get("回填任务", "")).casefold():
                continue
            if tracking_id and str(row.get("追踪ID", "")) != tracking_id:
                continue
            if period and not (
                str(row.get("费用期", "")) == period
                or str(row.get("归属费用期", "")) == period
            ):
                continue
            result.append(dict(row))
        result.sort(key=lambda item: (
            str(item.get("回执到达日期", "")),
            str(item.get("回执编号", "")),
        ))
        return result

    def effective_writes(
        self,
        *,
        as_of: date | None = None,
        task_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """输出成本分析可直接消费的有效分区写入量。

        只有「已确认」回执计入；失败尝试（已拒绝）和待补交回执都带排除原因。
        as_of 是导出时点：晚于该时点到达的回执视为尚未收到。
        """
        return self._annotate_receipts(as_of=as_of, task_name=task_name)

    def _annotate_receipts(
        self,
        *,
        as_of: date | None = None,
        task_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """给回执补上 计入/排除原因/归属费用期 三个分摊字段。"""
        periods = cost_allocation.list_period_rows()
        result: list[dict[str, Any]] = []
        for row in store.rows(RECEIPT_MODULE):
            if task_name and task_name.casefold() not in str(
                    row.get("回填任务", "")).casefold():
                continue
            item = dict(row)
            status = str(item.get("status", ""))
            arrival = cost_allocation.parse_date(item.get("回执到达日期"))
            origin = str(item.get("费用期", ""))
            attribution = str(item.get("归属费用期", "")) or \
                cost_allocation.resolve_attribution(origin, arrival, periods) or ""
            item["归属费用期"] = attribution
            item["计入"] = status == RECEIPT_CONFIRMED
            item["排除原因"] = ""
            if status == RECEIPT_REJECTED:
                item["排除原因"] = "失败尝试，写入量不参与分摊"
            elif status == RECEIPT_PENDING:
                item["排除原因"] = "待补交，尚无确认写入量"
            elif as_of is not None and (arrival is None or arrival > as_of):
                item["计入"] = False
                item["排除原因"] = (
                    f"回执 {arrival.isoformat()} 到达，晚于导出时点 {as_of.isoformat()}"
                    if arrival else "回执缺少到达日期，导出时点无法确认"
                )
            result.append(item)
        result.sort(key=lambda item: (
            str(item.get("分区日期", "")),
            str(item.get("回执编号", "")),
        ))
        return result

    # ---- 回执登记与更正 ----

    def create_receipt(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """登记一条回填回执（分区写入回报）。

        校验顺序：必填 → 追踪记录存在 → 账期关联（到达日期落在哪个费用期）。
        校验通过后才把「归属费用期」写死，后续成本重算都按它归期。
        """
        required = ["回执编号", "回填任务", "追踪ID", "分区日期", "写入量GB"]
        missing = [field for field in required if not str(values.get(field) or "").strip()]
        if missing:
            return None, f"缺少必填字段：{'、'.join(missing)}"
        receipt_no = str(values.get("回执编号", "")).strip()
        if any(str(row.get("回执编号", "")) == receipt_no
               for row in store.rows(RECEIPT_MODULE)):
            return None, f"回执编号 {receipt_no} 已登记，请勿重复提交"
        tracking_id = str(values.get("追踪ID", "")).strip()
        tracker = next(
            (row for row in store.rows(self.MODULE)
             if str(row.get("追踪ID", "")) == tracking_id), None)
        if tracker is None:
            return None, f"追踪记录 {tracking_id} 不存在，回执无法关联回填批次"
        try:
            gb = float(values.get("写入量GB") or 0)
        except (TypeError, ValueError):
            return None, "写入量GB 必须是数字"
        if gb < 0:
            return None, "写入量GB 不能为负数"
        status = str(values.get("status") or RECEIPT_CONFIRMED).strip()
        if status not in RECEIPT_STATUS_ORDER:
            return None, f"回执状态必须是 {'/'.join(RECEIPT_STATUS_ORDER)}"
        arrival = cost_allocation.parse_date(
            values.get("回执到达日期") or datetime.now().date().isoformat())
        if arrival is None:
            return None, "回执到达日期格式应为 YYYY-MM-DD"
        origin = str(values.get("费用期") or "").strip()
        if not origin:
            origin = arrival.strftime("%Y-%m")
        periods = cost_allocation.list_period_rows()
        attribution = cost_allocation.resolve_attribution(origin, arrival, periods)
        if attribution is None:
            return None, (
                f"回执到达日期 {arrival.isoformat()} 与费用期 {origin} 无可用账期关联，"
                "请先登记可承接的费用期")

        rows = store.rows(RECEIPT_MODULE)
        entry: dict[str, Any] = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "status": status,
            "pending": status == RECEIPT_PENDING,
            "abnormal": status == RECEIPT_REJECTED,
            "回执编号": receipt_no,
            "回填任务": str(values.get("回填任务", "")).strip(),
            "追踪ID": tracking_id,
            "分区日期": str(values.get("分区日期", "")).strip(),
            "目标管线": str(values.get("目标管线") or tracker.get("目标管线", "")).strip(),
            "费用期": origin,
            "写入量GB": round(gb, 3),
            "回执到达日期": arrival.isoformat(),
            "归属费用期": attribution,
            "关联批次": str(values.get("关联批次") or f"BATCH-{receipt_no}"),
            "备注": str(values.get("备注", "")),
        }
        rows.append(entry)
        return entry, (
            f"回执已登记，到达 {entry['回执到达日期']}，"
            f"归入 {attribution} 费用期")

    def correct_receipt(
        self, receipt_id: int, values: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str]:
        """更正回执（跨月更正入口）。

        先校验回执与账期的关联，再改写入量/状态，避免金额先算后改归期。
        """
        receipt = store.find(RECEIPT_MODULE, receipt_id)
        if receipt is None:
            return None, f"回执 {receipt_id} 不存在"
        arrival = cost_allocation.parse_date(
            values.get("回执到达日期") or receipt.get("回执到达日期"))
        if arrival is None:
            return None, "回执到达日期格式应为 YYYY-MM-DD"
        origin = str(values.get("费用期") or receipt.get("费用期") or "").strip()
        if not origin:
            origin = arrival.strftime("%Y-%m")
        periods = cost_allocation.list_period_rows()
        attribution = cost_allocation.resolve_attribution(origin, arrival, periods)
        if attribution is None:
            return None, (
                f"回执到达日期 {arrival.isoformat()} 与费用期 {origin} 无可用账期关联，"
                "更正被拒绝")
        status = str(values.get("status") or receipt.get("status"))
        if status not in RECEIPT_STATUS_ORDER:
            return None, f"回执状态必须是 {'/'.join(RECEIPT_STATUS_ORDER)}"
        try:
            gb = float(values.get("写入量GB", receipt.get("写入量GB", 0)))
        except (TypeError, ValueError):
            return None, "写入量GB 必须是数字"
        if gb < 0:
            return None, "写入量GB 不能为负数"

        receipt["回执到达日期"] = arrival.isoformat()
        receipt["费用期"] = origin
        receipt["归属费用期"] = attribution
        receipt["status"] = status
        receipt["pending"] = status == RECEIPT_PENDING
        receipt["abnormal"] = status == RECEIPT_REJECTED
        receipt["写入量GB"] = round(gb, 3)
        if values.get("备注") is not None:
            receipt["备注"] = str(values.get("备注"))
        return receipt, f"回执 {receipt.get('回执编号')} 已更正，归入 {attribution} 期"

    def confirm_receipt(self, receipt_id: int) -> tuple[dict[str, Any] | None, str]:
        """把待补交回执确认为有效吞吐，触发受影响费用重算。"""
        receipt = store.find(RECEIPT_MODULE, receipt_id)
        if receipt is None:
            return None, f"回执 {receipt_id} 不存在"
        if receipt.get("status") == RECEIPT_CONFIRMED:
            return receipt, "回执已是已确认状态"
        if not float(receipt.get("写入量GB") or 0):
            return None, "回执没有写入量，无法确认"
        receipt["status"] = RECEIPT_CONFIRMED
        receipt["pending"] = False
        receipt["abnormal"] = False
        return receipt, f"回执 {receipt.get('回执编号')} 已确认为有效吞吐"

    def reject_receipt(self, receipt_id: int) -> tuple[dict[str, Any] | None, str]:
        """把回执标记为失败尝试，写入量不再参与分摊。"""
        receipt = store.find(RECEIPT_MODULE, receipt_id)
        if receipt is None:
            return None, f"回执 {receipt_id} 不存在"
        receipt["status"] = RECEIPT_REJECTED
        receipt["pending"] = False
        receipt["abnormal"] = True
        return receipt, f"回执 {receipt.get('回执编号')} 已标记为失败尝试"

    def receipt_throughput(self, task_name: str | None = None) -> dict[str, Any]:
        """回填吞吐对账视图：确认量 / 失败量 / 待补交量分开统计。"""
        receipts = self._annotate_receipts(task_name=task_name)
        confirmed = [item for item in receipts if item.get("计入")]
        rejected = [item for item in receipts if item.get("status") == RECEIPT_REJECTED]
        pending = [item for item in receipts if item.get("status") == RECEIPT_PENDING]
        late = [
            item for item in receipts
            if str(item.get("归属费用期", "")) != str(item.get("费用期", ""))
        ]
        return {
            "task": task_name or "全部回填任务",
            "receipt_count": len(receipts),
            "confirmed_count": len(confirmed),
            "rejected_count": len(rejected),
            "pending_count": len(pending),
            "late_count": len(late),
            "confirmed_write_gb": round(sum(
                float(item.get("写入量GB") or 0) for item in confirmed), 3),
            "rejected_write_gb": round(sum(
                float(item.get("写入量GB") or 0) for item in rejected), 3),
            "pending_write_gb": round(sum(
                float(item.get("写入量GB") or 0) for item in pending), 3),
            "by_period": self._throughput_by_period(confirmed),
            "receipts": receipts,
            "snapshot_at": datetime.now().isoformat(),
        }

    @staticmethod
    def _throughput_by_period(receipts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        groups: dict[str, dict[str, Any]] = {}
        for item in receipts:
            period = str(item.get("归属费用期", ""))
            group = groups.setdefault(period, {
                "归属费用期": period,
                "回执数": 0,
                "有效写入量GB": 0.0,
            })
            group["回执数"] += 1
            group["有效写入量GB"] = round(
                group["有效写入量GB"] + float(item.get("写入量GB") or 0), 3)
        return [groups[key] for key in sorted(groups)]

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, msg = super().run_action(entry_id, action)
        if entry:
            entry["last_operated_at"] = datetime.now().isoformat()
        return entry, msg