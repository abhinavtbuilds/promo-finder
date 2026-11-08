"""Model-agnostic LLM client.

Two adapters cover nearly everything:
  * gemini            -> Google Gemini REST API (free tier)
  * openai_compatible -> OpenAI, Groq, OpenRouter, Together, Ollama (/v1), vLLM,
                         or any API Kuku provides that speaks the OpenAI chat format.
Swapping models = editing config.yaml (or env vars). No code changes.
"""
# Author: Abhinav Tanwar — Promo Finder, Kuku AI Generalist assignment (Nov 2026)
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field

import requests


@dataclass
class LLMConfig:
    provider: str = "gemini"              # gemini | openai_compatible
    model: str = "gemini-2.5-flash"
    base_url: str = ""                    # for openai_compatible, e.g. https://api.groq.com/openai/v1
    api_key_env: str = "GEMINI_API_KEY"   # name of the env var holding the key
    api_key: str = ""                     # optional: pass the key directly (web app); never written to disk
    temperature: float = 0.2
    max_retries: int = 4
    min_interval_s: float = 4.0           # crude rate limiting for free tiers
    extra: dict = field(default_factory=dict)  # provider-specific body fields, e.g. {reasoning_effort: low}


class LLMClient:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self.key = cfg.api_key or os.environ.get(cfg.api_key_env, "")
        if not self.key and cfg.provider != "openai_compatible":
            raise RuntimeError(f"Set {cfg.api_key_env} (or use --mode rules for the no-API baseline)")
        self._last = 0.0
        self.calls = 0
        self.tokens_in = 0
        self.tokens_out = 0

    # -- public ---------------------------------------------------------------
    def json(self, system: str, user: str) -> dict | list:
        """Call the model and parse a JSON answer, retrying on bad JSON / rate limits."""
        err = None
        for attempt in range(self.cfg.max_retries):
            try:
                text = self._call(system, user)
                return parse_json(text)
            except requests.HTTPError as e:
                code = e.response.status_code if e.response is not None else 0
                detail = (e.response.text[:300] if e.response is not None else "")
                err = f"HTTP {code}: {detail}"
                retry_400 = code == 400 and "json_validate_failed" in detail  # model ran out before valid JSON
                if code in (400, 401, 403, 404) and not retry_400:  # wrong key / model name: retrying won't help
                    break
                print(f"    (HTTP {code}, retry {attempt + 1}/{self.cfg.max_retries})")
                time.sleep(min(60, 5 * 2 ** attempt))
            except (requests.Timeout, requests.ConnectionError) as e:  # slow or dropped call: just retry
                err = f"network: {e.__class__.__name__}"
                print(f"    (timeout/connection issue, retry {attempt + 1}/{self.cfg.max_retries})")
                time.sleep(min(60, 5 * 2 ** attempt))
            except (ValueError, KeyError) as e:
                err = e
                time.sleep(min(60, 5 * 2 ** attempt))
        raise RuntimeError(f"LLM failed after {self.cfg.max_retries} tries: {err}")

    # -- adapters -------------------------------------------------------------
    def _throttle(self):
        wait = self.cfg.min_interval_s - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()

    def _call(self, system: str, user: str) -> str:
        self._throttle()
        self.calls += 1
        if self.cfg.provider == "gemini":
            return self._gemini(system, user)
        if self.cfg.provider == "openai_compatible":
            return self._openai(system, user)
        raise ValueError(f"unknown provider {self.cfg.provider}")

    def _gemini(self, system: str, user: str) -> str:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.cfg.model}:generateContent"
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": self.cfg.temperature, "responseMimeType": "application/json"},
        }
        # key goes in a header, not the URL, so it never shows up in error messages or logs
        r = requests.post(url, headers={"x-goog-api-key": self.key}, json=body, timeout=300)
        r.raise_for_status()
        data = r.json()
        usage = data.get("usageMetadata", {})
        self.tokens_in += usage.get("promptTokenCount", 0)
        self.tokens_out += usage.get("candidatesTokenCount", 0)
        return "".join(p.get("text", "") for p in data["candidates"][0]["content"]["parts"])

    def _openai(self, system: str, user: str) -> str:
        base = (self.cfg.base_url or "https://api.openai.com/v1").rstrip("/")
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        body = {
            "model": self.cfg.model,
            "temperature": self.cfg.temperature,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
            **self.cfg.extra,
        }
        r = requests.post(f"{base}/chat/completions", headers=headers, json=body, timeout=300)
        if r.status_code == 400 and "response_format" in r.text:  # some servers lack JSON mode
            body.pop("response_format")
            r = requests.post(f"{base}/chat/completions", headers=headers, json=body, timeout=300)
        r.raise_for_status()
        data = r.json()
        usage = data.get("usage", {})
        self.tokens_in += usage.get("prompt_tokens", 0)
        self.tokens_out += usage.get("completion_tokens", 0)
        return data["choices"][0]["message"]["content"]


def parse_json(text: str):
    """Tolerate ```json fences and leading chatter."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1)
    start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
    if start < 0:
        raise ValueError("no JSON in model output")
    return json.loads(text[start:])
