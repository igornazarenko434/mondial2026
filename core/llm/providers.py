"""Concrete LLM providers. Libraries are imported lazily so a missing SDK only
disables that provider (the router falls back) rather than breaking imports.
"""
from __future__ import annotations
import os
from core.llm.base import LLMProvider, LLMUnavailable
from config.llm import MODELS


class ClaudeProvider(LLMProvider):
    """Anthropic Claude. Auth via CLAUDE_CODE_OAUTH_TOKEN (your Pro/Max
    subscription's Agent SDK credit) or ANTHROPIC_API_KEY (pay-as-you-go)."""
    name = "claude"

    def available(self) -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY")
                    or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN"))

    def complete(self, system, prompt, json_mode=False, max_tokens=1024) -> str:
        try:
            from anthropic import Anthropic
        except ImportError as e:
            raise LLMUnavailable("pip install anthropic") from e
        client = Anthropic()  # reads ANTHROPIC_API_KEY / auth token from env
        msg = client.messages.create(
            model=MODELS["claude"], max_tokens=max_tokens,
            system=system + (" Respond with valid JSON only." if json_mode else ""),
            messages=[{"role": "user", "content": prompt}])
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


class GeminiProvider(LLMProvider):
    """Google Gemini. Free-tier API key from Google AI Studio -> GEMINI_API_KEY."""
    name = "gemini"

    def available(self) -> bool:
        return bool(os.environ.get("GEMINI_API_KEY"))

    def complete(self, system, prompt, json_mode=False, max_tokens=1024) -> str:
        try:
            from google import genai
            from google.genai import types
        except ImportError as e:
            raise LLMUnavailable("pip install google-genai") from e
        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        cfg = types.GenerateContentConfig(
            system_instruction=system, max_output_tokens=max_tokens,
            response_mime_type="application/json" if json_mode else "text/plain")
        resp = client.models.generate_content(
            model=MODELS["gemini"], contents=prompt, config=cfg)
        return resp.text


class OpenAIProvider(LLMProvider):
    """OpenAI. OPENAI_API_KEY (pay-as-you-go). NOTE: ChatGPT Plus does NOT
    include API access — that's a separate, billed product."""
    name = "openai"

    def available(self) -> bool:
        return bool(os.environ.get("OPENAI_API_KEY"))

    def complete(self, system, prompt, json_mode=False, max_tokens=1024) -> str:
        try:
            from openai import OpenAI
        except ImportError as e:
            raise LLMUnavailable("pip install openai") from e
        client = OpenAI()
        kw = {"response_format": {"type": "json_object"}} if json_mode else {}
        resp = client.chat.completions.create(
            model=MODELS["openai"], max_tokens=max_tokens,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": prompt}], **kw)
        return resp.choices[0].message.content


REGISTRY = {p.name: p for p in (ClaudeProvider(), GeminiProvider(), OpenAIProvider())}
