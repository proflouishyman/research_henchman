// Collapsible tier section wrapping a list of SourceCards.

import { useState } from 'react'
import { ChevronRight } from 'lucide-react'
import type { DossierEntry } from '../../types/library'
import { SourceCard } from './SourceCard'

interface Props {
  bucket: string  // "3"|"2"|"1"|"0"|"unscored"
  heading: string
  entries: DossierEntry[]
  defaultOpen: boolean
  /** Compact rendering for tier 0/1 (single-line summary). */
  compact?: boolean
}

export function TierSection({ bucket, heading, entries, defaultOpen, compact }: Props) {
  const [open, setOpen] = useState(defaultOpen)

  // Tier 0 (search false positives) gets a muted visual treatment to signal
  // de-emphasis without hiding the section.
  const isDeemphasized = bucket === '0'

  return (
    <section className={`bg-surface-card border border-border rounded-md${isDeemphasized ? ' opacity-70' : ''}`}>
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center justify-between px-4 py-2.5 text-left hover:bg-surface-muted transition-colors"
      >
        <div className="flex items-center gap-2">
          <ChevronRight
            size={14}
            className={`transition-transform ${open ? 'rotate-90' : ''} text-ink-muted`}
          />
          <span className="font-medium text-sm text-ink">{heading}</span>
        </div>
        <span className="text-xs text-ink-muted">
          {entries.length} {entries.length === 1 ? 'entry' : 'entries'}
        </span>
      </button>

      {open && (
        <div className="border-t border-border p-3 space-y-2">
          {entries.length === 0 ? (
            <p className="text-xs text-ink-muted px-1 py-2">No entries.</p>
          ) : (
            entries.map((entry) => (
              <SourceCard key={`${bucket}-${entry.id}`} entry={entry} compact={compact} />
            ))
          )}
        </div>
      )}
    </section>
  )
}
