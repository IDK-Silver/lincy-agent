<script setup lang="ts">
import { onMounted, onUnmounted, ref } from 'vue'
import { fetchClaudeAccounts, type ClaudeAccountsResponse } from '@/api/client'

const REFRESH_MS = 60_000

const data = ref<ClaudeAccountsResponse | null>(null)
const loading = ref(true)
let timer: number | undefined

async function refresh() {
  try {
    data.value = await fetchClaudeAccounts()
  } catch {
    data.value = { available: false, accounts: [], models: [], error: 'request failed' }
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  refresh()
  timer = window.setInterval(refresh, REFRESH_MS)
})

onUnmounted(() => {
  if (timer !== undefined) window.clearInterval(timer)
})

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
  if (!withDate) return `resets ${hhmm}`
  return `resets ${String(d.getMonth() + 1).padStart(2, '0')}/${String(d.getDate()).padStart(2, '0')} ${hhmm}`
}

function planLabel(tier: string | null | undefined, planType: string | null | undefined): string {
  const raw = tier ?? planType
  if (!raw) return ''
  return raw
    .replace(/^default_/, '')
    .split('_')
    .map((part) => (part ? part[0].toUpperCase() + part.slice(1) : part))
    .join(' ')
}
</script>

<template>
  <div class="border border-[#E5E7EB] rounded-lg p-4 shadow-[0_1px_2px_rgba(0,0,0,0.04)]">
    <div class="text-sm font-medium text-[#111827] mb-3">Claude Accounts</div>

    <div v-if="loading" class="text-xs text-[#6B7280]">Loading…</div>
    <div v-else-if="!data || !data.available" class="text-xs text-[#6B7280]">
      claude-code-proxy unavailable{{ data?.error ? ` — ${data.error}` : '' }}
    </div>
    <div v-else-if="data.accounts.length === 0" class="text-xs text-[#6B7280]">
      No Claude accounts in the proxy token store.
    </div>

    <div v-else class="space-y-4">
      <div
        v-for="acct in data.accounts"
        :key="acct.id"
        class="flex flex-col gap-2"
      >
        <div class="flex items-center gap-2 min-w-0">
          <span
            class="inline-block h-2 w-2 rounded-full shrink-0"
            :style="{ backgroundColor: STATUS_DOT[acct.status] ?? '#D1D5DB' }"
          />
          <span class="text-sm font-medium text-[#111827] truncate">
            {{ acct.account?.email ?? acct.id }}
          </span>
          <span
            v-if="planLabel(acct.account?.rate_limit_tier, acct.account?.plan_type)"
            class="text-[10px] text-[#6B7280] border border-[#E5E7EB] rounded px-1.5 py-0.5 shrink-0"
          >
            {{ planLabel(acct.account?.rate_limit_tier, acct.account?.plan_type) }}
          </span>
          <span class="text-[10px] text-[#6B7280] shrink-0 ml-auto uppercase tracking-wide">
            {{ acct.status }}
          </span>
        </div>

        <div v-if="acct.usage" class="grid grid-cols-2 gap-4">
          <div v-for="win in [
            { label: '5h', data: acct.usage.five_hour, withDate: false },
            { label: 'Week', data: acct.usage.seven_day, withDate: true },
          ]" :key="win.label">
            <div class="flex items-baseline justify-between mb-1">
              <span class="text-[10px] text-[#6B7280] uppercase tracking-wide">{{ win.label }}</span>
              <span class="text-xs text-[#111827] tabular-nums font-medium">
                {{ formatUtilization(win.data?.utilization) }}
              </span>
            </div>
            <div class="h-1.5 rounded-full bg-[#F3F4F6] overflow-hidden">
              <div
                class="h-full rounded-full"
                :style="{
                  width: barWidth(win.data?.utilization),
                  backgroundColor: barColor(win.data?.utilization),
                }"
              />
            </div>
            <div class="text-[10px] text-[#6B7280] mt-1 tabular-nums">
              {{ formatReset(win.data?.resets_at, win.withDate) }}
            </div>
          </div>
        </div>

        <div
          v-if="acct.error"
          class="text-xs"
          :class="acct.usage ? 'text-[#6B7280]' : 'text-[#EF4444]'"
        >
          {{ acct.stale ? 'stale — ' : '' }}{{ acct.error }}
        </div>
      </div>

      <div v-if="data.models.length" class="pt-3 border-t border-[#E5E7EB]">
        <div class="text-[10px] text-[#6B7280] uppercase tracking-wide mb-2">Models</div>
        <div class="flex flex-wrap gap-1.5">
          <span
            v-for="model in data.models"
            :key="model.id"
            class="text-[11px] text-[#111827] border border-[#E5E7EB] rounded px-1.5 py-0.5 font-mono"
            :title="model.display_name ?? model.id"
          >
            {{ model.id }}
          </span>
        </div>
      </div>
    </div>
  </div>
</template>
