// Per-gap dossier view: header, TopPicks strip, filters, four tier sections.
// Loads from /api/library/gaps/{gapId}/dossier.

import React, { useEffect, useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate, useParams } from 'react-router-dom'
import { ArrowLeft, GripVertical } from 'lucide-react'
import { fetchDossier, fileUrl } from '../../lib/library_api'
import { useLibraryStore } from '../../store/library'
import type { DossierEntry, LibraryDossier } from '../../types/library'
import { DossierFilters } from './DossierFilters'
import { TierSection } from './TierSection'
import { stampArticleGap } from './QueuePage'
import { attachDragCitations } from '../../lib/citations'

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

      {/* B7: TopPicks strip — 3 most citation-ready tier-3 entries. */}
      <TopPicks tiers={data.tiers} />

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

  // B11: Show "Pull more sources →" when tier-3 is empty OR gap is intro_promise with < 5 tier-3.
  const tier3Count = summary.tier_counts['3'] ?? 0
  const showPullMore =
    tier3Count === 0 || (gap.gap_type === 'intro_promise' && tier3Count < 5)

  return (
    <header className="border-b border-border pb-4">
      <div className="flex items-start gap-2 mb-1.5">
        <div className="flex items-center gap-2 flex-wrap flex-1">
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
        {/* B11: Pull more sources button — links to /runs/new?gap=<gap_id>. */}
        {showPullMore && (
          <a
            href={`/runs/new?gap=${encodeURIComponent(gap.gap_id)}`}
            className="shrink-0 text-[11px] font-medium text-accent hover:text-accent/80 border border-accent/30 bg-accent-light/50 rounded px-2 py-1 transition-colors"
            title="Start a new evidence pull run for this gap"
          >
            Pull more sources →
          </a>
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

/**
 * B7: TopPicks strip — 3 most citation-ready entries.
 *
 * Picks: tier-3 entries sorted by (has_pdf_path, source_priority, pub_date_desc).
 * Falls back to top tier-2 if no tier-3 entries exist. Hidden if no tier-3.
 *
 * SOURCE_PRIORITY matches the ordering established in the scoring pipeline:
 * SEC EDGAR > HathiTrust > ProQuest Historical > ProQuest > EBSCO.
 */
const SOURCE_PRIORITY_ORDER: Record<string, number> = {
  sec_edgar: 0,
  hathitrust_fulltext: 1,
  proquest_historical_newspapers: 2,
  proquest_us_newsstream: 3,
  proquest_international_newsstream: 4,
  ebsco_api: 5,
}

function pickTopEntries(tiers: Record<string, DossierEntry[]>): DossierEntry[] {
  const tier3 = tiers['3'] ?? []
  const pool = tier3.length > 0 ? tier3 : (tiers['2'] ?? [])
  if (pool.length === 0) return []

  const sorted = [...pool].sort((a, b) => {
    // 1. Prefer entries with a local PDF on disk.
    const aPdf = a.pdf_path ? 0 : 1
    const bPdf = b.pdf_path ? 0 : 1
    if (aPdf !== bPdf) return aPdf - bPdf

    // 2. Source priority (lower number = higher priority).
    const aSrc = SOURCE_PRIORITY_ORDER[a.source_id] ?? 99
    const bSrc = SOURCE_PRIORITY_ORDER[b.source_id] ?? 99
    if (aSrc !== bSrc) return aSrc - bSrc

    // 3. Most recent pub_date first (lexicographic — ISO dates sort correctly).
    return (b.pub_date ?? '').localeCompare(a.pub_date ?? '')
  })

  return sorted.slice(0, 3)
}

function TopPicks({ tiers }: { tiers: Record<string, DossierEntry[]> }) {
  const tier3Count = (tiers['3'] ?? []).length
  // Hide entirely if no tier-3 entries exist.
  if (tier3Count === 0) return null

  const picks = pickTopEntries(tiers)
  if (picks.length === 0) return null

  return (
    <div className="mt-4 mb-2 rounded-md border border-emerald-200 bg-emerald-50/50 p-3">
      <p className="text-[10px] font-semibold uppercase tracking-wider text-emerald-700 mb-2">
        Top picks — most citation-ready
      </p>
      <div className="space-y-2">
        {picks.map((entry) => (
          <TopPickRow key={entry.id} entry={entry} />
        ))}
      </div>
    </div>
  )
}

function TopPickRow({ entry }: { entry: DossierEntry }) {
  const onOpen = () => {
    if (entry.pdf_path) {
      window.open(fileUrl(entry.pdf_path), '_blank')
    } else if (entry.url) {
      window.open(entry.url, '_blank', 'noopener,noreferrer')
    }
  }

  const onDragStart = (ev: React.DragEvent) => {
    attachDragCitations(ev, entry)
    try { window.localStorage.setItem('library.onboarding.drag_seen', '1') } catch { /* ignore */ }
  }

  return (
    <div
      draggable
      onDragStart={onDragStart}
      className="flex items-start gap-3 bg-white rounded border border-emerald-100 px-3 py-2 hover:border-emerald-300 transition-colors group cursor-grab active:cursor-grabbing"
    >
      {/* Drag handle — always visible in TopPicks */}
      <span className="text-emerald-400 group-hover:text-emerald-600 mt-0.5 transition-colors shrink-0" title="Drag to Word — drops Chicago citation">
        <GripVertical size={14} />
      </span>
      <div className="flex-1 min-w-0">
        <p className="text-xs font-semibold text-ink truncate">{entry.title || '(untitled)'}</p>
        {entry.relevance_why && (
          <p className="text-[11px] italic text-ink-secondary line-clamp-2 mt-0.5">
            {entry.relevance_why}
          </p>
        )}
        <div className="flex items-center gap-2 mt-1 text-[10px] text-ink-muted">
          {entry.authors && <span>{entry.authors}</span>}
          {entry.pub_date && <span>{entry.pub_date}</span>}
          {entry.pdf_path && (
            <span className="text-emerald-600 font-medium">PDF available</span>
          )}
        </div>
      </div>
      <button
        onClick={onOpen}
        disabled={!entry.pdf_path && !entry.url}
        className="shrink-0 text-[11px] font-medium text-ink-secondary hover:text-ink disabled:opacity-40"
        title={entry.pdf_path ? 'Open PDF' : 'Open URL'}
      >
        Open
      </button>
    </div>
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
