import { computed, ref } from 'vue'
import { defineStore } from 'pinia'
import { fetchChatEvents, sendChatMessage, type WebChatEvent } from '@/api/client'

export const useChatStore = defineStore('chat', () => {
  const events = ref<WebChatEvent[]>([])
  const loading = ref(false)
  const sending = ref(false)
  const error = ref('')

  const messageEvents = computed(() =>
    events.value.filter((event) => event.kind === 'message' && event.content)
  )
  const latestStatus = computed(() => {
    for (let i = events.value.length - 1; i >= 0; i -= 1) {
      const status = events.value[i].status
      if (status) return status
    }
    return 'idle'
  })
  const processing = computed(() =>
    latestStatus.value === 'queued' || latestStatus.value === 'processing'
  )

  function addEvent(event: WebChatEvent) {
    if (events.value.some((existing) => existing.id === event.id)) return
    events.value = [...events.value, event].sort((a, b) =>
      a.created_at.localeCompare(b.created_at)
    )
  }

  function update(msg: Record<string, unknown>) {
    if (msg.type !== 'chat_event') return
    const event = msg.event as WebChatEvent | undefined
    if (!event?.id) return
    addEvent(event)
  }

  async function load(limit = 200) {
    loading.value = true
    error.value = ''
    try {
      const data = await fetchChatEvents(limit)
      events.value = data.events || []
    } catch (err) {
      error.value = err instanceof Error ? err.message : 'failed to load chat'
    } finally {
      loading.value = false
    }
  }

  async function send(content: string): Promise<boolean> {
    const text = content.trim()
    if (!text) return false
    sending.value = true
    error.value = ''
    try {
      const data = await sendChatMessage(text)
      addEvent(data.event)
      return true
    } catch (err) {
      error.value = err instanceof Error ? err.message : 'failed to send message'
      return false
    } finally {
      sending.value = false
    }
  }

  return {
    events,
    messageEvents,
    latestStatus,
    processing,
    loading,
    sending,
    error,
    addEvent,
    update,
    load,
    send,
  }
})
