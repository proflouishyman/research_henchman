// Modal for selecting a manuscript and starting a new run.

import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { X, FileText, Loader2, ChevronRight } from 'lucide-react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { fetchManuscripts, createRun } from '../../lib/api'
import { useUIStore } from '../../store/ui'

export function ManuscriptPicker() {
  const { setNewRunModalOpen, setSelectedRunId } = useUIStore()
  const qc = useQueryClient()

  const [selected, setSelected] = useState<string | null>(null)

  const { data: manuscripts, isLoading } = useQuery({
    queryKey: ['manuscripts'],
    queryFn: fetchManuscripts,
  })

  const mutation = useMutation({
    mutationFn: (path: string) => createRun(path),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ['runs'] })
      setSelectedRunId(data.run_id)
      setNewRunModalOpen(false)
    },
  })

  const handleStart = () => {
    if (!selected) return
    mutation.mutate(selected)
  }

  return (
    <AnimatePresence>
      {/* Backdrop */}
      <motion.div
        key="backdrop"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        className="fixed inset-0 z-40 bg-black/30 backdrop-blur-sm"
        onClick={() => setNewRunModalOpen(false)}
      />

      {/* Modal */}
      <motion.div
        key="modal"
        initial={{ opacity: 0, scale: 0.96, y: 8 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.96, y: 8 }}
        transition={{ duration: 0.18, ease: 'easeOut' }}
        className="fixed inset-0 z-50 flex items-center justify-center p-4"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="bg-surface-card rounded-xl shadow-modal w-full max-w-md">
          {/* Header */}
          <div className="flex items-center justify-between px-5 py-4 border-b border-border">
            <div>
              <h2 className="text-sm font-semibold text-ink">New Research Run</h2>
              <p className="text-xs text-ink-secondary mt-0.5">Select a manuscript to analyze</p>
            </div>
            <button
              onClick={() => setNewRunModalOpen(false)}
              className="p-1.5 rounded-md text-ink-muted hover:text-ink hover:bg-surface-muted transition-colors"
            >
              <X size={15} />
            </button>
          </div>

          {/* Manuscript list */}
          <div className="p-4 max-h-72 overflow-y-auto">
            {isLoading && (
              <div className="flex items-center justify-center py-8">
                <Loader2 size={20} className="animate-spin text-accent" />
              </div>
            )}

            {!isLoading && manuscripts?.length === 0 && (
              <p className="text-xs text-ink-muted text-center py-8">
                No manuscripts found in the workspace.
              </p>
            )}

            <div className="space-y-1">
              {manuscripts?.map((m) => {
                const name = m.name || m.path.split('/').pop() || m.path
                const isSelected = selected === m.path
                return (
                  <button
                    key={m.path}
                    onClick={() => setSelected(m.path)}
                    className={`w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-left transition-colors ${
                      isSelected
                        ? 'bg-accent-light border border-accent/40'
                        : 'hover:bg-surface-muted border border-transparent'
                    }`}
                  >
                    <FileText
                      size={15}
                      className={isSelected ? 'text-accent' : 'text-ink-muted'}
                    />
                    <span
                      className={`text-xs font-medium flex-1 truncate ${
                        isSelected ? 'text-ink' : 'text-ink-secondary'
                      }`}
                    >
                      {name}
                    </span>
                    {isSelected && <ChevronRight size={13} className="text-accent shrink-0" />}
                  </button>
                )
              })}
            </div>
          </div>

          {/* Footer */}
          <div className="flex items-center justify-end gap-2 px-5 py-3 border-t border-border">
            <button
              onClick={() => setNewRunModalOpen(false)}
              className="px-3 py-1.5 text-xs text-ink-secondary hover:text-ink transition-colors"
            >
              Cancel
            </button>
            <button
              onClick={handleStart}
              disabled={!selected || mutation.isPending}
              className="flex items-center gap-1.5 px-4 py-1.5 bg-accent text-white text-xs font-medium rounded-md hover:bg-accent-hover disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
            >
              {mutation.isPending && <Loader2 size={12} className="animate-spin" />}
              Start Analysis
            </button>
          </div>

          {mutation.isError && (
            <p className="px-5 pb-3 text-xs text-red-500">
              {(mutation.error as Error).message}
            </p>
          )}
        </div>
      </motion.div>
    </AnimatePresence>
  )
}
