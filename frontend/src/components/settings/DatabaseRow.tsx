// Per-database row in the settings modal with inline test login.

import { useState } from 'react'
import { CheckCircle, AlertTriangle, Loader2, ExternalLink } from 'lucide-react'
import { testSignIn } from '../../lib/api'
import type { Source } from '../../types/contracts'

interface DatabaseRowProps {
  source: Source
}

type TestStatus = 'idle' | 'loading' | 'ok' | 'blocked' | 'unreachable'

export function DatabaseRow({ source }: DatabaseRowProps) {
  const [status, setStatus] = useState<TestStatus>('idle')
  const [message, setMessage] = useState<string>('')

  const runTest = async () => {
    setStatus('loading')
    setMessage('')
    try {
      const results = await testSignIn([source.source_id])
      const result = results[0]
      if (result) {
        setStatus(result.status)
        setMessage(result.action_required ?? result.blocked_reason ?? '')
      } else {
        setStatus('unreachable')
      }
    } catch (err) {
      setStatus('unreachable')
      setMessage((err as Error).message)
    }
  }

  return (
    <div className="flex items-center justify-between py-2.5 border-b border-border last:border-0">
      <div className="flex items-center gap-3">
        {/* Status indicator */}
        <div className="w-5 flex items-center justify-center">
          {status === 'idle' && <div className="w-2 h-2 rounded-full bg-gray-300" />}
          {status === 'loading' && <Loader2 size={13} className="animate-spin text-accent" />}
          {status === 'ok' && <CheckCircle size={13} className="text-emerald-600" />}
          {(status === 'blocked' || status === 'unreachable') && (
            <AlertTriangle size={13} className="text-red-500" />
          )}
        </div>

        <div>
          <p className="text-xs font-medium text-ink">{source.name}</p>
          {message && (
            <p
              className={`text-[10px] mt-0.5 ${
                status === 'ok' ? 'text-emerald-600' : 'text-red-500'
              }`}
            >
              {message}
            </p>
          )}
          {source.url && (
            <a
              href={source.url}
              target="_blank"
              rel="noopener noreferrer"
              className="text-[10px] text-ink-muted hover:text-accent flex items-center gap-0.5 mt-0.5 transition-colors"
            >
              {source.url}
              <ExternalLink size={9} />
            </a>
          )}
        </div>
      </div>

      <div className="flex items-center gap-2">
        {/* Status badge */}
        {status !== 'idle' && status !== 'loading' && (
          <span
            className={`text-[10px] font-medium px-1.5 py-0.5 rounded border ${
              status === 'ok'
                ? 'bg-emerald-50 text-emerald-700 border-emerald-200'
                : 'bg-red-50 text-red-600 border-red-200'
            }`}
          >
            {status}
          </span>
        )}

        <button
          onClick={runTest}
          disabled={status === 'loading'}
          className="px-2.5 py-1 text-[10px] font-medium border border-border rounded text-ink-secondary hover:text-ink hover:bg-surface-muted disabled:opacity-50 transition-colors"
        >
          Test Login
        </button>
      </div>
    </div>
  )
}
