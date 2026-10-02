"""Model access.

- An Azure OpenAI chat deployment (azure.deployment in config.yaml), authenticated with the az CLI login (no API key): drives the browser
  agent and writes text (profile extraction, cover letters, free-text form answers).
- Cloudflare Workers AI clef / clef-flash: decision models that return calibrated probabilities for
  typed questions. Used for job triage (cheap, thousands of postings) and for verifying
  post-submit screenshots.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
from functools import lru_cache
from pathlib import Path

import httpx
from azure.identity import AzureCliCredential, get_bearer_token_provider
from openai import AsyncAzureOpenAI

from jobagent.config import Config

log = logging.getLogger(__name__)

COGNITIVE_SCOPE = "https://cognitiveservices.azure.com/.default"


@lru_cache
def azure_token_provider():
    # AzureCliCredential shells out to `az account get-access-token`; get_bearer_token_provider
    # caches the token and refreshes it before expiry, so long background runs keep working.
    return get_bearer_token_provider(AzureCliCredential(process_timeout=30), COGNITIVE_SCOPE)


def _azure_kwargs(cfg: Config) -> dict:
    return dict(
        azure_endpoint=cfg.get("azure.endpoint"),
        api_version=cfg.get("azure.api_version", "2025-04-01-preview"),
        azure_ad_token_provider=azure_token_provider(),
    )


@lru_cache
def _openai_client(endpoint: str, api_version: str) -> AsyncAzureOpenAI:
    return AsyncAzureOpenAI(
        azure_endpoint=endpoint, api_version=api_version,
        azure_ad_token_provider=azure_token_provider(), max_retries=4, timeout=180,
    )


class Luna:
    """Thin chat wrapper over the Azure deployment, with a fallback deployment on errors."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.client = _openai_client(cfg.get("azure.endpoint"), cfg.get("azure.api_version", "2025-04-01-preview"))
        self.deployments = [cfg.get("azure.deployment", "gpt-6-luna")]
        if fb := cfg.get("azure.fallback_deployment"):
            self.deployments.append(fb)

    async def chat(self, system: str, user: str, *, json_mode: bool = False, max_tokens: int = 4000) -> str:
        last: Exception | None = None
        for dep in self.deployments:
            try:
                resp = await self.client.chat.completions.create(
                    model=dep,
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
                    max_completion_tokens=max_tokens,
                    **({"response_format": {"type": "json_object"}} if json_mode else {}),
                )
                return resp.choices[0].message.content or ""
            except Exception as e:  # noqa: BLE001 - try the fallback deployment
                log.warning("luna deployment %s failed: %s", dep, e)
                last = e
        raise RuntimeError(f"all Azure deployments failed: {last}")

    async def chat_json(self, system: str, user: str, **kw) -> dict:
        return json.loads(await self.chat(system, user, json_mode=True, **kw))


def browser_llm(cfg: Config, deployment: str | None = None):
    """LLM object for browser-use's Agent."""
    from browser_use.llm.azure.chat import ChatAzureOpenAI

    dep = deployment or cfg.get("azure.deployment", "gpt-6-luna")
    # gpt-6 deployments are reasoning models: they reject custom temperature/frequency_penalty.
    return ChatAzureOpenAI(
        model=dep, azure_deployment=dep, use_responses_api=False,
        reasoning_models=[dep], reasoning_effort=cfg.get("azure.reasoning_effort", "low"),
        **_azure_kwargs(cfg),
    )


class Clef:
    """Cloudflare Workers AI decision models (@cf/cloudflare/clef, @cf/cloudflare/clef-flash)."""

    def __init__(self, cfg: Config, concurrency: int = 0):
        self.account = os.environ.get("CLOUDFLARE_ACCOUNT_ID") or os.environ["CF_ACCOUNT_ID"]
        token = os.environ.get("CLOUDFLARE_API_TOKEN") or os.environ["CF_API_TOKEN"]
        self.http = httpx.AsyncClient(
            base_url=f"https://api.cloudflare.com/client/v4/accounts/{self.account}/ai/run/",
            headers={"Authorization": f"Bearer {token}"}, timeout=120,  # image requests can take 15-30s
        )
        self.sem = asyncio.Semaphore(concurrency or cfg.get("cloudflare.concurrency", 48))

    async def ask(self, model: str, state, questions: dict, images: list[bytes] | None = None) -> dict:
        """model: 'clef' or 'clef-flash'. Returns the `answers` dict keyed by question id."""
        body: dict = {"model": model, "state": state, "questions": questions}
        if images:
            images = images[:4]  # the ~180 KB image budget is per call, not per image: split it
            body["images"] = [{"content_type": "image/jpeg",
                               "base64": base64.b64encode(shrink(i, 170_000 // len(images))).decode()}
                              for i in images]
        async with self.sem:
            for attempt in range(5):
                r = await self.http.post(f"@cf/cloudflare/{model}", json=body)
                if r.status_code in (429, 500, 502, 503, 504):
                    await asyncio.sleep(2 ** attempt)
                    continue
                data = r.json()
                if not data.get("success"):
                    raise RuntimeError(f"clef error: {data.get('errors')}")
                return data["result"]["answers"]
        raise RuntimeError(f"clef {model} kept failing: HTTP {r.status_code}")

    async def aclose(self):
        await self.http.aclose()


def shrink(img: bytes, max_bytes: int = 180_000) -> bytes:
    """Clef counts image pixels against its 65k-token context: images much over ~190 KB fail with a
    context-length error, so downscale to a JPEG under max_bytes."""
    from PIL import Image

    im = Image.open(io.BytesIO(img)).convert("RGB")
    width, quality = min(im.width, 1100), 80
    while True:
        out = io.BytesIO()
        im.resize((width, round(im.height * width / im.width))).save(out, "JPEG", quality=quality)
        if out.tell() <= max_bytes or width <= 360:
            return out.getvalue()
        width, quality = int(width * 0.85), max(55, quality - 5)


def confident(ans: dict, lo: float = 0.25, hi: float = 0.75, min_conf: float = 0.5) -> bool:
    """True when a clef answer is decisive enough to act on without a second opinion."""
    if ans["type"] == "noul":
        return not (lo < ans["noul"] < hi)
    return ans.get("confidence", 1.0) >= min_conf
