// Scrollable event feed with collapsible header.

import { useRef, useEffect, useState } from 'react'
import { ChevronDown, ChevronRight, Terminal } from 'lucide-react'
import type { Event } from '../../types/contracts'

interface EventLogProps {
  events: Event[]
  /** Auto-expand when the run is active */
  autoExpand?: boolean
}

function eventColor(status: string): string {
  switch (status) {
    case 'complete':
    case 'ok':
      return 'text-emerald-600'
    case 'failed':
    case 'error':
      return 'text-red-500'
    case 'warn':
    case 'warning':
      return 'text-amber-600'
    case 'start':
    case 'begin':
      return 'text-blue-500'
    default:
      return 'text-ink-secondary'
  }
}

function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' })
  } catch {
    return ''
  }
}

export function EventLog({ events, autoExpand = false }: EventLogProps) {
  const [expanded, setExpanded] = useState(autoExpand)
  const scrollRef = useRef<HTMLDivElement>(null)

  // Auto-scroll to bottom on new events
  useEffect(() => {
    if (expanded && scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
  }, [events, expanded])

  // Auto-expand when autoExpand prop changes to true
  useEffect(() => {
    if (autoExpand) setExpanded(true)
  }, [autoExpand])

  return (
    <div className="border border-border rounded-lg overflow-hidden">
      {/* Collapsible header */}
      <button
        onClick={() => setExpanded((e) => !e)}
        className="w-full flex items-center justify-between px-4 py-2.5 bg-surface-muted hover:bg-border/50 transition-colors"
      >
        <div className="flex items-center gap-2">
          <Terminal size={13} className="text-ink-muted" />
          <span className="text-xs font-semibold text-ink-secondary">Event Log</span>
          <span className="text-[10px] text-ink-muted bg-surface-card border border-border px-1.5 py-0.5 rounded">
            {events.length} events
          </span>
        </div>
        {expanded ? (
          <ChevronDown size={13} className="text-ink-muted" />
        ) : (
          <ChevronRight size={13} className="text-ink-muted" />
        )}
      </button>

      {/* Log body */}
      {expanded && (
        <div
          ref={scrollRef}
          className="max-h-64 overflow-y-auto bg-gray-950 font-mono"
        >
          {events.length === 0 && (
            <p className="text-[11px] text-gray-500 px-4 py-3">Waiting for events…</p>
          )}
          {events.map((e) => (
            <div key={e.event_id} className="flex gap-3 px-4 py-1 hover:bg-white/5 group">
              <span className="text-[10px] text-gray-500 shrink-0 pt-px tabular-nums">
                {formatTime(e.ts_utc)}
              </span>
              <span className={`text-[10px] shrink-0 uppercase font-medium ${eventColor(e.status)}`}>
                {e.stage}
              </span>
              <span className="text-[11px] text-gray-300 leading-relaxed">{e.message}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
