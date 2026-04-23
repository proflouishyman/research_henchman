// Full gap card showing claim, type/priority badges, confidence, sources, ladder.

import { useState } from 'react'
import { ChevronDown, ChevronRight, BookOpen, FolderOpen } from 'lucide-react'
import type { Gap, PlannedGap, GapPacket } from '../../types/contracts'
import { useUIStore } from '../../store/ui'
import { openGapFolder } from '../../lib/api'
import { ConfidenceBar } from './ConfidenceBar'
import { SourceBadges } from './SourceBadges'
import { AccordionLadder } from './AccordionLadder'

interface GapCardProps {
  runId: string
  gap: Gap | PlannedGap
  planGap?: PlannedGap
  packet?: GapPacket
}

function priorityClasses(priority: string): string {
  switch (priority) {
    case 'high':
      return 'bg-red-50 text-red-700 border-red-200'
    case 'medium':
      return 'bg-amber-50 text-amber-700 border-amber-200'
    default:
      return 'bg-gray-100 text-gray-500 border-gray-200'
  }
}

function gapTypeClasses(type: string): string {
  return type === 'explicit'
    ? 'bg-blue-50 text-blue-700 border-blue-200'
    : 'bg-violet-50 text-violet-700 border-violet-200'
}

export function GapCard({ runId, gap, planGap, packet }: GapCardProps) {
  const [expanded, setExpanded] = useState(false)
  const [folderOpened, setFolderOpened] = useState(false)
  const [folderError, setFolderError] = useState<string | null>(null)
  const { setSelectedGapId } = useUIStore()

  const handleOpenFolder = async (e: React.MouseEvent) => {
    e.stopPropagation()
    setFolderError(null)
    try {
      await openGapFolder(runId, gap.gap_id)
      setFolderOpened(true)
      setTimeout(() => setFolderOpened(false), 2000)
    } catch (err) {
      setFolderError((err as Error).message.includes('404') ? 'Folder not ready yet' : 'Could not open folder')
      setTimeout(() => setFolderError(null), 3000)
    }
  }

  const claim = gap.claim_text ?? ''
  const truncatedClaim = claim.length > 120 ? claim.slice(0, 120) + '…' : claim

  const confidence = planGap?.route_confidence ?? (gap as PlannedGap).route_confidence
  const sources = planGap?.preferred_sources ?? (gap as PlannedGap).preferred_sources ?? []
  const ladder = planGap?.query_ladder ?? (gap as PlannedGap).query_ladder
  const chapter = gap.chapter
  const gapType = gap.gap_type
  const priority = gap.priority

  const docCount = packet?.sources.reduce((acc, s) => acc + s.documents.length, 0) ?? 0

  return (
    <div className="bg-surface-card border border-border rounded-xl shadow-card overflow-hidden transition-shadow hover:shadow-panel">
      {/* Card header — always visible */}
      <div
        className="px-4 py-3 cursor-pointer"
        onClick={() => setExpanded((e) => !e)}
      >
        {/* Chapter + badges row */}
        <div className="flex items-center gap-2 mb-1.5">
          {chapter && (
            <span className="text-[10px] font-semibold text-ink-muted uppercase tracking-wider">
              {chapter}
            </span>
          )}
          <div className="flex items-center gap-1 ml-auto">
            <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded border ${gapTypeClasses(gapType)}`}>
              {gapType}
            </span>
            <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded border ${priorityClasses(priority)}`}>
              {priority}
            </span>
            {docCount > 0 && (
              <button
                onClick={(e) => {
                  e.stopPropagation()
                  setSelectedGapId(gap.gap_id)
                }}
                className="flex items-center gap-1 text-[10px] font-medium px-1.5 py-0.5 rounded border bg-accent-light border-accent/30 text-accent hover:bg-accent hover:text-white transition-colors"
              >
                <BookOpen size={9} />
                {docCount} doc{docCount !== 1 ? 's' : ''}
              </button>
            )}
            <button
              onClick={handleOpenFolder}
              title="Open in Finder"
              className={`flex items-center gap-1 text-[10px] font-medium px-1.5 py-0.5 rounded border transition-colors ${
                folderOpened
                  ? 'bg-emerald-50 border-emerald-200 text-emerald-700'
                  : folderError
                  ? 'bg-red-50 border-red-200 text-red-600'
                  : 'bg-surface-muted border-border text-ink-muted hover:text-ink hover:bg-border/50'
              }`}
            >
              <FolderOpen size={9} />
              {folderOpened ? 'Opened' : folderError ?? 'Folder'}
            </button>
          </div>
        </div>

        {/* Claim text */}
        <p className="text-xs text-ink leading-relaxed">
          {expanded ? claim : truncatedClaim}
        </p>

        {/* Confidence bar */}
        {confidence !== undefined && confidence > 0 && (
          <div className="mt-2">
            <ConfidenceBar value={confidence} />
          </div>
        )}

        {/* Source badges */}
        {sources.length > 0 && (
          <div className="mt-2">
            <SourceBadges sources={sources} />
          </div>
        )}

        {/* Expand toggle */}
        <button
          onClick={(e) => {
            e.stopPropagation()
            setExpanded((v) => !v)
          }}
          className="mt-2 flex items-center gap-1 text-[10px] text-ink-muted hover:text-ink-secondary transition-colors"
        >
          {expanded ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
          {expanded ? 'Collapse' : 'Show details'}
        </button>
      </div>

      {/* Expanded section */}
      {expanded && (
        <div className="px-4 pb-4 border-t border-border/50 pt-3 space-y-3">
          {/* Source text excerpt */}
          {(gap as Gap).source_text_excerpt && (
            <div>
              <p className="text-[10px] text-ink-muted mb-1 font-medium uppercase tracking-wide">
                Source excerpt
              </p>
              <blockquote className="text-[11px] font-mono text-ink-secondary leading-relaxed border-l-2 border-accent/40 pl-3 italic">
                {(gap as Gap).source_text_excerpt}
              </blockquote>
            </div>
          )}

          {/* Plan gap metadata */}
          {planGap && (
            <div className="grid grid-cols-2 gap-2">
              {planGap.claim_kind && (
                <div>
                  <p className="text-[10px] text-ink-muted font-medium">Claim kind</p>
                  <p className="text-[11px] text-ink-secondary">{planGap.claim_kind.replace(/_/g, ' ')}</p>
                </div>
              )}
              {planGap.evidence_need && (
                <div>
                  <p className="text-[10px] text-ink-muted font-medium">Evidence need</p>
                  <p className="text-[11px] text-ink-secondary">{planGap.evidence_need.replace(/_/g, ' ')}</p>
                </div>
              )}
              {planGap.rationale && (
                <div className="col-span-2">
                  <p className="text-[10px] text-ink-muted font-medium">Rationale</p>
                  <p className="text-[11px] text-ink-secondary leading-relaxed">{planGap.rationale}</p>
                </div>
              )}
              {planGap.needs_review && (
                <div className="col-span-2">
                  <span className="text-[10px] font-medium bg-amber-50 border border-amber-200 text-amber-700 px-2 py-0.5 rounded">
                    Needs review
                  </span>
                </div>
              )}
            </div>
          )}

          {/* Search queries */}
          {((planGap?.search_queries ?? (gap as PlannedGap).search_queries) ?? []).length > 0 && (
            <div>
              <p className="text-[10px] text-ink-muted mb-1.5 font-medium uppercase tracking-wide">
                Search queries
              </p>
              <ul className="space-y-1">
                {(planGap?.search_queries ?? (gap as PlannedGap).search_queries ?? (gap as Gap).suggested_queries ?? []).map(
                  (q, i) => (
                    <li key={i} className="text-[11px] font-mono text-ink-secondary leading-relaxed">
                      <span className="text-ink-muted mr-1">{i + 1}.</span>
                      {q}
                    </li>
                  )
                )}
              </ul>
            </div>
          )}

          {/* Accordion ladder */}
          {ladder && typeof ladder === 'object' && 'constrained' in ladder && (
            <AccordionLadder ladder={ladder} />
          )}

          {/* Action buttons row */}
          <div className="flex gap-2">
            {docCount > 0 && (
              <button
                onClick={() => setSelectedGapId(gap.gap_id)}
                className="flex-1 flex items-center justify-center gap-1.5 py-2 bg-accent-light border border-accent/30 rounded-lg text-xs font-medium text-accent hover:bg-accent hover:text-white transition-colors"
              >
                <BookOpen size={12} />
                View {docCount} doc{docCount !== 1 ? 's' : ''}
              </button>
            )}
            <button
              onClick={handleOpenFolder}
              title="Open gap folder in Finder"
              className={`flex items-center justify-center gap-1.5 py-2 px-3 rounded-lg text-xs font-medium border transition-colors ${
                folderOpened
                  ? 'bg-emerald-50 border-emerald-200 text-emerald-700'
                  : folderError
                  ? 'bg-red-50 border-red-200 text-red-600'
                  : 'bg-surface-muted border-border text-ink-secondary hover:text-ink hover:bg-border/50'
              } ${docCount === 0 ? 'flex-1' : ''}`}
            >
              <FolderOpen size={12} />
              {folderOpened ? 'Opened in Finder' : folderError ?? 'Open Folder'}
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
