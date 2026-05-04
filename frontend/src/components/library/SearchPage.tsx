// /write/search — full-corpus FTS5 search with snippet highlights.
//
// Layout: search bar at top (debounced 300 ms), filter rail on right
// (source toggles, score floor, year range, has-PDF, gap_id picker),
// results list reusing <SourceCard> with the snippet prop set.
//
// Pagination: "Load more" button (no infinite scroll for v2).

import { useEffect, useMemo, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { Search, X, ArrowDown } from 'lucide-react'
import { fetchLibraryIndex, searchArticles } from '../../lib/library_api'
import { useLibraryStore } from '../../store/library'
import { SourceCard } from './SourceCard'

const PAGE_SIZE = 50
const DEBOUNCE_MS = 300

const SOURCE_LABELS: Record<string, string> = {
  ebsco_api: 'EBSCO',
  proquest_us_newsstream: 'ProQuest US',
  proquest_international_newsstream: 'ProQuest Intl',
  proquest_historical_newspapers: 'ProQuest Historical',
  hathitrust_fulltext: 'HathiTrust',
  sec_edgar: 'SEC EDGAR',
}

export function SearchPage() {
  const navigate = useNavigate()
  const {
    searchQuery,
    setSearchQuery,
    searchFilters,
    setSearchFilters,
    resetSearchFilters,
    searchResults,
    searchTotal,
    searchOffset,
    searchLoading,
    searchError,
    setSearchResults,
    appendSearchResults,
    setSearchLoading,
    setSearchError,
    clearSearch,
  } = useLibraryStore()

  // Library index supplies source list + gap ids for the filter rail.
  const { data: index } = useQuery({
    queryKey: ['library-index'],
    queryFn: fetchLibraryIndex,
    staleTime: 30000,
  })

  const allGapIds = useMemo(() => {
    if (!index) return []
    const ids: string[] = []
    for (const c of index.chapters) for (const g of c.gaps) ids.push(g.gap_id)
    return ids.sort()
  }, [index])

  // Debounced fresh search whenever query or filters change.
  const debounceTimer = useRef<number | null>(null)
  useEffect(() => {
    if (debounceTimer.current) window.clearTimeout(debounceTimer.current)
    if (!searchQuery.trim()) {
      clearSearch()
      return
    }
    debounceTimer.current = window.setTimeout(() => {
      runSearch(0)
    }, DEBOUNCE_MS)
    return () => {
      if (debounceTimer.current) window.clearTimeout(debounceTimer.current)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchQuery, JSON.stringify(searchFilters)])

  const runSearch = async (offset: number) => {
    if (!searchQuery.trim()) return
    setSearchLoading(true)
    setSearchError(null)
    try {
      const data = await searchArticles(searchQuery.trim(), searchFilters, {
        limit: PAGE_SIZE,
        offset,
      })
      if (offset === 0) {
        setSearchResults(data.results, data.total, data.results.length)
      } else {
        appendSearchResults(data.results, data.total, offset + data.results.length)
      }
    } catch (err) {
      setSearchError(err instanceof Error ? err.message : String(err))
    } finally {
      setSearchLoading(false)
    }
  }

  const presentSources = index?.sources ?? []
  const canLoadMore =
    searchResults.length > 0 && searchResults.length < searchTotal && !searchLoading

  return (
    <div className="flex h-full">
      {/* Main column */}
      <div className="flex-1 overflow-y-auto p-6 max-w-4xl mx-auto">
        <header className="mb-4">
          <h1 className="text-lg font-semibold text-ink">Corpus search</h1>
          <p className="text-xs text-ink-muted mt-1">
            Full-text search across all pulled articles. Highlights show
            why a document matched.
          </p>
        </header>

        {/* Search bar */}
        <div className="relative mb-4">
          <Search
            size={14}
            className="absolute left-3 top-1/2 -translate-y-1/2 text-ink-muted"
          />
          <input
            type="search"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search abstracts, titles, authors…"
            className="w-full pl-9 pr-9 py-2 rounded-md border border-border bg-surface-card text-sm text-ink placeholder:text-ink-muted focus:outline-none focus:border-accent transition-colors"
          />
          {searchQuery && (
            <button
              onClick={() => {
                setSearchQuery('')
                clearSearch()
              }}
              className="absolute right-3 top-1/2 -translate-y-1/2 text-ink-muted hover:text-ink"
              title="Clear"
            >
              <X size={14} />
            </button>
          )}
        </div>

        {/* Result meta */}
        {searchQuery && (
          <div className="text-xs text-ink-muted mb-3">
            {searchLoading && searchResults.length === 0
              ? 'Searching…'
              : searchError
              ? <span className="text-status-blocked">Error: {searchError}</span>
              : `${searchTotal.toLocaleString()} match${searchTotal === 1 ? '' : 'es'}`}
          </div>
        )}

        {/* Results */}
        <div className="space-y-2">
          {searchResults.map((r) => (
            <div key={`s-${r.id}`} className="space-y-1">
              <SourceCard entry={r} snippet={r.snippet} />
              <div className="text-[10px] text-ink-muted pl-1">
                in gap{' '}
                <button
                  onClick={() => navigate(`/write/gaps/${r.gap_id}`)}
                  className="font-mono hover:text-ink underline"
                >
                  {r.gap_id}
                </button>
              </div>
            </div>
          ))}
        </div>

        {canLoadMore && (
          <div className="flex justify-center mt-6">
            <button
              onClick={() => runSearch(searchOffset)}
              className="flex items-center gap-1 text-xs font-medium px-3 py-1.5 rounded border border-border text-ink-secondary hover:text-ink hover:border-border-strong transition-colors"
            >
              <ArrowDown size={12} />
              Load more ({searchTotal - searchResults.length} remaining)
            </button>
          </div>
        )}

        {searchQuery && !searchLoading && searchResults.length === 0 && !searchError && (
          <div className="text-center text-sm text-ink-muted py-8">
            No matches.
          </div>
        )}
      </div>

      {/* Filter rail */}
      <aside className="w-64 shrink-0 border-l border-border bg-surface-card overflow-y-auto p-4 text-xs">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-[11px] font-semibold uppercase tracking-wider text-ink-muted">
            Filters
          </h2>
          <button
            onClick={resetSearchFilters}
            className="text-[10px] text-ink-muted hover:text-ink underline"
          >
            reset
          </button>
        </div>

        {/* Source checkboxes */}
        <FilterGroup label="Sources">
          {presentSources.map((src) => {
            const checked = searchFilters.sourceIds.includes(src)
            return (
              <label key={src} className="flex items-center gap-2 cursor-pointer py-0.5">
                <input
                  type="checkbox"
                  checked={checked}
                  onChange={() => {
                    const next = checked
                      ? searchFilters.sourceIds.filter((s) => s !== src)
                      : [...searchFilters.sourceIds, src]
                    setSearchFilters({ sourceIds: next })
                  }}
                  className="accent-accent"
                />
                <span className="text-ink-secondary">
                  {SOURCE_LABELS[src] ?? src}
                </span>
              </label>
            )
          })}
        </FilterGroup>

        {/* Score floor */}
        <FilterGroup label="Score ≥">
          <div className="flex items-center gap-3">
            {[0, 1, 2, 3].map((n) => (
              <label key={n} className="flex items-center gap-1 cursor-pointer">
                <input
                  type="radio"
                  name="search-score-min"
                  checked={searchFilters.scoreMin === n}
                  onChange={() => setSearchFilters({ scoreMin: n })}
                  className="accent-accent"
                />
                <span>{n}</span>
              </label>
            ))}
          </div>
        </FilterGroup>

        {/* Year range */}
        <FilterGroup label="Year">
          <div className="flex items-center gap-2">
            <input
              type="number"
              placeholder="from"
              value={searchFilters.yearFrom ?? ''}
              onChange={(e) =>
                setSearchFilters({
                  yearFrom: e.target.value ? Number(e.target.value) : null,
                })
              }
              className="w-16 px-1.5 py-0.5 rounded border border-border bg-surface text-xs"
            />
            <span className="text-ink-muted">–</span>
            <input
              type="number"
              placeholder="to"
              value={searchFilters.yearTo ?? ''}
              onChange={(e) =>
                setSearchFilters({
                  yearTo: e.target.value ? Number(e.target.value) : null,
                })
              }
              className="w-16 px-1.5 py-0.5 rounded border border-border bg-surface text-xs"
            />
          </div>
        </FilterGroup>

        {/* Has-PDF */}
        <FilterGroup label="Has PDF">
          <div className="flex items-center gap-3">
            {[
              { label: 'Any', value: null },
              { label: 'Yes', value: true },
              { label: 'No', value: false },
            ].map((opt) => (
              <label key={String(opt.value)} className="flex items-center gap-1 cursor-pointer">
                <input
                  type="radio"
                  name="search-has-pdf"
                  checked={searchFilters.hasPdf === opt.value}
                  onChange={() => setSearchFilters({ hasPdf: opt.value })}
                  className="accent-accent"
                />
                <span>{opt.label}</span>
              </label>
            ))}
          </div>
        </FilterGroup>

        {/* Gap id picker (autocomplete via datalist) */}
        <FilterGroup label="Gap">
          <input
            list="search-gap-ids"
            value={searchFilters.gapId}
            onChange={(e) => setSearchFilters({ gapId: e.target.value })}
            placeholder="any"
            className="w-full px-1.5 py-0.5 rounded border border-border bg-surface text-xs font-mono"
          />
          <datalist id="search-gap-ids">
            {allGapIds.map((id) => (
              <option key={id} value={id} />
            ))}
          </datalist>
        </FilterGroup>
      </aside>
    </div>
  )
}

function FilterGroup({
  label,
  children,
}: {
  label: string
  children: React.ReactNode
}) {
  return (
    <div className="mb-4">
      <p className="text-[10px] font-semibold uppercase tracking-wider text-ink-muted mb-1.5">
        {label}
      </p>
      <div className="text-ink-secondary">{children}</div>
    </div>
  )
}
