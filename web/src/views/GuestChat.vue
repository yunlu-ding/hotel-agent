<script setup lang="ts">
/**
 * 住客端：扫码进房 → 一句话说需求 → 拿到「工单号 + 承接部门 + 承诺时限」。
 *
 * 界面刻意保留三样"能被追问"的信息：
 *   ① 路由标签（这句话由哪个 Agent 处理、是否命中缓存）；
 *   ② 工单卡片（承诺的落点，不是一段道歉）；
 *   ③ 结构化提示（引导验证身份 / 降级提示），让客人知道"现在到哪一步了"。
 */
import { computed, nextTick, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { marked } from 'marked'
import {
  createSession,
  getWorkOrder,
  sendChat,
  type SessionInfo,
  type WorkOrder,
} from '../api'

interface ChatMessage {
  role: 'user' | 'assistant' | 'system'
  content: string
  meta?: Record<string, unknown>
  workOrders?: WorkOrder[]
  elapsedMs?: number
}

const STORES = [
  { id: 'H_SH_001', name: '外滩璟悦酒店（上海 · 商务）', room: '1208' },
  { id: 'H_SY_001', name: '亚龙湾海棠湾度假酒店（三亚 · 度假）', room: '302' },
]

// 显式取默认门店：tsconfig 开了 noUncheckedIndexedAccess，直接写 STORES[0].id 会报"可能为 undefined"
const DEFAULT_STORE = STORES[0]!
const storeId = ref(DEFAULT_STORE.id)
const roomNo = ref(DEFAULT_STORE.room)
const session = ref<SessionInfo | null>(null)
const messages = ref<ChatMessage[]>([])
const input = ref('')
const sending = ref(false)
const listRef = ref<HTMLElement | null>(null)

const categoryLabels: Record<string, string> = {
  SERVICE: '客房服务',
  MAINTENANCE: '工程报修',
  MAINTENANCE_SAFETY: '安全类报修',
  COMPLAINT: '投诉',
  ESCALATION: '人工升级',
  CHECKOUT_REQUEST: '延退/换房申请',
  INVOICE: '发票申请',
  LOST_FOUND: '失物招领',
}

const statusLabels: Record<string, string> = {
  CREATED: '已创建',
  ASSIGNED: '已派单',
  ACCEPTED: '处理中',
  DONE: '已完成',
  FAILED: '处理失败',
  ESCALATED: '超时升级',
  CANCELLED: '已取消',
}

const deptLabels: Record<string, string> = {
  HOUSEKEEPING: '客房部',
  ENGINEERING: '工程部',
  FRONT_DESK: '前台',
  DUTY_MANAGER: '值班经理',
}

const myWorkOrders = computed(() => {
  const seen = new Map<string, WorkOrder>()
  messages.value.forEach((message) => {
    message.workOrders?.forEach((order) => seen.set(order.work_order_id, order))
  })
  return Array.from(seen.values()).reverse()
})

function renderMarkdown(text: string): string {
  return marked.parse(text) as string
}

async function scrollToBottom() {
  await nextTick()
  const el = listRef.value
  if (el) el.scrollTop = el.scrollHeight
}

async function enter() {
  try {
    session.value = await createSession({
      store_id: storeId.value,
      room_no: roomNo.value,
      session_id: `web-${Date.now()}`,
    })
    messages.value = [
      {
        role: 'system',
        content:
          `已进入 ${STORES.find((s) => s.id === storeId.value)?.name} · 房间 ${roomNo.value}\n\n` +
          `当前阶段：${session.value.stage}（入住中）｜身份验证：` +
          `${session.value.auth_level >= 1 ? '已通过房间级' : '未验证（涉及您房间的事会先确认身份）'}`,
      },
    ]
    ElMessage.success('扫码成功，可以开始说需求了')
  } catch (error) {
    ElMessage.error((error as Error).message)
  }
}

function onStoreChange(id: string) {
  // 换门店时把房间号也带成该店的示例房间，避免"上海店 + 三亚房号"这种自相矛盾的组合
  const store = STORES.find((s) => s.id === id)
  if (store) roomNo.value = store.room
}

function leave() {
  session.value = null
  messages.value = []
}

async function send() {
  const text = input.value.trim()
  if (!text || sending.value) return
  if (!session.value) {
    ElMessage.warning('请先扫码进入房间')
    return
  }

  messages.value.push({ role: 'user', content: text })
  input.value = ''
  sending.value = true
  await scrollToBottom()

  const assistant: ChatMessage = { role: 'assistant', content: '', workOrders: [] }
  messages.value.push(assistant)

  try {
    await sendChat(
      { query: text, session_id: session.value.session_id, store_id: storeId.value },
      {
        onMeta: (meta) => {
          assistant.meta = meta
        },
        onContent: (chunk) => {
          assistant.content += chunk
          void scrollToBottom()
        },
        onWorkOrder: (order) => {
          assistant.workOrders?.push(order)
          void scrollToBottom()
        },
        onDone: (elapsed) => {
          assistant.elapsedMs = elapsed
        },
      },
    )
  } catch (error) {
    assistant.content = `请求失败：${(error as Error).message}`
  } finally {
    sending.value = false
    await scrollToBottom()
  }
}

async function refreshOrders() {
  for (const order of myWorkOrders.value) {
    try {
      const latest = await getWorkOrder(order.work_order_id)
      order.status = latest.status
      order.on_time = latest.on_time
      order.guest_notified = latest.guest_notified
    } catch {
      /* 单条失败不影响其他 */
    }
  }
  ElMessage.success('已刷新工单进度')
}

const quickAsks = [
  '早餐是几点？',
  '能送两瓶矿泉水吗？',
  '空调不制冷',
  '卫生间漏水了',
  '我要投诉隔壁太吵',
]
</script>

<template>
  <!-- 未进入房间时：整页引导。真实场景是扫房间二维码（二维码里带门店与房号），
       这里把参数放到界面上，方便本地演示与评测切换门店。 -->
  <div v-if="!session" class="entry-page">
    <el-card class="entry-card" shadow="always">
      <div class="entry-logo">随</div>
      <h2>扫码进入房间</h2>
      <p class="entry-hint">
        真实场景里，客人扫的是<strong>房间里的二维码</strong>——二维码里带着门店与房间号，
        这一步会顺带把客人定位到"入住中"。本地演示时手动选一下即可。
      </p>

      <el-form label-position="top" class="entry-form">
        <el-form-item label="门店">
          <el-select v-model="storeId" size="large" style="width: 100%" @change="onStoreChange">
            <el-option v-for="s in STORES" :key="s.id" :label="s.name" :value="s.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="房间号">
          <el-input v-model="roomNo" size="large" placeholder="例如 1208" />
        </el-form-item>
      </el-form>

      <el-button type="primary" size="large" class="entry-button" @click="enter">
        进入房间
      </el-button>

      <div class="entry-tips">
        <div class="entry-tips-title">进入后可以直接说需求，例如：</div>
        <div class="entry-tips-list">
          <el-tag v-for="ask in quickAsks" :key="ask" size="small" type="info">{{ ask }}</el-tag>
        </div>
        <div class="entry-tips-note">
          涉及您房间里的事（送物、报修）会先确认身份：房号 + 姓氏 + 离店日期。
        </div>
      </div>
    </el-card>
  </div>

  <div v-else class="guest-layout">
    <el-card class="chat-card" shadow="never">
      <template #header>
        <div class="card-header">
          <span>
            {{ STORES.find((s) => s.id === storeId)?.name }} · {{ roomNo }} 房
          </span>
          <div class="enter-area">
            <el-tag size="small" type="success">已进入</el-tag>
            <el-button text type="primary" @click="leave">重新扫码</el-button>
          </div>
        </div>
      </template>

      <div ref="listRef" class="messages">
        <el-empty v-if="!messages.length" description="说一句话试试，例如：能送两瓶矿泉水吗？" />
        <div v-for="(message, index) in messages" :key="index" :class="['msg', message.role]">
          <div v-if="message.role === 'system'" class="system-bubble">{{ message.content }}</div>
          <template v-else>
            <div class="bubble">
              <div v-if="message.meta" class="meta-row">
                <el-tag size="small" type="info">{{ message.meta.agent || '-' }}</el-tag>
                <el-tag v-if="message.meta.cached" size="small" type="success">缓存直返</el-tag>
                <el-tag v-if="message.meta.auth_required" size="small" type="warning">需要身份确认</el-tag>
                <el-tag v-if="message.meta.work_order_fallback" size="small" type="danger">兜底建单</el-tag>
                <span v-if="message.elapsedMs" class="elapsed">{{ message.elapsedMs }} ms</span>
              </div>
              <div v-if="message.role === 'assistant'" v-html="renderMarkdown(message.content)" />
              <div v-else>{{ message.content }}</div>
            </div>
          </template>
          <div v-for="order in message.workOrders" :key="order.work_order_id" class="order-card">
            <div class="order-title">
              <span>工单 {{ order.work_order_id }}</span>
              <el-tag size="small" :type="order.status === 'DONE' ? 'success' : 'primary'">
                {{ statusLabels[order.status] || order.status }}
              </el-tag>
            </div>
            <div class="order-body">
              <div>类别：{{ categoryLabels[order.category] || order.category }}</div>
              <div>承接：{{ deptLabels[order.assignee_dept] || order.assignee_dept }}</div>
              <div>承诺时限：{{ order.sla_minutes }} 分钟</div>
              <div v-if="order.due_at">截止：{{ new Date(order.due_at).toLocaleString() }}</div>
            </div>
          </div>
        </div>
      </div>

      <template #footer>
        <div class="quick-row">
          <el-tag
            v-for="ask in quickAsks"
            :key="ask"
            class="quick-tag"
            size="small"
            @click="input = ask"
          >
            {{ ask }}
          </el-tag>
        </div>
        <div class="input-row">
          <el-input
            v-model="input"
            placeholder="直接说您要办的事，例如：能送两瓶矿泉水吗？"
            :disabled="sending"
            @keyup.enter="send"
          />
          <el-button type="primary" :loading="sending" @click="send">发送</el-button>
        </div>
      </template>
    </el-card>

    <el-card class="side-card" shadow="never">
      <template #header>
        <div class="card-header">
          <span>我的工单</span>
          <el-button text type="primary" size="small" @click="refreshOrders">刷新进度</el-button>
        </div>
      </template>
      <el-empty v-if="!myWorkOrders.length" description="还没有工单" :image-size="60" />
      <div v-for="order in myWorkOrders" :key="order.work_order_id" class="side-order">
        <div class="side-order-id">{{ order.work_order_id }}</div>
        <div class="side-order-line">
          {{ categoryLabels[order.category] || order.category }} ·
          {{ deptLabels[order.assignee_dept] || order.assignee_dept }}
        </div>
        <div class="side-order-line">
          <el-tag size="small" :type="order.status === 'DONE' ? 'success' : 'primary'">
            {{ statusLabels[order.status] || order.status }}
          </el-tag>
          <span v-if="order.on_time === true" class="on-time">按时完成</span>
          <span v-if="order.guest_notified" class="notified">已回执</span>
        </div>
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.entry-page {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
}

.entry-card {
  width: 560px;
  max-width: 92vw;
  padding: 8px 12px 4px;
  text-align: center;
}

.entry-logo {
  width: 52px;
  height: 52px;
  margin: 0 auto 12px;
  border-radius: 14px;
  background: linear-gradient(135deg, #3f7afe, #6aa1ff);
  color: #fff;
  font-size: 26px;
  font-weight: 700;
  display: flex;
  align-items: center;
  justify-content: center;
}

.entry-card h2 {
  font-size: 20px;
  font-weight: 600;
  margin-bottom: 8px;
}

.entry-hint {
  font-size: 13px;
  color: #606266;
  line-height: 1.7;
  margin-bottom: 20px;
}

.entry-form {
  text-align: left;
}

.entry-button {
  width: 100%;
  margin-top: 4px;
}

.entry-tips {
  margin-top: 18px;
  padding-top: 14px;
  border-top: 1px dashed #e4e7ed;
  text-align: left;
}

.entry-tips-title {
  font-size: 13px;
  color: #909399;
  margin-bottom: 8px;
}

.entry-tips-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.entry-tips-note {
  margin-top: 10px;
  font-size: 12px;
  color: #a8abb2;
}

.guest-layout {
  display: grid;
  grid-template-columns: 1fr 320px;
  gap: 16px;
  height: 100%;
}

.chat-card,
.side-card {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.enter-area {
  display: flex;
  gap: 8px;
}

.messages {
  flex: 1;
  overflow-y: auto;
  padding: 4px 2px;
}

.msg {
  display: flex;
  flex-direction: column;
  margin-bottom: 14px;
}

.msg.user {
  align-items: flex-end;
}

.bubble {
  max-width: 78%;
  padding: 10px 14px;
  border-radius: 10px;
  background: #f4f6f9;
  line-height: 1.6;
  font-size: 14px;
}

.msg.user .bubble {
  background: #ecf2ff;
}

.system-bubble {
  align-self: center;
  max-width: 90%;
  padding: 8px 14px;
  border-radius: 8px;
  background: #fff8e6;
  color: #8a6d3b;
  font-size: 13px;
  white-space: pre-wrap;
}

.meta-row {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
}

.elapsed {
  font-size: 12px;
  color: #a8abb2;
}

.order-card {
  margin-top: 8px;
  padding: 12px 14px;
  border: 1px solid #c6e2ff;
  background: #f2f8ff;
  border-radius: 10px;
  max-width: 78%;
}

.order-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-weight: 600;
  margin-bottom: 6px;
}

.order-body {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 4px 12px;
  font-size: 13px;
  color: #4b5563;
}

.quick-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
}

.quick-tag {
  cursor: pointer;
}

.input-row {
  display: flex;
  gap: 8px;
}

.side-order {
  padding: 10px 0;
  border-bottom: 1px dashed #e4e7ed;
}

.side-order-id {
  font-family: Consolas, Monaco, monospace;
  font-size: 13px;
  font-weight: 600;
}

.side-order-line {
  font-size: 12px;
  color: #606266;
  margin-top: 4px;
  display: flex;
  align-items: center;
  gap: 8px;
}

.on-time {
  color: #67c23a;
}

.notified {
  color: #909399;
}

/*
 * 响应式：住客端的真实入口是**扫房间二维码 → 用手机打开**，
 * 所以这里不是"顺手适配一下"，而是**主场景**。
 * 窄屏下：两列变一列、"我的工单"挪到对话下方、气泡放宽到 88%。
 */
@media (max-width: 900px) {
  .guest-layout {
    grid-template-columns: 1fr;
    grid-template-rows: minmax(0, 1fr) auto;
    gap: 12px;
  }

  .side-card {
    max-height: 34vh;
    overflow-y: auto;
  }

  .bubble,
  .order-card {
    max-width: 88%;
  }

  .entry-card {
    padding: 4px 4px 0;
  }

  .card-header {
    flex-wrap: wrap;
    gap: 6px;
  }
}
</style>
