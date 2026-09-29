<script setup lang="ts">
/**
 * 随行管家 · 前端外壳
 *
 * 两个视图，对应产品的两条链路（04-PRD §4.1 功能地图）：
 *   住客端  —— 客人扫码进房 → 一句话说需求 → 拿到「工单号 + 承接部门 + 承诺时限」
 *   坐席台  —— 一线员工看工单池 → 接单 / 完成 / 失败 / 升级，并回执给客人
 *
 * 两个视图共用同一套后端契约（05-TDD §7），所以这里只做切换，不放业务逻辑。
 */
import { ref } from 'vue'
import GuestChat from './views/GuestChat.vue'
import StaffDesk from './views/StaffDesk.vue'

const tab = ref<'guest' | 'staff'>('guest')
</script>

<template>
  <el-container class="shell">
    <el-header class="topbar">
      <div class="brand">
        <div class="logo">随</div>
        <div class="brand-text">
          <h1>随行管家</h1>
          <p>酒店住客全旅程智能服务台</p>
        </div>
      </div>
      <el-radio-group v-model="tab" size="large">
        <el-radio-button value="guest">住客端</el-radio-button>
        <el-radio-button value="staff">坐席台</el-radio-button>
      </el-radio-group>
      <div class="topbar-hint">本地演示环境 · 对话与工单都落在本地 PostgreSQL</div>
    </el-header>

    <el-main class="main">
      <GuestChat v-if="tab === 'guest'" />
      <StaffDesk v-else />
    </el-main>
  </el-container>
</template>

<style scoped>
.shell {
  height: 100vh;
  background: #f5f7fa;
}

.topbar {
  display: flex;
  align-items: center;
  gap: 24px;
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
  height: 72px;
}

.brand {
  display: flex;
  align-items: center;
  gap: 12px;
}

.logo {
  width: 40px;
  height: 40px;
  border-radius: 10px;
  background: linear-gradient(135deg, #3f7afe, #6aa1ff);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 20px;
  font-weight: 700;
}

.brand-text h1 {
  margin: 0;
  font-size: 18px;
  line-height: 1.2;
}

.brand-text p {
  margin: 2px 0 0;
  font-size: 12px;
  color: #909399;
}

.topbar-hint {
  margin-left: auto;
  font-size: 12px;
  color: #a8abb2;
}

.main {
  padding: 16px;
  height: calc(100vh - 72px);
  overflow: hidden;
}

/* 窄屏（住客用手机扫房间二维码进来）：收紧内边距、隐藏说明文字，把空间留给对话 */
@media (max-width: 900px) {
  .topbar {
    height: 60px;
    gap: 10px;
    padding: 0 12px;
  }

  .brand-text p,
  .topbar-hint {
    display: none;
  }

  .brand-text h1 {
    font-size: 16px;
  }

  .main {
    padding: 8px;
    height: calc(100vh - 60px);
  }
}
</style>
