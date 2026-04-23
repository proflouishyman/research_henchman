"""LLM provider abstraction for Research Henchman.

Supported providers:
  ollama   (default) — local HTTP, no extra deps, works offline
  claude             — Anthropic SDK; requires ANTHROPIC_API_KEY
  openai             — OpenAI SDK; requires OPENAI_API_KEY

Select via ORCH_LLM_PROVIDER env var. All providers share the same
complete() / complete_json() surface so call sites are provider-agnostic.

Usage:
    client = make_llm_client(settings, model=settings.gap_analysis_model,
                              timeout=settings.gap_analysis_timeout_seconds)
    text   = client.complete(prompt=prompt)
    parsed = client.complete_json(prompt=prompt)
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class LLMProvider(str, Enum):
    OLLAMA = "ollama"
    CLAUDE = "claude"
    OPENAI = "openai"


@dataclass
class LLMClient:
    """Provider-agnostic LLM client. Sync only; no streaming."""

    provider: LLMProvider
    model: str
    base_url: str = "http://localhost:11434"   # Ollama only
    timeout_seconds: int = 120
    temperature: float = 0.1
    max_retries: int = 2

    def complete(
        self,
        *,
        prompt: str,
        system: str = "",
        temperature: Optional[float] = None,
    ) -> str:
        """Call the LLM and return the raw text response."""
        t = temperature if temperature is not None else self.temperature
        if self.provider == LLMProvider.OLLAMA:
            return self._ollama_complete(prompt=prompt, system=system, temperature=t)
        if self.provider == LLMProvider.CLAUDE:
            return self._claude_complete(prompt=prompt, system=system, temperature=t)
        if self.provider == LLMProvider.OPENAI:
            return self._openai_complete(prompt=prompt, system=system, temperature=t)
        raise NotImplementedError(f"LLM provider not implemented: {self.provider}")

    def complete_json(
        self,
        *,
        prompt: str,
        system: str = "",
        temperature: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Call LLM and parse the response as JSON, retrying on parse failures."""
        last_exc: Exception = RuntimeError("no_attempts")
        for attempt in range(self.max_retries + 1):
            raw = self.complete(prompt=prompt, system=system, temperature=temperature)
            try:
                return json.loads(_strip_json_fences(raw))
            except json.JSONDecodeError as exc:
                last_exc = exc
                # Try to extract embedded JSON object or array.
                extracted = _extract_json_fragment(raw)
                if extracted is not None:
                    return extracted
                if attempt < self.max_retries:
                    time.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"llm_json_parse_failed: {last_exc}") from last_exc

    # ------------------------------------------------------------------
    # Ollama backend
    # ------------------------------------------------------------------

    def _ollama_complete(self, *, prompt: str, system: str, temperature: float) -> str:
        full_prompt = f"{system.strip()}\n\n{prompt}" if system.strip() else prompt
        payload = {
            "model": self.model,
            "prompt": full_prompt,
            "stream": False,
            "options": {"temperature": temperature},
        }
        req = urllib.request.Request(
            f"{self.base_url.rstrip('/')}/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=max(1, self.timeout_seconds)) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="ignore"))
        return str(body.get("response", "")).strip()

    # ------------------------------------------------------------------
    # Claude backend (Anthropic SDK — optional dependency)
    # ------------------------------------------------------------------

    def _claude_complete(self, *, prompt: str, system: str, temperature: float) -> str:
        try:
            import anthropic  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "anthropic package not installed. Run: pip install anthropic"
            ) from exc

        client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from env
        kwargs: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": 4096,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system.strip():
            kwargs["system"] = [
                {
                    "type": "text",
                    "text": system.strip(),
                    "cache_control": {"type": "ephemeral"},  # prompt caching
                }
            ]
        message = client.messages.create(**kwargs)
        return message.content[0].text if message.content else ""

    # ------------------------------------------------------------------
    # OpenAI backend (openai SDK — optional dependency)
    # ------------------------------------------------------------------

    def _openai_complete(self, *, prompt: str, system: str, temperature: float) -> str:
        try:
            import openai  # type: ignore
        except ImportError as exc:
            raise RuntimeError(
                "openai package not installed. Run: pip install openai"
            ) from exc

        client = openai.OpenAI()  # reads OPENAI_API_KEY from env
        messages = []
        if system.strip():
            messages.append({"role": "system", "content": system.strip()})
        messages.append({"role": "user", "content": prompt})

        response = client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=temperature,
            timeout=self.timeout_seconds,
        )
        return response.choices[0].message.content or ""


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _strip_json_fences(text: str) -> str:
    """Remove markdown code fences so raw LLM output can be parsed as JSON."""
    stripped = text.strip()
    for fence in ("```json", "```"):
        if stripped.startswith(fence):
            stripped = stripped[len(fence):]
            break
    if stripped.endswith("```"):
        stripped = stripped[:-3]
    return stripped.strip()


def _extract_json_fragment(text: str) -> Optional[Dict[str, Any]]:
    """Attempt to extract the first JSON object or array from freeform text."""
    # Try outermost array first (common for gap analysis responses)
    m = re.search(r"\[[\s\S]*\]", text)
    if m:
        try:
            val = json.loads(m.group(0))
            if isinstance(val, (list, dict)):
                return val  # type: ignore[return-value]
        except json.JSONDecodeError:
            pass
    # Then object
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            val = json.loads(text[start : end + 1])
            if isinstance(val, dict):
                return val
        except json.JSONDecodeError:
            pass
    return None


# ------------------------------------------------------------------
# Factory
# ------------------------------------------------------------------

def make_llm_client(
    settings: Any,
    *,
    model: Optional[str] = None,
    timeout_seconds: Optional[int] = None,
    temperature: float = 0.1,
) -> LLMClient:
    """Build an LLMClient from OrchestratorSettings.

    Accepts optional overrides for model/timeout so call sites can pick
    the right model tier without re-reading settings from env.
    """
    provider_raw = getattr(settings, "llm_provider", "ollama") or "ollama"
    try:
        provider = LLMProvider(str(provider_raw).strip().lower())
    except ValueError:
        provider = LLMProvider.OLLAMA

    return LLMClient(
        provider=provider,
        model=(model or settings.llm_model or "qwen2.5:7b").strip(),
        base_url=getattr(settings, "ollama_base_url", "http://localhost:11434"),
        timeout_seconds=timeout_seconds if timeout_seconds is not None else getattr(settings, "llm_timeout_seconds", 120),
        temperature=temperature,
        max_retries=2,
    )
