"""LLM Router and provider client: Groq primary with Google Gemini fallback."""

import json
import re
from typing import Any, Callable, Dict, Generator, List, Optional, Tuple

import requests

from core.prompts import REPAIR_PROMPT

GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
DEFAULT_GEMINI_FALLBACKS = ["gemini-2.0-flash", "gemini-1.5-flash"]
DEFAULT_TIMEOUT_SECONDS = 30


class LLMError(Exception):
    """Base exception for LLM provider errors."""
    def __init__(self, message: str, provider: str = "", status_code: Optional[int] = None, recoverable: bool = False):
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.recoverable = recoverable


def _clean_json_text(text: str) -> str:
    """Extract raw JSON text from markdown fences or mixed output."""
    t = text.strip()
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, re.DOTALL)
    if match:
        return match.group(1).strip()
    first_brace = t.find("{")
    last_brace = t.rfind("}")
    if first_brace != -1 and last_brace != -1 and last_brace > first_brace:
        return t[first_brace : last_brace + 1].strip()
    return t


def parse_proposal_json(raw_text: str) -> Optional[Dict[str, Any]]:
    """Parse and validate structured proposal fields."""
    try:
        clean = _clean_json_text(raw_text)
        data = json.loads(clean)
        if not isinstance(data, dict):
            return None
        title = str(data.get("title", "")).strip()
        description = str(data.get("description", "")).strip()
        tags = data.get("tags", [])
        if not isinstance(tags, list):
            tags = [t.strip() for t in str(tags).split(",") if t.strip()]
        tags = [str(t).strip() for t in tags if str(t).strip()]
        rationale = str(data.get("rationale", "")).strip()
        variants = data.get("variants", [])

        if not title:
            return None

        return {
            "title": title,
            "description": description,
            "tags": tags,
            "rationale": rationale or "Grounded in video metadata and evidence.",
            "variants": variants if isinstance(variants, list) else [],
        }
    except Exception:
        return None


def call_groq_api(
    api_key: str,
    model: str,
    prompt: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Execute Groq chat completion request."""
    if not api_key:
        raise LLMError("Groq API key not configured.", provider="groq", recoverable=True)

    headers = {
        "Authorization": f"Bearer {api_key.strip()}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.4,
    }

    try:
        response = requests.post(
            GROQ_ENDPOINT,
            headers=headers,
            json=payload,
            timeout=timeout,
        )
    except requests.Timeout:
        raise LLMError(f"Groq request timed out after {timeout}s.", provider="groq", recoverable=True)
    except requests.RequestException as e:
        raise LLMError(f"Groq network error: {str(e)}", provider="groq", recoverable=True)

    if response.status_code == 200:
        data = response.json()
        choices = data.get("choices", [])
        if choices and "message" in choices[0]:
            return choices[0]["message"].get("content", "")
        raise LLMError("Groq returned empty completion.", provider="groq", recoverable=True)

    recoverable = response.status_code in {404, 408, 429, 500, 502, 503, 504}
    msg = f"Groq HTTP {response.status_code}: {response.text[:200]}"
    raise LLMError(msg, provider="groq", status_code=response.status_code, recoverable=recoverable)


def call_gemini_api(
    api_key: str,
    model: str,
    prompt: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Execute Gemini generateContent request."""
    if not api_key:
        raise LLMError("Gemini API key not configured.", provider="gemini", recoverable=True)

    url = GEMINI_ENDPOINT.format(model=model)
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": api_key.strip(),
    }
    payload = {
        "contents": [
            {"parts": [{"text": prompt}]}
        ],
        "generationConfig": {
            "temperature": 0.4,
        }
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=timeout)
    except requests.Timeout:
        raise LLMError(f"Gemini request timed out after {timeout}s.", provider="gemini", recoverable=True)
    except requests.RequestException as e:
        raise LLMError(f"Gemini network error: {str(e)}", provider="gemini", recoverable=True)

    if response.status_code == 200:
        data = response.json()
        candidates = data.get("candidates", [])
        if candidates and "content" in candidates[0]:
            parts = candidates[0]["content"].get("parts", [])
            if parts and "text" in parts[0]:
                return parts[0]["text"]
        raise LLMError("Gemini returned empty response.", provider="gemini", recoverable=True)

    recoverable = response.status_code in {404, 408, 429, 500, 502, 503, 504}
    msg = f"Gemini HTTP {response.status_code}: {response.text[:200]}"
    raise LLMError(msg, provider="gemini", status_code=response.status_code, recoverable=recoverable)


class LLMRouter:
    """Router that attempts Groq models first, then falls back to Gemini models."""

    def __init__(self, secrets: Dict[str, Any]):
        groq_cfg = secrets.get("groq", {})
        gemini_cfg = secrets.get("gemini", {})

        self.groq_key = groq_cfg.get("api_key")
        self.groq_model = groq_cfg.get("model", DEFAULT_GROQ_MODEL)
        self.groq_fallbacks = groq_cfg.get("fallback_models", [])
        if isinstance(self.groq_fallbacks, str):
            self.groq_fallbacks = [m.strip() for m in self.groq_fallbacks.split(",") if m.strip()]

        self.gemini_key = gemini_cfg.get("api_key")
        self.gemini_model = gemini_cfg.get("model", DEFAULT_GEMINI_MODEL)
        self.gemini_fallbacks = gemini_cfg.get("fallback_models", DEFAULT_GEMINI_FALLBACKS)
        if isinstance(self.gemini_fallbacks, str):
            self.gemini_fallbacks = [m.strip() for m in self.gemini_fallbacks.split(",") if m.strip()]

    def generate(self, prompt: str) -> Tuple[str, str, str]:
        """Generate text using Groq primary -> Gemini fallback.
        
        Returns:
            (generated_text, provider_name, model_used)
        """
        errors = []

        # Try Groq models first
        if self.groq_key:
            groq_models = [self.groq_model] + [m for m in self.groq_fallbacks if m != self.groq_model]
            for model in groq_models:
                try:
                    text = call_groq_api(self.groq_key, model, prompt)
                    return text, "Groq", model
                except LLMError as err:
                    errors.append(f"Groq ({model}): {str(err)}")
                    if not err.recoverable:
                        break
        else:
            errors.append("Groq: No API key configured.")

        # Try Gemini models next
        if self.gemini_key:
            gemini_models = [self.gemini_model] + [m for m in self.gemini_fallbacks if m != self.gemini_model]
            for model in gemini_models:
                try:
                    text = call_gemini_api(self.gemini_key, model, prompt)
                    return text, "Gemini", model
                except LLMError as err:
                    errors.append(f"Gemini ({model}): {str(err)}")
                    if not err.recoverable:
                        break
        else:
            errors.append("Gemini: No API key configured.")

        raise LLMError(f"All AI providers failed:\n" + "\n".join(errors))

    def generate_structured_proposal(self, prompt: str) -> Tuple[Dict[str, Any], str, str]:
        """Generate proposal and ensure structured output, with 1 repair retry on malformed JSON."""
        raw_text, provider, model = self.generate(prompt)
        parsed = parse_proposal_json(raw_text)

        if not parsed:
            # Attempt one repair round
            repair_prompt = f"{prompt}\n\n{REPAIR_PROMPT}\nRaw output to repair:\n{raw_text[:2000]}"
            raw_text, provider, model = self.generate(repair_prompt)
            parsed = parse_proposal_json(raw_text)

        if not parsed:
            raise LLMError(
                "AI returned unstructured output that could not be parsed into valid metadata fields. "
                "Please refine your instruction in chat.",
                provider=provider,
                recoverable=True,
            )

        return parsed, provider, model
