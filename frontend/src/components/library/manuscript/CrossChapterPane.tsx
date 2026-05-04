// CrossChapterPane — center pane for the virtual "Cross-chapter theses" chapter.
//
// Renders each null-chapter gap as a paragraph-shaped row with a gap badge in
// the gutter. Clicking a row fires onGapClick, which opens the dossier side panel.

import type { GapTreeRow } from '../../../types/library'

interface Props {
  gaps: GapTreeRow[]
  onGapClick: (gapId: string) => void
}

export function CrossChapterPane({ gaps, onGapClick }: Props) {
  if (gaps.length === 0) {
    return (
      <div className="p-12 text-sm text-ink-muted text-center">
        No cross-chapter theses found.
      </div>
    )
  }

  return (
    <div className="max-w-3xl mx-auto p-8">
      <header className="mb-6">
        <h1 className="text-lg font-semibold text-ink italic">Cross-chapter theses</h1>
        <p className="text-xs text-ink-muted mt-1">
          Intro promises whose target chapter wasn't auto-paired — high-priority cross-cutting claims.
        </p>
      </header>

      <div className="space-y-3">
        {gaps.map((gap) => (
          <CrossChapterGapRow key={gap.gap_id} gap={gap} onGapClick={onGapClick} />
        ))}
      </div>
    </div>
  )
}

function CrossChapterGapRow({
  gap,
  onGapClick,
}: {
  gap: GapTreeRow
  onGapClick: (gapId: string) => void
}) {
  const claim = gap.claim_text || gap.research_question || '(no claim text)'
  const t3 = gap.tier_counts['3'] ?? 0

  return (
    <div className="flex gap-3 group">
      {/* Gutter badge */}
      <div className="shrink-0 w-20 flex flex-col items-end gap-1 pt-1">
        <button
          onClick={() => onGapClick(gap.gap_id)}
          className="font-mono text-[10px] font-semibold px-1.5 py-0.5 rounded border bg-blue-50 text-blue-700 border-blue-200 hover:bg-blue-100 transition-colors"
          title={`Open dossier for ${gap.gap_id}`}
        >
          {gap.gap_id}
        </button>
        {t3 > 0 && (
          <span className="text-[9px] text-emerald-600 font-mono">{t3} tier-3</span>
        )}
      </div>

      {/* Paragraph-shaped claim text row */}
      <button
        onClick={() => onGapClick(gap.gap_id)}
        className="flex-1 text-left bg-surface-card border border-border hover:border-border-strong rounded-md px-4 py-3 transition-colors"
      >
        <p className="text-sm text-ink-secondary italic leading-relaxed">{claim}</p>
        {gap.research_question && gap.research_question !== claim && (
          <p className="text-xs text-ink-muted mt-1 border-l-2 border-accent/40 pl-2">
            {gap.research_question}
          </p>
        )}
        <div className="flex items-center gap-2 mt-2 text-[10px] font-mono text-ink-muted">
          <span>
            <span className="text-emerald-600 font-semibold">3:{t3}</span>
          </span>
          <span>2:{gap.tier_counts['2'] ?? 0}</span>
          <span>1:{gap.tier_counts['1'] ?? 0}</span>
          <span className="ml-auto">{gap.total_rows} total</span>
        </div>
      </button>
    </div>
  )
}
