"""费用期分摊引擎：把成本快照按回填有效分区写入量摊到分区。

设计约束（对应预算核对口径）：
- 只使用费用期快照里的计算/存储金额，不另造费率；
- 已分摊 + 暂未分摊 == 快照金额，按分（cent）严格闭合；
- 分母取 max(预期写入量, 已确认写入量)，因此已确认写入量超预期时也不会摊超快照；
- 失败尝试、待补交、导出时点尚未到达的回执都不进入分母，只在排除清单里留痕；
- 逐行金额用最大余数法分配，避免四舍五入导致明细合计和快照对不上。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any, Iterable

from app.store import store

COST_PERIOD_MODULE = "cost_period"
COST_SNAPSHOT_MODULE = "cost_analysis"
PERIOD_STATUS_CLOSED = "已结转"


def parse_date(value: Any) -> date | None:
    """把 2026-11-20 / 2026-11-20T10:00:00 之类的值统一成 date。"""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_decimal(value: Any, default: str = "0") -> Decimal:
    """把数字或 "180GB"/"15.6TB" 这类文本解析成 Decimal。"""
    if isinstance(value, bool) or value is None:
        return Decimal(default)
    if isinstance(value, (int, float, Decimal)):
        try:
            return Decimal(str(value))
        except InvalidOperation:
            return Decimal(default)
    text = str(value).strip().upper().replace(",", "")
    if not text:
        return Decimal(default)
    factor = Decimal("1")
    if text.endswith("TB"):
        factor = Decimal("1024")
        text = text[:-2]
    elif text.endswith("GB"):
        text = text[:-2]
    elif text.endswith("MB"):
        factor = Decimal("0.001")
        text = text[:-2]
    try:
        return Decimal(text) * factor
    except InvalidOperation:
        return Decimal(default)


def to_cents(value: Any) -> int:
    """金额转分，统一四舍五入到 2 位。"""
    amount = parse_decimal(value)
    return int((amount * 100).quantize(Decimal("1"), rounding="ROUND_HALF_UP"))


def from_cents(cents: int) -> float:
    return float((Decimal(cents) / 100).quantize(Decimal("0.01")))


def _round_half_up(value: Fraction) -> int:
    """非负 Fraction 的四舍五入。"""
    if value <= 0:
        return 0
    quotient, remainder = divmod(value.numerator, value.denominator)
    if 2 * remainder >= value.denominator:
        quotient += 1
    return quotient


def split_cents(total_cents: int, weights: list[Fraction], denominator: Fraction) -> list[int]:
    """按权重摊分金额，权重和可以小于分母（留存未分摊额）。

    返回的整数分列表满足：sum(result) == round(total_cents * sum(weights) / denominator)。
    """
    if total_cents <= 0 or not weights or denominator <= 0:
        return [0 for _ in weights]
    target = _round_half_up(Fraction(total_cents) * sum(weights) / denominator)
    exact = [Fraction(total_cents) * weight / denominator for weight in weights]
    floors = [int(value) for value in exact]
    remainder = target - sum(floors)
    order = sorted(
        range(len(weights)),
        key=lambda index: (-(exact[index] - floors[index]), index),
    )
    for index in order:
        if remainder <= 0:
            break
        floors[index] += 1
        remainder -= 1
    return floors


@dataclass
class PeriodContext:
    period: str
    cutoff: date
    expected_gb: Decimal
    compute_cents: int
    storage_cents: int
    snapshot_id: int
    snapshot_date: str
    snapshot_label: str
    snapshot_status: str
    closed: bool
    note: str

    @property
    def total_cents(self) -> int:
        return self.compute_cents + self.storage_cents


def list_period_rows() -> list[dict[str, Any]]:
    return list(store.rows(COST_PERIOD_MODULE))


def period_context(period: str) -> dict[str, Any] | None:
    """读取费用期快照上下文；快照金额是唯一费用来源。"""
    row = next(
        (item for item in list_period_rows() if item.get("费用期") == period), None)
    if row is None:
        return None
    try:
        snapshot_id = int(row.get("快照ID", 0))
    except (TypeError, ValueError):
        snapshot_id = 0
    snapshot = store.find(COST_SNAPSHOT_MODULE, snapshot_id)
    if snapshot is None:
        return None
    cutoff = parse_date(row.get("截止日期"))
    if cutoff is None:
        return None
    compute_cents = to_cents(snapshot.get("计算费用", 0))
    storage_cents = to_cents(snapshot.get("存储费用", 0))
    return {
        "period": period,
        "cutoff": cutoff,
        "expected_gb": parse_decimal(row.get("预期写入量GB", "0")),
        "compute_cents": compute_cents,
        "storage_cents": storage_cents,
        "total_cents": compute_cents + storage_cents,
        "snapshot_id": snapshot_id,
        "snapshot_date": str(snapshot.get("快照日期", "")),
        "snapshot_label": str(snapshot.get("管线标识", "")),
        "snapshot_status": str(snapshot.get("status", "")),
        "closed": str(row.get("结转状态", "")) == PERIOD_STATUS_CLOSED,
        "note": str(row.get("备注", "")),
    }


def resolve_attribution(
    receipt_period: str,
    arrival: date | None,
    periods: Iterable[dict[str, Any]] | None = None,
) -> str | None:
    """按「回执到达日期 <= 费用期截止日」选择归属费用期。

    回执在原始费用期截止前到达 → 归原费用期；
    截止后才到达 → 归入最早一个截止日不早于到达日、且晚于原费用期的费用期。
    找不到可承接的账期时返回 None，由调用方拒绝登记。
    """
    contexts = []
    for row in periods if periods is not None else list_period_rows():
        context = period_context(str(row.get("费用期", "")))
        if context is not None:
            contexts.append(context)
    contexts.sort(key=lambda item: item["period"])
    if not contexts:
        return None
    origin = next((item for item in contexts if item["period"] == receipt_period), None)
    if origin is None:
        return None
    if arrival is None:
        return None
    if arrival <= origin["cutoff"]:
        return origin["period"]
    for context in contexts:
        if context["period"] <= origin["period"]:
            continue
        if arrival <= context["cutoff"]:
            return context["period"]
    return None


def _group_write_lines(receipts: list[dict[str, Any]], period: str) -> list[dict[str, Any]]:
    """把同一批次、同一分区、同一管线的多条已确认回执合成一行。"""
    groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for receipt in receipts:
        if not receipt.get("计入"):
            continue
        if str(receipt.get("归属费用期", "")) != period:
            continue
        gb = parse_decimal(receipt.get("写入量GB", "0"))
        if gb <= 0:
            continue
        key = (
            str(receipt.get("目标管线", "")),
            str(receipt.get("分区日期", "")),
            str(receipt.get("回填任务", "")),
            str(receipt.get("关联批次", "")),
        )
        group = groups.get(key)
        if group is None:
            group = {
                "目标管线": key[0],
                "分区日期": key[1],
                "回填任务": key[2],
                "关联批次": key[3],
                "追踪ID": receipt.get("追踪ID", ""),
                "有效写入量GB": Decimal("0"),
                "回执编号": [],
                "回执ID": [],
            }
            groups[key] = group
        group["有效写入量GB"] += gb
        group["回执编号"].append(str(receipt.get("回执编号", "")))
        group["回执ID"].append(receipt.get("id"))
    lines = list(groups.values())
    lines.sort(key=lambda item: (item["分区日期"], item["目标管线"], item["回填任务"]))
    return lines


def _excluded_for_period(
    receipts: list[dict[str, Any]], period: str,
) -> list[dict[str, Any]]:
    """列出本费用期口径下被排除的回执，便于预算人员复核。"""
    excluded: list[dict[str, Any]] = []
    for receipt in receipts:
        if receipt.get("计入") and str(receipt.get("归属费用期", "")) == period:
            continue
        origin = str(receipt.get("费用期", ""))
        attribution = str(receipt.get("归属费用期", ""))
        if not (origin == period or attribution == period):
            continue
        reason = str(receipt.get("排除原因") or "")
        if not reason:
            if receipt.get("status") == "已拒绝":
                reason = "失败尝试，写入量不参与分摊"
            elif receipt.get("status") == "待补交":
                reason = "待补交，尚无确认写入量"
            elif attribution and attribution != period:
                reason = f"跨月补交，已归入 {attribution} 期"
            elif not receipt.get("计入"):
                reason = "回执在导出时点尚未到达"
        excluded.append({
            "回执ID": receipt.get("id"),
            "回执编号": receipt.get("回执编号", ""),
            "回填任务": receipt.get("回填任务", ""),
            "追踪ID": receipt.get("追踪ID", ""),
            "分区日期": receipt.get("分区日期", ""),
            "目标管线": receipt.get("目标管线", ""),
            "关联批次": receipt.get("关联批次", ""),
            "状态": receipt.get("status", ""),
            "写入量GB": float(parse_decimal(receipt.get("写入量GB", "0"))),
            "费用期": origin,
            "归属费用期": attribution,
            "排除原因": reason,
        })
    excluded.sort(key=lambda item: (str(item["分区日期"]), str(item["回执编号"])))
    return excluded


def reconcile(
    period: str,
    receipts: list[dict[str, Any]],
    as_of: date | None = None,
) -> dict[str, Any] | None:
    """计算某费用期的分区费用分摊结果。

    receipts 由 backfill_tracker 提供（含 计入/排除原因/归属费用期），
    本函数不再回查回填库，保证成本侧只消费「有效分区写入量」。
    """
    context = period_context(period)
    if context is None:
        return None

    if as_of is not None:
        filtered: list[dict[str, Any]] = []
        for receipt in receipts:
            arrival = parse_date(receipt.get("回执到达日期"))
            item = dict(receipt)
            if item.get("计入") and (arrival is None or arrival > as_of):
                item["计入"] = False
                item["排除原因"] = (
                    f"回执到达日期 {arrival.isoformat()} 晚于导出时点 {as_of.isoformat()}"
                    if arrival else "回执缺少到达日期，导出时点无法确认"
                )
            filtered.append(item)
        receipts = filtered

    lines = _group_write_lines(receipts, period)
    confirmed_gb = sum((line["有效写入量GB"] for line in lines), Decimal("0"))
    expected_gb = context["expected_gb"]
    denominator = max(expected_gb, confirmed_gb)
    if denominator <= 0:
        denominator = Decimal("1")

    weights = [Fraction(line["有效写入量GB"]) for line in lines]
    denominator_fraction = Fraction(denominator)
    compute_cents = split_cents(
        context["compute_cents"], weights, denominator_fraction)
    storage_cents = split_cents(
        context["storage_cents"], weights, denominator_fraction)

    for line, compute, storage in zip(lines, compute_cents, storage_cents):
        gb = line["有效写入量GB"]
        line["计算费用"] = from_cents(compute)
        line["存储费用"] = from_cents(storage)
        line["费用小计"] = from_cents(compute + storage)
        line["计算单价"] = from_cents(
            _round_half_up(Fraction(context["compute_cents"]) / denominator_fraction))
        line["有效写入量GB"] = float(gb)
        line["占比"] = round(float(Fraction(gb) / denominator_fraction * 100), 2)

    allocated_compute = sum(compute_cents)
    allocated_storage = sum(storage_cents)
    unallocated_compute = context["compute_cents"] - allocated_compute
    unallocated_storage = context["storage_cents"] - allocated_storage

    total_compute = allocated_compute + unallocated_compute
    total_storage = allocated_storage + unallocated_storage
    reconciled = (
        total_compute == context["compute_cents"]
        and total_storage == context["storage_cents"]
        and allocated_compute >= 0
        and allocated_storage >= 0
        and unallocated_compute >= 0
        and unallocated_storage >= 0
    )

    by_pipeline: dict[str, dict[str, Any]] = {}
    for line in lines:
        item = by_pipeline.setdefault(line["目标管线"], {
            "目标管线": line["目标管线"],
            "有效写入量GB": 0.0,
            "计算费用": 0.0,
            "存储费用": 0.0,
            "费用小计": 0.0,
            "分区数": 0,
        })
        item["有效写入量GB"] = round(item["有效写入量GB"] + line["有效写入量GB"], 2)
        item["计算费用"] = round(item["计算费用"] + line["计算费用"], 2)
        item["存储费用"] = round(item["存储费用"] + line["存储费用"], 2)
        item["费用小计"] = round(item["费用小计"] + line["费用小计"], 2)
        item["分区数"] += 1
    by_pipeline_rows = sorted(by_pipeline.values(), key=lambda item: item["目标管线"])

    return {
        "period": period,
        "snapshot": {
            "id": context["snapshot_id"],
            "label": context["snapshot_label"],
            "date": context["snapshot_date"],
            "status": context["snapshot_status"],
            "compute_cost": from_cents(context["compute_cents"]),
            "storage_cost": from_cents(context["storage_cents"]),
            "total_cost": from_cents(context["total_cents"]),
        },
        "cutoff_date": context["cutoff"].isoformat(),
        "closed": context["closed"],
        "expected_write_gb": float(expected_gb),
        "confirmed_write_gb": float(confirmed_gb),
        "denominator_gb": float(denominator),
        "as_of": as_of.isoformat() if as_of else None,
        "lines": lines,
        "excluded": _excluded_for_period(receipts, period),
        "by_pipeline": by_pipeline_rows,
        "totals": {
            "line_count": len(lines),
            "allocated_compute": from_cents(allocated_compute),
            "allocated_storage": from_cents(allocated_storage),
            "allocated_total": from_cents(allocated_compute + allocated_storage),
            "unallocated_compute": from_cents(unallocated_compute),
            "unallocated_storage": from_cents(unallocated_storage),
            "unallocated_total": from_cents(unallocated_compute + unallocated_storage),
            "snapshot_compute": from_cents(context["compute_cents"]),
            "snapshot_storage": from_cents(context["storage_cents"]),
            "snapshot_total": from_cents(context["total_cents"]),
            "confirmed_cost": from_cents(allocated_compute + allocated_storage),
            "pending_cost": from_cents(unallocated_compute + unallocated_storage),
            "reconciled": reconciled,
        },
    }


def period_summary(period: str, receipts: list[dict[str, Any]]) -> dict[str, Any] | None:
    """费用期列表用的轻量汇总。"""
    result = reconcile(period, receipts)
    if result is None:
        return None
    context = period_context(period) or {}
    totals = result["totals"]
    return {
        "period": period,
        "cutoff_date": result["cutoff_date"],
        "closed": bool(context.get("closed")),
        "snapshot_id": result["snapshot"]["id"],
        "snapshot_date": result["snapshot"]["date"],
        "snapshot_compute": totals["snapshot_compute"],
        "snapshot_storage": totals["snapshot_storage"],
        "snapshot_total": totals["snapshot_total"],
        "confirmed_write_gb": result["confirmed_write_gb"],
        "expected_write_gb": result["expected_write_gb"],
        "confirmed_cost": totals["confirmed_cost"],
        "pending_cost": totals["pending_cost"],
        "allocated_total": totals["allocated_total"],
        "unallocated_total": totals["unallocated_total"],
        "line_count": totals["line_count"],
        "reconciled": totals["reconciled"],
    }
