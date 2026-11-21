"""成本核对 API：把回填有效分区写入量摊成费用期分区费用，供预算下载核对文件。

路由分层约定：
- 回执（事实）由 backfill_tracker 负责登记与更正；
- 分摊（口径）由 cost_analysis 负责，金额只来自费用期快照；
- 导出（交付）分两段：先预览 JSON，再登记版本并下载 CSV。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.config import settings
from app.engine import cost_allocation
from app.services import service_for
from app.services.backfill_tracker import BackfillTrackerService
from app.services.cost_analysis import CostAnalysisService

router = APIRouter(prefix="/api/cost", tags=["成本核对"])

_receipts: BackfillTrackerService = service_for("backfill_tracker")  # type: ignore[assignment]
_costs: CostAnalysisService = service_for("cost_analysis")  # type: ignore[assignment]


def _parse_as_of(as_of: str | None):
    if not as_of:
        return None
    parsed = cost_allocation.parse_date(as_of)
    if parsed is None:
        raise HTTPException(status_code=400, detail=f"导出时点格式应为 YYYY-MM-DD：{as_of}")
    return parsed


def _require_period(period: str) -> dict[str, Any]:
    rows = cost_allocation.list_period_rows()
    if not any(str(row.get("费用期", "")) == period for row in rows):
        raise HTTPException(status_code=404, detail=f"费用期 {period} 不存在")
    return next(row for row in rows if str(row.get("费用期", "")) == period)


@router.get("/periods")
def list_periods() -> dict[str, Any]:
    """费用期列表：每期快照金额、已分摊、暂未分摊和核对结论。"""
    rows = _costs.list_periods()
    return {
        "items": rows,
        "total": len(rows),
        "rule": "已分摊 + 暂未分摊 = 快照金额；失败尝试写入量不参与分摊",
    }


@router.get("/periods/{period}")
def period_detail(
    period: str,
    as_of: str | None = Query(default=None, description="导出时点，缺省按当前全部已到达回执"),
) -> dict[str, Any]:
    """费用期分区费用核对详情。"""
    _require_period(period)
    result = _costs.period_reconciliation(period, as_of=_parse_as_of(as_of))
    if result is None:
        raise HTTPException(status_code=404, detail=f"费用期 {period} 无法对账")
    return result


@router.get("/periods/{period}/export")
def export_reconciliation(
    period: str,
    as_of: str | None = Query(default=None),
    reason: str = Query(default="", description="修订依据"),
    record: bool = Query(default=True, description="是否登记新版本"),
) -> StreamingResponse:
    """下载成本核对文件（CSV）。

    record=true 时先登记版本，文件名为 cost_reconciliation_<期>_v<版本>.csv；
    指定 version 时从已发出版本中原样取回旧文件。
    """
    _require_period(period)
    moment = _parse_as_of(as_of)
    if not record:
        rendered = _costs.reconciliation_csv(period, as_of=moment)
        if rendered is None:
            raise HTTPException(status_code=404, detail=f"费用期 {period} 无法导出")
        filename, content = rendered
    else:
        issued = _costs.issue_reconciliation(
            period, as_of=moment, reason=reason, trigger="预算导出")
        if issued is None:
            raise HTTPException(status_code=404, detail=f"费用期 {period} 无法导出")
        rendered = _costs.revision_csv(period, int(issued["版本号"]))
        if rendered is None:
            raise HTTPException(status_code=500, detail="版本登记失败")
        filename, content = rendered
    return StreamingResponse(
        iter([content]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/periods/{period}/revisions")
def list_revisions(period: str) -> dict[str, Any]:
    """已发出的成本核对文件版本及修订依据。"""
    _require_period(period)
    items = _costs.list_revisions(period)
    return {"items": items, "total": len(items)}


@router.get("/periods/{period}/revisions/{version}")
def download_revision(period: str, version: int) -> StreamingResponse:
    """按版本号取回原样发出的历史成本核对文件。

    已结转费用期也走这里查旧期金额，不因重算而丢历史。
    """
    _require_period(period)
    rendered = _costs.revision_csv(period, version)
    if rendered is None:
        raise HTTPException(
            status_code=404, detail=f"费用期 {period} 没有第 {version} 版核对文件")
    filename, content = rendered
    return StreamingResponse(
        iter([content]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/tasks/{task_name}/allocation")
def task_allocation(
    task_name: str,
    as_of: str | None = Query(default=None),
) -> dict[str, Any]:
    """按回填任务汇总其分区费用（可能横跨多个费用期）。"""
    return _costs.allocation_for_task(task_name, as_of=_parse_as_of(as_of))


@router.get("/write-throughput")
def write_throughput(task: str | None = Query(default=None)) -> dict[str, Any]:
    """回填有效分区写入量对账视图（确认/失败/待补交分开）。"""
    return _receipts.receipt_throughput(task)


@router.get("/receipts")
def list_receipts(
    task: str | None = Query(default=None),
    tracking_id: str | None = Query(default=None),
    period: str | None = Query(default=None),
) -> dict[str, Any]:
    """回执明细，便于逐笔追溯到成本核对文件。"""
    items = _receipts.list_receipts(
        task_name=task, tracking_id=tracking_id, period=period)
    return {"items": items, "total": len(items)}


@router.post("/receipts")
def create_receipt(body: dict[str, Any]) -> dict[str, Any]:
    """登记回填回执。

    body: 回执编号, 回填任务, 追踪ID, 分区日期, 写入量GB,
          费用期(可选), 回执到达日期(可选), 关联批次(可选), status(可选)
    """
    entry, message = _receipts.create_receipt(body)
    if entry is None:
        return {"ok": False, "message": message, "entry": None}
    return {"ok": True, "message": message, "entry": entry}


@router.post("/receipts/{receipt_id}/correct")
def correct_receipt(
    receipt_id: int,
    body: dict[str, Any],
    recalculate: bool = Query(default=True, description="更正后是否重算受影响费用期"),
) -> dict[str, Any]:
    """跨月更正回执：先校验回执与账期的关联，再改写入量并重算计算费/存储费。

    校验失败时直接拒绝，不会出现「金额先算、归期后改」的中间态。
    """
    before = next(
        (row for row in _receipts.list_receipts() if int(row.get("id", 0)) == receipt_id),
        None,
    )
    previous_period = str((before or {}).get("归属费用期", ""))
    entry, message = _receipts.correct_receipt(receipt_id, body)
    if entry is None:
        return {"ok": False, "message": message, "entry": None, "recalculated": []}
    recalculated: list[dict[str, Any]] = []
    if recalculate:
        periods = {
            previous_period,
            str(entry.get("归属费用期") or ""),
            str(entry.get("费用期") or ""),
        }
        recalculated = _costs.recalculate_after_throughput_change(
            sorted(periods),
            reason=str(body.get("备注") or f"回执 {entry.get('回执编号')} 跨月更正"),
        )
    return {
        "ok": True,
        "message": message,
        "entry": entry,
        "recalculated": [
            {key: value for key, value in item.items()
             if key in {"费用期", "版本号", "文件名", "修订依据", "变更摘要", "核对结论"}}
            for item in recalculated
        ],
    }


@router.post("/receipts/{receipt_id}/confirm")
def confirm_receipt(
    receipt_id: int,
    recalculate: bool = Query(default=True, description="确认后是否重算受影响费用期"),
    reason: str = Query(default=""),
) -> dict[str, Any]:
    """确认补交回执为有效吞吐，随后重算受影响费用并按新版本留档。"""
    entry, message = _receipts.confirm_receipt(receipt_id)
    if entry is None:
        return {"ok": False, "message": message, "entry": None, "recalculated": []}
    recalculated: list[dict[str, Any]] = []
    if recalculate:
        recalculated = _costs.recalculate_after_throughput_change(
            [str(entry.get("归属费用期") or entry.get("费用期") or "")],
            reason=reason or f"回执 {entry.get('回执编号')} 确认为有效吞吐",
        )
    return {
        "ok": True,
        "message": message,
        "entry": entry,
        "recalculated": [
            {key: value for key, value in item.items()
             if key in {"费用期", "版本号", "文件名", "修订依据", "变更摘要", "核对结论"}}
            for item in recalculated
        ],
    }


@router.post("/receipts/{receipt_id}/reject")
def reject_receipt(
    receipt_id: int,
    recalculate: bool = Query(default=True, description="标记后是否重算受影响费用期"),
    reason: str = Query(default=""),
) -> dict[str, Any]:
    """把回执标记为失败尝试，写入量退出分摊并触发重算。"""
    entry, message = _receipts.reject_receipt(receipt_id)
    if entry is None:
        return {"ok": False, "message": message, "entry": None, "recalculated": []}
    recalculated: list[dict[str, Any]] = []
    if recalculate:
        recalculated = _costs.recalculate_after_throughput_change(
            [str(entry.get("归属费用期") or entry.get("费用期") or "")],
            reason=reason or f"回执 {entry.get('回执编号')} 标记为失败尝试",
        )
    return {
        "ok": True,
        "message": message,
        "entry": entry,
        "recalculated": [
            {key: value for key, value in item.items()
             if key in {"费用期", "版本号", "文件名", "修订依据", "变更摘要", "核对结论"}}
            for item in recalculated
        ],
    }


@router.get("/settings")
def show_settings() -> dict[str, Any]:
    """核对口径提示：给前端展示当前规则，不暴露敏感配置。"""
    return {
        "app": settings.app_name,
        "currency_precision": 2,
        "allocation_denominator": "max(预期写入量, 已确认有效写入量)",
        "expense_source": "费用期快照（cost_analysis）",
        "excluded_writes": ["失败尝试（已拒绝）", "待补交", "导出时点未到达"],
    }
