"""Client for a local model behind an OpenAI-compatible endpoint (Ollama, llama.cpp, LM Studio...).

The client refuses non-local hosts. Outside models are reached only through
gateway.py, which abstracts the data first.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlparse

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


class AIError(Exception):
    pass


def is_local_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in LOCAL_HOSTS or host.endswith(".localhost")


def _extract_json(text: str) -> Any:
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"(\{.*\}|\[.*\])", text, re.S)
        if match:
            return json.loads(match.group(1))
        raise AIError("The model did not return JSON.")


class AIClient:
    def __init__(self, base_url: str, chat_model: str | None, embed_model: str | None,
                 api_key: str | None = None, timeout: float = 90, require_local: bool = True):
        if require_local and not is_local_url(base_url):
            raise AIError("The local AI endpoint must be on this machine (localhost).")
        self.base_url = base_url.rstrip("/")
        self.chat_model = chat_model or None
        self.embed_model = embed_model or None
        self.api_key = api_key
        self.timeout = timeout

    @property
    def name(self) -> str:
        return f"model:{self.chat_model or self.embed_model}"

    @classmethod
    def from_vault(cls, vault) -> "AIClient | None":
        url = vault.get_meta("ai_base_url")
        if not url:
            return None
        try:
            return cls(url, vault.get_meta("ai_chat_model"), vault.get_meta("ai_embed_model"))
        except AIError:
            return None

    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(
            self.base_url + path, data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as res:
                return json.loads(res.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise AIError(f"Could not reach the model at {self.base_url}: {exc}") from exc

    def chat(self, system: str, user: str, json_mode: bool = False,
             temperature: float = 0.2) -> str:
        if not self.chat_model:
            raise AIError("No chat model configured.")
        body = {"model": self.chat_model, "temperature": temperature,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}]}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        data = self._post("/chat/completions", body)
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise AIError("Unexpected response from the model.") from exc

    def chat_json(self, system: str, user: str) -> Any:
        return _extract_json(self.chat(system, user, json_mode=True))

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not self.embed_model:
            raise AIError("No embedding model configured.")
        data = self._post("/embeddings", {"model": self.embed_model, "input": texts})
        try:
            rows = sorted(data["data"], key=lambda d: d.get("index", 0))
            return [r["embedding"] for r in rows]
        except (KeyError, TypeError) as exc:
            raise AIError("Unexpected embedding response.") from exc

    def ping(self) -> bool:
        try:
            req = urllib.request.Request(self.base_url + "/models")
            with urllib.request.urlopen(req, timeout=5):
                return True
        except Exception:
            return False
