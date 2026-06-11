// /write/queue — flat list of starred articles, grouped by gap.
//
// A2 fix: uses POST /api/library/articles/resolve_gaps to map article_ids →
// gap_ids server-side. The previous localStorage-based useArticleGapMap is
// replaced with a single API call on mount so "visit the dossier once" is
// never required. Articles with no resolved gap show under "(no gap mapping)".
//
// Phase 5 (cross-gap resolution): articleGapMap now stores the FULL list of
// gap_ids per article (element 0 = primary, used for grouping; elements 1+
// are "also appears in" gaps rendered as clickable chips next to each row).

import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQueries } from '@tanstack/react-query'
import { Star } from 'lucide-react'
import { fetchDossier, resolveGaps } from '../../lib/library_api'
import { useLibraryStore } from '../../store/library'
import type { DossierEntry } from '../../types/library'
import { SourceCard } from './SourceCard'

const NO_GAP = '_no_gap_mapping'

export function QueuePage() {
  const { marks } = useLibraryStore()
  const starredIds = useMemo(
    () =>
      Object.entries(marks)
        .filter(([, m]) => m.starred)
        .map(([id]) => Number(id)),
    [marks],
  )

  // A2: Server-resolved article_id → gap_id mapping via POST resolve_gaps.
  // Phase 5: stores the FULL gap list per article (element 0 = primary gap for
  // grouping; remaining elements are "also appears in" cross-gap memberships).
  const [articleGapMap, setArticleGapMap] = useState<Record<number, string[]>>({})
  const [resolving, setResolving] = useState(false)

  useEffect(() => {
    if (starredIds.length === 0) return
    setResolving(true)
    resolveGaps(starredIds)
      .then(({ mapping }) => {
        // mapping shape: { article_id_str: [primary_gap_id, ...other_gap_ids] }
        // Preserve the full list; element 0 remains the primary for grouping.
        const m: Record<number, string[]> = {}
        for (const [articleIdStr, gapIds] of Object.entries(mapping)) {
          const numId = Number(articleIdStr)
          if (Number.isFinite(numId) && gapIds.length > 0) {
            m[numId] = gapIds
          }
        }
        setArticleGapMap(m)
      })
      .catch(() => {
        // Non-fatal — articles will show under no-gap-mapping group.
      })
      .finally(() => setResolving(false))
  // Re-resolve whenever the starred set changes.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [starredIds.join(',')])

  // Group starred ids by primary gap_id (element 0, or NO_GAP for unresolved).
  const grouped = useMemo(() => {
    const out: Record<string, number[]> = {}
    for (const id of starredIds) {
      // Primary gap is the first element; fall back to NO_GAP if unresolved.
      const gaps = articleGapMap[id]
      const primaryGap = gaps && gaps.length > 0 ? gaps[0] : NO_GAP
      if (!out[primaryGap]) out[primaryGap] = []
      out[primaryGap].push(id)
    }
    return out
  }, [starredIds, articleGapMap])

  // Fetch one dossier per unique known gap.
  const knownGaps = Object.keys(grouped).filter((g) => g !== NO_GAP)
  const dossierQueries = useQueries({
    queries: knownGaps.map((g) => ({
      queryKey: ['dossier', g],
      queryFn: () => fetchDossier(g),
      staleTime: 30000,
    })),
  })

  // Build {gap_id: {entries: DossierEntry[], header}}.
  const gapCards = useMemo(() => {
    const out: Array<{
      gap_id: string
      header: string
      entries: DossierEntry[]
    }> = []
    for (let i = 0; i < knownGaps.length; i++) {
      const g = knownGaps[i]
      const q = dossierQueries[i]
      if (!q.data) continue
      const wantIds = new Set(grouped[g] ?? [])
      const entries: DossierEntry[] = []
      for (const bucket of Object.values(q.data.tiers)) {
        for (const e of bucket) {
          if (wantIds.has(e.id)) entries.push(e)
        }
      }
      if (entries.length === 0) continue
      out.push({
        gap_id: g,
        header: q.data.gap.chapter || q.data.gap.claim_text || g,
        entries,
      })
    }
    return out
  }, [knownGaps, dossierQueries, grouped])

  if (starredIds.length === 0) {
    return (
      <div className="p-6 max-w-3xl mx-auto">
        <header className="mb-4">
          <h1 className="text-lg font-semibold text-ink">Reading queue</h1>
          <p className="text-xs text-ink-muted mt-1">
            Star articles in any dossier and they'll show up here.
          </p>
        </header>
        <div className="text-center text-sm text-ink-muted py-8">
          <Star size={24} className="mx-auto mb-2 text-ink-muted" />
          Nothing starred yet.
        </div>
      </div>
    )
  }

  return (
    <div className="p-6 max-w-4xl mx-auto">
      <header className="mb-4">
        <h1 className="text-lg font-semibold text-ink">Reading queue</h1>
        <p className="text-xs text-ink-muted mt-1">
          {starredIds.length} starred article{starredIds.length === 1 ? '' : 's'} ·
          grouped by gap.
          {resolving && <span className="ml-1 text-ink-muted">(resolving…)</span>}
        </p>
      </header>

      <div className="space-y-5">
        {gapCards.map((g) => (
          <section key={g.gap_id}>
            <div className="flex items-center gap-2 mb-2">
              <span className="font-mono text-[11px] font-semibold text-ink">{g.gap_id}</span>
              <h2 className="text-sm text-ink-secondary">{g.header}</h2>
            </div>
            <div className="space-y-2">
              {g.entries.map((e) => (
                <QueueRow
                  key={e.id}
                  entry={e}
                  alsoInGaps={(articleGapMap[e.id] ?? []).slice(1)}
                />
              ))}
            </div>
          </section>
        ))}
        {/* A2: Articles with no resolved gap mapping (data integrity issue). */}
        {(grouped[NO_GAP]?.length ?? 0) > 0 && (
          <section>
            <div className="flex items-center gap-2 mb-2">
              <span className="text-[11px] font-semibold text-ink-muted italic">
                (no gap mapping)
              </span>
              <span className="text-xs text-ink-muted">
                — {grouped[NO_GAP].length} article{grouped[NO_GAP].length === 1 ? '' : 's'} not
                linked to a gap in the database
              </span>
            </div>
          </section>
        )}
      </div>
    </div>
  )
}

/**
 * Compact queue row: collapsed by default; click the title to toggle the
 * full SourceCard. Reuses <SourceCard> compact mode for the collapsed
 * variant so the same star/copy primitives are always available.
 *
 * Phase 5: accepts alsoInGaps — gap_ids beyond the primary — and renders
 * a quiet "also in:" line with small monospace chips that navigate to the
 * respective gap dossier without toggling the row's expand state.
 */
function QueueRow({ entry, alsoInGaps = [] }: { entry: DossierEntry; alsoInGaps?: string[] }) {
  const [expanded, setExpanded] = useState(false)
  const navigate = useNavigate()
  return (
    <div onClick={() => setExpanded(!expanded)} className="cursor-pointer">
      <SourceCard entry={entry} compact={!expanded} />
      {alsoInGaps.length > 0 && (
        <div
          className="flex items-center gap-1 mt-1 pl-1"
          // Prevent the outer row toggle from firing when interacting with chips.
          onClick={(e) => e.stopPropagation()}
        >
          <span className="text-xs text-ink-muted">also in:</span>
          {alsoInGaps.map((gapId) => (
            <button
              key={gapId}
              onClick={(e) => {
                e.stopPropagation()
                navigate(`/write/gaps/${gapId}`)
              }}
              className="font-mono text-[11px] text-ink-muted hover:text-ink px-1 py-0.5 rounded bg-surface-card border border-border hover:border-border-strong transition-colors"
            >
              {gapId}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

/**
 * Stamp a (article_id → gap_id) mapping into localStorage. Called by
 * DossierView on every render so the queue page can resolve gap
 * membership for starred articles.
 *
 * NOTE: This function is kept for DossierView compatibility. The QueuePage
 * itself now uses the server-side resolve_gaps endpoint instead of this map.
 */
const ARTICLE_GAP_KEY = 'library.article_gap'

export function stampArticleGap(articleIds: number[], gapId: string): void {
  if (typeof window === 'undefined') return
  if (!gapId) return
  try {
    const raw = window.localStorage.getItem(ARTICLE_GAP_KEY)
    const cur: Record<string, string> = raw ? JSON.parse(raw) : {}
    let changed = false
    for (const id of articleIds) {
      if (cur[id] !== gapId) {
        cur[id] = gapId
        changed = true
      }
    }
    if (changed) {
      window.localStorage.setItem(ARTICLE_GAP_KEY, JSON.stringify(cur))
    }
  } catch {
    /* ignore. */
  }
}
