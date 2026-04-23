// Fetches the run list, polling every 5s when any run is active.

import { useQuery } from '@tanstack/react-query'
import { fetchRuns } from '../lib/api'
import type { RunRow } from '../types/contracts'

const ACTIVE_STATUSES = new Set<string>(['queued', 'analyzing', 'planning', 'pulling', 'ingesting', 'fitting'])

function hasActiveRun(runs: RunRow[]): boolean {
  return runs.some((r) => ACTIVE_STATUSES.has(r.status))
}

export function useRuns() {
  return useQuery({
    queryKey: ['runs'],
    queryFn: fetchRuns,
    // Poll every 5 s while any run is in-progress
    refetchInterval: (query) => {
      const data = query.state.data
      if (data && hasActiveRun(data)) return 5000
      return false
    },
    staleTime: 3000,
  })
}
