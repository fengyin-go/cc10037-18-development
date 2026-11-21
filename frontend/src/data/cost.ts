/** 成本核对相关类型：与 backend/app/engine/cost_allocation.py 的输出结构对齐。 */

export type CostLine = {
  分区日期: string
  目标管线: string
  回填任务: string
  追踪ID: string
  关联批次: string
  回执编号: string[]
  回执ID: number[]
  有效写入量GB: number
  占比: number
  计算费用: number
  存储费用: number
  费用小计: number
}

export type ExcludedReceipt = {
  回执ID: number
  回执编号: string
  回填任务: string
  追踪ID: string
  分区日期: string
  目标管线: string
  关联批次: string
  状态: string
  写入量GB: number
  费用期: string
  归属费用期: string
  排除原因: string
}

export type PeriodTotals = {
  line_count: number
  allocated_compute: number
  allocated_storage: number
  allocated_total: number
  unallocated_compute: number
  unallocated_storage: number
  unallocated_total: number
  snapshot_compute: number
  snapshot_storage: number
  snapshot_total: number
  confirmed_cost: number
  pending_cost: number
  reconciled: boolean
}

export type PeriodReconciliation = {
  period: string
  snapshot: {
    id: number
    label: string
    date: string
    status: string
    compute_cost: number
    storage_cost: number
    total_cost: number
  }
  cutoff_date: string
  closed: boolean
  expected_write_gb: number
  confirmed_write_gb: number
  denominator_gb: number
  as_of: string | null
  lines: CostLine[]
  excluded: ExcludedReceipt[]
  by_pipeline: {
    目标管线: string
    有效写入量GB: number
    计算费用: number
    存储费用: number
    费用小计: number
    分区数: number
  }[]
  totals: PeriodTotals
}

export type PeriodSummary = {
  period: string
  cutoff_date: string
  closed: boolean
  snapshot_id: number
  snapshot_date: string
  snapshot_compute: number
  snapshot_storage: number
  snapshot_total: number
  confirmed_write_gb: number
  expected_write_gb: number
  confirmed_cost: number
  pending_cost: number
  allocated_total: number
  unallocated_total: number
  line_count: number
  reconciled: boolean
  note: string
}

export type ReceiptRow = {
  id: number
  status: string
  pending: boolean
  abnormal: boolean
  回执编号: string
  回填任务: string
  追踪ID: string
  分区日期: string
  目标管线: string
  费用期: string
  写入量GB: number
  回执到达日期: string
  归属费用期: string
  关联批次: string
  备注: string
}

export type RevisionRow = {
  id: number
  费用期: string
  版本号: string
  文件名: string
  快照ID: string
  导出时点: string
  触发原因: string
  修订依据: string
  上一版本: string
  变更摘要: string
  确认写入量GB: string
  已分摊金额: string
  暂未分摊金额: string
  快照金额: string
  核对结论: string
  生成时间: string
}

export type WriteThroughput = {
  task?: string
  receipt_count?: number
  confirmed_count?: number
  rejected_count?: number
  pending_count?: number
  late_count?: number
  confirmed_write_gb?: number
  rejected_write_gb?: number
  pending_write_gb?: number
  by_period?: { 归属费用期: string; 回执数: number; 有效写入量GB: number }[]
}
