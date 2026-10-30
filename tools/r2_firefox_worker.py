#!/usr/bin/env python3
"""reCAPTCHA v2 worker - runs on /usr/bin/python3 (3.10), where the real Camoufox
+ Playwright live. The solver (3.11 venv, no camoufox) spawns this and speaks
JSON over stdin/stdout.

Key behaviour: a challenge (bframe) means THIS exit is burned - close the browser
and relaunch with a fresh residential exit instead of waiting 2 minutes on a
session Google has already flagged. Repeat within the time budget.

stdin : {"url":..., "sitekey":..., "proxy":"scheme://user:pass@host:port"|null,
         "timeout_s":150, "attempts":4, "challenge_grace_s":45}
stdout: {"token":..., "method":"firefox-v2", "elapsed":..., "cookies":[...], "attempts":n}
        or {"token":"", "error":..., "elapsed":..., "attempts":n}
Logs go to stderr only.
"""
import asyncio
import json
import os
import random
import re
import sys
import time

from camoufox.addons import DefaultAddons
from camoufox.async_api import AsyncCamoufox

_RRESP_RE = re.compile(r'\["rresp",\s*"([^"]+)"')
PROXY_FILE = os.environ.get("RCF2_PROXY_FILE",
                            "/home/ubuntu/.secrets/residential_proxies.txt")


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, file=sys.stderr, flush=True)


def proxy_from_file():
    try:
        lines = [l.strip() for l in open(PROXY_FILE) if l.strip() and not l.startswith("#")]
    except Exception:
        return None
    if not lines:
        return None
    parts = random.choice(lines).split(":")
    if len(parts) == 4:
        h, p, u, pw = parts
        return {"server": f"http://{h}:{p}", "username": u, "password": pw}
    if len(parts) == 2:
        return {"server": f"http://{parts[0]}:{parts[1]}"}
    return None


def norm_proxy(proxy):
    if not proxy or proxy == "pool":
        return proxy_from_file()
    m = re.match(r"^(\w+)://(?:([^:@/]+):([^@/]+)@)?([^/]+?)(?::(\d+))?/?$", proxy.strip())
    if not m:
        return proxy_from_file()
    scheme, user, pw, host, port = m.groups()
    out = {"server": f"{scheme}://{host}" + (f":{port}" if port else "")}
    if user:
        out["username"] = user
        out["password"] = pw or ""
    return out


async def attempt(req, px, budget_s, challenge_grace_s):
    """One exit. Returns (token, cookies) or (None, reason)."""
    url = req["url"]
    nav_ms = int(req.get("nav_timeout_ms") or 90000)
    captured = {}
    kw = {"headless": "virtual", "exclude_addons": [DefaultAddons.UBO],
          "humanize": True, "geoip": os.environ.get("RCF2_GEOIP", "0") == "1",
          "os": ["windows"], "fonts": ["Arial"], "main_world_eval": True}
    if px:
        kw["proxy"] = px
    deadline = time.monotonic() + budget_s
    challenge_since = None

    async with AsyncCamoufox(**kw) as browser:
        page = await browser.new_page()
        page.set_default_timeout(45000)

        async def on_resp(r):
            u = r.url or ""
            if "recaptcha/api2/" not in u and "/recaptcha/enterprise/" not in u:
                return
            if not any(k in u for k in ("reload", "userverify")):
                return
            try:
                txt = await r.text()
            except Exception:
                return
            m = _RRESP_RE.search(txt)
            if m and m.group(1):
                captured["token"] = m.group(1)

        page.on("response", lambda r: asyncio.create_task(on_resp(r)))
        await page.goto(url, wait_until="domcontentloaded", timeout=nav_ms)
        await asyncio.sleep(4)

        anchor = None
        for _ in range(15):
            for f in page.frames:
                if "recaptcha/api2/anchor" in (f.url or ""):
                    anchor = f
                    break
            if anchor:
                break
            await asyncio.sleep(1)

        async def click_box():
            if not anchor:
                return
            try:
                box = await (await anchor.frame_element()).bounding_box()
                if box:
                    await page.mouse.move(box["x"] + 28, box["y"] + 30)
                    await asyncio.sleep(0.3)
                    await page.mouse.click(box["x"] + 28, box["y"] + 30)
            except Exception:
                pass

        await click_box()
        i = 0
        while time.monotonic() < deadline:
            await asyncio.sleep(3)
            i += 1
            bframe = any("recaptcha/api2/bframe" in (f.url or "") for f in page.frames)

            # 1) LIVE widget state first. getResponse() is authoritative and can
            #    return a token even while a (decoy) challenge frame is open - a
            #    page may run an invisible widget that passes silently.
            live = ""
            try:
                live = await page.evaluate(
                    "() => (window.grecaptcha && window.grecaptcha.getResponse)"
                    " ? (window.grecaptcha.getResponse(0) || window.grecaptcha.getResponse() || '') : ''")
            except Exception:
                live = ""
            if not live:
                try:
                    live = await page.evaluate(
                        "() => (document.querySelector('#g-recaptcha-response')?.value ||"
                        " document.querySelector('textarea[name=\"g-recaptcha-response\"]')?.value || '')")
                except Exception:
                    live = ""
            if live:
                try:
                    cookies = await page.context.cookies()
                except Exception:
                    cookies = []
                log("SOLVED (live) len=%d" % len(live))
                return (live, cookies), None

            # 2) intercepted rresp - only trustworthy with no challenge frame open
            if not bframe and captured.get("token"):
                try:
                    cookies = await page.context.cookies()
                except Exception:
                    cookies = []
                log("SOLVED (rresp) len=%d" % len(captured["token"]))
                return (captured["token"], cookies), None

            if bframe:
                if challenge_since is None:
                    challenge_since = time.monotonic()
                    log("challenge opened - giving it", challenge_grace_s, "s")
                elif time.monotonic() - challenge_since > challenge_grace_s:
                    return None, "challenged"
                continue
            challenge_since = None
            if i % 6 == 0:
                await click_box()
        return None, "timeout"


async def run(req):
    t0 = time.monotonic()
    total_budget = max(90.0, min(float(req.get("timeout_s") or 150), 300.0))
    attempts = int(req.get("attempts") or 4)
    grace = float(req.get("challenge_grace_s") or 45)
    per_attempt = max(80.0, total_budget / max(1, attempts))
    last = "no attempt"
    fixed = req.get("proxy")
    for n in range(1, attempts + 1):
        left = total_budget - (time.monotonic() - t0)
        if left < 30:
            break
        px = norm_proxy(fixed if (n == 1 or fixed) else None)
        log(f"attempt {n}/{attempts} proxy={px.get('server') if px else 'direct'} "
            f"budget={min(per_attempt, left):.0f}s")
        try:
            res, reason = await attempt(req, px, min(per_attempt, left), grace)
        except Exception as e:
            res, reason = None, str(e).splitlines()[0][:120]
        if res:
            tok, cookies = res
            log("SOLVED len=%d after %.1fs (attempt %d)" % (len(tok), time.monotonic() - t0, n))
            return {"token": tok, "verify_success": True, "cookies": cookies,
                    "method": "firefox-v2", "elapsed": round(time.monotonic() - t0, 1),
                    "attempts": n}
        last = reason or "no token"
        log(f"attempt {n} failed: {last}")
    return {"token": "", "error": f"firefox-v2: {last}",
            "elapsed": round(time.monotonic() - t0, 1), "attempts": attempts}


def main():
    raw = sys.stdin.read()
    try:
        req = json.loads(raw or "{}")
    except Exception as e:
        print(json.dumps({"token": "", "error": f"bad request json: {e}"}))
        return
    try:
        res = asyncio.run(run(req))
    except Exception as e:
        res = {"token": "", "error": f"firefox-v2: {str(e).splitlines()[0][:160]}",
               "elapsed": 0}
    sys.stdout.write(json.dumps(res))
    sys.stdout.flush()


if __name__ == "__main__":
    main()
