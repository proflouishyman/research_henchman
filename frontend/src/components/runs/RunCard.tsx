// Single run row in the sidebar: manuscript name, status badge, timestamp.

import { Clock, FileText, AlertTriangle } from 'lucide-react'
import type { RunRow, RunStatus } from '../../types/contracts'
import { useUIStore } from '../../store/ui'

interface RunCardProps {
  run: RunRow
  isSelected: boolean
}

function statusLabel(status: RunStatus): string {
  const labels: Record<RunStatus, string> = {
    queued: 'Queued',
    analyzing: 'Analyzing',
    planning: 'Planning',
    pulling: 'Pulling',
    ingesting: 'Ingesting',
    fitting: 'Fitting',
    complete: 'Complete',
    failed: 'Failed',
    partial: 'Partial',
  }
  return labels[status] ?? status
}

function statusClasses(status: RunStatus): string {
  switch (status) {
    case 'complete':
      return 'bg-emerald-50 text-emerald-700 border border-emerald-200'
    case 'failed':
      return 'bg-red-50 text-red-700 border border-red-200'
    case 'partial':
      return 'bg-amber-50 text-amber-700 border border-amber-200'
    case 'queued':
      return 'bg-gray-100 text-gray-500 border border-gray-200'
    default:
      // Active stages
      return 'bg-amber-50 text-amber-700 border border-amber-200'
  }
}

function isActive(status: RunStatus): boolean {
  return ['queued', 'analyzing', 'planning', 'pulling', 'ingesting', 'fitting'].includes(status)
}

function formatRelativeTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime()
  const mins = Math.floor(diff / 60_000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `${hrs}h ago`
  return `${Math.floor(hrs / 24)}d ago`
}

function manuscriptName(path: string): string {
  return path.split('/').pop()?.replace(/\.[^.]+$/, '') ?? path
}

export function RunCard({ run, isSelected }: RunCardProps) {
  const { setSelectedRunId } = useUIStore()

  return (
    <button
      onClick={() => setSelectedRunId(run.run_id)}
      className={`w-full text-left px-3 py-3 rounded-lg transition-colors group ${
        isSelected
          ? 'bg-accent-light border border-accent/30'
          : 'hover:bg-surface-muted border border-transparent'
      }`}
    >
      {/* Manuscript name */}
      <div className="flex items-start gap-2 mb-1.5">
        <FileText
          size={13}
          className={`mt-0.5 shrink-0 ${isSelected ? 'text-accent' : 'text-ink-muted'}`}
        />
        <span
          className={`text-xs font-medium leading-snug line-clamp-2 ${
            isSelected ? 'text-ink' : 'text-ink-secondary'
          }`}
        >
          {manuscriptName(run.manuscript_path)}
        </span>
      </div>

      {/* Status row */}
      <div className="flex items-center justify-between pl-5">
        <div className="flex items-center gap-1.5">
          {isActive(run.status) && (
            <span className="relative flex h-1.5 w-1.5">
              <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-accent opacity-75" />
              <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-accent" />
            </span>
          )}
          {run.status === 'failed' && <AlertTriangle size={10} className="text-red-500" />}
          <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded ${statusClasses(run.status)}`}>
            {statusLabel(run.status)}
          </span>
        </div>

        <div className="flex items-center gap-1 text-ink-muted">
          <Clock size={10} />
          <span className="text-[10px]">{formatRelativeTime(run.created_at)}</span>
        </div>
      </div>

      {/* Stage detail */}
      {run.stage_detail && (
        <p className="text-[10px] text-ink-muted pl-5 mt-1 truncate">{run.stage_detail}</p>
      )}
    </button>
  )
}
