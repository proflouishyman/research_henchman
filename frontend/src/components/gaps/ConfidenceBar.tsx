// Colored progress bar for route_confidence (0–1 float → 0–100%).

interface ConfidenceBarProps {
  /** 0.0 to 1.0 */
  value: number
  showLabel?: boolean
}

function barColor(pct: number): string {
  if (pct >= 75) return 'bg-emerald-500'
  if (pct >= 50) return 'bg-amber-500'
  return 'bg-red-500'
}

export function ConfidenceBar({ value, showLabel = true }: ConfidenceBarProps) {
  const pct = Math.round(Math.max(0, Math.min(1, value)) * 100)

  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 h-1.5 bg-border rounded-full overflow-hidden">
        <div
          className={`h-full rounded-full transition-all duration-500 ${barColor(pct)}`}
          style={{ width: `${pct}%` }}
        />
      </div>
      {showLabel && (
        <span className="text-[10px] font-mono text-ink-muted w-7 text-right">{pct}%</span>
      )}
    </div>
  )
}
