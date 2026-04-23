// Event feed hook: SSE-first with polling fallback.
// Accumulates events in local state, deduplicating by event_id.

import { useState, useEffect, useRef, useCallback } from 'react'
import { useSSE } from '../lib/sse'
import { fetchEvents } from '../lib/api'
import type { Event } from '../types/contracts'

const ACTIVE_STATUSES = new Set<string>(['queued', 'analyzing', 'planning', 'pulling', 'ingesting', 'fitting'])

interface UseEventsResult {
  events: Event[]
  clearEvents: () => void
}

export function useEvents(runId: string | null, runStatus?: string): UseEventsResult {
  const [events, setEvents] = useState<Event[]>([])
  const seenIds = useRef(new Set<string>())

  // Track last polled time for deduplication
  const isActive = runStatus ? ACTIVE_STATUSES.has(runStatus) : false

  // Add events, deduplicating by event_id and keeping them sorted
  const addEvents = useCallback((incoming: Event[]) => {
    const fresh = incoming.filter((e) => !seenIds.current.has(e.event_id))
    if (fresh.length === 0) return
    fresh.forEach((e) => seenIds.current.add(e.event_id))
    setEvents((prev) => {
      const merged = [...prev, ...fresh]
      merged.sort((a, b) => a.ts_utc.localeCompare(b.ts_utc))
      return merged
    })
  }, [])

  // SSE stream — enabled while active
  useSSE(runId ? `/api/orchestrator/runs/${runId}/stream` : '', {
    enabled: !!runId && isActive,
    onEvent: (e) => addEvents([e]),
  })

  // Polling fallback: fetch event list when run exists but not streaming
  useEffect(() => {
    if (!runId) return
    if (isActive) return // SSE handles this

    // Single fetch for terminal states to populate historical events
    fetchEvents(runId)
      .then(addEvents)
      .catch(() => {})
  }, [runId, isActive, addEvents])

  // Also do an initial poll on mount / runId change so we load existing events
  useEffect(() => {
    if (!runId) return
    fetchEvents(runId)
      .then(addEvents)
      .catch(() => {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId])

  const clearEvents = useCallback(() => {
    setEvents([])
    seenIds.current.clear()
  }, [])

  // Reset when run changes
  useEffect(() => {
    setEvents([])
    seenIds.current.clear()
  }, [runId])

  return { events, clearEvents }
}
