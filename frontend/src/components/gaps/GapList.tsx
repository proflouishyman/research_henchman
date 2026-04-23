// List of gap cards, merging gap map and plan data.

import { BookMarked } from 'lucide-react'
import type { Gap, PlannedGap, GapPacket } from '../../types/contracts'
import { GapCard } from './GapCard'

interface GapListProps {
  gapMapGaps: Gap[]
  planGaps: PlannedGap[]
  documents?: GapPacket[]
}

export function GapList({ gapMapGaps, planGaps, documents }: GapListProps) {
  // Prefer plan gaps when available; fall back to gap map gaps
  const displayGaps: (Gap | PlannedGap)[] = planGaps.length > 0 ? planGaps : gapMapGaps

  // Build lookup maps
  const planByGapId = new Map<string, PlannedGap>(planGaps.map((g) => [g.gap_id, g]))
  const packetByGapId = new Map<string, GapPacket>(
    (documents ?? []).map((p) => [p.gap_id, p])
  )

  if (displayGaps.length === 0) return null

  return (
    <div>
      <div className="flex items-center gap-2 mb-3">
        <BookMarked size={14} className="text-ink-muted" />
        <h2 className="text-xs font-semibold text-ink-secondary uppercase tracking-wider">
          Research Gaps
        </h2>
        <span className="text-[10px] text-ink-muted bg-surface-muted border border-border px-1.5 py-0.5 rounded">
          {displayGaps.length}
        </span>
      </div>

      <div className="space-y-3">
        {displayGaps.map((gap) => {
          const gapMapGap = gapMapGaps.find((g) => g.gap_id === gap.gap_id)
          const planGap = planByGapId.get(gap.gap_id)
          const packet = packetByGapId.get(gap.gap_id)

          return (
            <GapCard
              key={gap.gap_id}
              gap={gapMapGap ?? gap}
              planGap={planGap}
              packet={packet}
            />
          )
        })}
      </div>
    </div>
  )
}
