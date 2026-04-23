// Collapsible section for one source's documents inside the evidence panel.

import { useState } from 'react'
import { ChevronDown, ChevronRight, Database } from 'lucide-react'
import type { SourcePacket } from '../../types/contracts'
import { DocumentCard } from './DocumentCard'

interface SourcePacketSectionProps {
  packet: SourcePacket
}

function sourceLabel(sourceId: string): string {
  return sourceId.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

export function SourcePacketSection({ packet }: SourcePacketSectionProps) {
  const [open, setOpen] = useState(true)

  return (
    <div className="border border-border rounded-lg overflow-hidden">
      {/* Header */}
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center gap-2 px-3 py-2.5 bg-surface-muted hover:bg-border/40 transition-colors text-left"
      >
        <Database size={13} className="text-ink-muted shrink-0" />
        <span className="text-xs font-semibold text-ink-secondary flex-1">
          {sourceLabel(packet.source_id)}
        </span>
        <span className="text-[10px] text-ink-muted bg-surface-card border border-border px-1.5 py-0.5 rounded">
          {packet.documents.length} doc{packet.documents.length !== 1 ? 's' : ''}
        </span>
        {open ? (
          <ChevronDown size={12} className="text-ink-muted" />
        ) : (
          <ChevronRight size={12} className="text-ink-muted" />
        )}
      </button>

      {/* Documents */}
      {open && (
        <div className="p-3 space-y-2 bg-surface-card">
          {packet.documents.length === 0 && (
            <p className="text-xs text-ink-muted text-center py-2">No documents available.</p>
          )}
          {packet.documents.map((doc) => (
            <DocumentCard key={doc.evidence_id} doc={doc} />
          ))}
        </div>
      )}
    </div>
  )
}
