// Left sidebar listing all runs.

import { useState } from 'react'
import { Layers, Trash2, Loader2 } from 'lucide-react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { useRuns } from '../../hooks/useRuns'
import { useUIStore } from '../../store/ui'
import { RunCard } from './RunCard'

async function resetAll(): Promise<{ reset: boolean; cleared: string[] }> {
  const res = await fetch('/api/orchestrator/reset', { method: 'POST' })
  if (!res.ok) throw new Error(`Reset failed: ${res.status}`)
  return res.json()
}

export function RunSidebar() {
  const { data: runs, isLoading, isError } = useRuns()
  const { selectedRunId, setSelectedRunId } = useUIStore()
  const qc = useQueryClient()
  const [confirming, setConfirming] = useState(false)

  const resetMutation = useMutation({
    mutationFn: resetAll,
    onSuccess: () => {
      setConfirming(false)
      setSelectedRunId(null)
      qc.invalidateQueries({ queryKey: ['runs'] })
    },
  })

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
            <span className="text-[10px] text-ink-muted bg-surface-muted px-1.5 py-0.5 rounded">
              {runs.length}
            </span>
          )}
          <div className="ml-auto flex items-center gap-1">
            {confirming ? (
              <>
                <span className="text-[10px] text-red-600 font-medium">Clear all?</span>
                <button
                  onClick={() => resetMutation.mutate()}
                  disabled={resetMutation.isPending}
                  className="text-[10px] px-2 py-0.5 bg-red-600 text-white rounded font-medium hover:bg-red-700 disabled:opacity-50 transition-colors"
                >
                  {resetMutation.isPending ? <Loader2 size={9} className="animate-spin" /> : 'Yes'}
                </button>
                <button
                  onClick={() => setConfirming(false)}
                  className="text-[10px] px-2 py-0.5 border border-border text-ink-muted rounded hover:text-ink transition-colors"
                >
                  No
                </button>
              </>
            ) : (
              <button
                onClick={() => setConfirming(true)}
                title="Clear all runs and exports"
                className="p-1 rounded text-ink-muted hover:text-red-500 hover:bg-red-50 transition-colors"
              >
                <Trash2 size={12} />
              </button>
            )}
          </div>
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
