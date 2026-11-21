<template>
  <section class="panel" data-testid="cost-reconciliation-panel">
    <header class="panel-head">
      <div>
        <h3>分区费用核对</h3>
        <p class="panel-desc">
          快照金额按回填有效分区写入量摊分；失败尝试不参与，未交齐的写入量计入待核金额。
        </p>
      </div>
      <button class="btn" type="button" data-testid="reload-periods" @click="reload">刷新费用期</button>
    </header>

    <div v-if="errorMessage" class="error-text">{{ errorMessage }}</div>

    <div class="period-tabs">
      <button
        v-for="item in periods"
        :key="item.period"
        type="button"
        class="period-tab"
        :class="{ active: item.period === selectedPeriod }"
        :data-testid="`period-${item.period}`"
        @click="selectPeriod(item.period)"
      >
        <strong>{{ item.period }}</strong>
        <span>{{ item.closed ? '已结转' : '开放' }}</span>
      </button>
    </div>

    <div v-if="summary" class="analysis-summary" data-testid="period-summary">
      <article class="analysis-card">
        <span class="stat-label">快照金额</span>
        <strong class="stat-value">{{ money(summary.snapshot_total) }}</strong>
        <small>计算 {{ money(summary.snapshot_compute) }} / 存储 {{ money(summary.snapshot_storage) }}</small>
      </article>
      <article class="analysis-card">
        <span class="stat-label">已确认费用</span>
        <strong class="stat-value">{{ money(summary.allocated_total) }}</strong>
        <small>有效写入量 {{ summary.confirmed_write_gb.toFixed(3) }} GB</small>
      </article>
      <article class="analysis-card">
        <span class="stat-label">暂未分摊（待核）</span>
        <strong class="stat-value">{{ money(summary.unallocated_total) }}</strong>
        <small>截止日 {{ summary.cutoff_date }}</small>
      </article>
      <article class="analysis-card">
        <span class="stat-label">核对结论</span>
        <strong class="stat-value" :class="summary.reconciled ? 'trend-up' : 'trend-down'">
          {{ summary.reconciled ? '一致' : '不一致' }}
        </strong>
        <small>已分摊 + 待核 = 快照金额</small>
      </article>
    </div>

    <form class="analysis-form" @submit.prevent="reloadDetail">
      <label>
        <span>导出时点</span>
        <input v-model="asOf" type="date" :max="today" data-testid="as-of" />
      </label>
      <label>
        <span>修订依据</span>
        <input v-model="reason" :placeholder="`例如：补交回执 ${currentPeriodLabel} 到达`" data-testid="export-reason" />
      </label>
      <button class="btn primary" type="button" data-testid="export-reconciliation" :disabled="!selectedPeriod" @click="exportFile">
        导出成本核对文件
      </button>
      <button class="btn ghost" type="button" data-testid="apply-as-of" @click="reloadDetail">按该时点核对</button>
    </form>

    <div v-if="detail" class="detail-block">
      <h4>已确认费用明细（逐笔关联已确认吞吐回执）</h4>
      <table class="data-table" data-testid="cost-lines">
        <thead>
          <tr>
            <th>分区日期</th><th>目标管线</th><th>回填任务</th><th>关联批次</th><th>回执编号</th>
            <th>有效写入量GB</th><th>占比%</th><th>计算费用</th><th>存储费用</th><th>小计</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="line in detail.lines" :key="`${line.分区日期}-${line.目标管线}-${line.关联批次}`">
            <td>{{ line.分区日期 }}</td>
            <td>{{ line.目标管线 }}</td>
            <td>{{ line.回填任务 }}</td>
            <td>{{ line.关联批次 }}</td>
            <td>{{ line.回执编号.join(' / ') }}</td>
            <td>{{ line.有效写入量GB.toFixed(3) }}</td>
            <td>{{ line.占比.toFixed(2) }}</td>
            <td>{{ money(line.计算费用) }}</td>
            <td>{{ money(line.存储费用) }}</td>
            <td>{{ money(line.费用小计) }}</td>
          </tr>
          <tr v-if="!detail.lines.length">
            <td colspan="10" class="empty-state">本期尚无已确认写入量，金额全部计入暂未分摊</td>
          </tr>
        </tbody>
      </table>

      <div class="analysis-summary" data-testid="totals-check">
        <article class="analysis-card">
          <span class="stat-label">明细合计</span>
          <strong class="stat-value">{{ money(detail.totals.allocated_total) }}</strong>
        </article>
        <article class="analysis-card">
          <span class="stat-label">暂未分摊</span>
          <strong class="stat-value">{{ money(detail.totals.unallocated_total) }}</strong>
        </article>
        <article class="analysis-card">
          <span class="stat-label">快照金额</span>
          <strong class="stat-value">{{ money(detail.totals.snapshot_total) }}</strong>
        </article>
        <article class="analysis-card">
          <span class="stat-label">差额</span>
          <strong class="stat-value">{{ money(delta) }}</strong>
        </article>
      </div>

      <h4>未参与分摊的回执</h4>
      <table class="data-table" data-testid="excluded-receipts">
        <thead>
          <tr><th>回执编号</th><th>分区日期</th><th>状态</th><th>写入量GB</th><th>费用期</th><th>归属期</th><th>原因</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in detail.excluded" :key="item.回执编号">
            <td>{{ item.回执编号 }}</td>
            <td>{{ item.分区日期 }}</td>
            <td>{{ item.状态 }}</td>
            <td>{{ item.写入量GB.toFixed(3) }}</td>
            <td>{{ item.费用期 }}</td>
            <td>{{ item.归属费用期 }}</td>
            <td>{{ item.排除原因 }}</td>
          </tr>
          <tr v-if="!detail.excluded.length">
            <td colspan="7" class="empty-state">没有排除的回执</td>
          </tr>
        </tbody>
      </table>

      <h4>已发出核对文件（旧版本按版本号取回）</h4>
      <table class="data-table" data-testid="revisions">
        <thead>
          <tr><th>版本</th><th>文件</th><th>导出时点</th><th>修订依据</th><th>变更摘要</th><th>核对结论</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in revisions" :key="item.id">
            <td>v{{ item.版本号 }}</td>
            <td>{{ item.文件名 }}</td>
            <td>{{ item.导出时点 || '实时' }}</td>
            <td>{{ item.修订依据 }}</td>
            <td>{{ item.变更摘要 }}</td>
            <td>{{ item.核对结论 }}</td>
            <td>
              <button class="link" type="button" :data-testid="`download-revision-${item.版本号}`" @click="downloadOld(item)">
                下载 v{{ item.版本号 }}
              </button>
            </td>
          </tr>
          <tr v-if="!revisions.length">
            <td colspan="7" class="empty-state">尚未发出核对文件</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  downloadReconciliation,
  downloadRevision,
  listCostPeriods,
  listCostRevisions,
  loadPeriodReconciliation,
} from '@/api/backend-service'
import type {
  PeriodReconciliation,
  PeriodSummary,
  RevisionRow,
} from '@/data/cost'

const periods = ref<PeriodSummary[]>([])
const selectedPeriod = ref('')
const detail = ref<PeriodReconciliation | null>(null)
const revisions = ref<RevisionRow[]>([])
const asOf = ref('')
const reason = ref('')
const errorMessage = ref('')
const today = new Date().toISOString().slice(0, 10)

const summary = computed(() =>
  periods.value.find((item) => item.period === selectedPeriod.value) ?? null,
)
const currentPeriodLabel = computed(() => selectedPeriod.value || '当期')
const delta = computed(() => {
  if (!detail.value) return 0
  return round2(detail.value.totals.allocated_total + detail.value.totals.unallocated_total - detail.value.totals.snapshot_total)
})

function round2(value: number) {
  return Math.round(value * 100) / 100
}

function money(value: number) {
  return `¥${value.toFixed(2)}`
}

async function reload() {
  errorMessage.value = ''
  try {
    periods.value = await listCostPeriods()
    if (!selectedPeriod.value && periods.value.length) {
      selectedPeriod.value = periods.value[periods.value.length - 1].period
    }
    await reloadDetail()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '费用期读取失败'
  }
}

async function reloadDetail() {
  if (!selectedPeriod.value) return
  errorMessage.value = ''
  try {
    detail.value = await loadPeriodReconciliation(selectedPeriod.value, asOf.value)
    revisions.value = await listCostRevisions(selectedPeriod.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '核对详情读取失败'
  }
}

async function selectPeriod(period: string) {
  selectedPeriod.value = period
  await reloadDetail()
}

async function exportFile() {
  errorMessage.value = ''
  try {
    await downloadReconciliation(selectedPeriod.value, asOf.value, reason.value)
    // 导出会登记新版本，同时可能刷新已分摊/待核金额，整体重拉一遍
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

async function downloadOld(item: RevisionRow) {
  errorMessage.value = ''
  try {
    await downloadRevision(item.费用期, Number(item.版本号))
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '历史文件下载失败'
  }
}

onMounted(reload)
</script>
