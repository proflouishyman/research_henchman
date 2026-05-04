// Outer layout for the writing-companion (/write) routes.
//
// Mirrors the runs-mode Layout but swaps the sidebar for a chapter list
// and the main panel becomes the route Outlet (gap list or dossier).

import { useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Outlet, useNavigate, useLocation, useParams } from 'react-router-dom'
import { TopBar } from '../layout/TopBar'
import { fetchLibraryIndex } from '../../lib/library_api'
import { useUIStore } from '../../store/ui'

export function WriteShell() {
  const { darkMode } = useUIStore()
  const location = useLocation()
  const navigate = useNavigate()

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

  return (
    <div className="flex flex-col h-screen bg-surface">
      <TopBar hideNewRun />

      <div className="flex flex-1 overflow-hidden">
        {/* Left: chapter sidebar */}
        <aside className="w-72 shrink-0 border-r border-border overflow-y-auto bg-surface-card">
          <div className="p-4 border-b border-border">
            <p className="text-xs text-ink-muted uppercase tracking-wider font-semibold mb-1">
              Library
            </p>
            {index ? (
              <p className="text-xs text-ink-secondary">
                {index.corpus_total_rows.toLocaleString()} rows · {index.corpus_scored_rows.toLocaleString()} scored ·{' '}
                {index.chapters.length} chapters
              </p>
            ) : (
              <p className="text-xs text-ink-muted">Loading…</p>
            )}
          </div>

          <nav className="py-2">
            {index?.chapters.map((c) => {
              const isActive = location.search.includes(
                `chapter=${encodeURIComponent(c.title)}`,
              )
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
                  <div className="font-medium truncate">{c.title}</div>
                  <div className="text-[10px] text-ink-muted mt-0.5">
                    {c.gap_count} gap{c.gap_count !== 1 ? 's' : ''}
                  </div>
                </button>
              )
            })}
          </nav>
        </aside>

        {/* Main route content */}
        <main className="flex-1 overflow-y-auto">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
