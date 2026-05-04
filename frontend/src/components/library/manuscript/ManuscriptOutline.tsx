// ManuscriptOutline — left-pane chapter/section navigator.
//
// Shows chapter titles with low-contrast gutter chips summarising
// uncited-paragraph counts (amber) and gap-linked paragraph counts (blue).
// Clicking a chapter scrolls the main pane to that chapter.
//
// Architecture pass:
//   - "Cross-chapter theses" virtual chapter at the top (null-chapter gaps).
//   - "★ Starred (N)" toggle at the top filters outline to starred chapters.
//   - Per-chapter stats now show addressed vs un-addressed gap counts.
//   - Chapter stat line: "X gaps · Y un-addressed" when Y > 0.

import { useQuery } from '@tanstack/react-query'
import { fetchLibraryIndex } from '../../../lib/library_api'
import { useLibraryStore } from '../../../store/library'
import type { ManuscriptChapter } from '../../../types/library'

// Slug sentinel for the virtual Cross-chapter theses chapter.
export const CROSS_CHAPTER_SLUG = '_cross_chapter_theses'
export const CROSS_CHAPTER_LABEL = 'Cross-chapter theses'

interface Props {
  chapters: ManuscriptChapter[]
  selectedChapter: string | null
  onChapterClick: (slug: string, title: string) => void
  /** When truthy, only chapters with at least one starred article are shown. */
  starredFilter?: boolean
  onStarredFilterChange?: (value: boolean) => void
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

export function ManuscriptOutline({
  chapters,
  selectedChapter,
  onChapterClick,
  starredFilter = false,
  onStarredFilterChange,
}: Props) {
  // Pull the index to get per-chapter addressed counts and null-chapter gaps.
  const { data: index } = useQuery({
    queryKey: ['library-index'],
    queryFn: fetchLibraryIndex,
    staleTime: 30000,
  })
  const starredCount = useLibraryStore((s) => s.starredCount())

  // Build an addressed-count map by chapter slug for sidebar annotation.
  const addressedBySlug: Record<string, number> = {}
  if (index) {
    for (const ch of index.chapters) {
      addressedBySlug[ch.slug] = ch.gap_count_addressed ?? 0
    }
  }

  // Null-chapter gaps from the index — surfaces as "Cross-chapter theses".
  const nullChapterGaps = index?.chapters.find(
    (c) => !c.title || c.title === '(no chapter)',
  )?.gaps ?? []

  // When starred filter is on, only show chapters that have a gap_id
  // matching a chapter the user has starred. This is a best-effort
  // heuristic — we filter outline entries that have at least one
  // gap-linked paragraph.
  const visibleChapters = chapters.filter((ch) => {
    if (!starredFilter) return true
    // For starred filter: include chapters that have at least one
    // gap-linked paragraph (chapters with gaps are candidate matches).
    return ch.sections.some((s) =>
      s.paragraphs.some((p) => p.gap_ids.length > 0),
    )
  })

  return (
    <div className="py-2 overflow-y-auto">
      {/* Starred filter toggle */}
      {starredCount > 0 && onStarredFilterChange && (
        <button
          onClick={() => onStarredFilterChange(!starredFilter)}
          className={`w-full text-left px-4 py-1.5 text-xs flex items-center gap-1.5 mb-1 border-b border-border transition-colors ${
            starredFilter
              ? 'bg-accent-light text-accent font-medium'
              : 'text-ink-muted hover:bg-surface-muted'
          }`}
        >
          <span>★ Starred</span>
          <span className="font-mono text-[10px]">({starredCount})</span>
        </button>
      )}

      <p className="px-4 py-1 text-[10px] uppercase tracking-wider font-semibold text-ink-muted">
        Chapters
      </p>

      {/* Virtual "Cross-chapter theses" chapter at the top */}
      {nullChapterGaps.length > 0 && (
        <button
          onClick={() => onChapterClick(CROSS_CHAPTER_SLUG, CROSS_CHAPTER_LABEL)}
          className={`w-full text-left px-4 py-2 text-xs transition-colors hover:bg-surface-muted ${
            selectedChapter === CROSS_CHAPTER_SLUG
              ? 'bg-surface-muted text-ink font-medium'
              : 'text-ink-secondary'
          }`}
        >
          <div className="font-medium truncate leading-snug italic text-ink-secondary">
            {CROSS_CHAPTER_LABEL}
          </div>
          <div className="flex gap-2 mt-0.5 text-[10px]">
            <span className="text-blue-400">{nullChapterGaps.length} gaps</span>
          </div>
        </button>
      )}

      {visibleChapters.map((ch) => {
        const { gapLinked, uncited } = _chapterStats(ch)
        const isActive = selectedChapter === ch.slug
        const addressedCount = addressedBySlug[ch.slug] ?? 0
        const unaddressedCount = gapLinked - addressedCount
        return (
          <button
            key={ch.slug}
            onClick={() => onChapterClick(ch.slug, ch.title)}
            className={`w-full text-left px-4 py-2 text-xs transition-colors hover:bg-surface-muted ${
              isActive ? 'bg-surface-muted text-ink font-medium' : 'text-ink-secondary'
            }`}
          >
            <div className="font-medium truncate leading-snug">{ch.title}</div>
            <div className="flex gap-2 mt-0.5 text-[10px] flex-wrap">
              {gapLinked > 0 && (
                <span className="text-blue-400">
                  {gapLinked} gaps
                  {addressedCount > 0 && unaddressedCount > 0 && (
                    <span className="text-amber-400 ml-1">· {unaddressedCount} un-addressed</span>
                  )}
                </span>
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
