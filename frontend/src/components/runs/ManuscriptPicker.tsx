// Modal for selecting a manuscript and starting a new run.
// Primary: native file picker (any .docx/.pdf/.md/.txt on the computer).
// Secondary: workspace list of already-present files.

import { useRef, useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { X, FileText, Loader2, Upload, ChevronRight, FolderOpen } from 'lucide-react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { fetchManuscripts, uploadManuscript, createRun } from '../../lib/api'
import { useUIStore } from '../../store/ui'

export function ManuscriptPicker() {
  const { setNewRunModalOpen, setSelectedRunId } = useUIStore()
  const qc = useQueryClient()
  const fileInputRef = useRef<HTMLInputElement>(null)

  // Either a workspace path (string) or an uploaded File object
  const [selectedPath, setSelectedPath] = useState<string | null>(null)
  const [pendingFile, setPendingFile] = useState<File | null>(null)

  const { data: manuscripts, isLoading: listLoading } = useQuery({
    queryKey: ['manuscripts'],
    queryFn: fetchManuscripts,
  })

  const uploadMutation = useMutation({
    mutationFn: (file: File) => uploadManuscript(file),
    onSuccess: (data) => {
      runMutation.mutate(data.stored_path)
    },
  })

  const runMutation = useMutation({
    mutationFn: (path: string) => createRun(path),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ['runs'] })
      setSelectedRunId(data.run_id)
      setNewRunModalOpen(false)
    },
  })

  const isPending = uploadMutation.isPending || runMutation.isPending
  const error = uploadMutation.error || runMutation.error

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (!file) return
    setPendingFile(file)
    setSelectedPath(null)
  }

  const handleStart = () => {
    if (pendingFile) {
      uploadMutation.mutate(pendingFile)
    } else if (selectedPath) {
      runMutation.mutate(selectedPath)
    }
  }

  const canStart = (pendingFile !== null || selectedPath !== null) && !isPending

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
              <p className="text-xs text-ink-secondary mt-0.5">
                Choose a manuscript from your computer or workspace
              </p>
            </div>
            <button
              onClick={() => setNewRunModalOpen(false)}
              className="p-1.5 rounded-md text-ink-muted hover:text-ink hover:bg-surface-muted transition-colors"
            >
              <X size={15} />
            </button>
          </div>

          <div className="p-5 space-y-4">
            {/* ── Primary: file picker from computer ── */}
            <div>
              <p className="text-[11px] font-semibold text-ink-secondary uppercase tracking-wider mb-2">
                From your computer
              </p>

              <input
                ref={fileInputRef}
                type="file"
                accept=".docx,.pdf,.md,.txt"
                className="hidden"
                onChange={handleFileChange}
              />

              {pendingFile ? (
                <div className="flex items-center gap-3 p-3 rounded-lg border border-accent/40 bg-accent-light">
                  <FileText size={16} className="text-accent shrink-0" />
                  <span className="text-xs font-medium text-ink flex-1 truncate">
                    {pendingFile.name}
                  </span>
                  <button
                    onClick={() => {
                      setPendingFile(null)
                      if (fileInputRef.current) fileInputRef.current.value = ''
                    }}
                    className="text-ink-muted hover:text-ink transition-colors"
                  >
                    <X size={13} />
                  </button>
                </div>
              ) : (
                <button
                  onClick={() => fileInputRef.current?.click()}
                  className="w-full flex items-center justify-center gap-2 px-4 py-3 border-2 border-dashed border-border rounded-lg text-xs font-medium text-ink-secondary hover:border-accent hover:text-accent hover:bg-accent-light transition-colors"
                >
                  <FolderOpen size={15} />
                  Browse files (.docx, .pdf, .md, .txt)
                </button>
              )}
            </div>

            {/* ── Secondary: workspace list ── */}
            {(manuscripts && manuscripts.length > 0) && (
              <div>
                <p className="text-[11px] font-semibold text-ink-secondary uppercase tracking-wider mb-2">
                  From workspace
                </p>
                <div className="space-y-1 max-h-48 overflow-y-auto">
                  {listLoading && (
                    <div className="flex justify-center py-4">
                      <Loader2 size={16} className="animate-spin text-accent" />
                    </div>
                  )}
                  {manuscripts.map((m) => {
                    const name = m.name || m.path.split('/').pop() || m.path
                    const isSelected = !pendingFile && selectedPath === m.path
                    return (
                      <button
                        key={m.path}
                        onClick={() => { setSelectedPath(m.path); setPendingFile(null) }}
                        className={`w-full flex items-center gap-3 px-3 py-2 rounded-lg text-left transition-colors ${
                          isSelected
                            ? 'bg-accent-light border border-accent/40'
                            : 'hover:bg-surface-muted border border-transparent'
                        }`}
                      >
                        <FileText size={13} className={isSelected ? 'text-accent' : 'text-ink-muted'} />
                        <span className={`text-xs flex-1 truncate ${isSelected ? 'text-ink font-medium' : 'text-ink-secondary'}`}>
                          {name}
                        </span>
                        {isSelected && <ChevronRight size={12} className="text-accent shrink-0" />}
                      </button>
                    )
                  })}
                </div>
              </div>
            )}
          </div>

          {/* Footer */}
          <div className="flex items-center justify-between px-5 py-3 border-t border-border">
            <div>
              {error && (
                <p className="text-xs text-red-500">{(error as Error).message}</p>
              )}
              {isPending && (
                <p className="text-xs text-ink-muted">
                  {uploadMutation.isPending ? 'Uploading…' : 'Starting run…'}
                </p>
              )}
            </div>
            <div className="flex items-center gap-2">
              <button
                onClick={() => setNewRunModalOpen(false)}
                className="px-3 py-1.5 text-xs text-ink-secondary hover:text-ink transition-colors"
              >
                Cancel
              </button>
              <button
                onClick={handleStart}
                disabled={!canStart}
                className="flex items-center gap-1.5 px-4 py-1.5 bg-accent text-white text-xs font-medium rounded-md hover:bg-accent-hover disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
              >
                {isPending ? (
                  <Loader2 size={12} className="animate-spin" />
                ) : (
                  <Upload size={12} />
                )}
                Start Analysis
              </button>
            </div>
          </div>
        </div>
      </motion.div>
    </AnimatePresence>
  )
}
