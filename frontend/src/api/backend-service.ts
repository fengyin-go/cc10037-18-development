import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type {
  PeriodReconciliation,
  PeriodSummary,
  ReceiptRow,
  RevisionRow,
  WriteThroughput,
} from '@/data/cost'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

// ---- 成本核对 / 费用期分摊 ----

function query(params: Record<string, string | undefined>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value) search.set(key, value)
  }
  const text = search.toString()
  return text ? `?${text}` : ''
}

export async function listCostPeriods(): Promise<PeriodSummary[]> {
  const response = await request('/api/cost/periods')
  if (!response.ok) throw new Error('费用期列表读取失败')
  const payload = await response.json()
  return payload.items as PeriodSummary[]
}

export async function loadPeriodReconciliation(
  period: string,
  asOf = '',
): Promise<PeriodReconciliation> {
  const response = await request(`/api/cost/periods/${encodeURIComponent(period)}${query({ as_of: asOf })}`)
  if (!response.ok) throw new Error('分区费用核对读取失败')
  return await response.json()
}

export async function listCostRevisions(period: string): Promise<RevisionRow[]> {
  const response = await request(`/api/cost/periods/${encodeURIComponent(period)}/revisions`)
  if (!response.ok) throw new Error('核对文件版本读取失败')
  const payload = await response.json()
  return payload.items as RevisionRow[]
}

async function triggerDownload(path: string, fallbackName: string): Promise<void> {
  const response = await request(path)
  if (!response.ok) throw new Error('核对文件下载失败')
  const disposition = response.headers.get('Content-Disposition') ?? ''
  const matched = /filename="?([^";]+)"?/.exec(disposition)
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = matched?.[1] ?? fallbackName
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function downloadReconciliation(
  period: string,
  asOf = '',
  reason = '',
): Promise<void> {
  await triggerDownload(
    `/api/cost/periods/${encodeURIComponent(period)}/export${query({ as_of: asOf, reason })}`,
    `cost_reconciliation_${period}.csv`,
  )
}

export async function downloadRevision(period: string, version: number): Promise<void> {
  await triggerDownload(
    `/api/cost/periods/${encodeURIComponent(period)}/revisions/${version}`,
    `cost_reconciliation_${period}_v${version}.csv`,
  )
}

export async function loadWriteThroughput(task = ''): Promise<WriteThroughput> {
  const response = await request(`/api/cost/write-throughput${query({ task })}`)
  if (!response.ok) throw new Error('有效写入量读取失败')
  return await response.json()
}

export async function listReceipts(task = ''): Promise<ReceiptRow[]> {
  const response = await request(`/api/cost/receipts${query({ task })}`)
  if (!response.ok) throw new Error('回执明细读取失败')
  const payload = await response.json()
  return payload.items as ReceiptRow[]
}

export async function confirmReceipt(id: number): Promise<ActionResult & { recalculated?: unknown[] }> {
  const response = await request(`/api/cost/receipts/${id}/confirm`, { method: 'POST' })
  if (!response.ok) throw new Error('回执确认失败')
  return await response.json()
}

export async function rejectReceipt(id: number): Promise<ActionResult & { recalculated?: unknown[] }> {
  const response = await request(`/api/cost/receipts/${id}/reject`, { method: 'POST' })
  if (!response.ok) throw new Error('回执标记失败')
  return await response.json()
}
