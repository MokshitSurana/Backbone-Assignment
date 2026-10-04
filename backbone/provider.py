"""Model providers, over the standard library only.

Groq and Anthropic are both reachable with plain HTTP, so the project keeps its
no-dependency promise even with the model path enabled. Everything here is
transport and accounting; no prompt and no clinical logic lives in this file.

Credentials are read from the environment, or from a gitignored `.env` in the
repository root so a key never has to be pasted into a shell history:

    GROQ_API_KEY=gsk_...
    BACKBONE_PROVIDER=groq            # optional; inferred from whichever key is set
    BACKBONE_MODEL=llama-3.3-70b-versatile
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import time
import urllib.error
import urllib.request

ENV_FILE = pathlib.Path(__file__).resolve().parents[1] / ".env"
_loaded = False

# Groq sits behind Cloudflare, which rejects urllib's default
# "Python-urllib/x.y" user agent with error 1010. Identify the client properly.
UA = "backbone-clinical-abstraction/1.1 (+https://github.com/; python-stdlib)"
_BASE_HEADERS = {"User-Agent": UA, "Accept": "application/json"}

# Free tiers meter tokens per minute, so a 31-document corpus will hit the
# ceiling. The governor below paces calls against a rolling one-minute window
# instead of discovering the limit by being refused.
TPM_BUDGET = int(os.environ.get("BACKBONE_TPM", "8000"))
_spend: list[tuple[float, int]] = []          # (timestamp, tokens)


def _record(tokens: int) -> None:
    _spend.append((time.time(), tokens))


def _wait_for_budget(est_tokens: int, verbose: bool = True) -> None:
    """Sleep until `est_tokens` fits inside the rolling per-minute budget."""
    while True:
        now = time.time()
        _spend[:] = [(t, n) for t, n in _spend if now - t < 60.0]
        used = sum(n for _, n in _spend)
        if used + est_tokens <= TPM_BUDGET or not _spend:
            return
        oldest = min(t for t, _ in _spend)
        nap = max(0.5, 60.0 - (now - oldest) + 0.5)
        if verbose:
            print(f"    [rate governor: {used} tokens used in the last minute, "
                  f"budget {TPM_BUDGET}; sleeping {nap:.1f}s]", flush=True)
        time.sleep(nap)


def _retry_after(msg: str, headers) -> float:
    """Honour the provider's own hint before falling back to a backoff."""
    try:
        ra = headers.get("retry-after") if headers else None
        if ra:
            return float(ra) + 0.5
    except (TypeError, ValueError):
        pass
    m = re.search(r"try again in ([\d.]+)s", msg or "")
    if m:
        return float(m.group(1)) + 0.5
    return 0.0

# Approximate published on-demand prices, USD per million tokens (input, output).
# Used only to put a cost line in the benchmark; free-tier usage costs nothing,
# and the measured token counts are the figures that matter.
PRICES = {
    # Groq
    "llama-3.3-70b-versatile": (0.59, 0.79),
    "llama-3.1-8b-instant": (0.05, 0.08),
    "openai/gpt-oss-120b": (0.15, 0.75),
    "openai/gpt-oss-20b": (0.10, 0.50),
    "qwen/qwen3-32b": (0.29, 0.59),
    "moonshotai/kimi-k2-instruct": (1.00, 3.00),
    "meta-llama/llama-4-scout-17b-16e-instruct": (0.11, 0.34),
    "meta-llama/llama-4-maverick-17b-128e-instruct": (0.20, 0.60),
    "deepseek-r1-distill-llama-70b": (0.75, 0.99),
    "gemma2-9b-it": (0.20, 0.20),
    # Anthropic
    "claude-opus-5": (15.0, 75.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}

DEFAULT_MODEL = {"groq": "openai/gpt-oss-120b", "anthropic": "claude-sonnet-5"}


def load_env() -> None:
    """Read a gitignored .env once, without overriding real environment vars."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    if not ENV_FILE.exists():
        return
    # utf-8-sig: PowerShell's `Set-Content -Encoding utf8` writes a BOM, which
    # would otherwise make the first key "﻿GROQ_API_KEY".
    for line in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip().lstrip("﻿")
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().lstrip("﻿")
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def provider() -> str:
    load_env()
    p = os.environ.get("BACKBONE_PROVIDER", "").strip().lower()
    if p:
        return p
    if os.environ.get("GROQ_API_KEY"):
        return "groq"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return "none"


def model() -> str:
    load_env()
    return os.environ.get("BACKBONE_MODEL") or DEFAULT_MODEL.get(provider(), "")


def available() -> tuple[bool, str]:
    p = provider()
    if p == "groq":
        return (True, "") if os.environ.get("GROQ_API_KEY") else \
            (False, "BACKBONE_PROVIDER=groq but GROQ_API_KEY is not set")
    if p == "anthropic":
        return (True, "") if os.environ.get("ANTHROPIC_API_KEY") else \
            (False, "BACKBONE_PROVIDER=anthropic but ANTHROPIC_API_KEY is not set")
    return False, ("no model credentials found; set GROQ_API_KEY or "
                   "ANTHROPIC_API_KEY in the environment or in a .env file")


def describe() -> str:
    ok, why = available()
    if not ok:
        return why
    return f"provider={provider()} model={model()}"


# ---------------------------------------------------------------------------
class ModelError(RuntimeError):
    pass


def _post(url: str, payload: dict, headers: dict, timeout: int = 180) -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          **_BASE_HEADERS, **headers})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _get(url: str, headers: dict, timeout: int = 60) -> dict:
    req = urllib.request.Request(url, method="GET",
                                 headers={**_BASE_HEADERS, **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # The provider's own message is far more useful than a traceback.
        detail = e.read().decode("utf-8", "replace")[:600]
        try:
            msg = json.loads(detail).get("error", {}).get("message") or detail
        except json.JSONDecodeError:
            msg = detail
        raise ModelError(f"HTTP {e.code} from {url}: {msg}") from None


def list_models() -> list[dict]:
    """Models this key can actually use, so a benchmark never guesses."""
    ok, why = available()
    if not ok:
        raise SystemExit(why)
    p = provider()
    if p == "groq":
        key = os.environ["GROQ_API_KEY"]
        data = _get("https://api.groq.com/openai/v1/models",
                    {"Authorization": f"Bearer {key}"})
        out = []
        for m in data.get("data", []):
            out.append({"id": m.get("id"), "owned_by": m.get("owned_by"),
                        "context": m.get("context_window"),
                        "priced": m.get("id") in PRICES})
        return sorted(out, key=lambda x: str(x["id"]))
    if p == "anthropic":
        key = os.environ["ANTHROPIC_API_KEY"]
        data = _get("https://api.anthropic.com/v1/models",
                    {"x-api-key": key, "anthropic-version": "2023-06-01"})
        return [{"id": m.get("id"), "owned_by": "anthropic",
                 "context": None, "priced": m.get("id") in PRICES}
                for m in data.get("data", [])]
    raise SystemExit("no provider")


def complete(system: str, user: str, *, max_tokens: int = 4000,
             temperature: float = 0.0, json_object: bool = True,
             retries: int = 8) -> tuple[str, dict]:
    """One completion. Returns (text, usage) with measured token counts."""
    ok, why = available()
    if not ok:
        raise ModelError(why)
    p, mdl = provider(), model()
    last: Exception | None = None
    # ~4 characters per token is close enough to pace with, plus the reply.
    est = (len(system) + len(user)) // 4 + min(max_tokens, 1200)
    for attempt in range(retries):
        try:
            if p == "groq":
                _wait_for_budget(est)
            t0 = time.perf_counter()
            if p == "groq":
                payload = {
                    "model": mdl,
                    "messages": [{"role": "system", "content": system},
                                 {"role": "user", "content": user}],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }
                if json_object and attempt < retries - 2:
                    # Strict JSON mode can refuse outright on a long document;
                    # the last attempts drop it and parse leniently instead.
                    payload["response_format"] = {"type": "json_object"}
                # Reasoning tokens count against the completion budget and can
                # truncate a long extraction. Rather than capping reasoning for
                # every call -- which measurably changes what the model
                # extracts -- leave it at the provider default and only reduce
                # it after a refusal. BACKBONE_REASONING overrides.
                forced = os.environ.get("BACKBONE_REASONING", "").strip()
                if "gpt-oss" in mdl:
                    if forced:
                        payload["reasoning_effort"] = forced
                    elif attempt > 0:
                        payload["reasoning_effort"] = "low"
                data = _post("https://api.groq.com/openai/v1/chat/completions",
                             payload,
                             {"Authorization": f"Bearer {os.environ['GROQ_API_KEY']}"})
                text = data["choices"][0]["message"]["content"] or ""
                u = data.get("usage") or {}
                in_tok = u.get("prompt_tokens", 0)
                out_tok = u.get("completion_tokens", 0)
            else:
                payload = {
                    "model": mdl, "max_tokens": max_tokens,
                    "temperature": temperature,
                    "system": system,
                    "messages": [{"role": "user", "content": user}],
                }
                data = _post("https://api.anthropic.com/v1/messages", payload,
                             {"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                              "anthropic-version": "2023-06-01"})
                text = "".join(b.get("text", "") for b in data.get("content", []))
                u = data.get("usage") or {}
                in_tok = u.get("input_tokens", 0)
                out_tok = u.get("output_tokens", 0)
            _record(in_tok + out_tok)
            pin, pout = PRICES.get(mdl, (0.0, 0.0))
            usage = {"provider": p, "model": mdl, "in_tokens": in_tok,
                     "out_tokens": out_tok,
                     "usd": round(in_tok / 1e6 * pin + out_tok / 1e6 * pout, 6),
                     "priced": mdl in PRICES,
                     "latency_ms": round((time.perf_counter() - t0) * 1000, 1),
                     "cached": False, "attempts": attempt + 1}
            return text, usage
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            last = ModelError(f"HTTP {e.code} from {p}: {detail}")
            # 429 = rate limit, 5xx = transient; both are worth waiting out.
            if (e.code in (408, 409, 429, 500, 502, 503, 504)
                    or "json_validate_failed" in detail) and attempt < retries - 1:
                hinted = _retry_after(detail, getattr(e, "headers", None))
                nap = hinted or min(2 ** attempt * 2, 30)
                if e.code == 429:
                    # The window is already full; make the governor aware of it.
                    _record(TPM_BUDGET)
                    print(f"    [429: waiting {nap:.1f}s]", flush=True)
                time.sleep(nap)
                continue
            raise last
        except (urllib.error.URLError, TimeoutError) as e:
            last = ModelError(f"network error talking to {p}: {e}")
            if attempt < retries - 1:
                time.sleep(min(2 ** attempt * 2, 30))
                continue
            raise last
    raise last or ModelError("unknown model failure")
