// SSE hook: subscribes to an EventSource stream and fires a callback per message.
// Falls back gracefully if SSE is unavailable or the connection drops.

import { useEffect, useRef, useCallback } from 'react'
import type { Event as PipelineEvent } from '../types/contracts'

interface UseSSEOptions {
  /** Called for each parsed event received from the stream. */
  onEvent: (event: PipelineEvent) => void
  /** Called when the stream closes or errors. */
  onClose?: () => void
  /** Whether the hook should actually connect. */
  enabled?: boolean
}

/**
 * Opens an SSE connection to the given URL and dispatches parsed JSON events
 * to `onEvent`. The connection is torn down when `enabled` goes false or the
 * component unmounts.
 */
export function useSSE(url: string, { onEvent, onClose, enabled = true }: UseSSEOptions): void {
  // Keep stable refs so effect doesn't need them in the dep array
  const onEventRef = useRef(onEvent)
  const onCloseRef = useRef(onClose)
  onEventRef.current = onEvent
  onCloseRef.current = onClose

  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const esRef = useRef<EventSource | null>(null)

  const connect = useCallback(() => {
    if (esRef.current) {
      esRef.current.close()
      esRef.current = null
    }

    const es = new EventSource(url)
    esRef.current = es

    es.onmessage = (e) => {
      try {
        const parsed = JSON.parse(e.data) as PipelineEvent
        onEventRef.current(parsed)
      } catch {
        // Non-JSON keep-alive pings — ignore
      }
    }

    es.onerror = () => {
      es.close()
      esRef.current = null
      onCloseRef.current?.()
      // Attempt reconnect after 4 s if still enabled
      reconnectTimer.current = setTimeout(() => {
        if (enabled) connect()
      }, 4000)
    }
  }, [url, enabled])

  useEffect(() => {
    if (!enabled) return

    connect()

    return () => {
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current)
      esRef.current?.close()
      esRef.current = null
    }
  }, [enabled, connect])
}
