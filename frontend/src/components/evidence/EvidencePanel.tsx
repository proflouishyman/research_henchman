// Slide-in right panel showing evidence documents for the selected gap.

import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { X, BookOpen, Loader2, FolderOpen } from 'lucide-react'
import { useUIStore } from '../../store/ui'
import { useDocuments } from '../../hooks/useDocuments'
import { useRun } from '../../hooks/useRun'
import { openGapFolder } from '../../lib/api'
import { SourcePacketSection } from './SourcePacketSection'

interface EvidencePanelProps {
  open: boolean
}

export function EvidencePanel({ open }: EvidencePanelProps) {
  const { selectedRunId, selectedGapId, setSelectedGapId, setEvidencePanelOpen } =
    useUIStore()
  const { data: run } = useRun(selectedRunId)
  const { data: documents, isLoading } = useDocuments(selectedRunId, run?.status)
  const [folderOpened, setFolderOpened] = useState(false)
  const [folderError, setFolderError] = useState<string | null>(null)

  const handleOpenFolder = async () => {
    if (!selectedRunId || !selectedGapId) return
    setFolderError(null)
    try {
      await openGapFolder(selectedRunId, selectedGapId)
      setFolderOpened(true)
      setTimeout(() => setFolderOpened(false), 2000)
    } catch (err) {
      setFolderError((err as Error).message.includes('404') ? 'Not ready' : 'Error')
      setTimeout(() => setFolderError(null), 3000)
    }
  }

  const handleClose = () => {
    setSelectedGapId(null)
    setEvidencePanelOpen(false)
  }

  // Find the selected gap packet
  const packet = documents?.find((p) => p.gap_id === selectedGapId)

  // Find gap claim text from run data
  const gapClaim =
    run?.research_plan?.gaps?.find((g) => g.gap_id === selectedGapId)?.claim_text ??
    run?.gap_map?.gaps?.find((g) => g.gap_id === selectedGapId)?.claim_text ??
    'Selected gap'

  return (
    <AnimatePresence>
      {open && (
        <motion.aside
          key="evidence-panel"
          initial={{ width: 0, opacity: 0 }}
          animate={{ width: 420, opacity: 1 }}
          exit={{ width: 0, opacity: 0 }}
          transition={{ duration: 0.22, ease: [0.4, 0, 0.2, 1] }}
          className="shrink-0 border-l border-border bg-surface-card flex flex-col overflow-hidden"
          style={{ minWidth: 0 }}
        >
          {/* Panel header */}
          <div className="flex items-start justify-between gap-3 px-4 py-3.5 border-b border-border shrink-0">
            <div className="flex items-start gap-2 min-w-0">
              <BookOpen size={15} className="text-accent mt-0.5 shrink-0" />
              <div className="min-w-0">
                <p className="text-[10px] font-semibold text-ink-muted uppercase tracking-wider mb-0.5">
                  Evidence
                </p>
                <p className="text-xs font-medium text-ink leading-snug line-clamp-3">{gapClaim}</p>
              </div>
            </div>
            <div className="flex items-center gap-1 shrink-0">
              <button
                onClick={handleOpenFolder}
                title="Open gap folder in Finder"
                className={`p-1.5 rounded-md transition-colors ${
                  folderOpened
                    ? 'text-emerald-600 bg-emerald-50'
                    : folderError
                    ? 'text-red-500 bg-red-50'
                    : 'text-ink-muted hover:text-accent hover:bg-accent-light'
                }`}
              >
                <FolderOpen size={14} />
              </button>
              <button
                onClick={handleClose}
                className="p-1.5 rounded-md text-ink-muted hover:text-ink hover:bg-surface-muted transition-colors"
              >
                <X size={14} />
              </button>
            </div>
          </div>

          {/* Content */}
          <div className="flex-1 overflow-y-auto p-4 space-y-3">
            {isLoading && (
              <div className="flex items-center justify-center py-12">
                <Loader2 size={20} className="animate-spin text-accent" />
              </div>
            )}

            {!isLoading && !packet && (
              <div className="flex flex-col items-center justify-center py-12 text-center">
                <BookOpen size={28} className="text-ink-muted mb-3" />
                <p className="text-xs text-ink-muted leading-relaxed">
                  No documents found for this gap yet.
                  <br />
                  Run must complete before evidence appears.
                </p>
              </div>
            )}

            {packet?.sources.map((source) => (
              <SourcePacketSection key={source.source_id} packet={source} />
            ))}
          </div>
        </motion.aside>
      )}
    </AnimatePresence>
  )
}
