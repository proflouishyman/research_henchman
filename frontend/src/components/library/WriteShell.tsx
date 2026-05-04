// Outer layout for the writing-companion (/write) routes.
//
// Mirrors the runs-mode Layout but swaps the sidebar for a chapter list
// and the main panel becomes the route Outlet (gap list, dossier,
// search, characters, or queue).
//
// Wave 2: sidebar gains nav links — Gaps · Search · Characters ·
// Reading queue — above the per-chapter list. The chapter list is
// still present (it's a useful filter for /write/gaps).

import { useEffect } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Outlet, useNavigate, useLocation } from 'react-router-dom'
import { Library, Search, Users, Star } from 'lucide-react'
import { TopBar } from '../layout/TopBar'
import { fetchLibraryIndex } from '../../lib/library_api'
import { useUIStore } from '../../store/ui'
import { useLibraryStore } from '../../store/library'
import { ToastHost } from './Toast'

export function WriteShell() {
  const { darkMode } = useUIStore()
  const location = useLocation()
  const navigate = useNavigate()
  const starredCount = useLibraryStore((s) => s.starredCount())

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

  // Top-level routes available from the sidebar nav. Wave 2 adds
  // search / characters / queue.
  const navItems: Array<{
    path: string
    label: string
    icon: React.ReactNode
    badge?: string
  }> = [
    { path: '/write/gaps', label: 'Gaps', icon: <Library size={13} /> },
    { path: '/write/search', label: 'Search', icon: <Search size={13} /> },
    { path: '/write/characters', label: 'Characters', icon: <Users size={13} /> },
    {
      path: '/write/queue',
      label: 'Reading queue',
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
          </div>
        </aside>

        {/* Main route content */}
        <main className="flex-1 overflow-y-auto">
          <Outlet />
        </main>
      </div>

      {/* Global toast host for copy-feedback etc. */}
      <ToastHost />
    </div>
  )
}
