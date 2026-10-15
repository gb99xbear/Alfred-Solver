"""Brave Search API PoW solver (Argon2id proof-of-work).

Brave's API dashboard (api-dashboard.search.brave.com) protects register/login with
its OWN proof-of-work captcha - NOT Turnstile/hCaptcha/reCAPTCHA. Reverse-engineered
from the SvelteKit worker (chunk CuVRxiCZ.js) on 2026-10-16:

    GET /api-reference/captcha ->
        {set_token, tokens:[hex...], zero_count:N,
         hash_function_params:{iterations,memory_size,hash_length,parallelism},
         solution_limit:M}
    for each token:
        for attempt in 0..M:
            nonce = random 16 bytes (hex)
            h = Argon2id(secret=token, salt=nonce, t=iterations, m=memory_size,
                         p=parallelism, hash_len=hash_length)
            if hex(h).startswith("0" * zero_count): solution[token] = nonce; break
    answer = {set_token, solutions:{token:nonce}, taken_time}

Submit `answer` (JSON string) to the form's captcha field. Measured: 25 tokens,
~460 attempts, ~0.8s on the Oracle box.

Runs keyless and browser-free.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from typing import Any, Dict, Optional

log = logging.getLogger("brave_pow")

BRAVE_CAPTCHA_URL = "https://api-dashboard.search.brave.com/api-reference/captcha"
_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def _solve_token_sync(token: str, zero_count: int, params: Dict[str, int], limit: int):
    """Find a 16-byte hex nonce making hex(Argon2id(token, nonce)) start with zero_count zeros."""
    from argon2.low_level import hash_secret_raw, Type
    target = "0" * zero_count
    t = int(params.get("iterations", 2))
    m = int(params.get("memory_size", 512))
    p = int(params.get("parallelism", 1))
    dk = int(params.get("hash_length", 32))
    for attempt in range(limit):
        nonce = secrets.token_bytes(16)
        h = hash_secret_raw(secret=token.encode(), salt=nonce,
                            time_cost=t, memory_cost=m, parallelism=p,
                            hash_len=dk, type=Type.ID, version=0x13)
        if h.hex().startswith(target):
            return nonce.hex(), attempt + 1
    return None, limit


async def solve_brave_pow(challenge_json: Optional[str] = None,
                          url: Optional[str] = None,
                          timeout_s: float = 90) -> Dict[str, Any]:
    """Solve a Brave PoW challenge.

    challenge_json: the JSON body of GET /api-reference/captcha (if already fetched)
    url:            override the challenge URL (default: Brave's endpoint)
    Returns {solved, set_token, solutions, taken_time, tokens, attempts, answer} or {solved:False,error}
    """
    t0 = time.time()
    ch_url = (url or BRAVE_CAPTCHA_URL)

    # 1. fetch the challenge
    if challenge_json:
        ch = json.loads(challenge_json) if isinstance(challenge_json, str) else challenge_json
    else:
        import urllib.request
        req = urllib.request.Request(ch_url, headers={"User-Agent": _UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                ch = json.loads(r.read().decode())
        except Exception as exc:
            return {"solved": False, "error": f"challenge fetch failed: {exc}"}

    tokens = ch.get("tokens") or []
    if not tokens:
        return {"solved": False, "error": "challenge has no tokens"}
    zero = int(ch.get("zero_count", 0))
    params = ch.get("hash_function_params") or {}
    limit = int(ch.get("solution_limit", 5000))

    # 2. solve each token off the event loop (CPU-bound Argon2)
    loop = asyncio.get_event_loop()
    solutions: Dict[str, str] = {}
    attempts_total = 0
    deadline = t0 + timeout_s
    for tok in tokens:
        if time.time() > deadline:
            return {"solved": False, "error": "timeout before all tokens solved",
                    "solutions": solutions}
        nonce, att = await loop.run_in_executor(
            None, _solve_token_sync, tok, zero, params, limit)
        attempts_total += att
        if nonce is None:
            return {"solved": False, "error": f"no solution for token {tok} within {limit}"}
        solutions[tok] = nonce

    dt = int((time.time() - t0) * 1000)
    answer = json.dumps({"set_token": ch.get("set_token"),
                         "solutions": solutions, "taken_time": dt})
    log.info("brave_pow solved %d tokens in %d ms (%d attempts)", len(tokens), dt, attempts_total)
    return {"solved": True, "set_token": ch.get("set_token"),
            "solutions": solutions, "taken_time": dt,
            "tokens": len(tokens), "attempts": attempts_total,
            "answer": answer}
