<script setup lang="ts">
/**
 * 坐席台：一线员工的工单池。
 *
 * 设计原则来自 05-TDD §5.4：员工要一眼看清"要做什么、送到哪、什么时候要"，
 * 所以列表里**必须**有房间号、承诺时限、是否即将超时——而不是只有一个"客人有需求"。
 *
 * 动作只有五个：接单 / 完成 / 失败（必填原因）/ 升级 / 回执。
 * 刻意不做排班与绩效——那是酒店运营系统的事（01 不做清单第 9 条）。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  getMetrics,
  getWorkOrder,
  listWorkOrders,
  transitionWorkOrder,
  type WorkOrder,
  type WorkOrderEvent,
} from '../api'

const STORES = [
  { id: 'H_SH_001', name: '外滩璟悦酒店（上海）' },
  { id: 'H_SY_001', name: '三亚海棠湾度假酒店' },
]

const statusOptions = [
  { value: '', label: '全部状态' },
  { value: 'ASSIGNED', label: '已派单待接单' },
  { value: 'ACCEPTED', label: '处理中' },
  { value: 'ESCALATED', label: '超时已升级' },
  { value: 'DONE', label: '已完成' },
  { value: 'FAILED', label: '处理失败' },
]

const categoryLabels: Record<string, string> = {
  SERVICE: '客房服务',
  MAINTENANCE: '工程报修',
  MAINTENANCE_SAFETY: '安全类报修',
  COMPLAINT: '投诉',
  ESCALATION: '人工升级',
  CHECKOUT_REQUEST: '延退/换房',
  INVOICE: '发票申请',
  LOST_FOUND: '失物招领',
}

const statusLabels: Record<string, string> = {
  CREATED: '已创建',
  ASSIGNED: '待接单',
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

const storeId = ref(STORES[0]!.id)
const status = ref('')
const orders = ref<WorkOrder[]>([])
const loading = ref(false)
const metrics = ref<Record<string, any> | null>(null)
const detailVisible = ref(false)
const detail = ref<(WorkOrder & { events: WorkOrderEvent[] }) | null>(null)

let timer: number | undefined

const summary = computed(() => {
  const box = metrics.value?.work_orders as Record<string, any> | undefined
  return {
    工单总数: box?.['工单总数'] ?? 0,
    闭环率: box?.['闭环率'] == null ? '—' : `${Math.round(box['闭环率'] * 100)}%`,
    按时闭环率: box?.['按时闭环率'] == null ? '—' : `${Math.round(box['按时闭环率'] * 100)}%`,
    安全类: box?.['安全类工单'] ?? 0,
    鉴权通过率: (metrics.value?.auth as Record<string, any>)?.['通过率'] == null
      ? '—'
      : `${Math.round(((metrics.value?.auth as Record<string, any>)['通过率']) * 100)}%`,
  }
})

function overdueMinutes(order: WorkOrder): number | null {
  if (!order.due_at || order.status === 'DONE') return null
  const diff = (Date.now() - new Date(order.due_at).getTime()) / 60000
  return diff > 0 ? Math.floor(diff) : null
}

async function load() {
  loading.value = true
  try {
    const [list, board] = await Promise.all([
      listWorkOrders({ store_id: storeId.value, status: status.value, limit: 50 }),
      getMetrics(storeId.value),
    ])
    orders.value = list.items
    metrics.value = board
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    loading.value = false
  }
}

async function act(order: WorkOrder, action: 'accept' | 'done' | 'fail' | 'escalate' | 'notify') {
  try {
    let note: string | undefined
    if (action === 'fail') {
      // 失败原因本身就是评测数据（02-问题定义 P1 的失败归因），所以强制填写
      const result = await ElMessageBox.prompt('请填写失败原因（会进入失败归因统计）', '处理失败', {
        confirmButtonText: '提交',
        cancelButtonText: '取消',
        inputPlaceholder: '例如：布草间缺货',
      })
      note = result.value
    }
    await transitionWorkOrder(order.work_order_id, action, { note })
    ElMessage.success('已更新')
    await load()
    if (detailVisible.value) await openDetail(order.work_order_id)
  } catch (error) {
    if (error === 'cancel' || (error as Error)?.message === 'cancel') return
    ElMessage.error((error as Error).message)
  }
}

async function openDetail(id: string) {
  detail.value = await getWorkOrder(id)
  detailVisible.value = true
}

onMounted(() => {
  void load()
  timer = window.setInterval(() => void load(), 10000) // 10 秒轮询：演示环境够用，不引入 WebSocket
})
onUnmounted(() => {
  if (timer) window.clearInterval(timer)
})
</script>

<template>
  <div class="desk">
    <div class="stat-row">
      <el-card v-for="(value, key) in summary" :key="key" shadow="never" class="stat-card">
        <div class="stat-value">{{ value }}</div>
        <div class="stat-label">{{ key }}</div>
      </el-card>
    </div>

    <el-card shadow="never" class="table-card">
      <template #header>
        <div class="toolbar">
          <el-select v-model="storeId" style="width: 240px" @change="load">
            <el-option v-for="s in STORES" :key="s.id" :label="s.name" :value="s.id" />
          </el-select>
          <el-select v-model="status" style="width: 170px" @change="load">
            <el-option v-for="s in statusOptions" :key="s.value" :label="s.label" :value="s.value" />
          </el-select>
          <el-button :loading="loading" @click="load">刷新</el-button>
          <span class="hint">按承诺时限排序 · 每 10 秒自动刷新</span>
        </div>
      </template>

      <el-table :data="orders" height="100%" @row-click="(row: WorkOrder) => openDetail(row.work_order_id)">
        <el-table-column prop="work_order_id" label="工单号" width="180" />
        <el-table-column prop="room_no" label="房间" width="80" />
        <el-table-column label="类别" width="120">
          <template #default="{ row }">
            {{ categoryLabels[row.category] || row.category }}
          </template>
        </el-table-column>
        <el-table-column label="优先级" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="row.priority === 'P0' ? 'danger' : row.priority === 'P1' ? 'warning' : 'info'">
              {{ row.priority }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="intent" label="客人要办的事" min-width="200" show-overflow-tooltip />
        <el-table-column label="承接部门" width="110">
          <template #default="{ row }">{{ deptLabels[row.assignee_dept] || row.assignee_dept }}</template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag size="small" :type="row.status === 'DONE' ? 'success' : row.status === 'ESCALATED' ? 'danger' : 'primary'">
              {{ statusLabels[row.status] || row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="时限" width="120">
          <template #default="{ row }">
            <span v-if="overdueMinutes(row)" class="overdue">已超 {{ overdueMinutes(row) }} 分钟</span>
            <span v-else-if="row.due_at">{{ new Date(row.due_at).toLocaleTimeString() }} 前</span>
            <span v-else>—</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="260" fixed="right">
          <template #default="{ row }">
            <el-button v-if="row.status === 'ASSIGNED'" size="small" type="primary" @click.stop="act(row, 'accept')">
              接单
            </el-button>
            <el-button v-if="row.status === 'ACCEPTED'" size="small" type="success" @click.stop="act(row, 'done')">
              完成
            </el-button>
            <el-button v-if="row.status === 'ACCEPTED'" size="small" @click.stop="act(row, 'fail')">失败</el-button>
            <el-button v-if="row.status !== 'DONE'" size="small" type="warning" @click.stop="act(row, 'escalate')">
              升级
            </el-button>
            <el-button v-if="row.status === 'DONE'" size="small" @click.stop="act(row, 'notify')">回执</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-drawer v-model="detailVisible" title="工单详情与事件链" size="520px">
      <template v-if="detail">
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item label="工单号">{{ detail.work_order_id }}</el-descriptions-item>
          <el-descriptions-item label="房间">{{ detail.room_no || '—' }}</el-descriptions-item>
          <el-descriptions-item label="类别">{{ categoryLabels[detail.category] || detail.category }}</el-descriptions-item>
          <el-descriptions-item label="承接部门">{{ deptLabels[detail.assignee_dept] || detail.assignee_dept }}</el-descriptions-item>
          <el-descriptions-item label="状态">{{ statusLabels[detail.status] || detail.status }}</el-descriptions-item>
          <el-descriptions-item label="承诺时限">{{ detail.sla_minutes }} 分钟</el-descriptions-item>
          <el-descriptions-item label="按时完成">{{ detail.on_time === null ? '—' : detail.on_time ? '是' : '否' }}</el-descriptions-item>
          <el-descriptions-item label="已回执">{{ detail.guest_notified ? '是' : '否' }}</el-descriptions-item>
        </el-descriptions>

        <h4>事件链</h4>
        <el-timeline>
          <el-timeline-item
            v-for="(event, index) in detail.events"
            :key="index"
            :timestamp="event.at ? new Date(event.at).toLocaleString() : ''"
          >
            <b>{{ event.event_type }}</b>
            <span v-if="event.note"> · {{ event.note }}</span>
          </el-timeline-item>
        </el-timeline>
      </template>
    </el-drawer>
  </div>
</template>

<style scoped>
.desk {
  display: flex;
  flex-direction: column;
  gap: 12px;
  height: 100%;
}

.stat-row {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: 12px;
}

.stat-card :deep(.el-card__body) {
  padding: 12px 16px;
}

.stat-value {
  font-size: 22px;
  font-weight: 700;
  color: #303133;
}

.stat-label {
  font-size: 12px;
  color: #909399;
  margin-top: 4px;
}

.table-card {
  flex: 1;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}

.table-card :deep(.el-card__body) {
  flex: 1;
  overflow: hidden;
}

.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
}

.hint {
  margin-left: auto;
  font-size: 12px;
  color: #a8abb2;
}

.overdue {
  color: #f56c6c;
  font-weight: 600;
}

/* 响应式：坐席台是前台电脑上用的，所以窄屏只需"能用"——
   指标卡从 5 列降到 2 列，表格横向滚动（Element Plus 自带），动作按钮保持可点。 */
@media (max-width: 1200px) {
  .stat-row {
    grid-template-columns: repeat(3, 1fr);
  }
}

@media (max-width: 900px) {
  .stat-row {
    grid-template-columns: repeat(2, 1fr);
  }

  .toolbar {
    flex-wrap: wrap;
  }

  .hint {
    display: none;
  }
}
</style>
