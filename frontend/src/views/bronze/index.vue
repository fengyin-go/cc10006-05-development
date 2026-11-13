<template>
  <section class="page" data-module="bronze">
    <header class="page-head">
      <div>
        <h2>原始层管理（来源通道归属控制）</h2>
        <p class="page-desc">
          只有来源通道负责人（或其受托人）可启用/停用同步；其他角色只读。停用后依赖的清洗表自动进入待处理清单，启用时恢复。
        </p>
      </div>
      <div class="page-actions">
        <label class="operator-switch">
          操作者
          <select :value="session.operatorId" @change="switchOperator(($event.target as HTMLSelectElement).value)">
            <option v-for="op in OPERATORS" :key="op.用户ID" :value="op.用户ID">
              {{ op.姓名 }}（{{ op.角色 }}/{{ op.值班组 }}）
            </option>
          </select>
        </label>
        <button class="btn" type="button" @click="exportRows">导出原始层清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>归属判定</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <span class="role-badge" :class="{ readonly: !row.can_toggle }">
              {{ row.role_view ?? '—' }}
            </span>
            <small v-if="!row.can_toggle" class="readonly-hint">仅可读 · {{ row.read_scope }}</small>
          </td>
          <td class="row-actions">
            <button class="link" type="button" @click="runAction('提交校验', row)">提交校验</button>
            <template v-if="row.can_toggle">
              <button class="link" type="button" @click="runAction('启用同步', row)">启用同步</button>
              <button class="link danger" type="button" @click="runAction('停用同步', row)">停用同步</button>
            </template>
            <button class="link" type="button" @click="explain(row)">状态解释</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 2" class="empty-state">暂无原始层管理数据</td>
        </tr>
      </tbody>
    </table>

    <section v-if="pendingItems.length" class="pending-panel">
      <h3>清洗业务面待处理清单（随上游停用同步产生）</h3>
      <ul>
        <li v-for="item in pendingItems" :key="item.id">
          {{ item.清洗表编号 }} ← {{ item.上游原始表 }}（{{ item.待处理原因 }}，入队前：{{ item.入队前状态 }}）
        </li>
      </ul>
    </section>

    <pre v-if="explanation" class="explain-box">{{ explanation }}</pre>

    <footer class="page-foot">
      <span>共 {{ total }} 条原始表记录 · 当前操作者：{{ session.operator }}（{{ session.operatorId }}）</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { operatorHeaders, request } from '@/api/client'
import { OPERATORS, useSessionStore } from '@/stores/session'

type Row = Record<string, string | number | boolean | null>

const ENDPOINT = '/api/bronze'
const columns = ['表编号', '来源通道', '目标路径', '状态', '通道负责人', '所属部门', '连接参数', '最近校验时间', '数据校验']
const filterFields = ['表编号', '来源通道', '目标路径']

const session = useSessionStore()
const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const pendingItems = ref<Record<string, any>[]>([])
const explanation = ref('')
const filters = ref<Record<string, string>>({})

const stats = computed(() => [
  { label: '原始表总数', value: total.value },
  { label: '当前可启停表', value: rows.value.filter((r) => r.can_toggle).length },
  { label: '清洗待处理', value: pendingItems.value.length },
])

function switchOperator(operatorId: string) {
  const op = OPERATORS.find((item) => item.用户ID === operatorId)
  if (op) session.setOperator(op)
  void reload()
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      headers: operatorHeaders(session.operatorId),
      body: JSON.stringify({ values: { action } }),
    })
    if (!response.ok) {
      const payload = await response.json().catch(() => null)
      if (response.status === 403 && payload?.detail) {
        const detail = payload.detail
        const owner = detail.ownership ? `（归属通道 ${detail.ownership.来源通道}，负责人 ${detail.ownership.负责人}）` : ''
        throw new Error(`${detail.message ?? '越权操作被拒绝'}${owner}`)
      }
      throw new Error(payload?.detail ?? '原始层动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '原始层操作失败'
  }
}

async function explain(row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/state-explanation`)
    if (!response.ok) throw new Error('状态解释读取失败')
    explanation.value = JSON.stringify(await response.json(), null, 2)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '状态解释读取失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const [listResp, pendingResp] = await Promise.all([
      request(`${ENDPOINT}?${query}`, { headers: operatorHeaders(session.operatorId) }),
      request('/api/access/silver-pending'),
    ])
    if (!listResp.ok) throw new Error('原始表列表读取失败')
    const payload = await listResp.json()
    rows.value = (payload.items ?? []) as Row[]
    total.value = payload.total ?? rows.value.length
    if (pendingResp.ok) {
      pendingItems.value = ((await pendingResp.json()).items ?? []) as Record<string, any>[]
    }
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '原始层列表读取失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.operator-switch {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}
.operator-switch select {
  padding: 6px 8px;
}
.role-badge {
  padding: 2px 8px;
  border-radius: 10px;
  background: #e6f4ea;
  color: #1e7e34;
  font-size: 12px;
}
.role-badge.readonly {
  background: #f1f3f5;
  color: #6c757d;
}
.readonly-hint {
  display: block;
  color: #868e96;
  margin-top: 2px;
}
.link.danger {
  color: #c92a2a;
}
.pending-panel {
  margin-top: 16px;
  padding: 12px 16px;
  border: 1px solid #ffe3e3;
  background: #fff9f9;
  border-radius: 8px;
}
.pending-panel h3 {
  margin: 0 0 8px;
  font-size: 14px;
  color: #c92a2a;
}
.explain-box {
  margin-top: 12px;
  padding: 12px;
  background: #f8f9fa;
  border-radius: 8px;
  font-size: 12px;
  max-height: 320px;
  overflow: auto;
}
</style>
