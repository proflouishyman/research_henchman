// /write/queue — flat list of starred articles, grouped by gap.
//
// Marks are localStorage-only (Wave 2 scope). To populate the cards we
// fetch each unique gap_id's dossier and pluck out the entries whose
// id is in the starred set. Falls back gracefully if a gap is missing.

import { useEffect, useMemo, useState } from 'react'
import { useQueries } from '@tanstack/react-query'
import { Star } from 'lucide-react'
import { fetchDossier } from '../../lib/library_api'
import { useLibraryStore } from '../../store/library'
import type { DossierEntry } from '../../types/library'
import { SourceCard } from './SourceCard'

export function QueuePage() {
  const { marks, isStarred } = useLibraryStore()
  const starredIds = useMemo(
    () =>
      Object.entries(marks)
        .filter(([, m]) => m.starred)
        .map(([id]) => Number(id)),
    [marks],
  )

  // We don't know the gap_id from localStorage alone — we have to look
  // it up in the dossiers. Strategy: maintain a small cache mapping
  // article_id → gap_id learned the last time the user saw the entry
  // (stamped by SourceCard render). Fall back to scanning all gaps if
  // the cache is cold.
  const articleGapMap = useArticleGapMap(starredIds)

  // Group starred ids by gap_id (or "_unknown" while still loading).
  const grouped = useMemo(() => {
    const out: Record<string, number[]> = {}
    for (const id of starredIds) {
      const gap = articleGapMap[id] || '_unknown'
      if (!out[gap]) out[gap] = []
      out[gap].push(id)
    }
    return out
  }, [starredIds, articleGapMap])

  // Fetch one dossier per unique known gap. ``_unknown`` is omitted —
  // the user will see a placeholder for those until they revisit the
  // dossier (which writes the article→gap mapping).
  const knownGaps = Object.keys(grouped).filter((g) => g !== '_unknown')
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
                <QueueRow key={e.id} entry={e} />
              ))}
            </div>
          </section>
        ))}
        {grouped['_unknown']?.length > 0 && (
          <section>
            <p className="text-xs text-ink-muted italic">
              {grouped['_unknown'].length} starred article(s) from gaps you
              haven't reopened yet — visit the dossier once to populate them.
            </p>
          </section>
        )}
      </div>
    </div>
  )
}

/**
 * Compact queue row: collapsed by default; click the title to toggle the
 * full SourceCard. Reuses <SourceCard> compact mode for the collapsed
 * variant so the same star/copy/read primitives are always available.
 */
function QueueRow({ entry }: { entry: DossierEntry }) {
  const [expanded, setExpanded] = useState(false)
  return (
    <div onClick={() => setExpanded(!expanded)} className="cursor-pointer">
      <SourceCard entry={entry} compact={!expanded} />
    </div>
  )
}

/**
 * Hook: maintain a localStorage-backed map from article_id → gap_id so
 * /write/queue can look up which dossier each starred article belongs
 * to without a backend table.
 *
 * Population happens here on demand: any time the queue is rendered we
 * try to refine the cache from the dossier responses. The actual
 * write also happens on dossier render via a side effect — see
 * useStampArticleGap below, which DossierView calls.
 */
const ARTICLE_GAP_KEY = 'library.article_gap'

function useArticleGapMap(starredIds: number[]): Record<number, string> {
  const [map, setMap] = useState<Record<number, string>>(() => loadArticleGapMap())
  // Reload whenever the starred set changes — cheap, single localStorage read.
  useEffect(() => {
    setMap(loadArticleGapMap())
  }, [starredIds.join(',')])
  return map
}

function loadArticleGapMap(): Record<number, string> {
  if (typeof window === 'undefined') return {}
  try {
    const raw = window.localStorage.getItem(ARTICLE_GAP_KEY)
    if (!raw) return {}
    const parsed = JSON.parse(raw) as Record<string, string>
    const out: Record<number, string> = {}
    for (const [k, v] of Object.entries(parsed)) {
      const n = Number(k)
      if (Number.isFinite(n) && typeof v === 'string') out[n] = v
    }
    return out
  } catch {
    return {}
  }
}

/**
 * Stamp a (article_id → gap_id) mapping into localStorage. Called by
 * DossierView on every render so the queue page can resolve gap
 * membership for starred articles.
 */
export function stampArticleGap(articleIds: number[], gapId: string): void {
  if (typeof window === 'undefined') return
  if (!gapId) return
  try {
    const cur = loadArticleGapMap()
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
