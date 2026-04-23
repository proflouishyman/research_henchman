# Provider Abstractions

## Overview

Two provider abstraction layers decouple the pipeline from specific LLM and browser backends. Both follow the same pattern:
1. A thin factory function reads the provider from settings/env.
2. The chosen provider is transparent to callers — same interface regardless.
3. Default providers preserve existing behavior exactly.

---

## LLM Client (`layers/llm_client.py`)

### Classes
- `LLMProvider` — enum: `OLLAMA`, `CLAUDE`, `OPENAI`
- `LLMClient` — dataclass wrapping provider + model + connection settings
- `make_llm_client(settings, *, model, timeout_seconds, temperature)` — factory

### Interface
```python
client.complete(*, prompt: str, system: str = "", temperature: float | None = None) -> str
client.complete_json(*, prompt: str, system: str = "", temperature: float | None = None) -> dict
```

`complete_json` handles JSON extraction robustly: strips markdown code fences, extracts the first `{...}` fragment, and retries with explicit JSON-mode prompt if the first attempt fails.

### Backends

| Provider | Transport | SDK |
|---|---|---|
| `ollama` (default) | `urllib` → Ollama `/api/generate` | none (stdlib only) |
| `claude` | Anthropic Messages API | `anthropic` (optional) |
| `openai` | OpenAI Chat Completions API | `openai` (optional) |

The `claude` backend uses `cache_control: {"type": "ephemeral"}` on the system prompt for prompt caching.

Optional SDK imports: if `anthropic` or `openai` is not installed, a clear `ImportError` message is raised pointing to the missing package.

### Configuration
```
ORCH_LLM_PROVIDER=ollama   # or: claude, openai
ORCH_OLLAMA_BASE_URL=http://localhost:11434
ORCH_ANALYSIS_MODEL=qwen2.5:7b
# For claude backend:
ANTHROPIC_API_KEY=...
# For openai backend:
OPENAI_API_KEY=...
```

### Usage in layers
- `layers/analysis.py` — gap extraction and scoring
- `layers/reflection.py` — plan reflection and review
- `layers/search_policy.py` — claim classification (uses `complete_json`)
- `artifact_export.py` — per-gap synthesis generation (Ollama, temperature=0.3)

---

## Browser Client (`adapters/browser_client.py`)

### Classes
- `BrowserProvider` — enum: `PLAYWRIGHT_CDP`, `HTTP`, `CLAUDE_CU`
- `PageResult` — dataclass wrapping a fetched page
- `BrowserClient` — thin client with fetch/probe/open_tabs
- `make_browser_client(settings)` — factory

### Interface
```python
client.fetch(url: str, *, timeout_ms: int = 15000) -> PageResult
client.probe_login(url: str, *, timeout_ms: int = 10000) -> PageResult
client.open_tabs(urls: list[str]) -> None
```

### `PageResult` fields
```python
url: str
status_code: int
content: bytes
content_type: str
blocked: bool          # CAPTCHA / login wall / access-denied detected
blocked_reason: str    # "captcha" | "login" | "access_denied" | "timeout"
action_required: str   # human-readable hint for the user
error: str
elapsed_ms: int
```

`blocked` detection applies regex patterns against page content for captcha, challenge, login-redirect, and access-denied signals.

### Backends

| Provider | Mechanism |
|---|---|
| `playwright_cdp` (default) | Playwright async with CDP attach; CDP→HTTP fallback on connect error |
| `http` | `urllib.request` (no JS, no cookies, simple GET) |
| `claude_cu` | Stub — raises `NotImplementedError` with integration notes for future Anthropic Computer Use API |

### Configuration
```
ORCH_BROWSER_PROVIDER=playwright_cdp   # or: http, claude_cu
ORCH_PLAYWRIGHT_CDP_URL=http://localhost:9222
```

### Usage in adapters
- `adapters/seed_url_fetch.py` — seed URL fetch and sign-in probing
- `main.py` — sign-in tab opening via `open_tabs()`

---

## Adding a new provider

### New LLM backend
1. Add a value to `LLMProvider`.
2. Add a `_<name>_complete()` method on `LLMClient`.
3. Dispatch it in `complete()`.
4. Add env-value handling in `make_llm_client()`.

### New browser backend
1. Add a value to `BrowserProvider`.
2. Implement `fetch()`, `probe_login()`, `open_tabs()` for the new provider inside `BrowserClient`.
3. Add env-value handling in `make_browser_client()`.
