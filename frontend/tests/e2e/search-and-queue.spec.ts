// E16: search-and-queue spec
//
// Tests:
//   1. /write/search?q=China returns >0 results (A1 URL-seed fix).
//   2. Star a card; navigate to /write/queue; the starred card is visible
//      without re-visiting the dossier (A2 resolve_gaps fix).
//
// NOTE: Test 2 requires the backend marks API to be running and the
// resolve_gaps endpoint to be live. If the server is not up these tests
// are expected to fail at the network level.

import { test, expect } from '@playwright/test'

test.describe('Search URL seeding + Queue resolution', () => {
  test('/write/search?q=China returns at least 1 result', async ({ page }) => {
    // A1: ?q= param should seed the search box and fire immediately.
    await page.goto('/write/search?q=China')

    // Wait for the search input to be populated.
    const input = page.locator('input[type="search"], input[placeholder*="Search"]').first()
    await expect(input).toBeVisible({ timeout: 10000 })

    // The query should be pre-filled from the URL param.
    await expect(input).toHaveValue('China', { timeout: 5000 })

    // At least one source-card should appear in the results.
    await expect(page.locator('[data-testid="source-card"]').first()).toBeVisible({
      timeout: 15000,
    })

    // The match count text should indicate >0 results.
    // It renders as "<N> matches" or "<N> match".
    const metaText = page.locator('div').filter({ hasText: /\d+ match/ }).first()
    await expect(metaText).toBeVisible({ timeout: 10000 })
  })

  test('/write/queue shows starred articles without visiting dossier', async ({ page }) => {
    // A2: QueuePage must use resolve_gaps API to show starred cards without
    // the "visit the dossier once" message.
    //
    // Strategy:
    //   1. Navigate to the search page and star the first result.
    //   2. Wait for the mark to be persisted to DB.
    //   3. Navigate to /write/queue without visiting any dossier.
    //   4. The old "visit the dossier" message must NOT appear.
    //   5. Starred article count header should indicate > 0 articles.
    //
    // This test relies on at least one article existing in the search results
    // AND the resolve_gaps endpoint returning the article's gap.

    // Step 1: Search for results.
    await page.goto('/write/search?q=China')
    await page.locator('[data-testid="source-card"]').first().waitFor({ timeout: 15000 })

    // Step 2: Star the first card.
    // SearchPage renders full cards — the star button should be accessible.
    const firstCard = page.locator('[data-testid="source-card"]').first()
    // The star button has title="Star (add to reading queue)" or title="Unstar".
    const starBtn = firstCard.locator('button').filter({ hasText: '' }).nth(2) // 3rd button: Open, Cite, Star
    // More robust: find by title attribute.
    const starByTitle = firstCard.locator('button[title*="Star"]')
    if (await starByTitle.count() > 0) {
      await starByTitle.first().click()
    } else {
      // Fallback: use position (Open=0, Cite=1, Star=2).
      await starBtn.click()
    }

    // Wait for the optimistic update + API call to complete.
    await page.waitForTimeout(2000)

    // Step 3: Navigate to /write/queue (no dossier visited in this session).
    await page.goto('/write/queue')

    // Step 4: The OLD message must NOT appear.
    await expect(page.locator('text=visit the dossier once')).toHaveCount(0)

    // Step 5: The queue header should indicate at least 1 starred article.
    // QueuePage renders: "N starred article(s) · grouped by gap."
    await expect(page.getByText(/starred article/)).toBeVisible({ timeout: 8000 })

    // After resolve_gaps + dossier fetch, at least one SourceCard should appear.
    // The two async hops (resolve_gaps + dossier) can take several seconds.
    await expect(page.locator('[data-testid="source-card"]').first()).toBeVisible({
      timeout: 20000,
    })
  })
})
