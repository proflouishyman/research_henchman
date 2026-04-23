// Quality rank chip: high=green, medium=amber, seed=gray.

interface QualityBadgeProps {
  rank: 'high' | 'medium' | 'seed'
  label?: string
}

const rankStyles: Record<string, string> = {
  high: 'bg-emerald-50 text-emerald-700 border-emerald-200',
  medium: 'bg-amber-50 text-amber-700 border-amber-200',
  seed: 'bg-gray-100 text-gray-500 border-gray-200',
}

export function QualityBadge({ rank, label }: QualityBadgeProps) {
  const styles = rankStyles[rank] ?? rankStyles.seed
  const displayLabel = label ?? rank

  return (
    <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded border ${styles}`}>
      {displayLabel}
    </span>
  )
}
