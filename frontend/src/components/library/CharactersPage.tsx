// /write/characters — main-characters dashboard.
//
// Shows one card per company-profile gap with a tier histogram bar
// chart, top-3 tier-3 titles, and a one-click "Open dossier" button.
//
// Top filter tabs: All / Empty / Thin / Supplementary — derived from
// the gap_tree.rationale substring (set by the Pass A/F detector when
// it tagged the gap as company_profile).

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { fetchCharacters } from '../../lib/library_api'
import type { CharacterCard } from '../../types/library'

type RationaleFilter = 'all' | 'empty' | 'thin' | 'supplementary'

const FILTER_LABELS: Record<RationaleFilter, string> = {
  all: 'All',
  empty: 'Empty section',
  thin: 'Thin section',
  supplementary: 'Supplementary',
}

/** Match a gap's rationale text against the user-selected category. */
function matchesFilter(rationale: string, f: RationaleFilter): boolean {
  if (f === 'all') return true
  const r = (rationale || '').toLowerCase()
  if (f === 'empty') return r.includes('empty')
  if (f === 'thin') return r.includes('thin')
  if (f === 'supplementary') return r.includes('supplementary') || r.includes('covered')
  return true
}

export function CharactersPage() {
  const [filter, setFilter] = useState<RationaleFilter>('all')

  const { data, isLoading, error } = useQuery({
    queryKey: ['library-characters'],
    queryFn: fetchCharacters,
    staleTime: 30000,
  })

  if (isLoading) return <div className="p-6 text-sm text-ink-muted">Loading characters…</div>
  if (error) return <div className="p-6 text-sm text-status-blocked">Failed to load characters.</div>
  if (!data) return null

  const visible = data.characters.filter((c) => matchesFilter(c.rationale, filter))

  return (
    <div className="p-6 max-w-6xl mx-auto">
      <header className="mb-4">
        <h1 className="text-lg font-semibold text-ink">Main characters</h1>
        <p className="text-xs text-ink-muted mt-1">
          {data.characters.length} company-profile gap{data.characters.length === 1 ? '' : 's'} —
          books, biographies, regulatory filings, news coverage.
        </p>
      </header>

      <nav className="flex items-center gap-1 mb-4 border-b border-border">
        {(Object.keys(FILTER_LABELS) as RationaleFilter[]).map((f) => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            className={`text-xs px-3 py-1.5 -mb-px border-b-2 transition-colors ${
              filter === f
                ? 'border-accent text-ink font-medium'
                : 'border-transparent text-ink-muted hover:text-ink'
            }`}
          >
            {FILTER_LABELS[f]}
          </button>
        ))}
      </nav>

      {visible.length === 0 ? (
        <div className="text-sm text-ink-muted py-8 text-center">
          No matching characters.
        </div>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4 gap-3">
          {visible.map((c) => (
            <CharacterCardView key={c.gap_id} character={c} />
          ))}
        </div>
      )}
    </div>
  )
}

function CharacterCardView({ character }: { character: CharacterCard }) {
  const navigate = useNavigate()
  const claim = character.claim_text || character.research_question || character.gap_id
  // First sentence (or first 80 chars) — character names are usually
  // the first clause of the claim, e.g. "Mercado Libre" → full claim.
  const headline = claim.split(/[.\n]/)[0].slice(0, 80)
  const counts = character.tier_histogram

  return (
    <article className="bg-surface-card border border-border rounded-md p-3 flex flex-col hover:border-border-strong transition-colors">
      <header className="mb-2">
        <h2 className="text-sm font-semibold text-ink leading-snug truncate" title={claim}>
          {headline}
        </h2>
        <div className="flex items-center gap-2 mt-1">
          <span className="font-mono text-[10px] text-ink-muted">{character.gap_id}</span>
          {character.tier !== null && (
            <span className="text-[10px] font-medium px-1.5 py-0.5 rounded border bg-accent-light text-accent border-accent/30">
              tier {character.tier}
            </span>
          )}
        </div>
      </header>

      <TierHistogram counts={counts} total={character.total_rows} />

      {character.top_tier3_titles.length > 0 && (
        <ul className="mt-2 space-y-0.5 text-[11px] text-ink-secondary">
          {character.top_tier3_titles.map((t, i) => (
            <li key={i} className="truncate" title={t}>
              <span className="text-emerald-600 mr-1">●</span>
              {t}
            </li>
          ))}
        </ul>
      )}

      {character.rationale && (
        <p className="mt-2 text-[10px] text-ink-muted italic line-clamp-2" title={character.rationale}>
          {character.rationale}
        </p>
      )}

      <button
        onClick={() => navigate(`/write/gaps/${character.gap_id}`)}
        className="mt-3 w-full text-xs font-medium bg-surface-muted hover:bg-accent-light text-ink-secondary hover:text-accent border border-border rounded px-2 py-1.5 transition-colors"
      >
        Open dossier
      </button>
    </article>
  )
}

/** 4-bar histogram, heights proportional, colored by tier. */
function TierHistogram({
  counts,
  total,
}: {
  counts: Record<string, number>
  total: number
}) {
  const max = Math.max(
    counts['3'] ?? 0,
    counts['2'] ?? 0,
    counts['1'] ?? 0,
    counts['0'] ?? 0,
    1, // Avoid division by zero — empty histogram still draws.
  )
  const bars = [
    { tier: 3, count: counts['3'] ?? 0, color: 'bg-emerald-500', label: 'tier 3' },
    { tier: 2, count: counts['2'] ?? 0, color: 'bg-blue-500', label: 'tier 2' },
    { tier: 1, count: counts['1'] ?? 0, color: 'bg-gray-400', label: 'tier 1' },
    { tier: 0, count: counts['0'] ?? 0, color: 'bg-gray-300', label: 'tier 0' },
  ]
  return (
    <div>
      <div className="flex items-end gap-1 h-12">
        {bars.map((b) => (
          <div
            key={b.tier}
            className="flex-1 flex flex-col items-center justify-end"
            title={`${b.label}: ${b.count}`}
          >
            <div
              className={`w-full rounded-sm ${b.color} transition-all`}
              style={{ height: `${(b.count / max) * 100}%`, minHeight: b.count > 0 ? 2 : 0 }}
            />
          </div>
        ))}
      </div>
      <div className="flex items-center justify-between text-[10px] text-ink-muted mt-1 font-mono">
        <span className="text-emerald-600">3:{counts['3'] ?? 0}</span>
        <span className="text-blue-600">2:{counts['2'] ?? 0}</span>
        <span>1:{counts['1'] ?? 0}</span>
        <span>0:{counts['0'] ?? 0}</span>
        <span className="text-ink-muted">· {total} total</span>
      </div>
    </div>
  )
}
