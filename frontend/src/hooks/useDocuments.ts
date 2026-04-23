// Fetches gap document packets for a completed or partial run.

import { useQuery } from '@tanstack/react-query'
import { fetchDocuments } from '../lib/api'

export function useDocuments(runId: string | null, runStatus?: string) {
  // Only fetch once the run has produced results
  const hasDocs = runStatus === 'complete' || runStatus === 'partial' || runStatus === 'fitting'

  return useQuery({
    queryKey: ['documents', runId],
    queryFn: () => fetchDocuments(runId!),
    enabled: !!runId && hasDocs,
    staleTime: 30_000, // Documents don't change after run completes
  })
}
