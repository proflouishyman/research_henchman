// Pre-run sign-in confirmation modal.

import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { X, ExternalLink, CheckCircle, AlertTriangle, Loader2 } from 'lucide-react'
import { useMutation } from '@tanstack/react-query'
import { fetchSignInPreflight, testSignIn, openSignIn } from '../../lib/api'
import type { SignInTarget, SignInResult } from '../../types/contracts'

interface SignInSplashProps {
  manuscriptPath: string
  onClose: () => void
  onConfirm: () => void
}

export function SignInSplash({ manuscriptPath, onClose, onConfirm }: SignInSplashProps) {
  const [targets, setTargets] = useState<SignInTarget[]>([])
  const [results, setResults] = useState<SignInResult[]>([])
  const [preflightDone, setPreflightDone] = useState(false)

  const preflightMutation = useMutation({
    mutationFn: () => fetchSignInPreflight(manuscriptPath),
    onSuccess: (data) => {
      setTargets(data)
      setPreflightDone(true)
    },
  })

  const testMutation = useMutation({
    mutationFn: () => testSignIn(targets.map((t) => t.source_id), manuscriptPath),
    onSuccess: (data) => setResults(data),
  })

  const openMutation = useMutation({
    mutationFn: () =>
      openSignIn(
        targets.map((t) => t.source_id),
        targets.map((t) => t.url)
      ),
  })

  const resultBySourceId = new Map<string, SignInResult>(results.map((r) => [r.source_id, r]))

  const statusIcon = (r: SignInResult) => {
    switch (r.status) {
      case 'ok':
        return <CheckCircle size={12} className="text-emerald-600" />
      case 'blocked':
        return <AlertTriangle size={12} className="text-red-500" />
      default:
        return <AlertTriangle size={12} className="text-gray-400" />
    }
  }

  return (
    <AnimatePresence>
      <motion.div
        key="backdrop"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        className="fixed inset-0 z-50 bg-black/40 backdrop-blur-sm"
        onClick={onClose}
      />

      <motion.div
        key="splash"
        initial={{ opacity: 0, scale: 0.95, y: 10 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.95, y: 10 }}
        transition={{ duration: 0.2, ease: 'easeOut' }}
        className="fixed inset-0 z-50 flex items-center justify-center p-4"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="bg-surface-card rounded-xl shadow-modal w-full max-w-md">
          {/* Header */}
          <div className="flex items-center justify-between px-5 py-4 border-b border-border">
            <div>
              <h2 className="text-sm font-semibold text-ink">Pre-Run Sign-In</h2>
              <p className="text-xs text-ink-secondary mt-0.5">
                Verify library access before starting the run
              </p>
            </div>
            <button
              onClick={onClose}
              className="p-1.5 rounded-md text-ink-muted hover:text-ink hover:bg-surface-muted transition-colors"
            >
              <X size={15} />
            </button>
          </div>

          {/* Body */}
          <div className="p-5 space-y-4">
            {/* Step 1: Analyze sources */}
            {!preflightDone && (
              <div className="text-center py-4">
                <p className="text-xs text-ink-secondary mb-4 leading-relaxed">
                  Analyze the manuscript to determine which library databases are needed, then verify
                  your sign-in status.
                </p>
                <button
                  onClick={() => preflightMutation.mutate()}
                  disabled={preflightMutation.isPending}
                  className="flex items-center gap-2 mx-auto px-4 py-2 bg-accent text-white text-xs font-medium rounded-md hover:bg-accent-hover disabled:opacity-50 transition-colors"
                >
                  {preflightMutation.isPending && <Loader2 size={13} className="animate-spin" />}
                  Analyze Sources
                </button>
                {preflightMutation.isError && (
                  <p className="text-xs text-red-500 mt-2">
                    {(preflightMutation.error as Error).message}
                  </p>
                )}
              </div>
            )}

            {/* Step 2: Targets list */}
            {preflightDone && targets.length > 0 && (
              <div>
                <p className="text-xs font-semibold text-ink-secondary mb-2">Required databases</p>
                <div className="space-y-2">
                  {targets.map((t) => {
                    const result = resultBySourceId.get(t.source_id)
                    return (
                      <div
                        key={t.source_id}
                        className="flex items-center justify-between p-2.5 rounded-lg bg-surface-muted border border-border"
                      >
                        <div className="flex items-center gap-2">
                          {result ? statusIcon(result) : <div className="w-3 h-3 rounded-full bg-gray-300" />}
                          <span className="text-xs font-medium text-ink">{t.name}</span>
                        </div>
                        <div className="flex items-center gap-2">
                          {result && (
                            <span
                              className={`text-[10px] font-medium px-1.5 py-0.5 rounded ${
                                result.status === 'ok'
                                  ? 'bg-emerald-50 text-emerald-700'
                                  : result.status === 'blocked'
                                  ? 'bg-red-50 text-red-600'
                                  : 'bg-gray-100 text-gray-500'
                              }`}
                            >
                              {result.status}
                            </span>
                          )}
                          <a
                            href={t.url}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="p-1 rounded text-ink-muted hover:text-accent transition-colors"
                          >
                            <ExternalLink size={11} />
                          </a>
                        </div>
                      </div>
                    )
                  })}
                </div>

                {/* Open pages + test buttons */}
                <div className="flex items-center gap-2 mt-3">
                  <button
                    onClick={() => openMutation.mutate()}
                    disabled={openMutation.isPending}
                    className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium border border-border rounded-md text-ink-secondary hover:text-ink hover:bg-surface-muted disabled:opacity-50 transition-colors"
                  >
                    {openMutation.isPending && <Loader2 size={11} className="animate-spin" />}
                    <ExternalLink size={11} />
                    Open Sign-In Pages
                  </button>
                  <button
                    onClick={() => testMutation.mutate()}
                    disabled={testMutation.isPending}
                    className="flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium bg-surface-muted border border-border rounded-md text-ink-secondary hover:text-ink disabled:opacity-50 transition-colors"
                  >
                    {testMutation.isPending && <Loader2 size={11} className="animate-spin" />}
                    Test Login
                  </button>
                </div>
              </div>
            )}

            {preflightDone && targets.length === 0 && (
              <p className="text-xs text-ink-muted text-center py-4">
                No required databases identified for this manuscript.
              </p>
            )}
          </div>

          {/* Footer */}
          <div className="flex items-center justify-end gap-2 px-5 py-3 border-t border-border">
            <button
              onClick={onClose}
              className="px-3 py-1.5 text-xs text-ink-secondary hover:text-ink transition-colors"
            >
              Cancel
            </button>
            <button
              onClick={onConfirm}
              className="px-4 py-1.5 bg-accent text-white text-xs font-medium rounded-md hover:bg-accent-hover transition-colors"
            >
              Continue to Run
            </button>
          </div>
        </div>
      </motion.div>
    </AnimatePresence>
  )
}
