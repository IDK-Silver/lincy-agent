<script setup lang="ts">
import { onMounted } from 'vue'
import DashboardLayout from '@/layouts/DashboardLayout.vue'
import { useWebSocketStore } from '@/stores/websocket'
import { useLiveStore } from '@/stores/live'
import { useDashboardStore } from '@/stores/dashboard'
import { useChatStore } from '@/stores/chat'

const wsStore = useWebSocketStore()
const liveStore = useLiveStore()
const dashStore = useDashboardStore()
const chatStore = useChatStore()

onMounted(() => {
  wsStore.connect()
  wsStore.onMessage((msg) => {
    liveStore.update(msg)
    chatStore.update(msg)
    if (msg.type === 'session_updated' || msg.type === 'session_created') {
      dashStore.refresh()
    }
  })
  liveStore.refresh()
})
</script>

<template>
  <DashboardLayout />
</template>
