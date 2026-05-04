// Outer layout for the writing-companion (/write) routes.
//
// Mirrors the runs-mode Layout but swaps the sidebar for a chapter list
// and the main panel becomes the route Outlet (gap list, dossier,
// search, characters, or queue).
//
// Wave 2: sidebar gains nav links — Gaps · Search · Characters ·
// Reading queue — above the per-chapter list.
//
// Architecture pass:
//   - Refresh button in the stats line (only when on manuscript route).
//   - "Reading queue" nav renamed to "Starred" to match the route.
//   - Chapter list sidebar shows "X gaps · Y un-addressed" counts.
//   - "?" help button + shortcuts modal in top bar.

import { useEffect, useState, useCallback } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Outlet, useNavigate, useLocation } from 'react-router-dom'
import { BookOpen, Library, Search, Users, Star, RefreshCw, HelpCircle, X } from 'lucide-react'
import { TopBar } from '../layout/TopBar'
import { fetchLibraryIndex, refreshManuscript } from '../../lib/library_api'
import { useUIStore } from '../../store/ui'
import { useLibraryStore } from '../../store/library'
import { ToastHost, showToast } from './Toast'
import { CROSS_CHAPTER_LABEL } from './ChapterGroupedGapList'

export function WriteShell() {
  const { darkMode } = useUIStore()
  const location = useLocation()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const starredCount = useLibraryStore((s) => s.starredCount())

  const [refreshing, setRefreshing] = useState(false)
  const [helpOpen, setHelpOpen] = useState(false)

  // Prefetch the index here so both the sidebar and the gap list
  // share the same react-query cache key.
  const { data: index } = useQuery({
    queryKey: ['library-index'],
    queryFn: fetchLibraryIndex,
    staleTime: 30000,
  })

  // Apply dark mode (mirrors runs Layout).
  useEffect(() => {
    if (darkMode) document.documentElement.classList.add('dark')
    else document.documentElement.classList.remove('dark')
  }, [darkMode])

  // "?" key shortcut — open help modal from anywhere in WriteShell.
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key !== '?') return
      if ((e.target as HTMLElement).tagName === 'INPUT') return
      setHelpOpen((prev) => !prev)
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [])

  const isManuscriptRoute = location.pathname.startsWith('/write/manuscript')

  // Force-refresh the manuscript parser cache.
  const handleRefresh = useCallback(async () => {
    if (refreshing) return
    setRefreshing(true)
    try {
      const result = await refreshManuscript()
      // Invalidate react-query caches so outline + index reload.
      await queryClient.invalidateQueries({ queryKey: ['manuscript-structure'] })
      await queryClient.invalidateQueries({ queryKey: ['library-index'] })
      showToast(`Manuscript refreshed (${result.paragraph_count} paragraphs)`)
    } catch {
      showToast('Refresh failed — check server logs.')
    } finally {
      setRefreshing(false)
    }
  }, [refreshing, queryClient])

  // Top-level routes. v3 adds Manuscript between index and Gaps.
  // Order: Manuscript · Gaps · Search · Characters · Starred.
  // Architecture pass: "Reading queue" removed from nav; use /write/queue directly.
  const navItems: Array<{
    path: string
    label: string
    icon: React.ReactNode
    badge?: string
  }> = [
    { path: '/write/manuscript', label: 'Manuscript', icon: <BookOpen size={13} /> },
    { path: '/write/gaps', label: 'Gaps', icon: <Library size={13} /> },
    { path: '/write/search', label: 'Search', icon: <Search size={13} /> },
    { path: '/write/characters', label: 'Characters', icon: <Users size={13} /> },
    {
      path: '/write/queue',
      label: 'Starred',
      icon: <Star size={13} />,
      badge: starredCount > 0 ? String(starredCount) : undefined,
    },
  ]

  return (
    <div className="flex flex-col h-screen bg-surface">
      <TopBar hideNewRun />

      <div className="flex flex-1 overflow-hidden">
        {/* Left: chapter sidebar */}
        <aside className="w-72 shrink-0 border-r border-border overflow-y-auto bg-surface-card">
          <div className="p-4 border-b border-border">
            <div className="flex items-center justify-between mb-1">
              <p className="text-xs text-ink-muted uppercase tracking-wider font-semibold">
                Library
              </p>
              <div className="flex items-center gap-1">
                {/* Refresh button — only on manuscript route */}
                {isManuscriptRoute && (
                  <button
                    onClick={handleRefresh}
                    disabled={refreshing}
                    className="flex items-center gap-1 text-[11px] text-ink-muted hover:text-ink disabled:opacity-50 transition-colors"
                    title="Re-parse manuscript (force-refresh cache)"
                  >
                    <RefreshCw
                      size={11}
                      className={refreshing ? 'animate-spin' : ''}
                    />
                    {refreshing ? '' : '↻'}
                  </button>
                )}
                {/* Help button */}
                <button
                  onClick={() => setHelpOpen(true)}
                  className="text-ink-muted hover:text-ink transition-colors ml-1"
                  title="Keyboard shortcuts & help (?)"
                >
                  <HelpCircle size={13} />
                </button>
              </div>
            </div>
            {index ? (
              <p className="text-xs text-ink-secondary">
                {index.corpus_total_rows.toLocaleString()} rows · {index.corpus_scored_rows.toLocaleString()} scored ·{' '}
                {index.chapters.length} chapters
              </p>
            ) : (
              <p className="text-xs text-ink-muted">Loading…</p>
            )}
          </div>

          {/* Wave 2 nav rail — top-level routes. */}
          <nav className="py-2 border-b border-border">
            {navItems.map((item) => {
              const active =
                location.pathname === item.path ||
                location.pathname.startsWith(item.path + '/')
              return (
                <button
                  key={item.path}
                  onClick={() => navigate(item.path)}
                  className={`w-full text-left px-4 py-1.5 text-xs flex items-center gap-2 transition-colors hover:bg-surface-muted ${
                    active ? 'bg-surface-muted text-ink font-medium' : 'text-ink-secondary'
                  }`}
                >
                  <span className="text-ink-muted">{item.icon}</span>
                  <span className="flex-1">{item.label}</span>
                  {item.badge && (
                    <span className="text-[10px] font-mono text-accent bg-accent-light px-1.5 py-0.5 rounded">
                      {item.badge}
                    </span>
                  )}
                </button>
              )
            })}
          </nav>

          {/* Per-chapter filter list (only useful when on /write/gaps). */}
          <div className="py-2">
            <p className="px-4 py-1 text-[10px] uppercase tracking-wider font-semibold text-ink-muted">
              Chapters
            </p>
            {index?.chapters.map((c) => {
              // Normalize "(no chapter)" display title.
              const isNullChapter = !c.title || c.title === '(no chapter)'
              const displayTitle = isNullChapter ? CROSS_CHAPTER_LABEL : c.title
              const isActive = location.search.includes(
                `chapter=${encodeURIComponent(c.title)}`,
              )
              const addressedCount = c.gap_count_addressed ?? 0
              const unaddressedCount = c.gap_count - addressedCount
              return (
                <button
                  key={c.slug}
                  onClick={() =>
                    navigate(`/write/gaps?chapter=${encodeURIComponent(c.title)}`)
                  }
                  className={`w-full text-left px-4 py-2 text-xs transition-colors hover:bg-surface-muted ${
                    isActive ? 'bg-surface-muted text-ink' : 'text-ink-secondary'
                  }`}
                >
                  <div className="font-medium truncate">{displayTitle}</div>
                  <div className="text-[10px] text-ink-muted mt-0.5">
                    {c.gap_count} gap{c.gap_count !== 1 ? 's' : ''}
                    {addressedCount > 0 && (
                      <span className="text-amber-500 ml-1">
                        · {unaddressedCount} un-addressed
                      </span>
                    )}
                  </div>
                </button>
              )
            })}
          </div>
        </aside>

        {/* Main route content */}
        <main className="flex-1 overflow-y-auto">
          <Outlet />
        </main>
      </div>

      {/* Global toast host for copy-feedback etc. */}
      <ToastHost />

      {/* Help modal */}
      {helpOpen && <HelpModal onClose={() => setHelpOpen(false)} />}
    </div>
  )
}

/** Keyboard shortcuts + workflow help modal. */
function HelpModal({ onClose }: { onClose: () => void }) {
  // Close on Escape.
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [onClose])

  const shortcuts = [
    { key: 'j / k', desc: 'Next / prev paragraph in manuscript reader' },
    { key: 'o', desc: 'Open dossier panel for current paragraph' },
    { key: 'Escape', desc: 'Close dossier side panel' },
    { key: '?', desc: 'Open / close this help modal' },
  ]

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40"
      onClick={onClose}
    >
      <div
        className="bg-surface-card border border-border rounded-lg shadow-panel w-[420px] p-6"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-sm font-semibold text-ink">Keyboard shortcuts & help</h2>
          <button onClick={onClose} className="text-ink-muted hover:text-ink">
            <X size={16} />
          </button>
        </div>

        {/* Shortcuts table */}
        <table className="w-full text-xs mb-5">
          <tbody>
            {shortcuts.map(({ key, desc }) => (
              <tr key={key} className="border-b border-border last:border-0">
                <td className="py-1.5 pr-4 font-mono text-[11px] font-semibold text-ink whitespace-nowrap">
                  {key}
                </td>
                <td className="py-1.5 text-ink-secondary">{desc}</td>
              </tr>
            ))}
          </tbody>
        </table>

        {/* Workflow hint */}
        <div className="text-[11px] text-ink-muted leading-relaxed">
          <p className="mb-1.5">
            <strong className="text-ink">Drag to Word:</strong> grab the grip icon on any
            source card and drop it into Word or Pages. The Chicago citation lands as
            formatted text.
          </p>
          <p>
            <strong className="text-ink">Writing workflow:</strong> open a chapter in
            Manuscript, press <kbd className="font-mono bg-surface-muted px-1 rounded">j</kbd>{' '}
            to walk paragraphs, click a gap badge to open the dossier, star the
            best sources, then drag them into your draft.
          </p>
        </div>
      </div>
    </div>
  )
}
