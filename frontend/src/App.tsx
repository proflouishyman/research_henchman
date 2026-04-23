// Root application component — applies dark mode class and renders layout.

import { useEffect } from 'react'
import { useUIStore } from './store/ui'
import { Layout } from './components/layout/Layout'

export default function App() {
  const { darkMode } = useUIStore()

  // Apply dark mode class on the document root
  useEffect(() => {
    if (darkMode) {
      document.documentElement.classList.add('dark')
    } else {
      document.documentElement.classList.remove('dark')
    }
  }, [darkMode])

  return <Layout />
}
