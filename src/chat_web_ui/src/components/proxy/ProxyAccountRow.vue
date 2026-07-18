<script setup lang="ts">
import { ArrowUp, X } from 'lucide-vue-next'

interface UsageRow {
  key: string
  label: string
  utilization: number | null | undefined
  resetsAt: string | null | undefined
  withDate: boolean
}

defineProps<{
  name: string
  title: string
  status: string
  plan: string
  rows: UsageRow[]
  error: string | null
  stale: boolean
  canPromote: boolean
  canRemove: boolean
  busy: boolean
}>()

const emit = defineEmits<{
  promote: []
  remove: []
}>()

const STATUS_DOT: Record<string, string> = {
  active: '#22C55E',
  standby: '#D1D5DB',
  benched: '#F59E0B',
  unusable: '#EF4444',
}

function barColor(pct: number | null | undefined): string {
  if (pct == null) return '#D1D5DB'
  if (pct >= 90) return '#EF4444'
  if (pct >= 70) return '#F59E0B'
  return '#111827'
}

function barWidth(pct: number | null | undefined): string {
  if (pct == null) return '0%'
  return `${Math.min(Math.max(pct, 0), 100)}%`
}

function formatUtilization(pct: number | null | undefined): string {
  if (pct == null) return '—'
  return `${Math.round(pct)}%`
}

function formatReset(iso: string | null | undefined, withDate: boolean): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const hhmm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  if (!withDate) return hhmm
  return `${String(d.getMonth() + 1).padStart(2, '0')}/${String(d.getDate()).padStart(2, '0')} ${hhmm}`
}
</script>

<template>
  <div class="flex flex-col gap-2 py-3 first:pt-0 last:pb-0">
    <div class="flex items-center gap-2 min-w-0">
      <span
        class="inline-block h-2 w-2 rounded-full shrink-0"
        :style="{ backgroundColor: STATUS_DOT[status] ?? '#D1D5DB' }"
        :title="status"
      />
      <span
        class="text-sm font-medium text-[#111827] truncate flex-1 min-w-0"
        :title="title"
      >
        {{ name }}
      </span>
      <span
        v-if="plan"
        class="text-[10px] text-[#6B7280] border border-[#E5E7EB] rounded px-1.5 py-0.5 shrink-0"
      >
        {{ plan }}
      </span>
      <button
        v-if="canPromote"
        type="button"
        class="p-1 rounded border border-[#E5E7EB] text-[#6B7280] hover:text-[#111827] hover:border-[#D1D5DB] disabled:opacity-50 shrink-0"
        :disabled="busy"
        title="Make this the highest-priority account"
        @click="emit('promote')"
      >
        <ArrowUp class="h-3 w-3" />
      </button>
      <button
        v-if="canRemove"
        type="button"
        class="p-1 rounded border border-[#E5E7EB] text-[#6B7280] hover:text-[#EF4444] hover:border-[#EF4444] disabled:opacity-50 shrink-0"
        :disabled="busy"
        title="Remove this account from the token store"
        @click="emit('remove')"
      >
        <X class="h-3 w-3" />
      </button>
    </div>

    <div v-if="rows.length" class="grid grid-cols-[auto_1fr_auto_auto] items-center gap-x-2 gap-y-1">
      <template v-for="row in rows" :key="row.key">
        <span class="text-[10px] text-[#6B7280] uppercase tracking-wide">{{ row.label }}</span>
        <div class="h-1.5 rounded-full bg-[#F3F4F6] overflow-hidden">
          <div
            class="h-full rounded-full"
            :style="{
              width: barWidth(row.utilization),
              backgroundColor: barColor(row.utilization),
            }"
          />
        </div>
        <span class="text-xs text-[#111827] tabular-nums font-medium text-right">
          {{ formatUtilization(row.utilization) }}
        </span>
        <span
          class="text-[10px] text-[#6B7280] tabular-nums whitespace-nowrap text-right"
          :title="'resets ' + formatReset(row.resetsAt, row.withDate)"
        >
          {{ formatReset(row.resetsAt, row.withDate) }}
        </span>
      </template>
    </div>

    <div
      v-if="error"
      class="text-xs"
      :class="rows.length ? 'text-[#6B7280]' : 'text-[#EF4444]'"
    >
      {{ stale ? 'stale — ' : '' }}{{ error }}
    </div>
  </div>
</template>
