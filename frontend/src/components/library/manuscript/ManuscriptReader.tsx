// ManuscriptReader — top-level page for /write/manuscript.
//
// Three-pane layout:
//   Left (w-56)   — ManuscriptOutline (chapter navigator)
//   Center        — ChapterScroll (paragraph reader)
//   Right (w-96)  — DossierSidePanel (slides in when a gap-linked paragraph
//                   is clicked; hidden otherwise)
//
// URL structure:
//   /write/manuscript                        — chapter 1
//   /write/manuscript/:chapterSlug           — anchored to a chapter
//   /write/manuscript/:chapterSlug/:paraId   — anchored to a paragraph + panel open
//
// Keyboard shortcuts:
//   j / k    — next / prev visible paragraph in the current chapter
//   o        — open dossier panel for current paragraph
//   Escape   — close side panel (handled inside DossierSidePanel)
//
// Architecture pass:
//   - Virtual "Cross-chapter theses" chapter at the top of outline.
//   - Starred filter in outline — filters outline to chapters with gaps.
//   - Panel update highlight (200ms border accent) when paragraph changes.

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { fetchManuscriptStructure, fetchLibraryIndex } from '../../../lib/library_api'
import { useLibraryStore } from '../../../store/library'
import type { ManuscriptParagraph } from '../../../types/library'
import { ManuscriptOutline, CROSS_CHAPTER_SLUG, CROSS_CHAPTER_LABEL } from './ManuscriptOutline'
import { ChapterScroll } from './ChapterScroll'
import { DossierSidePanel } from './DossierSidePanel'
import { CrossChapterPane } from './CrossChapterPane'

export function ManuscriptReader() {
  const { chapterSlug, paraId } = useParams<{
    chapterSlug?: string
    paraId?: string
  }>()
  const navigate = useNavigate()

  const {
    selectedChapter, setSelectedChapter,
    selectedParaId, setSelectedParaId,
    dossierSidePanelOpen, setDossierSidePanelOpen,
  } = useLibraryStore()

  // State for which gap_ids are showing in the side panel
  const [activePanelGapIds, setActivePanelGapIds] = useState<string[]>([])
  // B8: The paragraph text that triggered the panel open, for "Citing this passage:".
  const [activePanelParaText, setActivePanelParaText] = useState<string | undefined>(undefined)
  // Starred filter for the outline.
  const [starredFilter, setStarredFilter] = useState(false)
  // Panel highlight state (200ms top-border accent when paragraph changes).
  const [panelHighlight, setPanelHighlight] = useState(false)

  // Fetch manuscript structure (cached server-side + react-query)
  const { data, isLoading, error } = useQuery({
    queryKey: ['manuscript-structure'],
    queryFn: fetchManuscriptStructure,
    staleTime: 60000,
  })

  // Index — needed for cross-chapter virtual chapter gaps.
  const { data: index } = useQuery({
    queryKey: ['library-index'],
    queryFn: fetchLibraryIndex,
    staleTime: 30000,
  })

  // A3: Find the first chapter with meaningful body content (word count > 200).
  // This skips preamble/title-only sections so the default landing is useful.
  const firstBodyChapter = useMemo(() => {
    if (!data) return null
    for (const ch of data.chapters) {
      const wordCount = ch.sections
        .flatMap((s) => s.paragraphs)
        .filter((p) => !p.is_heading && p.text.trim())
        .reduce((acc, p) => acc + p.text.split(/\s+/).length, 0)
      if (wordCount > 200) return ch
    }
    // Fallback: first chapter regardless.
    return data.chapters[0] ?? null
  }, [data])

  // Null-chapter gaps — drives the virtual "Cross-chapter theses" pane.
  const nullChapterGaps = useMemo(() => {
    if (!index) return []
    return index.chapters.find(
      (c) => !c.title || c.title === '(no chapter)',
    )?.gaps ?? []
  }, [index])

  // True when the user has selected the virtual cross-chapter chapter.
  const isCrossChapterSelected = chapterSlug === CROSS_CHAPTER_SLUG

  // Derive chapter from URL slug or default to first body chapter.
  const activeChapter = useMemo(() => {
    if (!data) return null
    if (chapterSlug && chapterSlug !== CROSS_CHAPTER_SLUG) {
      return data.chapters.find((c) => c.slug === chapterSlug) ?? data.chapters[0] ?? null
    }
    if (!chapterSlug) return firstBodyChapter
    return null  // cross-chapter pane doesn't map to a ManuscriptChapter
  }, [data, chapterSlug, firstBodyChapter])

  // Sync store with URL params
  useEffect(() => {
    if (activeChapter) setSelectedChapter(activeChapter.slug)
    else if (isCrossChapterSelected) setSelectedChapter(CROSS_CHAPTER_SLUG)
  }, [activeChapter, isCrossChapterSelected, setSelectedChapter])

  useEffect(() => {
    if (paraId) {
      setSelectedParaId(paraId)
    } else {
      setSelectedParaId(null)
      setDossierSidePanelOpen(false)
    }
  }, [paraId, setSelectedParaId, setDossierSidePanelOpen])

  // Scroll selected paragraph into view
  const scrollRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (paraId) {
      const el = document.getElementById(`para-${paraId}`)
      if (el) el.scrollIntoView({ block: 'center', behavior: 'smooth' })
    }
  }, [paraId, activeChapter])

  // Handle chapter outline click → navigate to chapter
  const handleChapterClick = useCallback(
    (slug: string) => {
      navigate(`/write/manuscript/${slug}`)
    },
    [navigate],
  )

  // Handle gap click → open side panel + update URL
  const handleGapClick = useCallback(
    (gapId: string, clickedParaId: string, allGapIds: string[], paraText?: string) => {
      setActivePanelGapIds(allGapIds.length > 0 ? allGapIds : [gapId])
      // B8: Capture the paragraph text for the "Citing this passage:" label.
      setActivePanelParaText(paraText)
      setDossierSidePanelOpen(true)
      // Brief highlight on the side panel to signal context changed.
      setPanelHighlight(true)
      setTimeout(() => setPanelHighlight(false), 200)
      if (activeChapter) {
        navigate(`/write/manuscript/${activeChapter.slug}/${clickedParaId}`, { replace: true })
      }
    },
    [activeChapter, navigate, setDossierSidePanelOpen],
  )

  // Handle cross-chapter gap click → open side panel (no para URL).
  const handleCrossChapterGapClick = useCallback(
    (gapId: string) => {
      setActivePanelGapIds([gapId])
      setActivePanelParaText(undefined)
      setDossierSidePanelOpen(true)
      navigate(`/write/manuscript/${CROSS_CHAPTER_SLUG}`, { replace: true })
    },
    [navigate, setDossierSidePanelOpen],
  )

  // Handle side-panel close
  const handlePanelClose = useCallback(() => {
    setDossierSidePanelOpen(false)
    setSelectedParaId(null)
    setActivePanelParaText(undefined)
    const targetSlug = activeChapter?.slug ?? chapterSlug ?? ''
    if (targetSlug) {
      navigate(`/write/manuscript/${targetSlug}`, { replace: true })
    }
  }, [activeChapter, chapterSlug, navigate, setDossierSidePanelOpen, setSelectedParaId])

  // Flat paragraph list for keyboard navigation (only for normal chapters).
  const flatParas: ManuscriptParagraph[] = useMemo(() => {
    if (!activeChapter) return []
    return activeChapter.sections.flatMap(
      (s) => s.paragraphs.filter((p) => !p.is_heading && p.text.trim()),
    )
  }, [activeChapter])

  // Keyboard j/k navigation + "o" to open panel for current paragraph.
  useEffect(() => {
    if (!activeChapter) return

    const handler = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement).tagName === 'INPUT') return

      if (e.key === 'j' || e.key === 'k') {
        const curIdx = paraId ? flatParas.findIndex((p) => p.para_id === paraId) : -1
        const nextIdx = e.key === 'j'
          ? Math.min(curIdx + 1, flatParas.length - 1)
          : Math.max(curIdx - 1, 0)
        const next = flatParas[nextIdx]
        if (next && activeChapter) {
          navigate(`/write/manuscript/${activeChapter.slug}/${next.para_id}`, { replace: true })
          document.getElementById(`para-${next.para_id}`)?.scrollIntoView({ block: 'center', behavior: 'smooth' })
          // Update panel highlight when paragraph changes via keyboard.
          if (dossierSidePanelOpen) {
            setPanelHighlight(true)
            setTimeout(() => setPanelHighlight(false), 200)
          }
        }
      }

      if (e.key === 'o' && paraId) {
        // Find the current paragraph and open its dossier.
        const para = flatParas.find((p) => p.para_id === paraId)
        if (para && para.gap_ids.length > 0) {
          setActivePanelGapIds(para.gap_ids)
          setActivePanelParaText(para.text)
          setDossierSidePanelOpen(true)
        }
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [activeChapter, paraId, navigate, flatParas, dossierSidePanelOpen, setDossierSidePanelOpen])

  // ------- Render -------

  if (isLoading) {
    return (
      <div className="flex-1 flex items-center justify-center text-sm text-ink-muted">
        Parsing manuscript…
      </div>
    )
  }

  if (error || !data) {
    return (
      <div className="flex-1 flex items-center justify-center text-sm text-status-blocked">
        Failed to load manuscript. Check the server logs.
      </div>
    )
  }

  // The manuscript reader needs to fill the parent height (WriteShell's main is
  // overflow-y-auto). We use fixed positioning for the side panel and a sticky
  // outline — this keeps the layout simple and avoids nested-scroll issues.
  return (
    <div className="flex min-h-screen">
      {/* Left: sticky chapter outline (scrolls with page but stays visible) */}
      <aside className="w-52 shrink-0 border-r border-border bg-surface-card sticky top-0 self-start max-h-screen overflow-y-auto">
        <ManuscriptOutline
          chapters={data.chapters}
          selectedChapter={selectedChapter}
          onChapterClick={handleChapterClick}
          starredFilter={starredFilter}
          onStarredFilterChange={setStarredFilter}
        />
      </aside>

      {/* Center: chapter body (normal document flow) */}
      <main ref={scrollRef} className={`flex-1 ${dossierSidePanelOpen ? 'mr-96' : ''} transition-all`}>
        {isCrossChapterSelected ? (
          <CrossChapterPane
            gaps={nullChapterGaps}
            onGapClick={handleCrossChapterGapClick}
          />
        ) : activeChapter ? (
          <ChapterScroll
            chapter={activeChapter}
            selectedParaId={selectedParaId}
            onGapClick={(gapId, clickedParaId) => {
              const section = activeChapter.sections.find((s) =>
                s.paragraphs.some((p) => p.para_id === clickedParaId),
              )
              const para = section?.paragraphs.find((p) => p.para_id === clickedParaId)
              // B8: pass paragraph text through for "Citing this passage:" label.
              handleGapClick(gapId, clickedParaId, para?.gap_ids ?? [gapId], para?.text)
            }}
          />
        ) : (
          <div className="p-12 text-sm text-ink-muted text-center">No chapters found.</div>
        )}
      </main>

      {/* Right: fixed side panel (slides in from right).
          Persistent — only closes on × / Escape / mode switch, not outside click. */}
      {dossierSidePanelOpen && activePanelGapIds.length > 0 && (
        <div
          className={`fixed right-0 top-0 bottom-0 w-96 z-30 flex flex-col shadow-panel transition-all ${
            panelHighlight ? 'border-t-2 border-accent' : 'border-t-2 border-transparent'
          }`}
        >
          <DossierSidePanel
            gapIds={activePanelGapIds}
            paraId={selectedParaId ?? 'panel'}
            onClose={handlePanelClose}
            paragraphText={activePanelParaText}
          />
        </div>
      )}
    </div>
  )
}
