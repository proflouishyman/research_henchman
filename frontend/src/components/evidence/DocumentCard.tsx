// One document inside a source packet: quality badge, excerpt, link, blocked warning.

import { ExternalLink, AlertTriangle } from 'lucide-react'
import type { LinkedDocument } from '../../types/contracts'
import { QualityBadge } from './QualityBadge'

interface DocumentCardProps {
  doc: LinkedDocument
}

export function DocumentCard({ doc }: DocumentCardProps) {
  const title = doc.title || 'Untitled document'
  const isBlocked = !!doc.blocked_reason

  return (
    <div
      className={`p-3 rounded-lg border ${
        isBlocked ? 'bg-orange-50 border-orange-200' : 'bg-surface-card border-border'
      }`}
    >
      {/* Title row */}
      <div className="flex items-start justify-between gap-2 mb-1.5">
        <p className="text-xs font-medium text-ink leading-snug flex-1 line-clamp-2">{title}</p>
        <div className="flex items-center gap-1 shrink-0">
          <QualityBadge rank={doc.quality_rank} label={doc.quality_label || doc.quality_rank} />
          {doc.anchor_url && (
            <a
              href={doc.anchor_url}
              target="_blank"
              rel="noopener noreferrer"
              className="p-1 rounded text-ink-muted hover:text-accent transition-colors"
              title="Open source"
            >
              <ExternalLink size={11} />
            </a>
          )}
        </div>
      </div>

      {/* Blocked banner */}
      {isBlocked && (
        <div className="flex items-start gap-1.5 p-2 bg-orange-100 border border-orange-300 rounded-md mb-2">
          <AlertTriangle size={12} className="text-orange-600 shrink-0 mt-px" />
          <div>
            <p className="text-[10px] font-semibold text-orange-700">Access blocked</p>
            {doc.blocked_reason && (
              <p className="text-[10px] text-orange-600 mt-0.5">{doc.blocked_reason}</p>
            )}
            {doc.action_required && (
              <p className="text-[10px] text-orange-700 font-medium mt-1">{doc.action_required}</p>
            )}
          </div>
        </div>
      )}

      {/* Excerpt */}
      {doc.excerpt && (
        <p className="text-[11px] font-mono text-ink-secondary leading-relaxed line-clamp-4 bg-surface-muted rounded p-2 border border-border/50">
          {doc.excerpt}
        </p>
      )}

      {/* Locator */}
      {doc.source_locator && (
        <p className="text-[10px] text-ink-muted mt-1.5 truncate font-mono">
          {doc.source_locator}
        </p>
      )}
    </div>
  )
}
