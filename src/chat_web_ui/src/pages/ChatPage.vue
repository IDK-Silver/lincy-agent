<script setup lang="ts">
import { nextTick, onMounted, ref, watch } from 'vue'
import { Send } from 'lucide-vue-next'
import { useChatStore } from '@/stores/chat'
import type { WebChatEvent } from '@/api/client'

const chat = useChatStore()
const draft = ref('')
const messagesEl = ref<HTMLElement | null>(null)

function formatTime(value: string): string {
  return new Date(value).toLocaleTimeString('en', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}

function statusLabel(status: string): string {
  if (status === 'queued') return 'Queued'
  if (status === 'processing') return 'Processing'
  if (status === 'error') return 'Error'
  return 'Ready'
}

function statusClass(status: string): string {
  if (status === 'queued' || status === 'processing') return 'bg-[#111827]'
  if (status === 'error') return 'bg-[#EF4444]'
  return 'bg-[#22C55E]'
}

function isUser(event: WebChatEvent): boolean {
  return event.role === 'user'
}

function scrollToBottom() {
  const el = messagesEl.value
  if (!el) return
  el.scrollTop = el.scrollHeight
}

async function submit() {
  const ok = await chat.send(draft.value)
  if (!ok) return
  draft.value = ''
  await nextTick()
  scrollToBottom()
}

onMounted(async () => {
  await chat.load()
  await nextTick()
  scrollToBottom()
})

watch(
  () => chat.events.length,
  () => nextTick(scrollToBottom),
)
</script>

<template>
  <div class="mx-auto flex h-[calc(100vh-132px)] w-full max-w-4xl flex-col md:h-[calc(100vh-98px)]">
    <div class="flex items-center justify-between border-b border-[#E5E7EB] pb-3">
      <h1 class="text-base font-semibold text-[#111827]">Chat</h1>
      <div class="flex items-center gap-2 text-xs text-[#6B7280]">
        <span class="h-2 w-2 rounded-full" :class="statusClass(chat.latestStatus)" />
        <span>{{ statusLabel(chat.latestStatus) }}</span>
      </div>
    </div>

    <div
      ref="messagesEl"
      class="min-h-0 flex-1 overflow-y-auto py-4 pr-1"
    >
      <div v-if="chat.loading" class="py-10 text-center text-sm text-[#9CA3AF]">
        Loading
      </div>
      <div
        v-else-if="chat.messageEvents.length === 0"
        class="flex h-full items-center justify-center text-sm text-[#D1D5DB]"
      >
        No messages yet
      </div>
      <div v-else class="space-y-3">
        <div
          v-for="event in chat.messageEvents"
          :key="event.id"
          class="flex"
          :class="isUser(event) ? 'justify-end' : 'justify-start'"
        >
          <div
            class="max-w-[86%] rounded-lg px-3 py-2 text-sm leading-6 shadow-[0_1px_2px_rgba(0,0,0,0.04)] md:max-w-[72%]"
            :class="isUser(event)
              ? 'bg-[#111827] text-white'
              : 'border border-[#E5E7EB] bg-white text-[#111827]'"
          >
            <p class="whitespace-pre-wrap break-words">{{ event.content }}</p>
            <div
              class="mt-1 text-[11px] tabular-nums"
              :class="isUser(event) ? 'text-[#D1D5DB]' : 'text-[#9CA3AF]'"
            >
              {{ formatTime(event.created_at) }}
            </div>
          </div>
        </div>
      </div>
    </div>

    <div class="shrink-0 border-t border-[#E5E7EB] pt-3">
      <p v-if="chat.error" class="mb-2 text-xs text-[#EF4444]">{{ chat.error }}</p>
      <form class="flex items-end gap-2" @submit.prevent="submit">
        <textarea
          v-model="draft"
          rows="1"
          class="max-h-32 min-h-11 flex-1 resize-none rounded-md border border-[#D1D5DB] px-3 py-2 text-sm leading-6 text-[#111827] outline-none transition-colors placeholder:text-[#9CA3AF] focus:border-[#111827]"
          placeholder="Message Lincy"
          :disabled="chat.sending"
          @keydown.enter.exact.prevent="submit"
        />
        <button
          type="submit"
          title="Send"
          class="flex h-11 w-11 shrink-0 items-center justify-center rounded-md bg-[#111827] text-white transition-colors hover:bg-black disabled:cursor-not-allowed disabled:bg-[#D1D5DB]"
          :disabled="chat.sending || !draft.trim()"
        >
          <Send class="h-4 w-4" />
        </button>
      </form>
    </div>
  </div>
</template>
