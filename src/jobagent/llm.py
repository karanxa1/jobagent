"""Model access.

- The main LLM (config `llm:`; or the legacy `azure:` section) drives the browser agent and writes text (profile
  extraction, cover letters, free-text form answers). Providers: azure (az login or AZURE_OPENAI_API_KEY), openai,
  openrouter, gemini, anthropic, ollama, groq, deepseek, mistral, openai_compatible.
- Decision models return calibrated probabilities for typed questions (noul / choice / score). Used for job
  triage (thousands of postings) and for verifying post-submit screenshots. Either Cloudflare Workers AI
  clef / clef-flash (`Clef`), or the main LLM asked for probabilities in one JSON call (`LLMDecider`).
  `make_decider(cfg)` picks one.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import math
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache

import httpx
from openai import AsyncAzureOpenAI, AsyncOpenAI

from jobagent.config import Config

log = logging.getLogger(__name__)

COGNITIVE_SCOPE = "https://cognitiveservices.azure.com/.default"
AZURE_API_VERSION = "2025-04-01-preview"

# provider -> (OpenAI-compatible base URL for the text client, env vars holding the key, in order)
PROVIDERS: dict[str, tuple[str | None, tuple[str, ...]]] = {
    "azure": (None, ("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_KEY")),
    "openai": (None, ("OPENAI_API_KEY",)),
    "openrouter": ("https://openrouter.ai/api/v1", ("OPENROUTER_API_KEY",)),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai/", ("GEMINI_API_KEY", "GOOGLE_API_KEY")),
    "anthropic": (None, ("ANTHROPIC_API_KEY",)),
    "ollama": ("http://localhost:11434/v1", ()),
    "groq": ("https://api.groq.com/openai/v1", ("GROQ_API_KEY",)),
    "deepseek": ("https://api.deepseek.com/v1", ("DEEPSEEK_API_KEY",)),
    "mistral": ("https://api.mistral.ai/v1", ("MISTRAL_API_KEY",)),
    "openai_compatible": (None, ("OPENAI_COMPATIBLE_API_KEY",)),
}
ALIASES = {"google": "gemini", "claude": "anthropic", "azure_openai": "azure", "compatible": "openai_compatible",
           "custom": "openai_compatible"}
# providers whose chat-completions API takes max_completion_tokens; the rest take max_tokens
_MAX_COMPLETION_TOKENS = {"azure", "openai"}


# ---- settings -------------------------------------------------------------------------------------------------

@dataclass
class LLMSettings:
    provider: str
    model: str
    fallback_model: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    api_version: str = AZURE_API_VERSION     # azure only
    reasoning_effort: str | None = None
    max_output_tokens: int | None = None     # optional cap on every request's output tokens
    headers: dict = field(default_factory=dict)

    @property
    def models(self) -> list[str]:
        return [self.model] + ([self.fallback_model] if self.fallback_model and self.fallback_model != self.model else [])


def _env_key(names: tuple[str, ...]) -> str | None:
    return next((os.environ[n] for n in names if os.environ.get(n)), None)


def llm_settings(cfg: Config) -> LLMSettings:
    """Resolve the main LLM from config. No `llm:` section (or llm.provider: azure) = the legacy `azure:` section,
    with az login auth unless AZURE_OPENAI_API_KEY is set."""
    provider = str(cfg.get("llm.provider") or "azure").strip().lower()
    provider = ALIASES.get(provider, provider)
    if provider not in PROVIDERS:
        raise ValueError(f"unknown llm.provider {provider!r}; use one of {', '.join(PROVIDERS)}")
    default_url, key_envs = PROVIDERS[provider]
    key_env = cfg.get("llm.api_key_env")  # optional: read the key from a differently named variable
    api_key = os.environ.get(key_env) if key_env else _env_key(key_envs)
    effort = cfg.get("llm.reasoning_effort")
    max_out = cfg.get("llm.max_output_tokens")
    if provider == "azure":
        return LLMSettings(
            provider="azure",
            model=cfg.get("llm.model") or cfg.get("azure.deployment", "gpt-6-luna"),
            fallback_model=cfg.get("llm.fallback_model") or cfg.get("azure.fallback_deployment"),
            base_url=cfg.get("llm.base_url") or cfg.get("azure.endpoint"),
            api_key=api_key,
            api_version=cfg.get("llm.api_version") or cfg.get("azure.api_version", AZURE_API_VERSION),
            reasoning_effort=effort or cfg.get("azure.reasoning_effort", "low"),
            max_output_tokens=max_out,
        )
    model = cfg.get("llm.model")
    if not model:
        raise ValueError(f"llm.model is required for llm.provider {provider}")
    base_url = cfg.get("llm.base_url") or default_url
    if provider == "openai_compatible" and not base_url:
        raise ValueError("llm.base_url is required for llm.provider openai_compatible")
    if provider in ("openai", "openrouter", "gemini", "groq", "deepseek", "mistral", "anthropic") and not api_key:
        raise ValueError(f"llm.provider {provider} needs {' or '.join(key_envs)} in .env")
    headers = dict(cfg.get("llm.headers") or {})
    if provider == "openrouter":
        headers.setdefault("X-Title", "jobagent")
    return LLMSettings(provider=provider, model=str(model), fallback_model=cfg.get("llm.fallback_model"),
                       base_url=base_url, api_key=api_key, reasoning_effort=effort, max_output_tokens=max_out,
                       headers=headers)


# ---- clients --------------------------------------------------------------------------------------------------

@lru_cache
def azure_token_provider():
    from azure.identity import AzureCliCredential, get_bearer_token_provider

    # AzureCliCredential shells out to `az account get-access-token`; get_bearer_token_provider
    # caches the token and refreshes it before expiry, so long background runs keep working.
    return get_bearer_token_provider(AzureCliCredential(process_timeout=30), COGNITIVE_SCOPE)


def _azure_auth(s: LLMSettings) -> dict:
    return {"api_key": s.api_key} if s.api_key else {"azure_ad_token_provider": azure_token_provider()}


@lru_cache
def _openai_client(endpoint: str, api_version: str) -> AsyncAzureOpenAI:
    """Azure client authenticated with `az login` (the original setup)."""
    return AsyncAzureOpenAI(
        azure_endpoint=endpoint, api_version=api_version,
        azure_ad_token_provider=azure_token_provider(), max_retries=4, timeout=180,
    )


@lru_cache
def _azure_key_client(endpoint: str, api_version: str, api_key: str) -> AsyncAzureOpenAI:
    return AsyncAzureOpenAI(azure_endpoint=endpoint, api_version=api_version, api_key=api_key,
                            max_retries=4, timeout=180)


@lru_cache
def _compat_client(base_url: str | None, api_key: str | None, headers: tuple) -> AsyncOpenAI:
    return AsyncOpenAI(base_url=base_url, api_key=api_key or "not-needed", default_headers=dict(headers) or None,
                       max_retries=4, timeout=180)


@lru_cache
def _anthropic_client(base_url: str | None, api_key: str | None):
    from anthropic import AsyncAnthropic

    return AsyncAnthropic(api_key=api_key, base_url=base_url, max_retries=4, timeout=300)


def text_client(s: LLMSettings):
    if s.provider == "azure":
        if s.api_key:
            return _azure_key_client(s.base_url, s.api_version, s.api_key)
        return _openai_client(s.base_url, s.api_version)
    if s.provider == "anthropic":
        return _anthropic_client(s.base_url if s.base_url else None, s.api_key)
    return _compat_client(s.base_url, s.api_key, tuple(sorted(s.headers.items())))


def parse_json(text: str):
    """JSON from a model reply that may wrap it in prose or ``` fences."""
    text = (text or "").strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    if m := re.search(r"```(?:json)?\s*(.*?)```", text, re.S):
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass
    dec = json.JSONDecoder()
    for i, ch in enumerate(text):  # first decodable object/array in the text
        if ch in "{[":
            try:
                return dec.raw_decode(text[i:])[0]
            except json.JSONDecodeError:
                continue
    raise ValueError(f"no JSON in model reply: {text[:200]!r}")


def _jpeg_b64(img: bytes, max_bytes: int = 400_000) -> str:
    return base64.b64encode(shrink(img, max_bytes)).decode()


def _bad_param(e: Exception, *words: str) -> bool:
    """A 400/422 complaining about one of `words` (an unsupported request option)."""
    msg = str(e).lower()
    return getattr(e, "status_code", None) in (400, 422) and any(w in msg for w in words)


class LLM:
    """Text / JSON chat over the configured provider, with a fallback model on errors."""

    def __init__(self, cfg: Config, settings: LLMSettings | None = None):
        self.cfg = cfg
        self.s = settings or llm_settings(cfg)
        self.client = text_client(self.s)
        self.deployments = self.s.models  # name kept from the Azure-only version
        self._no_json_mode = False

    @property
    def provider(self) -> str:
        return self.s.provider

    async def chat(self, system: str, user: str, *, json_mode: bool = False, max_tokens: int = 4000,
                   images: list[bytes] | None = None, model: str | None = None,
                   reasoning_effort: str | None = None) -> str:
        if self.s.max_output_tokens:
            max_tokens = min(max_tokens, int(self.s.max_output_tokens))
        models = [model, *[m for m in self.s.models if m != model]] if model else self.s.models
        last: Exception | None = None
        for m in models:
            try:
                if self.s.provider == "anthropic":
                    return await self._anthropic(m, system, user, json_mode, max_tokens, images)
                return await self._compat(m, system, user, json_mode, max_tokens, images, reasoning_effort)
            except Exception as e:  # noqa: BLE001 - try the fallback model
                log.warning("llm %s model %s failed: %s", self.s.provider, m, e)
                last = e
        raise RuntimeError(f"all {self.s.provider} models failed: {last}")

    async def _compat(self, model, system, user, json_mode, max_tokens, images, effort) -> str:
        if json_mode and self.s.provider != "azure" and "json" not in (system + user).lower():
            system += "\nRespond with a single JSON object."
        content: str | list = user
        if images:
            content = [{"type": "text", "text": user}] + [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{_jpeg_b64(i)}"}} for i in images]
        kw: dict = {}
        if max_tokens:
            kw["max_completion_tokens" if self.s.provider in _MAX_COMPLETION_TOKENS else "max_tokens"] = max_tokens
        if json_mode and not self._no_json_mode:
            kw["response_format"] = {"type": "json_object"}
        if effort:
            kw["reasoning_effort"] = effort
        for _ in range(4):  # drop request options the provider/model rejects, one at a time
            try:
                resp = await self.client.chat.completions.create(
                    model=model, messages=[{"role": "system", "content": system}, {"role": "user", "content": content}],
                    **kw)
                break
            except Exception as e:
                if "response_format" in kw and _bad_param(e, "response_format", "json_object", "json mode", "json_mode"):
                    log.info("%s/%s: no JSON mode, using prompt-only JSON", self.s.provider, model)
                    self._no_json_mode = True
                    kw.pop("response_format")
                elif "reasoning_effort" in kw and _bad_param(e, "reasoning"):
                    kw.pop("reasoning_effort")
                elif any(k in kw for k in ("max_tokens", "max_completion_tokens")) and _bad_param(
                        e, "max_tokens", "max_completion_tokens", "max_output", "maximum number of tokens"):
                    kw.pop("max_tokens", None)
                    kw.pop("max_completion_tokens", None)
                else:
                    raise
        else:
            raise RuntimeError(f"{self.s.provider}/{model}: request kept failing")
        if not resp.choices:
            raise RuntimeError(f"{self.s.provider}/{model}: empty response")
        return resp.choices[0].message.content or ""

    async def _anthropic(self, model, system, user, json_mode, max_tokens, images) -> str:
        if json_mode:
            system += "\nRespond with a single JSON object and nothing else (no prose, no code fences)."
        content: list = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                      "data": _jpeg_b64(i)}} for i in images or []]
        content.append({"type": "text", "text": user})
        kw = dict(model=model, system=system, messages=[{"role": "user", "content": content}])
        try:  # streamed: the SDK refuses large non-streaming max_tokens
            async with self.client.messages.stream(max_tokens=max_tokens or 8192, **kw) as stream:
                msg = await stream.get_final_message()
        except Exception as e:
            if not (max_tokens and max_tokens > 8192 and _bad_param(e, "max_tokens")):
                raise
            async with self.client.messages.stream(max_tokens=8192, **kw) as stream:  # smaller models cap output
                msg = await stream.get_final_message()
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")

    async def chat_json(self, system: str, user: str, **kw) -> dict:
        text = await self.chat(system, user, json_mode=True, **kw)
        try:
            return parse_json(text)
        except ValueError:
            if self.s.provider == "azure" and not self._no_json_mode:
                raise  # native JSON mode: a parse failure is a real error, as before
            text = await self.chat(system + "\nYour previous reply was not valid JSON. Reply with JSON only.",
                                   user, json_mode=True, **kw)
            return parse_json(text)


Luna = LLM  # the original name (Azure deployment wrapper); kept for callers


# ---- browser-use chat model -----------------------------------------------------------------------------------

def browser_llm(cfg: Config, deployment: str | None = None):
    """LLM object for browser-use's Agent (the provider's own browser-use chat class)."""
    s = llm_settings(cfg)
    model = deployment or s.model
    effort = s.reasoning_effort
    p = s.provider
    if p == "azure":
        from browser_use.llm.azure.chat import ChatAzureOpenAI

        # gpt-6 deployments are reasoning models: they reject custom temperature/frequency_penalty.
        return ChatAzureOpenAI(
            model=model, azure_deployment=model, use_responses_api=False,
            reasoning_models=[model], reasoning_effort=effort or "low",
            azure_endpoint=s.base_url, api_version=s.api_version, **_azure_auth(s),
        )
    if p in ("openai", "openai_compatible"):
        from browser_use.llm.openai.chat import ChatOpenAI

        kw: dict = {"model": model, "api_key": s.api_key or "not-needed", "base_url": s.base_url,
                    "default_headers": s.headers or None}
        if effort:  # reasoning_effort set = a reasoning model: no temperature / frequency_penalty
            kw.update(reasoning_effort=effort, reasoning_models=[model])
        elif p == "openai_compatible":
            kw.update(frequency_penalty=None)  # not every compatible server accepts it
        return ChatOpenAI(**kw)
    if p == "openrouter":
        from browser_use.llm.openrouter.chat import ChatOpenRouter

        return ChatOpenRouter(model=model, api_key=s.api_key, base_url=s.base_url, default_headers=s.headers or None,
                              http_referer=s.headers.get("HTTP-Referer"),
                              extra_body={"reasoning": {"effort": effort}} if effort else None)
    if p == "gemini":
        from browser_use.llm.google.chat import ChatGoogle

        kw = {"model": model, "api_key": s.api_key}
        if effort and "gemini-3" in model and effort in ("minimal", "low", "medium", "high"):
            kw["thinking_level"] = effort
        return ChatGoogle(**kw)
    if p == "anthropic":
        from browser_use.llm.anthropic.chat import ChatAnthropic

        return ChatAnthropic(model=model, api_key=s.api_key,
                             base_url=cfg.get("llm.base_url") or None)
    if p == "ollama":
        from browser_use.llm.ollama.chat import ChatOllama

        host = re.sub(r"/v1/?$", "", s.base_url or "http://localhost:11434")
        return ChatOllama(model=model, host=host)
    if p == "groq":
        from browser_use.llm.groq.chat import ChatGroq

        return ChatGroq(model=model, api_key=s.api_key, base_url=cfg.get("llm.base_url") or None)
    if p == "deepseek":
        from browser_use.llm.deepseek.chat import ChatDeepSeek

        return ChatDeepSeek(model=model, api_key=s.api_key, base_url=s.base_url)
    if p == "mistral":
        from browser_use.llm.mistral.chat import ChatMistral

        return ChatMistral(model=model, api_key=s.api_key, base_url=s.base_url)
    raise ValueError(f"no browser-use model for provider {p}")


def browser_fallback_llm(cfg: Config):
    """browser-use fallback model (llm.fallback_model / azure.fallback_deployment), or None."""
    fb = llm_settings(cfg).fallback_model
    return browser_llm(cfg, fb) if fb else None


# ---- decision models ------------------------------------------------------------------------------------------

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


DECIDER_SYSTEM = """You are a careful, calibrated classifier. You read a STATE (and any attached images) and answer
every question with probabilities that reflect your real uncertainty: use values near 0 or 1 only when the evidence is
clear, and spread probability across options when it is ambiguous or the information is missing.

Answer formats, by question type:
- "noul" (yes/no): {"p_true": <probability the statement is true, 0..1>}
- "choice": {"probabilities": {"<option>": <p>, ...}} with EVERY listed option as a key (exact spelling), summing to 1
- "score": {"probabilities": {"0": <p>, "1": <p>, ...}} over EVERY level index, summing to 1

Respond with one JSON object: {"answers": {"<question id>": {...}, ...}} covering every question id."""


def _options(criteria) -> list[str]:
    if isinstance(criteria, dict):
        return [str(k) for k in criteria]
    return [str(c) for c in criteria or []]


def _describe(qid: str, q: dict) -> str:
    t = q.get("type")
    out = [f'### {qid} (type "{t}")', q.get("instructions", "")]
    crit = q.get("criteria")
    if t == "noul":
        if isinstance(crit, dict):
            out += [f"- true means: {crit.get('true', 'yes')}", f"- false means: {crit.get('false', 'no')}"]
    elif t == "choice":
        out.append("Options:")
        if isinstance(crit, dict):
            out += [f'- "{k}"' + (f": {v}" if v else "") for k, v in crit.items()]
        else:
            out += [f'- "{k}"' for k in _options(crit)]
    elif t == "score":
        out.append("Levels (low to high):")
        out += [f'- "{i}": {c}' for i, c in enumerate(_options(crit))]
    return "\n".join(x for x in out if x)


def _weight(v, default: float = 0.0) -> float:
    """A non-negative number from a model value: 0.7, "0.7", "70%", {"p_true": 0.7}, true/false."""
    if isinstance(v, dict):
        v = next((v[k] for k in ("p_true", "p", "probability", "noul", "true", "yes") if k in v), default)
    if isinstance(v, bool):
        return float(v)
    pct = isinstance(v, str) and v.strip().endswith("%")
    try:
        f = float(v.strip().rstrip("%")) if isinstance(v, str) else float(v)
    except (TypeError, ValueError):
        return default
    if math.isnan(f) or math.isinf(f):
        return default
    return max(0.0, f / 100 if pct else f)


def _num(v, default: float = 0.0) -> float:
    """A probability in [0, 1]; values in [2, 100] are read as percentages."""
    f = _weight(v, default)
    return min(1.0, f / 100 if 2 <= f <= 100 else f)


def _distribution(raw, keys: list[str], aliases: dict[str, str] | None = None) -> dict[str, float]:
    """Map the model's probabilities onto `keys` (case-insensitive; `aliases` maps other spellings to a key),
    clamp and normalise."""
    if isinstance(raw, dict) and "probabilities" in raw:
        raw = raw["probabilities"]
    if isinstance(raw, list):  # [p0, p1, ...]
        raw = {str(i): v for i, v in enumerate(raw)}
    raw = raw if isinstance(raw, dict) else {}
    norm: dict[str, float] = {}
    for k, v in raw.items():
        k = str(k).strip().strip('"').lower()
        k = (aliases or {}).get(k, k)
        norm[k] = norm.get(k, 0.0) + _weight(v)  # weights or percentages are fine: normalised below
    probs = {k: norm.get(k.lower(), 0.0) for k in keys}
    total = sum(probs.values())
    if total <= 0:
        return {k: round(1 / len(keys), 4) for k in keys}
    return {k: round(v / total, 4) for k, v in probs.items()}


def _confidence(probs: dict[str, float]) -> float:
    """1 - normalised entropy: 1 = all mass on one option, 0 = uniform."""
    if len(probs) < 2:
        return 1.0
    h = -sum(p * math.log(p) for p in probs.values() if p > 0)
    return round(max(0.0, 1 - h / math.log(len(probs))), 4)


def to_answers(questions: dict, raw: dict) -> dict:
    """Shape an LLM's probabilities like clef's `answers` (see Clef.ask). Raises KeyError for a missing question."""
    raw = raw.get("answers", raw) if isinstance(raw, dict) else {}
    out = {}
    for qid, q in questions.items():
        if qid not in raw:
            raise KeyError(f"model skipped question {qid!r}")
        a, t = raw[qid], q.get("type")
        if t == "noul":
            out[qid] = {"type": "noul", "noul": round(_num(a, 0.5), 4)}
        elif t == "choice":
            probs = _distribution(a, _options(q.get("criteria")))
            out[qid] = {"type": "choice", "choice": max(probs, key=probs.get), "probabilities": probs,
                        "confidence": _confidence(probs)}
        elif t == "score":
            levels = _options(q.get("criteria"))
            probs = _distribution(a, [str(i) for i in range(len(levels))],
                                  {c.strip().lower(): str(i) for i, c in enumerate(levels)})
            out[qid] = {"type": "score", "score": round(sum(int(k) * p for k, p in probs.items()), 4),
                        "legend": {str(i): c for i, c in enumerate(levels)}, "probabilities": probs,
                        "confidence": _confidence(probs)}
        else:
            raise ValueError(f"unknown question type {t!r} for {qid}")
    return out


class LLMDecider:
    """Clef-compatible decider on the main LLM: same ask() signature, same answer shapes, one JSON call per ask."""

    def __init__(self, cfg: Config, llm: LLM | None = None, concurrency: int = 0):
        self.cfg = cfg
        self.llm = llm or LLM(cfg)
        self.sem = asyncio.Semaphore(concurrency or cfg.get("decider.concurrency", 8))
        main = self.llm.s.model
        self.models = {"clef": cfg.get("decider.model") or main,
                       "clef-flash": cfg.get("decider.fast_model") or cfg.get("decider.model") or main}
        self.max_state_chars = int(cfg.get("decider.max_state_chars", 60_000))
        self.effort = cfg.get("decider.reasoning_effort") or self.llm.s.reasoning_effort

    def resolve(self, model: str) -> str:
        """'clef' / 'clef-flash' -> decider.model / decider.fast_model; anything else is a literal model id."""
        return self.models.get(model, model)

    async def ask(self, model: str, state, questions: dict, images: list[bytes] | None = None) -> dict:
        """model: 'clef' / 'clef-flash' (mapped to decider.model / decider.fast_model) or a literal model id."""
        model = self.resolve(model)
        st = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False, indent=1)
        if len(st) > self.max_state_chars:
            st = st[: self.max_state_chars] + "\n...[truncated]"
        qs = "\n\n".join(_describe(k, q) for k, q in questions.items())
        user = f"STATE:\n{st}\n\nQUESTIONS:\n{qs}"
        images = (images or [])[:4]
        async with self.sem:
            if images:
                try:
                    return await self._call(model, user + f"\n\n{len(images)} image(s) attached, oldest first.",
                                            questions, images)
                except Exception as e:  # noqa: BLE001 - e.g. a text-only model: retry blind, and say so
                    log.warning("decider call with images failed (%s); retrying without them", e)
                    user += ("\n\nNOTE: the screenshot(s) for this question could not be attached. Judge from the "
                             "text alone and keep probabilities near 0.5 where only the image could tell.")
            return await self._call(model, user, questions, None)

    async def _call(self, model, user, questions, images) -> dict:
        last: Exception | None = None
        for _ in range(2):
            raw = await self.llm.chat(DECIDER_SYSTEM, user, json_mode=True, max_tokens=8000, images=images,
                                      model=model, reasoning_effort=self.effort)
            try:
                return to_answers(questions, parse_json(raw))
            except (ValueError, KeyError) as e:
                last = e
                log.info("decider reply unusable (%s); asking again", e)
        raise RuntimeError(f"decider: no usable answer: {last}")

    async def aclose(self):
        pass


Decider = Clef | LLMDecider


def _cloudflare_env() -> bool:
    return bool((os.environ.get("CLOUDFLARE_ACCOUNT_ID") or os.environ.get("CF_ACCOUNT_ID"))
                and (os.environ.get("CLOUDFLARE_API_TOKEN") or os.environ.get("CF_API_TOKEN")))


def make_decider(cfg: Config, llm: LLM | None = None) -> Decider:
    """decider.provider: cloudflare -> Clef; llm -> LLMDecider; unset -> Clef if Cloudflare keys are in .env."""
    prov = str(cfg.get("decider.provider") or "auto").strip().lower()
    if prov == "cloudflare" or (prov == "auto" and _cloudflare_env()):
        return Clef(cfg, int(cfg.get("decider.concurrency") or 0))
    if prov not in ("auto", "llm"):
        raise ValueError(f"unknown decider.provider {prov!r}; use cloudflare or llm")
    return LLMDecider(cfg, llm)


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
    """True when a decider answer is decisive enough to act on without a second opinion."""
    if ans["type"] == "noul":
        return not (lo < ans["noul"] < hi)
    return ans.get("confidence", 1.0) >= min_conf
