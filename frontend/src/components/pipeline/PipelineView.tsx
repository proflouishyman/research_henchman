// Main pipeline view: stage rail, gap list, event log for the selected run.

import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { RefreshCw, AlertTriangle, FileText, FolderOpen } from 'lucide-react'
import { useUIStore } from '../../store/ui'
import { useRun } from '../../hooks/useRun'
import { useEvents } from '../../hooks/useEvents'
import { useDocuments } from '../../hooks/useDocuments'
import { retryRun, openRunFolder } from '../../lib/api'
import { PipelineRail } from './PipelineRail'
import { EventLog } from './EventLog'
import { GapList } from '../gaps/GapList'

function manuscriptName(path: string): string {
  return path.split('/').pop()?.replace(/\.[^.]+$/, '') ?? path
}

export function PipelineView() {
  const { selectedRunId } = useUIStore()
  const { data: run, isLoading } = useRun(selectedRunId)
  const { events } = useEvents(selectedRunId, run?.status)
  const { data: documents } = useDocuments(selectedRunId, run?.status)
  const qc = useQueryClient()

  const [bundleOpened, setBundleOpened] = useState(false)
  const [bundleError, setBundleError] = useState<string | null>(null)

  const retryMutation = useMutation({
    mutationFn: () => retryRun(selectedRunId!),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['runs'] })
      qc.invalidateQueries({ queryKey: ['run', selectedRunId] })
    },
  })

  const handleOpenBundle = async () => {
    if (!selectedRunId) return
    setBundleError(null)
    try {
      await openRunFolder(selectedRunId)
      setBundleOpened(true)
      setTimeout(() => setBundleOpened(false), 2000)
    } catch {
      setBundleError('Not ready')
      setTimeout(() => setBundleError(null), 3000)
    }
  }

  if (!selectedRunId) {
    return (
      <div className="flex flex-col items-center justify-center h-full text-center px-8">
        <div className="w-14 h-14 bg-surface-muted rounded-2xl flex items-center justify-center mb-4">
          <FileText size={24} className="text-ink-muted" />
        </div>
        <h2 className="text-sm font-semibold text-ink-secondary mb-1">No run selected</h2>
        <p className="text-xs text-ink-muted max-w-xs leading-relaxed">
          Select a run from the sidebar or click <strong>New Run</strong> to begin gap analysis on a
          manuscript.
        </p>
      </div>
    )
  }

  if (isLoading) {
    return (
      <div className="flex items-center justify-center h-40">
        <div className="w-6 h-6 border-2 border-accent/30 border-t-accent rounded-full animate-spin" />
      </div>
    )
  }

  if (!run) {
    return (
      <div className="p-6">
        <p className="text-sm text-red-500">Run not found.</p>
      </div>
    )
  }

  const isActive = ['queued', 'analyzing', 'planning', 'pulling', 'ingesting', 'fitting'].includes(
    run.status
  )

  const gaps = run.research_plan?.gaps ?? run.gap_map?.gaps ?? []
  const planGaps = run.research_plan?.gaps ?? []

  return (
    <div className="flex flex-col gap-0 h-full">
      {/* Run header */}
      <div className="px-6 pt-5 pb-4 border-b border-border bg-surface-card">
        <div className="flex items-start justify-between gap-4">
          <div>
            <h1 className="text-base font-semibold text-ink leading-tight">
              {manuscriptName(run.manuscript_path)}
            </h1>
            <p className="text-xs text-ink-muted mt-1 font-mono truncate max-w-sm">
              {run.manuscript_path}
            </p>
          </div>

          <div className="flex items-center gap-2 shrink-0">
            {run.status === 'complete' && (
              <button
                onClick={handleOpenBundle}
                title="Open research bundle in Finder"
                className={`flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md border transition-colors ${
                  bundleOpened
                    ? 'bg-emerald-50 border-emerald-200 text-emerald-700'
                    : bundleError
                    ? 'bg-red-50 border-red-200 text-red-600'
                    : 'bg-surface-muted border-border text-ink-secondary hover:text-ink hover:bg-border/50'
                }`}
              >
                <FolderOpen size={12} />
                {bundleOpened ? 'Opened' : bundleError ?? 'Open Bundle'}
              </button>
            )}
            {(run.status === 'failed' || run.status === 'partial') && (
              <button
                onClick={() => retryMutation.mutate()}
                disabled={retryMutation.isPending}
                className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium text-white bg-accent rounded-md hover:bg-accent-hover disabled:opacity-50 transition-colors"
              >
                <RefreshCw size={12} className={retryMutation.isPending ? 'animate-spin' : ''} />
                Retry
              </button>
            )}
          </div>
        </div>

        {/* Stage rail */}
        <div className="mt-3">
          <PipelineRail status={run.status} />
        </div>

        {/* Error banner */}
        {run.status === 'failed' && run.error && (
          <div className="mt-3 flex items-start gap-2 p-3 bg-red-50 border border-red-200 rounded-lg">
            <AlertTriangle size={14} className="text-red-500 shrink-0 mt-0.5" />
            <p className="text-xs text-red-700 leading-relaxed">{run.error}</p>
          </div>
        )}

        {/* Plan summary */}
        {run.research_plan?.plan_summary && (
          <p className="mt-3 text-xs text-ink-secondary leading-relaxed italic border-l-2 border-accent/40 pl-3">
            {run.research_plan.plan_summary}
          </p>
        )}

        {/* Stats row */}
        <div className="mt-3 flex items-center gap-4 text-xs text-ink-muted">
          {run.gap_map && (
            <>
              <span>
                <strong className="text-ink">{run.gap_map.explicit_count}</strong> explicit
              </span>
              <span>
                <strong className="text-ink">{run.gap_map.implicit_count}</strong> implicit
              </span>
            </>
          )}
          {planGaps.length > 0 && (
            <span>
              <strong className="text-ink">{planGaps.length}</strong> planned gaps
            </span>
          )}
          {documents && (
            <span>
              <strong className="text-ink">{documents.length}</strong> gap packets
            </span>
          )}
        </div>
      </div>

      {/* Scrollable content */}
      <div className="flex-1 overflow-y-auto px-6 py-4 space-y-4">
        {/* Event log */}
        <EventLog events={events} autoExpand={isActive} />

        {/* Gap list — shows research plan gaps when available, otherwise gap map */}
        {gaps.length > 0 && (
          <GapList
            runId={selectedRunId}
            gapMapGaps={run.gap_map?.gaps ?? []}
            planGaps={planGaps}
            documents={documents}
          />
        )}
      </div>
    </div>
  )
}
