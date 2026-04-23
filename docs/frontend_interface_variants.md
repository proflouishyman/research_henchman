# Frontend Interface Variants

> **Note (v3):** The primary frontend is now the React app in `frontend/`. Build it with `cd frontend && npm run build`. The three style variants described below are retained in `static/index.html` as a fallback only.

This app now supports three switchable interface variants from the top-right `Interface Style` selector in `static/index.html`.

Selection is persisted in browser local storage (`orchestrator_v2_ui_variant`) so operators can compare and keep a preferred style.

## Variant 1: `editorial`
- Visual thesis: calm manuscript-first workspace with warm paper tones and serif-forward headline hierarchy.
- Content plan:
  - Hero identity + short orientation copy
  - Run controls and launch state on the left
  - Plan, stage, documents, and logs on the right
  - Settings as a separate tab with library + credential workflow
- Interaction thesis:
  - subtle panel entrance motion
  - active stage pulse + heartbeat cadence
  - clear tab state transitions

## Variant 2: `operations`
- Visual thesis: denser run-operations console with uppercase command-center framing and tighter geometry.
- Content plan:
  - Explicit control header and style selector
  - Broader launch panel width for high-frequency run starts
  - Run monitor optimized for scanning status and events
  - Same settings workflow with stronger utilitarian contrast
- Interaction thesis:
  - fast state transitions with reduced ornament
  - strong primary action visibility
  - high-clarity status affordances for active/failure states

## Variant 3: `atlas`
- Visual thesis: airy research map style with softer contrast and spacious panel rhythm for longer review sessions.
- Content plan:
  - expressive masthead for orientation
  - balanced left-right workflow split
  - monitor surfaces tuned for longer reading and route inspection
  - settings kept consistent with run workflow to reduce mode-switch fatigue
- Interaction thesis:
  - gentle motion and depth
  - generous spacing to reduce visual noise
  - consistent control placements across sections

## Responsive behavior
- All three variants collapse to a single-column layout at <= `1100px`.
- Variant-specific desktop grid overrides are explicitly reset in mobile breakpoints so no horizontal overflow occurs.

## Notes
- API contracts and run behavior are unchanged; this is presentation-only.
- Existing monitor behavior, settings persistence, and document rendering remain intact across all variants.
