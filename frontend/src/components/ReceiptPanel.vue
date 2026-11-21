<template>
  <section class="panel" data-testid="receipt-panel">
    <header class="panel-head">
      <div>
        <h3>回填回执 / 有效分区写入量</h3>
        <p class="panel-desc">
          只有已确认回执计入成本分摊；失败尝试、待补交和导出时点未到达的回执会单独列出原因。
        </p>
      </div>
      <button class="btn" type="button" data-testid="reload-receipts" @click="reload">刷新回执</button>
    </header>

    <div v-if="errorMessage" class="error-text">{{ errorMessage }}</div>
    <div v-if="notice" class="notice-text" data-testid="receipt-notice">{{ notice }}</div>

    <div class="analysis-summary">
      <article class="analysis-card">
        <span class="stat-label">已确认写入量</span>
        <strong class="stat-value">{{ throughput.confirmed_write_gb?.toFixed(3) ?? '0.000' }} GB</strong>
        <small>{{ throughput.confirmed_count ?? 0 }} 条已确认回执</small>
      </article>
      <article class="analysis-card">
        <span class="stat-label">失败尝试（不参与分摊）</span>
        <strong class="stat-value">{{ throughput.rejected_write_gb?.toFixed(3) ?? '0.000' }} GB</strong>
        <small>{{ throughput.rejected_count ?? 0 }} 条已拒绝</small>
      </article>
      <article class="analysis-card">
        <span class="stat-label">待补交</span>
        <strong class="stat-value">{{ throughput.pending_write_gb?.toFixed(3) ?? '0.000' }} GB</strong>
        <small>{{ throughput.pending_count ?? 0 }} 条待补交</small>
      </article>
      <article class="analysis-card">
        <span class="stat-label">跨月补交</span>
        <strong class="stat-value">{{ throughput.late_count ?? 0 }}</strong>
        <small>归入到达日所在费用期</small>
      </article>
    </div>

    <table class="data-table" data-testid="receipt-table">
      <thead>
        <tr>
          <th>回执编号</th><th>回填任务</th><th>追踪ID</th><th>分区日期</th><th>写入量GB</th>
          <th>到达日期</th><th>费用期</th><th>归属费用期</th><th>状态</th><th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in receipts" :key="row.id" :data-testid="`receipt-${row.id}`" :data-status="row.status">
          <td>{{ row.回执编号 }}</td>
          <td>{{ row.回填任务 }}</td>
          <td>{{ row.追踪ID }}</td>
          <td>{{ row.分区日期 }}</td>
          <td>{{ Number(row.写入量GB).toFixed(3) }}</td>
          <td>{{ row.回执到达日期 }}</td>
          <td>{{ row.费用期 }}</td>
          <td :class="{ 'moved-period': row.归属费用期 !== row.费用期 }">{{ row.归属费用期 }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button
              v-if="row.status !== '已确认'"
              class="link"
              type="button"
              :data-testid="`confirm-receipt-${row.id}`"
              :disabled="!Number(row.写入量GB)"
              @click="confirm(row)"
            >确认有效</button>
            <button
              v-if="row.status !== '已拒绝'"
              class="link"
              type="button"
              :data-testid="`reject-receipt-${row.id}`"
              @click="reject(row)"
            >标记失败</button>
          </td>
        </tr>
        <tr v-if="!receipts.length">
          <td colspan="10" class="empty-state">暂无回执</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  confirmReceipt,
  listReceipts,
  loadWriteThroughput,
  rejectReceipt,
} from '@/api/backend-service'
import type { ReceiptRow, WriteThroughput } from '@/data/cost'

const receipts = ref<ReceiptRow[]>([])
const throughput = ref<WriteThroughput>({})
const errorMessage = ref('')
const notice = ref('')

async function reload() {
  errorMessage.value = ''
  try {
    receipts.value = await listReceipts()
    throughput.value = await loadWriteThroughput()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '回执读取失败'
  }
}

async function confirm(row: ReceiptRow) {
  notice.value = ''
  errorMessage.value = ''
  try {
    const result = await confirmReceipt(Number(row.id))
    if (!result.ok) throw new Error(result.message)
    notice.value = result.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '确认失败'
  }
}

async function reject(row: ReceiptRow) {
  notice.value = ''
  errorMessage.value = ''
  try {
    const result = await rejectReceipt(Number(row.id))
    if (!result.ok) throw new Error(result.message)
    notice.value = result.message
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '标记失败'
  }
}

onMounted(reload)
</script>
