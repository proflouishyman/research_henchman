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
//   j / k  — next / prev visible paragraph in the current chapter
//   Escape — close side panel (handled inside DossierSidePanel)

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { fetchManuscriptStructure } from '../../../lib/library_api'
import { useLibraryStore } from '../../../store/library'
import type { ManuscriptParagraph } from '../../../types/library'
import { ManuscriptOutline } from './ManuscriptOutline'
import { ChapterScroll } from './ChapterScroll'
import { DossierSidePanel } from './DossierSidePanel'

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

  // Fetch manuscript structure (cached server-side + react-query)
  const { data, isLoading, error } = useQuery({
    queryKey: ['manuscript-structure'],
    queryFn: fetchManuscriptStructure,
    staleTime: 60000,
  })

  // Derive chapter from URL slug or default to first chapter
  const activeChapter = useMemo(() => {
    if (!data) return null
    if (chapterSlug) {
      return data.chapters.find((c) => c.slug === chapterSlug) ?? data.chapters[0] ?? null
    }
    return data.chapters[0] ?? null
  }, [data, chapterSlug])

  // Sync store with URL params
  useEffect(() => {
    if (activeChapter) setSelectedChapter(activeChapter.slug)
  }, [activeChapter, setSelectedChapter])

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
    (gapId: string, clickedParaId: string, allGapIds: string[]) => {
      setActivePanelGapIds(allGapIds.length > 0 ? allGapIds : [gapId])
      setDossierSidePanelOpen(true)
      if (activeChapter) {
        navigate(`/write/manuscript/${activeChapter.slug}/${clickedParaId}`, { replace: true })
      }
    },
    [activeChapter, navigate, setDossierSidePanelOpen],
  )

  // Handle side-panel close
  const handlePanelClose = useCallback(() => {
    setDossierSidePanelOpen(false)
    setSelectedParaId(null)
    if (activeChapter) {
      navigate(`/write/manuscript/${activeChapter.slug}`, { replace: true })
    }
  }, [activeChapter, navigate, setDossierSidePanelOpen, setSelectedParaId])

  // Keyboard j/k navigation within visible paragraphs
  useEffect(() => {
    if (!activeChapter) return
    const flatParas: ManuscriptParagraph[] = activeChapter.sections.flatMap(
      (s) => s.paragraphs.filter((p) => !p.is_heading && p.text.trim()),
    )

    const handler = (e: KeyboardEvent) => {
      if (e.key !== 'j' && e.key !== 'k') return
      if ((e.target as HTMLElement).tagName === 'INPUT') return
      const curIdx = paraId ? flatParas.findIndex((p) => p.para_id === paraId) : -1
      const nextIdx = e.key === 'j'
        ? Math.min(curIdx + 1, flatParas.length - 1)
        : Math.max(curIdx - 1, 0)
      const next = flatParas[nextIdx]
      if (next && activeChapter) {
        navigate(`/write/manuscript/${activeChapter.slug}/${next.para_id}`, { replace: true })
        document.getElementById(`para-${next.para_id}`)?.scrollIntoView({ block: 'center', behavior: 'smooth' })
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [activeChapter, paraId, navigate])

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
        />
      </aside>

      {/* Center: chapter body (normal document flow) */}
      <main ref={scrollRef} className={`flex-1 ${dossierSidePanelOpen ? 'mr-96' : ''} transition-all`}>
        {activeChapter ? (
          <ChapterScroll
            chapter={activeChapter}
            selectedParaId={selectedParaId}
            onGapClick={(gapId, clickedParaId) => {
              const section = activeChapter.sections.find((s) =>
                s.paragraphs.some((p) => p.para_id === clickedParaId),
              )
              const para = section?.paragraphs.find((p) => p.para_id === clickedParaId)
              handleGapClick(gapId, clickedParaId, para?.gap_ids ?? [gapId])
            }}
          />
        ) : (
          <div className="p-12 text-sm text-ink-muted text-center">No chapters found.</div>
        )}
      </main>

      {/* Right: fixed side panel (slides in from right) */}
      {dossierSidePanelOpen && activePanelGapIds.length > 0 && selectedParaId && (
        <div className="fixed right-0 top-0 bottom-0 w-96 z-30 flex flex-col shadow-panel">
          <DossierSidePanel
            gapIds={activePanelGapIds}
            paraId={selectedParaId}
            onClose={handlePanelClose}
          />
        </div>
      )}
    </div>
  )
}
