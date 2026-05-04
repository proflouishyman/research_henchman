// ManuscriptOutline — left-pane chapter/section navigator.
//
// Shows chapter titles with low-contrast gutter chips summarising
// uncited-paragraph counts (amber) and gap-linked paragraph counts (blue).
// Clicking a chapter scrolls the main pane to that chapter.

import type { ManuscriptChapter } from '../../../types/library'

interface Props {
  chapters: ManuscriptChapter[]
  selectedChapter: string | null
  onChapterClick: (slug: string, title: string) => void
}

function _chapterStats(chapter: ManuscriptChapter) {
  let gapLinked = 0
  let uncited = 0
  for (const section of chapter.sections) {
    for (const para of section.paragraphs) {
      if (para.is_heading || !para.text.trim()) continue
      if (para.gap_ids.length > 0) gapLinked++
      if (para.footnote_count === 0 && para.text.length > 100) uncited++
    }
  }
  return { gapLinked, uncited }
}

export function ManuscriptOutline({ chapters, selectedChapter, onChapterClick }: Props) {
  return (
    <div className="py-2 overflow-y-auto">
      <p className="px-4 py-1 text-[10px] uppercase tracking-wider font-semibold text-ink-muted">
        Chapters
      </p>
      {chapters.map((ch) => {
        const { gapLinked, uncited } = _chapterStats(ch)
        const isActive = selectedChapter === ch.slug
        return (
          <button
            key={ch.slug}
            onClick={() => onChapterClick(ch.slug, ch.title)}
            className={`w-full text-left px-4 py-2 text-xs transition-colors hover:bg-surface-muted ${
              isActive ? 'bg-surface-muted text-ink font-medium' : 'text-ink-secondary'
            }`}
          >
            <div className="font-medium truncate leading-snug">{ch.title}</div>
            <div className="flex gap-2 mt-0.5 text-[10px]">
              {gapLinked > 0 && (
                <span className="text-blue-400">{gapLinked} gaps</span>
              )}
              {uncited > 0 && (
                <span className="text-amber-400">{uncited} uncited</span>
              )}
            </div>
          </button>
        )
      })}
    </div>
  )
}
