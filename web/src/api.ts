/**
 * 后端契约的薄封装。
 *
 * 单独抽出来的理由：住客端与坐席台用的是**同一批接口**，
 * 如果各自拼 URL，改后端时就会漏改一处（这个项目里已经踩过"两套口径"的坑）。
 */

const BASE = (import.meta as unknown as { env?: Record<string, string> }).env?.VITE_API_BASE
  || 'http://127.0.0.1:5000'

export interface SessionInfo {
  session_id: string
  store_id: string
  room_no?: string | null
  stage: string
  auth_level: number
  need_auth: boolean
  note?: string
}

export interface WorkOrder {
  work_order_id: string
  category: string
  status: string
  assignee_dept: string
  priority?: string
  sla_minutes: number
  due_at?: string | null
  room_no?: string | null
  on_time?: boolean | null
  guest_notified?: boolean
  created_at?: string
  fallback?: boolean
  intent?: string
}

export interface WorkOrderEvent {
  event_type: string
  from?: string | null
  to?: string | null
  actor?: string | null
  note?: string | null
  at?: string | null
}

/** 建立会话（真实场景由"扫房间二维码"触发，二维码里带 store_id 与 room_no）。 */
export async function createSession(payload: {
  store_id: string
  room_no?: string
  order_id?: string
  session_id?: string
}): Promise<SessionInfo> {
  const resp = await fetch(`${BASE}/api/session`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  if (!resp.ok) throw new Error(`建立会话失败：HTTP ${resp.status}`)
  return resp.json()
}

export interface ChatHandlers {
  onMeta?: (meta: Record<string, unknown>) => void
  onContent?: (chunk: string) => void
  onWorkOrder?: (order: WorkOrder) => void
  onDone?: (elapsedMs: number) => void
}

/**
 * 发一轮对话并消费 SSE。
 *
 * 为什么不用 EventSource：我们需要 POST（要带 session_id / store_id），
 * 而 EventSource 只支持 GET。所以用 fetch + 流式读取，手动按 `data: ` 切帧。
 */
export async function sendChat(
  payload: { query: string; session_id: string; store_id: string; user_id?: string },
  handlers: ChatHandlers,
): Promise<void> {
  const resp = await fetch(`${BASE}/api/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ user_id: 'user_1001', ...payload }),
  })
  if (!resp.ok || !resp.body) throw new Error(`对话请求失败：HTTP ${resp.status}`)

  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const frames = buffer.split('\n\n')
    buffer = frames.pop() ?? ''
    for (const frame of frames) {
      const line = frame.trim()
      if (!line.startsWith('data: ')) continue
      const event = JSON.parse(line.slice(6))
      if (event.meta) handlers.onMeta?.(event.meta)
      else if (event.content) handlers.onContent?.(event.content)
      else if (event.work_order) handlers.onWorkOrder?.(event.work_order)
      else if (event.done) handlers.onDone?.(event.elapsed_ms)
    }
  }
}

export async function listWorkOrders(params: {
  store_id?: string
  status?: string
  dept?: string
  limit?: number
}): Promise<{ count: number; items: WorkOrder[] }> {
  const query = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') query.append(key, String(value))
  })
  const resp = await fetch(`${BASE}/api/staff/work-orders?${query}`)
  if (!resp.ok) throw new Error(`读取工单失败：HTTP ${resp.status}`)
  return resp.json()
}

export async function getWorkOrder(id: string): Promise<WorkOrder & { events: WorkOrderEvent[] }> {
  const resp = await fetch(`${BASE}/api/staff/work-orders/${id}`)
  if (!resp.ok) throw new Error(`读取工单详情失败：HTTP ${resp.status}`)
  return resp.json()
}

export async function transitionWorkOrder(
  id: string,
  action: 'accept' | 'done' | 'fail' | 'escalate' | 'notify',
  body: { actor?: string; note?: string; assignee_name?: string } = {},
): Promise<Record<string, unknown>> {
  const resp = await fetch(`${BASE}/api/staff/work-orders/${id}/${action}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ actor: 'staff-web', ...body }),
  })
  const payload = await resp.json().catch(() => ({}))
  if (!resp.ok) {
    // 后端对非法流转返回 409，这里把原文交给界面显示（不要吞掉理由）
    throw new Error((payload as { detail?: string }).detail || `操作失败：HTTP ${resp.status}`)
  }
  return payload
}

export async function getMetrics(storeId?: string): Promise<Record<string, unknown>> {
  const query = storeId ? `?store_id=${storeId}` : ''
  const resp = await fetch(`${BASE}/api/metrics${query}`)
  if (!resp.ok) throw new Error(`读取看板失败：HTTP ${resp.status}`)
  return resp.json()
}

export { BASE }
