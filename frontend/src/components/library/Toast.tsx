// Tiny toast primitive for the writing-companion UI.
//
// One-line API: ``showToast("Chicago citation copied")`` — auto-dismisses
// after 1.6s. We deliberately don't pull in a toast library — a 30-line
// home-grown one is sufficient for the "Copy → toast" affordance.
//
// Implementation: a single global subscriber list. The ``<ToastHost>``
// component renders inside <WriteShell> and listens; any caller can
// emit by importing ``showToast``.

import { useEffect, useState } from 'react'

interface ToastEntry {
  id: number
  text: string
}

type Listener = (msgs: ToastEntry[]) => void

const listeners = new Set<Listener>()
let queue: ToastEntry[] = []
let nextId = 1

/** Emit a transient toast. Auto-dismisses after 1.6s. */
export function showToast(text: string): void {
  if (!text) return
  const id = nextId++
  queue = [...queue, { id, text }]
  listeners.forEach((fn) => fn(queue))
  window.setTimeout(() => {
    queue = queue.filter((t) => t.id !== id)
    listeners.forEach((fn) => fn(queue))
  }, 1600)
}

/** Mount once at the app shell. Renders a fixed bottom-right stack. */
export function ToastHost() {
  const [items, setItems] = useState<ToastEntry[]>([])
  useEffect(() => {
    const fn: Listener = (msgs) => setItems([...msgs])
    listeners.add(fn)
    return () => {
      listeners.delete(fn)
    }
  }, [])
  if (items.length === 0) return null
  return (
    <div className="fixed bottom-6 right-6 z-50 flex flex-col gap-2 pointer-events-none">
      {items.map((t) => (
        <div
          key={t.id}
          className="bg-ink text-surface-card text-xs px-3 py-2 rounded-md shadow-panel pointer-events-auto"
          role="status"
        >
          {t.text}
        </div>
      ))}
    </div>
  )
}
