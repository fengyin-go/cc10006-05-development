<template>
  <section class="page" data-module="access">
    <header class="page-head">
      <div>
        <h2>来源通道归属与启停权限</h2>
        <p class="page-desc">
          维护通道负责人转交与请假委托（到期自动收回），审计事实只追加、不可变，可按操作来源追溯责任记录。
        </p>
      </div>
      <div class="page-actions">
        <label class="operator-switch">
          操作者
          <select :value="session.operatorId" @change="switchOperator(($event.target as HTMLSelectElement).value)">
            <option v-for="op in OPERATORS" :key="op.用户ID" :value="op.用户ID">
              {{ op.姓名 }}（{{ op.角色 }}）
            </option>
          </select>
        </label>
      </div>
    </header>

    <h3>来源通道（连接参数按当前角色脱敏）</h3>
    <table class="data-table">
      <thead>
        <tr>
          <th>来源通道</th><th>通道名称</th><th>所属部门</th><th>值班组</th>
          <th>负责人</th><th>生效委托</th><th>连接参数（账户名受控）</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="ch in channels" :key="ch.id">
          <td>{{ ch.来源通道 }}</td>
          <td>{{ ch.通道名称 }}</td>
          <td>{{ ch.所属部门 }}</td>
          <td>{{ ch.值班组 }}</td>
          <td>{{ ch.负责人 }}</td>
          <td>{{ ch.生效委托 ? `${ch.生效委托.受托人}（至 ${ch.生效委托.到期时间}）` : '—' }}</td>
          <td class="conn">{{ ch.连接参数 }}</td>
        </tr>
      </tbody>
    </table>

    <div class="action-grid">
      <form class="mini-form" @submit.prevent="doTransfer">
        <h3>负责人转交（永久）</h3>
        <input v-model="transferForm.channel" placeholder="来源通道，如 CH-ORDER" />
        <input v-model="transferForm.newOwnerId" placeholder="新负责人用户ID，如 U-DUTY-02" />
        <button class="btn primary" type="submit">执行转交</button>
      </form>
      <form class="mini-form" @submit.prevent="doDelegate">
        <h3>请假委托（到期自动收回）</h3>
        <input v-model="delegateForm.channel" placeholder="来源通道，如 CH-ORDER" />
        <input v-model="delegateForm.delegateId" placeholder="受托人用户ID，如 U-DUTY-01" />
        <input v-model="delegateForm.expiresAt" placeholder="到期时间 ISO，如 2026-11-13T18:00:00" />
        <button class="btn primary" type="submit">创建委托</button>
      </form>
    </div>

    <h3>启停审计台账（只追加）</h3>
    <form class="filter-bar" @submit.prevent="loadFacts">
      <label class="filter-item"><span>操作来源</span>
        <input v-model="factFilter.source" placeholder="如 web / api / ops-console" />
      </label>
      <label class="filter-item"><span>操作对象</span>
        <input v-model="factFilter.objectId" placeholder="表编号，如 BRON-101" />
      </label>
      <label class="filter-item"><span>操作人ID</span>
        <input v-model="factFilter.operatorId" placeholder="如 U-OWNER-01" />
      </label>
      <button class="btn" type="submit">查询责任记录</button>
    </form>
    <table class="data-table">
      <thead>
        <tr>
          <th>日志编号</th><th>事实类型</th><th>操作类型</th><th>操作对象</th>
          <th>操作人</th><th>操作来源</th><th>提交时间</th><th>裁决时间</th><th>裁决结果</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="fact in facts" :key="fact.id">
          <td>{{ fact.日志编号 }}</td>
          <td>{{ fact.事实类型 }}</td>
          <td>{{ fact.操作类型 }}</td>
          <td>{{ fact.操作对象 }}</td>
          <td>{{ fact.操作人 }}（{{ fact.操作人ID }}）</td>
          <td>{{ fact.操作来源 }}</td>
          <td>{{ fact.提交时间 }}</td>
          <td>{{ fact.裁决时间 ?? '—' }}</td>
          <td>{{ fact.裁决结果 }}</td>
        </tr>
        <tr v-if="!facts.length">
          <td colspan="9" class="empty-state">暂无符合条件的审计事实</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span v-if="message" :class="messageOk ? 'ok-text' : 'error-text'">{{ message }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import { operatorHeaders, request } from '@/api/client'
import { OPERATORS, useSessionStore } from '@/stores/session'

const session = useSessionStore()
const channels = ref<Record<string, any>[]>([])
const facts = ref<Record<string, any>[]>([])
const message = ref('')
const messageOk = ref(true)

const transferForm = reactive({ channel: 'CH-ORDER', newOwnerId: 'U-DUTY-02' })
const delegateForm = reactive({
  channel: 'CH-ORDER',
  delegateId: 'U-DUTY-01',
  expiresAt: '2026-11-13T18:00:00',
})
const factFilter = reactive({ source: '', objectId: '', operatorId: '' })

function switchOperator(operatorId: string) {
  const op = OPERATORS.find((item) => item.用户ID === operatorId)
  if (op) session.setOperator(op)
  void loadChannels()
}

async function loadChannels() {
  const resp = await request('/api/access/channels', {
    headers: operatorHeaders(session.operatorId),
  })
  if (resp.ok) channels.value = (await resp.json()).items
}

async function doTransfer() {
  const resp = await request(`/api/access/channels/${transferForm.channel}/transfer`, {
    method: 'POST',
    headers: operatorHeaders(session.operatorId),
    body: JSON.stringify({ values: { new_owner_id: transferForm.newOwnerId } }),
  })
  const payload = await resp.json().catch(() => ({}))
  messageOk.value = resp.ok
  message.value = resp.ok ? payload.message : `操作被拒绝：${payload.detail ?? ''}`
  await loadChannels()
}

async function doDelegate() {
  const resp = await request(`/api/access/channels/${delegateForm.channel}/delegations`, {
    method: 'POST',
    headers: operatorHeaders(session.operatorId),
    body: JSON.stringify({
      values: {
        delegate_id: delegateForm.delegateId,
        expires_at: delegateForm.expiresAt,
        reason: '负责人请假',
      },
    }),
  })
  const payload = await resp.json().catch(() => ({}))
  messageOk.value = resp.ok
  message.value = resp.ok ? payload.message : `操作被拒绝：${payload.detail ?? ''}`
  await loadChannels()
}

async function loadFacts() {
  const params = new URLSearchParams()
  if (factFilter.source) params.set('source', factFilter.source)
  if (factFilter.objectId) params.set('object_id', factFilter.objectId)
  if (factFilter.operatorId) params.set('operator_id', factFilter.operatorId)
  const resp = await request(`/api/access/audit-facts?${params.toString()}`)
  if (resp.ok) facts.value = (await resp.json()).items
}

onMounted(() => {
  void loadChannels()
  void loadFacts()
})
</script>

<style scoped>
.operator-switch select {
  padding: 6px 8px;
}
.conn {
  font-family: monospace;
  font-size: 12px;
  max-width: 360px;
}
.action-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  margin: 16px 0;
}
.mini-form {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 16px;
  border: 1px solid #e9ecef;
  border-radius: 8px;
}
.mini-form h3 {
  margin: 0;
  font-size: 14px;
}
.ok-text {
  color: #1e7e34;
}
</style>
