// DossierSidePanel — slide-in panel that shows the gap dossier(s) for a
// clicked paragraph. Reuses DossierView internals via the same API call.
//
// Multiple gap_ids on one paragraph → tab strip at the top.
// ESC key closes. URL updates to /write/manuscript/:chapter/:paraId.

import { useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { X } from 'lucide-react'
import { fetchDossier } from '../../../lib/library_api'
import { useLibraryStore } from '../../../store/library'
import { TierSection } from '../TierSection'
import type { LibraryDossier, DossierEntry } from '../../../types/library'

interface Props {
  gapIds: string[]   // gap_ids for the selected paragraph
  paraId: string
  onClose: () => void
  /** B8: The manuscript paragraph text that was clicked, displayed at top in
   *  italics so the user confirms the source list applies to their passage. */
  paragraphText?: string
}

export function DossierSidePanel({ gapIds, paraId, onClose, paragraphText }: Props) {
  const { dossierFilters } = useLibraryStore()
  const [activeGapIdx, setActiveGapIdx] = useState(0)

  // Reset tab when paragraph changes.
  useEffect(() => {
    setActiveGapIdx(0)
  }, [paraId])

  // Close on Escape key.
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [onClose])

  const activeGapId = gapIds[activeGapIdx] ?? gapIds[0]

  const { data, isLoading, error } = useQuery({
    queryKey: ['dossier', activeGapId],
    queryFn: () => fetchDossier(activeGapId!),
    enabled: !!activeGapId,
    staleTime: 30000,
  })

  const filteredTiers = useMemo(() => {
    if (!data) return null
    return _filterTiers(data, dossierFilters.sourceIds, dossierFilters.scoreMin, dossierFilters.hasPdf)
  }, [data, dossierFilters])

  return (
    <aside className="h-full flex flex-col bg-surface-card border-l border-border">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-border shrink-0">
        <span className="text-xs font-semibold text-ink-muted uppercase tracking-wider">
          Dossier
        </span>
        <button onClick={onClose} className="text-ink-muted hover:text-ink transition-colors">
          <X size={14} />
        </button>
      </div>

      {/* Tab strip — one tab per gap_id */}
      {gapIds.length > 1 && (
        <div className="flex gap-1 px-3 py-2 border-b border-border shrink-0 overflow-x-auto">
          {gapIds.map((gid, idx) => (
            <button
              key={gid}
              onClick={() => setActiveGapIdx(idx)}
              className={`text-[10px] font-mono px-2 py-1 rounded border transition-colors ${
                idx === activeGapIdx
                  ? 'bg-accent-light text-accent border-accent/30 font-semibold'
                  : 'text-ink-muted border-border hover:bg-surface-muted'
              }`}
            >
              {gid}
            </button>
          ))}
        </div>
      )}

      {/* B8: Citing-this-passage context block — anchors the source list to the
          paragraph the user clicked. Shown when panel opens via paragraph click. */}
      {paragraphText && (
        <div className="px-4 py-3 border-b border-border bg-accent-light/30 shrink-0">
          <p className="text-[10px] font-semibold uppercase tracking-wider text-accent mb-1">
            Citing this passage:
          </p>
          <p className="text-xs italic text-ink-secondary leading-relaxed line-clamp-4">
            {paragraphText}
          </p>
        </div>
      )}

      {/* Dossier content */}
      <div className="flex-1 overflow-y-auto p-4 space-y-3">
        {isLoading && (
          <p className="text-xs text-ink-muted">Loading dossier for {activeGapId}…</p>
        )}
        {error && (
          <p className="text-xs text-status-blocked">Failed to load dossier.</p>
        )}
        {data && filteredTiers && (
          <>
            {/* Gap header */}
            <div className="border-b border-border pb-3 mb-3">
              <div className="flex items-center gap-1.5 mb-1 flex-wrap">
                <span className="font-mono text-xs font-semibold text-ink">{data.gap.gap_id}</span>
                {data.gap.gap_type && (
                  <span className="text-[10px] px-1.5 py-0.5 rounded border bg-emerald-50 text-emerald-700 border-emerald-200">
                    {data.gap.gap_type}
                  </span>
                )}
              </div>
              <p className="text-xs font-medium text-ink mb-0.5">{data.gap.chapter}</p>
              {data.gap.claim_text && (
                <p className="text-xs text-ink-secondary">{data.gap.claim_text}</p>
              )}
              {data.gap.research_question && (
                <blockquote className="mt-2 border-l-2 border-accent pl-2 text-xs text-ink-secondary italic">
                  {data.gap.research_question}
                </blockquote>
              )}
              <p className="mt-2 text-[10px] text-ink-muted">
                {data.summary.consolidated} sources ·{' '}
                <span className="text-emerald-600">tier 3: {data.summary.tier_counts['3'] ?? 0}</span>
              </p>
            </div>

            {/* B6: compact=true for all tiers — side panel is 384px wide. */}
            <TierSection bucket="3" heading="Tier 3 — cite-worthy" entries={filteredTiers['3']} defaultOpen compact />
            <TierSection bucket="2" heading="Tier 2 — adjacent" entries={filteredTiers['2']} defaultOpen={false} compact />
            <TierSection bucket="1" heading="Tier 1 — tangential" entries={filteredTiers['1']} defaultOpen={false} compact />
          </>
        )}
      </div>
    </aside>
  )
}

function _filterTiers(
  d: LibraryDossier,
  selectedSources: string[],
  scoreMin: number,
  hasPdf: boolean,
): Record<string, DossierEntry[]> {
  const out: Record<string, DossierEntry[]> = {}
  const sourceFilter = new Set(selectedSources)
  for (const [bucket, rows] of Object.entries(d.tiers)) {
    const numericBucket = bucket !== 'unscored' ? Number(bucket) : null
    if (numericBucket !== null && numericBucket < scoreMin) {
      out[bucket] = []
      continue
    }
    out[bucket] = rows.filter((entry) => {
      if (sourceFilter.size > 0) {
        const allSrc = [entry.source_id, ...entry.also_in_sources]
        if (!allSrc.some((s) => sourceFilter.has(s))) return false
      }
      if (hasPdf && !entry.pdf_path) return false
      return true
    })
  }
  return out
}
