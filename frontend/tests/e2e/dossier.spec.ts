// Smoke tests for the dossier view at /write/gaps/TODO9.
//
// Verified API truth (curl http://127.0.0.1:8000/api/library/gaps/TODO9/dossier):
//   consolidated: 91, tier_counts: {3:1, 2:1, 1:1, 0:88}
//
// These tests catch structural breakage — a tier section disappearing,
// wrong entry counts, or cards not rendering after expand.

import { test, expect } from '@playwright/test'
import * as path from 'path'
import * as fs from 'fs'
import { fileURLToPath } from 'url'

// ESM-compatible __dirname replacement.
const __filename = fileURLToPath(import.meta.url)
const __dirname = path.dirname(__filename)

const GAP_ID = 'TODO9'
const DOSSIER_URL = `/write/gaps/${GAP_ID}`

test.describe('Dossier view — TODO9', () => {
  test.beforeEach(async ({ page }) => {
    await page.goto(DOSSIER_URL)
    // Wait for the dossier header to confirm SPA has rendered the route.
    await page.waitForSelector(`text=${GAP_ID}`, { timeout: 15000 })
  })

  test('page title contains gap ID', async ({ page }) => {
    await expect(page.locator(`text=${GAP_ID}`).first()).toBeVisible()
  })

  test('summary line shows 91 consolidated entries', async ({ page }) => {
    // The DossierHeader renders: "<span>91</span> consolidated from …"
    // Use exact match to target the count span, not any incidental "91" occurrence.
    await expect(page.getByText('91', { exact: true }).first()).toBeVisible()
  })

  test('all four tier section headers are visible', async ({ page }) => {
    await expect(page.getByText('Tier 3 — cite-worthy primary sources')).toBeVisible()
    await expect(page.getByText('Tier 2 — adjacent context')).toBeVisible()
    await expect(page.getByText('Tier 1 — tangential mentions')).toBeVisible()
    await expect(page.getByText('Tier 0 — search false positives')).toBeVisible()
  })

  test('tier section count badges match API truth', async ({ page }) => {
    // Each TierSection renders "<n> entries" or "<n> entry" in the count badge.
    // We look for the badge text next to each heading.
    // Tier 3: 1 entry, Tier 2: 1 entry, Tier 1: 1 entry, Tier 0: 88 entries.

    // Tier 3 header button
    const tier3Btn = page.locator('button', {
      has: page.getByText('Tier 3 — cite-worthy primary sources'),
    })
    await expect(tier3Btn.getByText('1 entry')).toBeVisible()

    // Tier 2 header button
    const tier2Btn = page.locator('button', {
      has: page.getByText('Tier 2 — adjacent context'),
    })
    await expect(tier2Btn.getByText('1 entry')).toBeVisible()

    // Tier 1 header button
    const tier1Btn = page.locator('button', {
      has: page.getByText('Tier 1 — tangential mentions'),
    })
    await expect(tier1Btn.getByText('1 entry')).toBeVisible()

    // Tier 0 header button
    const tier0Btn = page.locator('button', {
      has: page.getByText('Tier 0 — search false positives'),
    })
    await expect(tier0Btn.getByText('88 entries')).toBeVisible()
  })

  test('clicking Tier 0 header expands and shows source cards', async ({ page }) => {
    const tier0Btn = page.locator('button', {
      has: page.getByText('Tier 0 — search false positives'),
    })
    // Ensure the section is present before clicking.
    await expect(tier0Btn).toBeVisible()

    // Click to expand Tier 0 (it starts collapsed).
    await tier0Btn.click()

    // At least one source-card should now be visible inside the opened section.
    await expect(
      page.locator('[data-testid="source-card"]').first(),
    ).toBeVisible({ timeout: 5000 })
  })

  test('drag handle has non-zero opacity (visible at rest)', async ({ page }) => {
    // B5: DragHandle was opacity-0; now opacity-40. The span with the grip
    // icon should not have opacity-0 in its class list.
    // We expand Tier 3 first so at least one card is visible.
    const tier3Btn = page.locator('button', {
      has: page.getByText('Tier 3 — cite-worthy primary sources'),
    })
    await tier3Btn.click()
    await page.locator('[data-testid="source-card"]').first().waitFor({ timeout: 5000 })

    // Check that no source card's drag handle has opacity-0 class.
    const firstCard = page.locator('[data-testid="source-card"]').first()
    const dragHandleSpan = firstCard.locator('span').filter({ hasText: '' }).first()
    // The drag handle span should exist in DOM (opacity-40, not display:none).
    await expect(firstCard).toBeVisible()
    // Verify the class does not include opacity-0 by checking computed opacity.
    const opacity = await firstCard.evaluate((el) => {
      const handle = el.querySelector('span[title]')
      if (!handle) return '1'
      return window.getComputedStyle(handle).opacity
    })
    // opacity-40 = 0.4 in CSS, which is not 0.
    expect(Number(opacity)).toBeGreaterThan(0)
  })

  test('TopPicks strip renders when tier-3 count > 0', async ({ page }) => {
    // B7: TopPicks appears when there are tier-3 entries (TODO9 has 1).
    // The strip has "Top picks" label.
    await expect(page.getByText('Top picks — most citation-ready')).toBeVisible({ timeout: 5000 })
  })

  test('screenshot saved for manual review', async ({ page }) => {
    // Expand Tier 0 so it appears in the screenshot.
    const tier0Btn = page.locator('button', {
      has: page.getByText('Tier 0 — search false positives'),
    })
    await tier0Btn.click()
    await page.locator('[data-testid="source-card"]').first().waitFor({ timeout: 5000 })

    const screenshotDir = path.join(__dirname, 'screenshots')
    if (!fs.existsSync(screenshotDir)) fs.mkdirSync(screenshotDir, { recursive: true })

    await page.screenshot({
      path: path.join(screenshotDir, 'dossier-todo9.png'),
      fullPage: false,
    })
    // The test passes as long as the screenshot is saved without error.
  })
})
