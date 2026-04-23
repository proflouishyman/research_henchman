// Left sidebar listing all runs.

import { Layers } from 'lucide-react'
import { useRuns } from '../../hooks/useRuns'
import { useUIStore } from '../../store/ui'
import { RunCard } from './RunCard'

export function RunSidebar() {
  const { data: runs, isLoading, isError } = useRuns()
  const { selectedRunId } = useUIStore()

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="px-4 py-3 border-b border-border">
        <div className="flex items-center gap-2">
          <Layers size={13} className="text-ink-muted" />
          <span className="text-xs font-semibold text-ink-secondary uppercase tracking-wider">
            Runs
          </span>
          {runs && (
            <span className="ml-auto text-[10px] text-ink-muted bg-surface-muted px-1.5 py-0.5 rounded">
              {runs.length}
            </span>
          )}
        </div>
      </div>

      {/* Run list */}
      <div className="flex-1 overflow-y-auto p-2 space-y-1">
        {isLoading && (
          <div className="px-3 py-8 text-center">
            <div className="w-5 h-5 border-2 border-accent/30 border-t-accent rounded-full animate-spin mx-auto" />
          </div>
        )}

        {isError && (
          <p className="px-3 py-4 text-xs text-red-500 text-center">Failed to load runs</p>
        )}

        {!isLoading && !isError && runs?.length === 0 && (
          <div className="px-3 py-8 text-center">
            <p className="text-xs text-ink-muted leading-relaxed">
              No runs yet. Click <strong className="text-ink-secondary">New Run</strong> to start.
            </p>
          </div>
        )}

        {runs?.map((run) => (
          <RunCard key={run.run_id} run={run} isSelected={run.run_id === selectedRunId} />
        ))}
      </div>
    </div>
  )
}
