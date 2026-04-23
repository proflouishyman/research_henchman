// Settings modal: library profile, credentials, database test login.

import { useState } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import { X, Save, Loader2, Settings } from 'lucide-react'
import { useQuery, useMutation } from '@tanstack/react-query'
import { fetchSettings, saveSettings, fetchSources, fetchLibraryProfiles } from '../../lib/api'
import { useUIStore } from '../../store/ui'
import { DatabaseRow } from './DatabaseRow'

export function SettingsModal() {
  const { setSettingsModalOpen } = useUIStore()
  const [activeTab, setActiveTab] = useState<'library' | 'credentials' | 'databases'>('library')
  const [updates, setUpdates] = useState<Record<string, string>>({})
  const [saved, setSaved] = useState(false)

  const { data: settings } = useQuery({ queryKey: ['settings'], queryFn: fetchSettings })
  const { data: sources } = useQuery({ queryKey: ['sources'], queryFn: fetchSources })
  const { data: profiles } = useQuery({
    queryKey: ['library-profiles'],
    queryFn: fetchLibraryProfiles,
  })

  const saveMutation = useMutation({
    mutationFn: () => saveSettings(updates),
    onSuccess: () => {
      setSaved(true)
      setTimeout(() => setSaved(false), 2500)
      setUpdates({})
    },
  })

  const handleChange = (key: string, value: string) => {
    setUpdates((prev) => ({ ...prev, [key]: value }))
  }

  const CREDENTIAL_FIELDS = [
    { key: 'CROSSREF_API_KEY', label: 'Crossref API Key' },
    { key: 'BLS_API_KEY', label: 'BLS API Key' },
    { key: 'EBSCO_PROF', label: 'EBSCO Profile ID' },
    { key: 'EBSCO_PWD', label: 'EBSCO Password' },
    { key: 'ORCH_OLLAMA_BASE_URL', label: 'Ollama Base URL' },
    { key: 'ORCH_PLAYWRIGHT_CDP_URL', label: 'Playwright CDP URL' },
  ]

  return (
    <AnimatePresence>
      <motion.div
        key="backdrop"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        className="fixed inset-0 z-40 bg-black/30 backdrop-blur-sm"
        onClick={() => setSettingsModalOpen(false)}
      />

      <motion.div
        key="modal"
        initial={{ opacity: 0, scale: 0.96, y: 8 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.96, y: 8 }}
        transition={{ duration: 0.18, ease: 'easeOut' }}
        className="fixed inset-0 z-50 flex items-center justify-center p-4"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="bg-surface-card rounded-xl shadow-modal w-full max-w-lg max-h-[80vh] flex flex-col">
          {/* Header */}
          <div className="flex items-center justify-between px-5 py-4 border-b border-border shrink-0">
            <div className="flex items-center gap-2">
              <Settings size={15} className="text-ink-muted" />
              <h2 className="text-sm font-semibold text-ink">Settings</h2>
            </div>
            <button
              onClick={() => setSettingsModalOpen(false)}
              className="p-1.5 rounded-md text-ink-muted hover:text-ink hover:bg-surface-muted transition-colors"
            >
              <X size={15} />
            </button>
          </div>

          {/* Tabs */}
          <div className="flex border-b border-border shrink-0">
            {(['library', 'credentials', 'databases'] as const).map((tab) => (
              <button
                key={tab}
                onClick={() => setActiveTab(tab)}
                className={`px-4 py-2.5 text-xs font-medium transition-colors capitalize ${
                  activeTab === tab
                    ? 'border-b-2 border-accent text-accent'
                    : 'text-ink-secondary hover:text-ink'
                }`}
              >
                {tab}
              </button>
            ))}
          </div>

          {/* Body */}
          <div className="flex-1 overflow-y-auto p-5">
            {/* Library tab */}
            {activeTab === 'library' && (
              <div className="space-y-4">
                <div>
                  <label className="block text-xs font-medium text-ink mb-1.5">
                    Library System
                  </label>
                  <select
                    value={
                      updates['ORCH_LIBRARY_SYSTEM'] ??
                      (settings?.library_system as string) ??
                      ''
                    }
                    onChange={(e) => handleChange('ORCH_LIBRARY_SYSTEM', e.target.value)}
                    className="w-full text-xs border border-border rounded-lg px-3 py-2 bg-surface text-ink focus:outline-none focus:border-accent transition-colors"
                  >
                    <option value="">Select library…</option>
                    {(profiles ?? []).map((p) => (
                      <option key={p} value={p}>
                        {p.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())}
                      </option>
                    ))}
                  </select>
                </div>

                <div>
                  <label className="block text-xs font-medium text-ink mb-1.5">
                    LLM Provider
                  </label>
                  <input
                    type="text"
                    value={
                      updates['ORCH_LLM_PROVIDER'] ??
                      (settings?.llm_provider as string) ??
                      ''
                    }
                    onChange={(e) => handleChange('ORCH_LLM_PROVIDER', e.target.value)}
                    placeholder="e.g. ollama, openai"
                    className="w-full text-xs border border-border rounded-lg px-3 py-2 bg-surface text-ink focus:outline-none focus:border-accent transition-colors"
                  />
                </div>
              </div>
            )}

            {/* Credentials tab */}
            {activeTab === 'credentials' && (
              <div className="space-y-3">
                <p className="text-xs text-ink-muted mb-4">
                  Credentials are saved to the project <code className="font-mono text-[10px] bg-surface-muted px-1 py-0.5 rounded">.env</code> file.
                </p>
                {CREDENTIAL_FIELDS.map(({ key, label }) => (
                  <div key={key}>
                    <label className="block text-[11px] font-medium text-ink-secondary mb-1">
                      {label}
                    </label>
                    <input
                      type={key.includes('KEY') || key.includes('PWD') ? 'password' : 'text'}
                      value={updates[key] ?? (settings?.[key] as string) ?? ''}
                      onChange={(e) => handleChange(key, e.target.value)}
                      placeholder={key}
                      className="w-full text-xs font-mono border border-border rounded-lg px-3 py-2 bg-surface text-ink focus:outline-none focus:border-accent transition-colors"
                    />
                  </div>
                ))}
              </div>
            )}

            {/* Databases tab */}
            {activeTab === 'databases' && (
              <div>
                <p className="text-xs text-ink-muted mb-3">
                  Detected library databases. Test individual sign-in access.
                </p>
                {(!sources || sources.length === 0) && (
                  <p className="text-xs text-ink-muted text-center py-6">
                    No sources detected. Check library profile configuration.
                  </p>
                )}
                {sources?.map((s) => (
                  <DatabaseRow key={s.source_id} source={s} />
                ))}
              </div>
            )}
          </div>

          {/* Footer */}
          {(activeTab === 'library' || activeTab === 'credentials') && (
            <div className="flex items-center justify-between px-5 py-3 border-t border-border shrink-0">
              <div>
                {saved && (
                  <span className="text-xs text-emerald-600 font-medium">
                    Settings saved.
                  </span>
                )}
                {saveMutation.isError && (
                  <span className="text-xs text-red-500">
                    {(saveMutation.error as Error).message}
                  </span>
                )}
              </div>
              <button
                onClick={() => saveMutation.mutate()}
                disabled={saveMutation.isPending || Object.keys(updates).length === 0}
                className="flex items-center gap-1.5 px-4 py-1.5 bg-accent text-white text-xs font-medium rounded-md hover:bg-accent-hover disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
              >
                {saveMutation.isPending ? (
                  <Loader2 size={12} className="animate-spin" />
                ) : (
                  <Save size={12} />
                )}
                Save Changes
              </button>
            </div>
          )}
        </div>
      </motion.div>
    </AnimatePresence>
  )
}
