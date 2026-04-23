// Small chips showing preferred_sources for a gap.

const SOURCE_COLORS: Record<string, string> = {
  jstor: 'bg-blue-50 text-blue-700 border-blue-200',
  project_muse: 'bg-purple-50 text-purple-700 border-purple-200',
  ebsco_api: 'bg-indigo-50 text-indigo-700 border-indigo-200',
  ebscohost: 'bg-indigo-50 text-indigo-700 border-indigo-200',
  proquest_historical_newspapers: 'bg-rose-50 text-rose-700 border-rose-200',
  americas_historical_newspapers: 'bg-orange-50 text-orange-700 border-orange-200',
  gale_primary_sources: 'bg-yellow-50 text-yellow-700 border-yellow-200',
  bls: 'bg-teal-50 text-teal-700 border-teal-200',
  crossref: 'bg-green-50 text-green-700 border-green-200',
  loc: 'bg-amber-50 text-amber-700 border-amber-200',
  arxiv: 'bg-sky-50 text-sky-700 border-sky-200',
}

const DEFAULT_COLOR = 'bg-gray-50 text-gray-600 border-gray-200'

function sourceBadgeColor(sourceId: string): string {
  return SOURCE_COLORS[sourceId.toLowerCase()] ?? DEFAULT_COLOR
}

function sourceLabel(sourceId: string): string {
  return sourceId.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

interface SourceBadgesProps {
  sources: string[]
  maxVisible?: number
}

export function SourceBadges({ sources, maxVisible = 4 }: SourceBadgesProps) {
  if (!sources || sources.length === 0) return null

  const visible = sources.slice(0, maxVisible)
  const overflow = sources.length - maxVisible

  return (
    <div className="flex flex-wrap gap-1">
      {visible.map((s) => (
        <span
          key={s}
          className={`text-[10px] font-medium px-1.5 py-0.5 rounded border ${sourceBadgeColor(s)}`}
        >
          {sourceLabel(s)}
        </span>
      ))}
      {overflow > 0 && (
        <span className="text-[10px] text-ink-muted px-1.5 py-0.5">+{overflow} more</span>
      )}
    </div>
  )
}
