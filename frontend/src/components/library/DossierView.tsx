// Per-gap dossier view: header, filters, four tier sections.
// Loads from /api/library/gaps/{gapId}/dossier.

import { useEffect, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft } from 'lucide-react'
import { fetchDossier } from '../../lib/library_api'
import { useLibraryStore } from '../../store/library'
import type { DossierEntry, LibraryDossier } from '../../types/library'
import { DossierFilters } from './DossierFilters'
import { TierSection } from './TierSection'
import { stampArticleGap } from './QueuePage'

export function DossierView() {
  const { gapId } = useParams<{ gapId: string }>()
  const navigate = useNavigate()
  const setSelectedGapId = useLibraryStore((s) => s.setSelectedGapId)
  const { dossierFilters, expandedTiers } = useLibraryStore()

  useEffect(() => {
    setSelectedGapId(gapId ?? null)
    return () => setSelectedGapId(null)
  }, [gapId, setSelectedGapId])

  const { data, isLoading, error } = useQuery({
    queryKey: ['dossier', gapId],
    queryFn: () => fetchDossier(gapId!),
    enabled: !!gapId,
    staleTime: 30000,
  })

  // Filter rows per the user's source/score/has-PDF settings before rendering.
  const filteredTiers = useMemo(() => {
    if (!data) return null
    return filterTiers(data, dossierFilters.sourceIds, dossierFilters.scoreMin, dossierFilters.hasPdf)
  }, [data, dossierFilters])

  // Stamp the article→gap mapping into localStorage so /write/queue
  // can resolve which dossier each starred article belongs to. Marks
  // are browser-local in Wave 2; this is the cheapest correct fix.
  useEffect(() => {
    if (!data || !gapId) return
    const ids: number[] = []
    for (const bucket of Object.values(data.tiers)) {
      for (const e of bucket) ids.push(e.id)
    }
    stampArticleGap(ids, gapId)
  }, [data, gapId])

  if (!gapId) return <div className="p-6 text-sm text-ink-muted">No gap selected.</div>
  if (isLoading) return <div className="p-6 text-sm text-ink-muted">Loading dossier…</div>
  if (error) return <div className="p-6 text-sm text-status-blocked">Failed to load dossier.</div>
  if (!data || !filteredTiers) return null

  const presentSources = collectPresentSources(data)

  return (
    <div className="p-6 max-w-5xl mx-auto">
      <button
        onClick={() => navigate(-1)}
        className="flex items-center gap-1 text-xs text-ink-muted hover:text-ink mb-3"
      >
        <ArrowLeft size={12} />
        Back
      </button>

      <DossierHeader dossier={data} />

      <div className="my-4">
        <DossierFilters availableSources={presentSources} />
      </div>

      <div className="space-y-4">
        <TierSection
          bucket="3"
          heading="Tier 3 — cite-worthy primary sources"
          entries={filteredTiers['3']}
          defaultOpen={expandedTiers.includes('3')}
        />
        <TierSection
          bucket="2"
          heading="Tier 2 — adjacent context"
          entries={filteredTiers['2']}
          defaultOpen={expandedTiers.includes('2')}
        />
        <TierSection
          bucket="1"
          heading="Tier 1 — tangential mentions"
          entries={filteredTiers['1']}
          defaultOpen={expandedTiers.includes('1')}
          compact
        />
        <TierSection
          bucket="0"
          heading="Tier 0 — search false positives"
          entries={filteredTiers['0']}
          defaultOpen={expandedTiers.includes('0')}
          compact
        />
        {filteredTiers['unscored']?.length > 0 && (
          <TierSection
            bucket="unscored"
            heading="Unscored — LLM has not seen these yet"
            entries={filteredTiers['unscored']}
            defaultOpen={expandedTiers.includes('unscored')}
            compact
          />
        )}
      </div>
    </div>
  )
}

function DossierHeader({ dossier }: { dossier: LibraryDossier }) {
  const { gap, summary } = dossier
  return (
    <header className="border-b border-border pb-4">
      <div className="flex items-center gap-2 mb-1.5">
        <span className="font-mono text-xs font-semibold text-ink">{gap.gap_id}</span>
        {gap.gap_type && (
          <span className="text-[10px] font-medium px-1.5 py-0.5 rounded border bg-emerald-50 text-emerald-700 border-emerald-200">
            {gap.gap_type}
          </span>
        )}
        {gap.tier !== null && (
          <span className="text-[10px] font-medium px-1.5 py-0.5 rounded border bg-accent-light text-accent border-accent/30">
            tier {gap.tier}
          </span>
        )}
        {gap.status && (
          <span className="text-[10px] text-ink-muted uppercase tracking-wider">
            {gap.status}
          </span>
        )}
        {gap.evidence_target > 0 && (
          <span className="text-[10px] text-ink-muted">
            target: {gap.evidence_target} sources
          </span>
        )}
      </div>
      <h1 className="text-xl font-semibold text-ink mb-1">{gap.chapter || 'Gap dossier'}</h1>
      {gap.claim_text && gap.claim_text !== gap.chapter && (
        <p className="text-sm text-ink-secondary mb-2">{gap.claim_text}</p>
      )}
      {gap.research_question && (
        <blockquote className="border-l-2 border-accent pl-3 text-sm text-ink-secondary italic">
          {gap.research_question}
        </blockquote>
      )}

      <div className="mt-3 text-xs text-ink-muted">
        <span className="font-medium text-ink-secondary">{summary.consolidated}</span> consolidated
        from {summary.total_rows} raw rows ·{' '}
        <span className="text-emerald-600 font-medium">tier 3: {summary.tier_counts['3'] ?? 0}</span> ·{' '}
        <span className="text-blue-600 font-medium">tier 2: {summary.tier_counts['2'] ?? 0}</span> ·{' '}
        tier 1: {summary.tier_counts['1'] ?? 0} · tier 0: {summary.tier_counts['0'] ?? 0}
        {summary.tier_counts['unscored'] ? ` · unscored: ${summary.tier_counts['unscored']}` : ''}
      </div>
    </header>
  )
}

/** Collect the set of source_ids actually present in this dossier. */
function collectPresentSources(d: LibraryDossier): string[] {
  const set = new Set<string>()
  for (const bucket of Object.values(d.tiers)) {
    for (const e of bucket) {
      if (e.source_id) set.add(e.source_id)
      for (const s of e.also_in_sources) if (s) set.add(s)
    }
  }
  return Array.from(set).sort()
}

/** Apply user filters, returning the same tier dict shape with rows removed. */
function filterTiers(
  d: LibraryDossier,
  selectedSources: string[],
  scoreMin: number,
  hasPdf: boolean,
): Record<string, DossierEntry[]> {
  const out: Record<string, DossierEntry[]> = {}
  const sourceFilter = new Set(selectedSources)
  for (const [bucket, rows] of Object.entries(d.tiers)) {
    // Score filter applies only to numeric buckets (skip "unscored" when scoreMin > 0).
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
