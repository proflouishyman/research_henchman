// Route-level smoke tests — verify that each top-level /write surface loads
// without an error overlay and contains at least the expected structural element.
//
// These are intentionally coarse. They fail loudly when a route is broken at
// the structural level (missing route, API 500 that breaks render, etc.)
// but do not assert fine-grained content.

import { test, expect } from '@playwright/test'

test.describe('Route smoke tests', () => {
  test('/runs loads (existing surface)', async ({ page }) => {
    await page.goto('/runs')
    // The runs layout has a top bar and a pipeline tree — look for any visible
    // text that belongs to the runs shell.
    await expect(page.locator('body')).toBeVisible()
    // No red "Failed to load" error overlay.
    await expect(page.locator('text=Failed to load')).toHaveCount(0)
  })

  test('/write redirects to /write/manuscript (not /write/gaps)', async ({ page }) => {
    await page.goto('/write')
    // A3: index → Navigate to="manuscript"
    await page.waitForURL('**/write/manuscript', { timeout: 5000 })
    await expect(page.url()).toContain('/write/manuscript')
  })

  test('/write/gaps loads with sidebar visible', async ({ page }) => {
    await page.goto('/write/gaps')
    // WriteShell renders a left nav rail with the route links.
    await expect(page.getByText('Gaps')).toBeVisible({ timeout: 10000 })
    // No top-level error.
    await expect(page.locator('text=Failed to load')).toHaveCount(0)
  })

  test('/write/search loads with search input visible', async ({ page }) => {
    await page.goto('/write/search')
    // SearchPage renders a text input for the search query.
    await expect(page.locator('input[type="text"], input[placeholder]').first()).toBeVisible({
      timeout: 10000,
    })
  })

  test('/write/characters loads with at least 10 character cards visible', async ({ page }) => {
    await page.goto('/write/characters')
    // CharactersPage renders a grid of CharacterCardView components.
    // Wait up to 12s for the API response.
    await expect(page.locator('.grid > *').nth(9)).toBeVisible({ timeout: 12000 })
  })

  test('/write/manuscript loads with chapter outline visible', async ({ page }) => {
    await page.goto('/write/manuscript')
    // ManuscriptOutline renders "Chapters" label in the left pane.
    await expect(page.getByText('Chapters')).toBeVisible({ timeout: 15000 })
  })

  test('/write/queue loads (may be empty)', async ({ page }) => {
    await page.goto('/write/queue')
    // QueuePage always renders — content may be empty but no error overlay.
    await expect(page.locator('body')).toBeVisible()
    await expect(page.locator('text=Failed to load')).toHaveCount(0)
  })
})
