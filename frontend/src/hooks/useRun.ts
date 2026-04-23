// Fetches a single run, polling while it is in an active state.

import { useQuery } from '@tanstack/react-query'
import { fetchRun } from '../lib/api'

const ACTIVE_STATUSES = new Set<string>(['queued', 'analyzing', 'planning', 'pulling', 'ingesting', 'fitting'])

export function useRun(runId: string | null) {
  return useQuery({
    queryKey: ['run', runId],
    queryFn: () => fetchRun(runId!),
    enabled: runId !== null,
    refetchInterval: (query) => {
      const status = query.state.data?.status
      if (status && ACTIVE_STATUSES.has(status)) return 3000
      return false
    },
    staleTime: 2000,
  })
}
