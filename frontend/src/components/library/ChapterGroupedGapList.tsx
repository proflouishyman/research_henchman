// Chapter-grouped list of gaps, driven by /api/library/index.
//
// Each gap row shows its gap_id, gap_type badge, claim_text, and a
// mini tier-counts bar so the user can see at a glance which gaps
// have the most cite-worthy material.
//
// Architecture pass: "addressed" gaps (linked paragraph has a footnote)
// are rendered at opacity-50 with a ✓ check, and a "Show addressed"
// toggle (default OFF) hides them so un-addressed gaps are front and center.

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { Check } from 'lucide-react'
import { fetchLibraryIndex } from '../../lib/library_api'
import type { GapTreeRow, LibraryChapter } from '../../types/library'

// Canonical label for gaps with no chapter assignment.
export const CROSS_CHAPTER_LABEL = 'Cross-chapter theses'

export function ChapterGroupedGapList() {
  const { data, isLoading, error } = useQuery({
    queryKey: ['library-index'],
    queryFn: fetchLibraryIndex,
    staleTime: 30000,
  })
  const [params] = useSearchParams()
  const chapterFilter = params.get('chapter') || ''
  // "Show addressed" toggle — default OFF so unfinished gaps lead.
  const [showAddressed, setShowAddressed] = useState(false)

  if (isLoading) return <div className="p-6 text-sm text-ink-muted">Loading library…</div>
  if (error) return <div className="p-6 text-sm text-status-blocked">Failed to load library.</div>
  if (!data) return null

  const visibleChapters = chapterFilter
    ? data.chapters.filter((c) => c.title === chapterFilter)
    : data.chapters

  const totalAddressed = visibleChapters.reduce(
    (acc, c) => acc + (c.gap_count_addressed ?? 0),
    0,
  )

  return (
    <div className="p-6 max-w-5xl mx-auto">
      <header className="mb-4 flex items-start justify-between gap-4">
        <div>
          <h1 className="text-lg font-semibold text-ink">Gap library</h1>
          <p className="text-xs text-ink-muted mt-1">
            {chapterFilter
              ? `Filtered to chapter: ${chapterFilter}`
              : `Browse all gaps across ${data.chapters.length} chapters.`}
          </p>
        </div>
        {/* "Show addressed" toggle — only shown when there are addressed gaps. */}
        {totalAddressed > 0 && (
          <button
            onClick={() => setShowAddressed(!showAddressed)}
            className={`flex items-center gap-1.5 text-[11px] font-medium px-2 py-1 rounded border transition-colors ${
              showAddressed
                ? 'bg-emerald-50 text-emerald-700 border-emerald-200'
                : 'text-ink-muted border-border hover:bg-surface-muted'
            }`}
            title="Toggle visibility of gaps that are already addressed (have footnotes)"
          >
            <Check size={11} />
            {showAddressed ? 'Hiding addressed' : 'Show addressed'}
            <span className="font-mono ml-0.5">({totalAddressed})</span>
          </button>
        )}
      </header>

      {visibleChapters.length === 0 && (
        <div className="text-sm text-ink-muted">No chapters match.</div>
      )}

      <div className="space-y-6">
        {visibleChapters.map((chapter) => (
          <ChapterBlock key={chapter.slug} chapter={chapter} showAddressed={showAddressed} />
        ))}
      </div>
    </div>
  )
}

function ChapterBlock({
  chapter,
  showAddressed,
}: {
  chapter: LibraryChapter
  showAddressed: boolean
}) {
  // B10: Null/empty chapter titles render as "Cross-chapter theses" with an
  // explainer tooltip — these are intro promises whose chapter wasn't auto-paired.
  const isNullChapter = !chapter.title || chapter.title.trim() === '' ||
    chapter.title === '(no chapter)'
  const displayTitle = isNullChapter ? CROSS_CHAPTER_LABEL : chapter.title
  const titleTooltip = isNullChapter
    ? 'Intro promises whose target chapter wasn\'t auto-paired — likely high-priority cross-cutting claims.'
    : undefined

  // Filter gaps according to the "show addressed" toggle.
  const visibleGaps = showAddressed
    ? chapter.gaps
    : chapter.gaps.filter((g) => !g.addressed)

  if (visibleGaps.length === 0) return null

  const addressedCount = chapter.gap_count_addressed ?? 0

  return (
    <section>
      <h2 className="text-sm font-semibold text-ink mb-2">
        <span title={titleTooltip}>{displayTitle}</span>{' '}
        <span className="text-xs font-normal text-ink-muted">
          ({chapter.gap_count} gap{chapter.gap_count !== 1 ? 's' : ''}
          {addressedCount > 0 && (
            <span className="text-emerald-600 ml-1">· {addressedCount} addressed</span>
          )}
          )
        </span>
      </h2>
      <ul className="space-y-1.5">
        {visibleGaps.map((g) => (
          <GapRow key={g.gap_id} gap={g} />
        ))}
      </ul>
    </section>
  )
}

function GapRow({ gap }: { gap: GapTreeRow }) {
  const navigate = useNavigate()
  const claim = gap.claim_text || gap.research_question || '(no claim)'
  const isAddressed = !!gap.addressed
  return (
    <li>
      <button
        onClick={() => navigate(`/write/gaps/${gap.gap_id}`)}
        className={`w-full text-left bg-surface-card border border-border hover:border-border-strong rounded-md px-3 py-2 transition-colors ${
          isAddressed ? 'opacity-50' : ''
        }`}
      >
        <div className="flex items-center gap-2 mb-1">
          <span className="font-mono text-[11px] font-semibold text-ink">{gap.gap_id}</span>
          <GapTypeBadge type={gap.gap_type} />
          {gap.tier !== null && <TierBadge tier={gap.tier} />}
          {isAddressed && (
            <span
              className="flex items-center gap-0.5 text-[10px] text-emerald-600 font-medium"
              title="Addressed — at least one linked paragraph has a footnote"
            >
              <Check size={10} />
              addressed
            </span>
          )}
          {gap.status && (
            <span className="text-[10px] text-ink-muted uppercase tracking-wider">
              {gap.status}
            </span>
          )}
        </div>
        <div className="text-xs text-ink-secondary line-clamp-2 mb-1.5">{claim}</div>
        <TierCountsBar counts={gap.tier_counts} total={gap.total_rows} />
      </button>
    </li>
  )
}

function GapTypeBadge({ type }: { type: string }) {
  const colorByType: Record<string, string> = {
    intro_promise: 'bg-blue-50 text-blue-700 border-blue-200',
    research_gap: 'bg-purple-50 text-purple-700 border-purple-200',
    company_profile: 'bg-emerald-50 text-emerald-700 border-emerald-200',
    editorial_todo: 'bg-amber-50 text-amber-700 border-amber-200',
  }
  const styles = colorByType[type] || 'bg-gray-100 text-gray-600 border-gray-200'
  return (
    <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded border ${styles}`}>
      {type || 'gap'}
    </span>
  )
}

function TierBadge({ tier }: { tier: number }) {
  return (
    <span className="text-[10px] font-medium px-1.5 py-0.5 rounded border bg-accent-light text-accent border-accent/30">
      tier {tier}
    </span>
  )
}

function TierCountsBar({ counts, total }: { counts: Record<string, number>; total: number }) {
  const t3 = counts['3'] ?? 0
  const t2 = counts['2'] ?? 0
  const t1 = counts['1'] ?? 0
  const t0 = counts['0'] ?? 0
  const unscored = counts['unscored'] ?? 0
  return (
    <div className="flex items-center gap-2 text-[10px] font-mono text-ink-muted">
      <span title="Tier 3 — cite-worthy">
        <span className="text-emerald-600 font-semibold">3:{t3}</span>
      </span>
      <span title="Tier 2 — adjacent context">
        <span className="text-blue-600 font-semibold">2:{t2}</span>
      </span>
      <span title="Tier 1 — tangential">1:{t1}</span>
      <span title="Tier 0 — false positive" className="text-ink-muted">
        0:{t0}
      </span>
      {unscored > 0 && <span title="Unscored">u:{unscored}</span>}
      <span className="ml-auto">{total} total</span>
    </div>
  )
}
