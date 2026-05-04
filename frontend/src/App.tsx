// Root application component — applies dark mode class and renders router.
//
// Two top-level modes: /runs (existing pipeline tree) and /write (new
// writing-companion library/dossier browser). The shared TopBar in each
// mode lets the user flip between them.

import { useEffect } from 'react'
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom'
import { useUIStore } from './store/ui'
import { useLibraryStore } from './store/library'
import { Layout } from './components/layout/Layout'
import { WriteShell } from './components/library/WriteShell'
import { ChapterGroupedGapList } from './components/library/ChapterGroupedGapList'
import { DossierView } from './components/library/DossierView'
import { SearchPage } from './components/library/SearchPage'
import { CharactersPage } from './components/library/CharactersPage'
import { QueuePage } from './components/library/QueuePage'
import { ManuscriptReader } from './components/library/manuscript/ManuscriptReader'

export default function App() {
  const { darkMode } = useUIStore()
  const hydrateMarks = useLibraryStore((s) => s.hydrateMarks)

  // Apply dark mode class on the document root
  useEffect(() => {
    if (darkMode) {
      document.documentElement.classList.add('dark')
    } else {
      document.documentElement.classList.remove('dark')
    }
  }, [darkMode])

  // Hydrate marks from DB on app start (migrates legacy localStorage marks).
  useEffect(() => {
    hydrateMarks().catch(() => undefined)
  }, [hydrateMarks])

  return (
    <BrowserRouter>
      <Routes>
        {/* Default: keep existing /runs experience as the home view. */}
        <Route path="/" element={<Navigate to="/runs" replace />} />

        {/* Runs (legacy) tree — Layout owns the runs sidebar + pipeline. */}
        <Route path="/runs/*" element={<Layout />} />

        {/* Library / writing companion. */}
        <Route path="/write" element={<WriteShell />}>
          <Route index element={<Navigate to="gaps" replace />} />
          {/* v3: Manuscript reader — before gaps so slug matching is unambiguous */}
          <Route path="manuscript" element={<ManuscriptReader />} />
          <Route path="manuscript/:chapterSlug" element={<ManuscriptReader />} />
          <Route path="manuscript/:chapterSlug/:paraId" element={<ManuscriptReader />} />
          <Route path="gaps" element={<ChapterGroupedGapList />} />
          <Route path="gaps/:gapId" element={<DossierView />} />
          {/* Wave 2 routes */}
          <Route path="search" element={<SearchPage />} />
          <Route path="characters" element={<CharactersPage />} />
          <Route path="queue" element={<QueuePage />} />
        </Route>

        {/* Catch-all → runs. */}
        <Route path="*" element={<Navigate to="/runs" replace />} />
      </Routes>
    </BrowserRouter>
  )
}
