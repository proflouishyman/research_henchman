// ParagraphRow — renders one paragraph with left-gutter chips.
//
// Gutter chips (left side, low-contrast until hovered):
//   - Footnote count: "⚓ 3" when N>0; "⚠ no cites" when N=0 and body > 100 chars.
//   - Gap badges: one per gap_id (label derived from prefix: CP/IP/TODO).
//   - Bracket-TODO marker: yellow highlight when bracketed_todos is non-empty.
//
// Clicking the row (or any gap badge) fires onGapClick(gap_id).

import type { ManuscriptParagraph } from '../../../types/library'

interface Props {
  para: ManuscriptParagraph
  isSelected: boolean
  onGapClick: (gapId: string) => void
}

function gapBadgeColor(gapId: string): string {
  if (gapId.startsWith('CP')) return 'bg-emerald-100 text-emerald-700 border-emerald-200'
  if (gapId.startsWith('IP')) return 'bg-blue-100 text-blue-700 border-blue-200'
  if (gapId.startsWith('TODO')) return 'bg-amber-100 text-amber-700 border-amber-200'
  return 'bg-surface-muted text-ink-secondary border-border'
}

export function ParagraphRow({ para, isSelected, onGapClick }: Props) {
  const hasCites = para.footnote_count > 0
  const bodyLong = para.text.length > 100
  const hasTodos = para.bracketed_todos.length > 0

  if (para.is_heading) {
    const headingClass = para.heading_level === 1
      ? 'text-base font-bold text-ink mt-6 mb-1'
      : para.heading_level === 2
      ? 'text-sm font-semibold text-ink-secondary mt-4 mb-0.5'
      : 'text-xs font-medium text-ink-muted mt-3'
    return (
      <div className="flex gap-2 px-4 py-1">
        <div className="w-20 shrink-0" />
        <p className={headingClass}>{para.text}</p>
      </div>
    )
  }

  if (!para.text.trim()) return null

  return (
    <div
      className={`flex gap-2 px-4 py-1.5 group cursor-pointer transition-colors hover:bg-surface-muted ${
        isSelected ? 'bg-accent-light border-l-2 border-accent' : ''
      } ${hasTodos ? 'bg-amber-50/50' : ''}`}
      onClick={() => {
        if (para.gap_ids.length > 0) onGapClick(para.gap_ids[0])
      }}
    >
      {/* Left gutter: 80px wide, holds chips */}
      <div className="w-20 shrink-0 flex flex-col gap-0.5 items-end pt-0.5">
        {/* Footnote chip */}
        {hasCites ? (
          <span className="text-[10px] font-mono text-ink-muted group-hover:text-ink-secondary transition-colors whitespace-nowrap">
            ⚓{para.footnote_count}
          </span>
        ) : bodyLong ? (
          <span className="text-[10px] font-mono text-amber-400 group-hover:text-amber-500 transition-colors whitespace-nowrap">
            ⚠ cite?
          </span>
        ) : null}

        {/* Gap badges */}
        {para.gap_ids.map((gid) => (
          <button
            key={gid}
            onClick={(e) => {
              e.stopPropagation()
              onGapClick(gid)
            }}
            className={`text-[9px] font-mono px-1 py-0.5 rounded border opacity-60 group-hover:opacity-100 transition-opacity ${gapBadgeColor(gid)}`}
          >
            {gid}
          </button>
        ))}

        {/* Bracket-TODO chip */}
        {hasTodos && (
          <span className="text-[9px] font-mono text-amber-600 bg-amber-100 border border-amber-200 px-1 py-0.5 rounded opacity-70 group-hover:opacity-100 transition-opacity">
            TODO
          </span>
        )}
      </div>

      {/* Paragraph body */}
      <p
        className={`flex-1 text-sm leading-relaxed text-ink-secondary ${
          hasTodos ? 'text-amber-900' : ''
        }`}
      >
        {para.text}
      </p>
    </div>
  )
}
