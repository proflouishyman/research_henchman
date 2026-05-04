// Dossier filter strip: source toggles + score floor + has-PDF flag.

import { useLibraryStore } from '../../store/library'

interface Props {
  /** Source IDs that actually appear in the current dossier. */
  availableSources: string[]
}

const SOURCE_LABELS: Record<string, string> = {
  ebsco_api: 'EBSCO',
  proquest_us_newsstream: 'ProQuest US',
  proquest_international_newsstream: 'ProQuest Intl',
  proquest_historical_newspapers: 'ProQuest Historical',
  hathitrust_fulltext: 'HathiTrust',
  sec_edgar: 'SEC EDGAR',
}

function srcLabel(id: string): string {
  return SOURCE_LABELS[id] || id
}

export function DossierFilters({ availableSources }: Props) {
  const { dossierFilters, toggleSourceId, setScoreMin, setHasPdf, resetFilters } = useLibraryStore()

  return (
    <div className="bg-surface-card border border-border rounded-md px-4 py-3 flex flex-wrap items-center gap-x-6 gap-y-2 text-xs">
      {/* Source toggles */}
      <div className="flex items-center gap-2">
        <span className="text-ink-muted font-medium">Sources:</span>
        {availableSources.length === 0 && <span className="text-ink-muted">(none)</span>}
        {availableSources.map((src) => {
          const active =
            dossierFilters.sourceIds.length === 0 ||
            dossierFilters.sourceIds.includes(src)
          return (
            <button
              key={src}
              onClick={() => toggleSourceId(src)}
              className={`px-2 py-0.5 rounded border text-[11px] transition-colors ${
                active
                  ? 'bg-accent-light border-accent/30 text-accent'
                  : 'bg-surface-muted border-border text-ink-muted'
              }`}
              title={src}
            >
              {srcLabel(src)}
            </button>
          )
        })}
      </div>

      {/* Score floor */}
      <div className="flex items-center gap-2">
        <span className="text-ink-muted font-medium">Score ≥</span>
        {[3, 2, 1, 0].map((n) => (
          <label key={n} className="flex items-center gap-1 cursor-pointer">
            <input
              type="radio"
              name="score-min"
              checked={dossierFilters.scoreMin === n}
              onChange={() => setScoreMin(n)}
              className="accent-accent"
            />
            <span>{n}</span>
          </label>
        ))}
      </div>

      {/* Has-PDF toggle */}
      <label className="flex items-center gap-1.5 cursor-pointer">
        <input
          type="checkbox"
          checked={dossierFilters.hasPdf}
          onChange={(e) => setHasPdf(e.target.checked)}
          className="accent-accent"
        />
        <span>PDF only</span>
      </label>

      {/* Reset */}
      {(dossierFilters.sourceIds.length > 0 ||
        dossierFilters.scoreMin > 0 ||
        dossierFilters.hasPdf) && (
        <button
          onClick={resetFilters}
          className="ml-auto text-ink-muted hover:text-ink underline"
        >
          reset
        </button>
      )}
    </div>
  )
}
