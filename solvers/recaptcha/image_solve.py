"""Solve the reCAPTCHA v2 IMAGE challenge with a vision model.

The audio fallback is IP-blocked, but the image grid opens normally. We screenshot
the grid, slice it into tiles, ask the Mistral/ONNX key-pool yes/no per tile, click the
positives, and submit. Handles the three layouts:

  - 3x3 / 4x4 static : classify every tile once, click matches, verify.
  - dynamic (3x3)    : after a match is clicked the tile reloads a new image, so we
                       loop — classify, click new matches, wait for reload — until a
                       round finds nothing new (or a round cap), then verify.

Vision-solve is never 100% (reCAPTCHA images are deliberately ambiguous); success is
judged by reCAPTCHA accepting the submit (checkbox -> checked / token appears), not by
our own confidence. Caller retries on failure.
"""
import asyncio
import base64
import io
import json
import logging
import time
import re

from pathlib import Path

from PIL import Image

log = logging.getLogger(__name__)

_BFRAME = "/bframe"
_VERIFY = "#recaptcha-verify-button"
_CLASSIFY_CONCURRENCY = 6  # more parallel when Mistral keys available
_MAX_DYNAMIC_ROUNDS = 8


async def _find_bframe(page):
    for fr in page.frames:
        if _BFRAME in (fr.url or ""):
            return fr
    return None


async def _challenge_meta(bf) -> dict:
    return await bf.evaluate("""() => {
        const t = document.querySelector('table');
        const desc = document.querySelector(
            '.rc-imageselect-desc-no-canonical, .rc-imageselect-desc');
        const strong = desc?.querySelector('strong')?.innerText;
        const rows = t ? t.rows.length : 0;
        const cols = t && t.rows[0] ? t.rows[0].cells.length : 0;
        return {
            target: strong || (desc ? desc.innerText.split(String.fromCharCode(10))[0] : ''),
            rows, cols,
            dynamic: !!document.querySelector(
                '.rc-imageselect-dynamic-selected') ||
                /click verify once there are none/i.test(desc?.innerText || ''),
            has_table: !!t,
        };
    }""")


async def _fresh_meta(bf) -> dict:
    """Ambil target + saiz grid + signature dalam SATU evaluate.

    Bila tile reload, DOM berubah. Mengambil target berasingan daripada
    screenshot menyebabkan mismatch (target lama + gambar baru).
    """
    try:
        return await bf.evaluate("""() => {
            const t = document.querySelector('table');
            const desc = document.querySelector(
                '.rc-imageselect-desc-no-canonical, .rc-imageselect-desc');
            const strong = desc?.querySelector('strong')?.innerText || '';
            const rows = t ? t.rows.length : 0;
            const cols = t && t.rows[0] ? t.rows[0].cells.length : 0;
            const imgs = [];
            if (t) {
                for (const td of t.querySelectorAll('td[role=button], .rc-imageselect-tile')) {
                    const im = td.querySelector('img');
                    if (im) imgs.push((im.src || '').slice(-40));
                }
            }
            return {
                target: (strong || (desc ? desc.innerText.split(String.fromCharCode(10))[0] : '')).trim(),
                rows, cols,
                dynamic: !!document.querySelector('.rc-imageselect-dynamic-selected') ||
                    /click verify once there are none/i.test(desc?.innerText || ''),
                has_table: !!t,
                imgs: imgs,
                sig: imgs.join(',').slice(0, 400),
            };
        }""")
    except Exception as e:
        log.info("_fresh_meta gagal: %s", str(e).splitlines()[0][:60])
        return {}



# ---------------------------------------------------------------------------
# Screenshot grid yang tahan lasak (2026-10-26)
# ---------------------------------------------------------------------------

async def _shot_grid(bf, tries: int = 3, timeout: float = 25.0,
                     target: str = "", grid_n: int = 0):
    """Screenshot grid sebagai bytes PNG. Guna frame-shot + crop (lebih laju
    daripada element screenshot pada mesin tanpa GPU).

    Pulang bytes PNG atau None.
    """
    table = None
    try:
        table = await bf.query_selector("table")
    except Exception:
        pass

    box = None
    if table is not None:
        try:
            box = await table.bounding_box()
        except Exception:
            box = None

    for attempt in range(1, tries + 1):
        # 1) frame screenshot + crop (laju)
        if box:
            try:
                # bf ialah Frame - screenshot ada pada Page, bukan Frame
                _pg = getattr(bf, 'page', None)
                _shot = _pg.screenshot() if _pg is not None else bf.screenshot()
                full = await asyncio.wait_for(_shot, timeout=timeout)
                img = Image.open(io.BytesIO(full)).convert("RGB")
                x0 = max(0, int(box["x"]))
                y0 = max(0, int(box["y"]))
                x1 = min(img.width, int(box["x"] + box["width"]))
                y1 = min(img.height, int(box["y"] + box["height"]))
                if x1 - x0 > 40 and y1 - y0 > 40:
                    crop = img.crop((x0, y0, x1, y1))
                    buf = io.BytesIO()
                    crop.save(buf, "PNG")
                    # GRID-META-COLLECT: simpan grid unik (dedupe hash). Tiada await di sini.
                    try:
                        import os as _os, hashlib as _hl, json as _js
                        _d = "/home/ubuntu/shot/rc_grids"
                        _os.makedirs(_d, exist_ok=True)
                        _h = _hl.sha1(buf.getvalue()).hexdigest()[:12]
                        _idx = _os.path.join(_d, "index.json")
                        try:
                            _seen = _js.load(open(_idx))
                        except Exception:
                            _seen = []
                        if _h not in _seen and len(_seen) < 400:
                            _seen.append(_h)
                            _js.dump(_seen, open(_idx, "w"))
                            open(_os.path.join(_d, "g_%s.png" % _h), "wb").write(buf.getvalue())
                            if target:
                                _rec = _js.dumps({"h": _h, "target": target[:60],
                                                  "n": grid_n})
                                with open(_os.path.join(_d, "meta.jsonl"), "a") as _mf:
                                    _mf.write(_rec + chr(10))
                    except Exception as _e:
                        log.warning("GRID-META gagal: %s", str(_e).splitlines()[0][:90])
                    return buf.getvalue()
                log.info("crop terlalu kecil (%dx%d) - cuba element shot",
                         x1 - x0, y1 - y0)
            except Exception as e:
                log.info("frame+crop cuba %d gagal: %s", attempt,
                         str(e).splitlines()[0][:55])

        # 2) element screenshot
        if table is not None:
            try:
                return await asyncio.wait_for(table.screenshot(), timeout=timeout)
            except Exception as e:
                log.info("element shot cuba %d gagal: %s", attempt,
                         str(e).splitlines()[0][:55])

        await asyncio.sleep(0.6)

    log.warning("screenshot grid gagal selepas %d cubaan", tries)
    return None


async def _classify_grid(bf, keypool, target: str, n: int) -> list:
    """Screenshot the grid, slice into n*n tiles, classify each concurrently.

    Returns the list of tile indices (row-major) classified as containing target.
    """
    table = await bf.query_selector("table")
    if not table:
        return []
    try:
        await asyncio.wait_for(table.wait_for_element_state("stable", timeout=3000),
                               timeout=5)
    except Exception:
        pass
    grid_png = await _shot_grid(bf, target=target, grid_n=n)
    if not grid_png:
        return []
    img = Image.open(io.BytesIO(grid_png)).convert("RGB")
    W, H = img.size
    tw, th = W // n, H // n

    sem = asyncio.Semaphore(_CLASSIFY_CONCURRENCY)

    async def judge(idx, r, c):
        tile = img.crop((c * tw, r * th, (c + 1) * tw, (r + 1) * th))
        buf = io.BytesIO(); tile.save(buf, "PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        async with sem:
            yes = await asyncio.to_thread(keypool.classify, b64, target)
        return idx if yes else None

    tasks = [judge(r * n + c, r, c) for r in range(n) for c in range(n)]
    results = await asyncio.gather(*tasks)
    return [i for i in results if i is not None]


# ---------------------------------------------------------------------------
# Gemini grid classifier (2026-10-26): ONNX gagal untuk reCAPTCHA moden
# ---------------------------------------------------------------------------

_TRAIN = Path("/home/ubuntu/survey_automation/recaptcha_training.jsonl")


def _gemini_pool_rc():
    """KeyPool ke 9Router Gemini untuk reCAPTCHA."""
    try:
        from solvers.common.mistral import KeyPool
        return KeyPool(api_key="9router",
                       endpoint="http://127.0.0.1:20128/v1/chat/completions",
                       model="ag/gemini-3.8-flash")
    except Exception as e:
        log.warning("gemini pool rc gagal: %s", str(e)[:70])
        return None


def _grid_overlay_b64(img, n: int) -> str:
    """Tanda nombor pada setiap sel grid, pulang base64 PNG."""
    from PIL import ImageDraw
    w, h = img.size
    im = img.copy()
    draw = ImageDraw.Draw(im)
    cw, ch = w // n, h // n
    k = 0
    for r in range(n):
        for c in range(n):
            x0, y0 = c * cw, r * ch
            draw.rectangle([x0, y0, x0 + cw, y0 + ch], outline="red", width=3)
            tx, ty = x0 + 5, y0 + 5
            draw.rectangle([tx - 2, ty - 2, tx + 34, ty + 24], fill="yellow")
            draw.text((tx, ty), str(k), fill="black")
            k += 1
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


async def _classify_grid_gemini(bf, target: str, n: int) -> list:
    """Satu panggilan Gemini pada grid bernombor. Pulang index positif."""
    table = await bf.query_selector("table")
    if not table:
        return []
    png = await _shot_grid(bf, target=target, grid_n=n)
    if not png:
        return []
    img = Image.open(io.BytesIO(png)).convert("RGB")
    W, H = img.size
    if W < 60 or H < 60:
        log.warning("grid terlalu kecil: %dx%d", W, H)
        return []

    gridded = _grid_overlay_b64(img, n)
    N = n * n
    prompt = (
        f"The image has a {n}x{n} numbered grid (cells 0..{N - 1}, yellow label "
        f"top-left of each cell). Task: click every cell that contains a {target}.\n\n"
        f"RULES:\n"
        f"- Count a cell if the {target} is ENTIRELY in it.\n"
        f"- Count a cell if a {target} is PARTIALLY in it (cut at the edge) - "
        f"reCAPTCHA requires these too.\n"
        f"- Do NOT count empty cells, sky, road markings, or unrelated objects.\n\n"
        f"Reply ONLY the cell numbers separated by commas, e.g. 0, 4, 7. "
        f"If none: reply NONE"
    )

    gp = _gemini_pool_rc()
    txt = ""
    model = ""
    if gp is not None:
        try:
            txt = await asyncio.to_thread(gp.ask, gridded, prompt)
            model = "gemini"
        except Exception as e:
            log.warning("gemini classify gagal: %s", str(e).splitlines()[0][:60])

    if not (txt or "").strip():
        try:
            from solvers.common.mistral import KeyPool as _KP
            mp = _KP()
            txt = await asyncio.to_thread(mp.ask, gridded, prompt)
            model = "mistral"
        except Exception as e:
            log.warning("mistral classify gagal: %s", str(e).splitlines()[0][:60])

    if not (txt or "").strip():
        log.warning("tiada model jawab untuk reCAPTCHA grid")
        return []

    log.info("grid %s(%dx%d, %r) -> %r", model, n, n, target, (txt or "")[:110].replace("\n", " "))

    if "none" in (txt or "").lower()[:40]:
        return []

    picks = []
    for tok in re.findall(r"\d{1,2}", txt or ""):
        k = int(tok)
        if 0 <= k < N and k not in picks:
            picks.append(k)

    try:
        _TRAIN.parent.mkdir(parents=True, exist_ok=True)
        with _TRAIN.open("a") as fh:
            fh.write(json.dumps({"ts": time.time(), "n": n, "target": target[:40],
                                 "model": model, "picks": picks}) + "\n")
    except Exception:
        pass

    return picks


async def _classify_grid_twopass(bf, target: str, n: int) -> list:
    """Two-pass: senarai setiap sel, kemudian tapis terhadap target.

    PASS 1: "Senaraikan apa yang kau nampak dalam SETIAP sel 0..N-1."
    PASS 2: "Ini senarai. Sel mana mengandungi <target>? Kira separa juga."
    """
    table = await bf.query_selector("table")
    if not table:
        return []
    png = await _shot_grid(bf, target=target, grid_n=n)
    if not png:
        return []
    img = Image.open(io.BytesIO(png)).convert("RGB")
    W, H = img.size
    if W < 60 or H < 60:
        return []

    gridded = _grid_overlay_b64(img, n)
    N = n * n

    gp = _gemini_pool_rc()
    if gp is None:
        return []

    # ---- PASS 1: senarai lengkap ----
    p1 = (
        f"The image is a {n}x{n} grid numbered 0 to {N - 1} "
        f"(yellow label top-left of each cell).\n\n"
        f"For EVERY cell, describe what you see in 2-6 words. "
        f"Do not skip any cell. Use exactly this format, one per line:\n"
        f"0: <description>\n1: <description>\n...\n{N - 1}: <description>"
    )
    try:
        desc = await asyncio.to_thread(gp.ask, gridded, p1, 8, 90, 700)
    except Exception as e:
        log.warning("twopass pass1 gagal: %s", str(e).splitlines()[0][:60])
        return []
    desc = (desc or "").strip()
    if not desc:
        return []
    log.info("twopass pass1 (%dx%d) -> %s", n, n, desc[:160].replace("\n", " | "))

    # ---- PASS 2: tapis ----
    p2 = (
        f"A {n}x{n} grid was described cell by cell:\n\n{desc}\n\n"
        f"Task: click every cell containing a {target}.\n"
        f"Include cells where the {target} is partially visible, cut at the edge, "
        f"or cropped by the tile boundary - reCAPTCHA counts those.\n"
        f"Exclude cells that are only road, sky, background, or unrelated objects.\n\n"
        f"Reply ONLY the cell numbers separated by commas. If truly none, reply NONE."
    )
    try:
        ans = await asyncio.to_thread(gp.ask, gridded, p2, 8, 60, 200)
    except Exception as e:
        log.warning("twopass pass2 gagal: %s", str(e).splitlines()[0][:60])
        return []

    ans = (ans or "").strip()
    log.info("twopass pass2 (%r) -> %r", target, ans[:100].replace("\n", " "))

    if "none" in ans.lower()[:20]:
        return []

    picks = []
    for tok in re.findall(r"\d{1,2}", ans):
        k = int(tok)
        if 0 <= k < N and k not in picks:
            picks.append(k)

    try:
        _TRAIN.parent.mkdir(parents=True, exist_ok=True)
        with _TRAIN.open("a") as fh:
            fh.write(json.dumps({"ts": time.time(), "mode": "twopass", "n": n,
                                 "target": target[:40], "picks": picks}) + "\n")
    except Exception:
        pass

    return picks


async def _classify_grid_loose(bf, keypool, target: str, n: int) -> list:
    """Sama seperti _classify_grid tapi classifier dipanggil dengan prompt
    lebih permisif (penerangan ada 'maybe/partial' dikira positif).

    Dipakai sebagai fallback bila round pertama pulang kosong.
    """
    table = await bf.query_selector("table")
    if not table:
        return []
    try:
        grid_png = await asyncio.wait_for(table.screenshot(), timeout=10)
    except Exception:
        return []
    img = Image.open(io.BytesIO(grid_png)).convert("RGB")
    W, H = img.size
    tw, th = W // n, H // n
    log.info("classify loose: grid png %dx%d, tile %dx%d", W, H, tw, th)

    sem = asyncio.Semaphore(_CLASSIFY_CONCURRENCY)

    async def judge(idx, r, c):
        tile = img.crop((c * tw, r * th, (c + 1) * tw, (r + 1) * th))
        buf = io.BytesIO(); tile.save(buf, "PNG")
        b64 = base64.b64encode(buf.getvalue()).decode()
        prompt = (f"Does this small image tile contain part or all of a {target}? "
                  f"Even a partial, blurred or edge-of-frame {target} counts. "
                  f"Answer YES or NO only.")
        async with sem:
            try:
                txt = await asyncio.to_thread(keypool.ask, b64, prompt)
            except Exception:
                return None
        t = (txt or "").strip().lower()[:40]
        return idx if ("yes" in t and "no" not in t.split()[:1]) else None

    tasks = [judge(r * n + c, r, c) for r in range(n) for c in range(n)]
    results = await asyncio.gather(*tasks)
    return [i for i in results if i is not None]


async def _tiles(bf):
    return await bf.query_selector_all("td[role=button], .rc-imageselect-tile")


async def solve_image_challenge(page, keypool, max_rounds: int = _MAX_DYNAMIC_ROUNDS) -> bool:
    """Run the image challenge to a submit. Returns True if we pressed verify
    without an obvious error (caller confirms via token/checkbox)."""
    bf = await _find_bframe(page)
    if not bf:
        return False
    meta = await _challenge_meta(bf)
    if not meta.get("has_table") or not meta.get("target"):
        await asyncio.sleep(1.5)
        bf = await _find_bframe(page) or bf
        meta = await _challenge_meta(bf)
        if not meta.get("has_table") or not meta.get("target"):
            log.info("no image grid present"); return False
    n = meta["rows"]
    if not n:
        await asyncio.sleep(1.5)
        meta = await _challenge_meta(bf)
        n = meta.get("rows") or 0
        if not n:
            log.warning("grid rows unreadable — skipping to avoid mis-slice")
            return False
    target = meta["target"]
    dynamic = bool(meta["dynamic"])
    log.info("image challenge: target=%r grid=%dx%d dynamic=%s",
             target, n, meta["cols"], dynamic)

    rounds = max_rounds if dynamic else 2
    clicked_any = False
    empty_streak = 0
    last_sig = ""
    for rnd in range(rounds):
        # SINKRON: ambil target + grid serentak sebelum classify
        fm = await _fresh_meta(bf)
        if fm.get("target") and fm["target"] != target:
            log.info("round %d: target BERUBAH %r -> %r", rnd + 1, target, fm["target"])
            target = fm["target"]
        if fm.get("rows") and fm["rows"] != n:
            log.info("round %d: grid berubah %d -> %d", rnd + 1, n, fm["rows"])
            n = fm["rows"]
        sig = fm.get("sig") or ""
        if sig and sig == last_sig:
            log.info("round %d: grid TIDAK berubah (sig sama)", rnd + 1)
        last_sig = sig or last_sig

        positives = await _classify_grid_twopass(bf, target, n)
        if not positives:
            positives = await _classify_grid_gemini(bf, target, n)
        if not positives:
            positives = await _classify_grid(bf, keypool, target, n)
        if not positives and rnd == 0:
            # round pertama kosong: cuba semula + classifier longgar
            await asyncio.sleep(2.0)
            positives = await _classify_grid(bf, keypool, target, n)
        if not positives:
            # jalan terakhir: classifier permisif
            positives = await _classify_grid_loose(bf, keypool, target, n)
            if positives:
                log.info("loose fallback -> %s", positives)
        log.info("round %d/%d: %d/%d tiles match %r -> %s",
                 rnd + 1, rounds, len(positives), n * n, target, positives)
        if not positives:
            empty_streak += 1
            # dynamic: one empty round can be mid-reload — recheck once
            if dynamic and empty_streak <= 3 and clicked_any:
                await asyncio.sleep(2.5)
                bf = await _find_bframe(page) or bf
                positives = await _classify_grid_twopass(bf, target, n)
                if not positives:
                    positives = await _classify_grid_gemini(bf, target, n)
                if not positives:
                    positives = await _classify_grid(bf, keypool, target, n)
                if not positives:
                    positives = await _classify_grid_loose(bf, keypool, target, n)
                if positives:
                    empty_streak = 0
                    log.info("round %d recheck (streak %d): %s",
                             rnd + 1, empty_streak, positives)
                elif empty_streak >= 3:
                    log.info("round %d: 3 kosong berturut - berhenti", rnd + 1)
                    break
                else:
                    continue
            else:
                break
        tiles = await _tiles(bf)
        for idx in sorted(positives):
            if idx < len(tiles):
                try:
                    await tiles[idx].click(timeout=3000)
                    clicked_any = True
                    await asyncio.sleep(0.25 if dynamic else 0.12)
                except Exception as e:
                    log.debug("tile %d click: %s", idx, str(e).splitlines()[0])
        if dynamic:
            await asyncio.sleep(3.0)  # tile reload settle
            bf = await _find_bframe(page) or bf
            nm = await _fresh_meta(bf)
            if nm.get("target") and nm["target"] != target:
                log.info("selepas klik: target bertukar ke %r", nm["target"])
                target = nm["target"]
            continue
        if rnd == 0 and clicked_any:
            await asyncio.sleep(0.8)
            continue
        break

    if not clicked_any:
        log.info("no tiles matched %r", target)
        # still press verify — an all-correct 'none' answer is a valid solve
    for _vtry in range(3):
        try:
            await page.frame_locator(
                "iframe[title*='recaptcha challenge']").locator(
                _VERIFY).click(timeout=4000, force=True)
            break
        except Exception as e:
            log.warning("verify click attempt %d: %s", _vtry + 1, str(e).splitlines()[0])
            if _vtry == 2:
                try:
                    bf2 = await _find_bframe(page)
                    if bf2:
                        await bf2.evaluate(
                            "() => { const b=document.querySelector('"
                            "#recaptcha-verify-button'); if(b) b.click(); }")
                        log.info("verify click: JS fallback succeeded")
                        break
                except Exception as e2:
                    log.warning("verify JS fallback: %s", str(e2).splitlines()[0])
                return False
            await asyncio.sleep(1)
            bf = await _find_bframe(page) or bf
    await asyncio.sleep(2.5)
    return True
