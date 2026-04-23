// Horizontal stage track showing pipeline progress.

import { STAGES, StageIndicator } from './StageIndicator'
import type { StageState } from './StageIndicator'
import type { RunStatus } from '../../types/contracts'

interface PipelineRailProps {
  status: RunStatus
}

const STAGE_ORDER = ['analyzing', 'planning', 'pulling', 'ingesting', 'fitting', 'complete']

function stageStateForRun(stageId: string, runStatus: RunStatus): StageState {
  if (runStatus === 'failed') {
    // Find the active stage index to determine which ones are complete vs failed
    const stageIdx = STAGE_ORDER.indexOf(stageId)
    const statusIdx = STAGE_ORDER.indexOf(runStatus)
    // For failed runs, mark all earlier stages as complete, current as failed
    if (statusIdx >= 0 && stageIdx < statusIdx) return 'complete'
    if (statusIdx >= 0 && stageIdx === statusIdx) return 'failed'
    return 'pending'
  }

  if (runStatus === 'complete' || runStatus === 'partial') {
    return 'complete'
  }

  const runIdx = STAGE_ORDER.indexOf(runStatus)
  const stageIdx = STAGE_ORDER.indexOf(stageId)

  if (runIdx < 0) return 'pending'
  if (stageIdx < runIdx) return 'complete'
  if (stageIdx === runIdx) return 'active'
  return 'pending'
}

export function PipelineRail({ status }: PipelineRailProps) {
  return (
    <div className="flex items-center flex-wrap gap-y-2 py-3 px-1">
      {STAGES.map((stage, i) => (
        <StageIndicator
          key={stage.id}
          stage={stage}
          state={stageStateForRun(stage.id, status)}
          isLast={i === STAGES.length - 1}
        />
      ))}
    </div>
  )
}
