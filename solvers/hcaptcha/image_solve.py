"""Solve the hCaptcha IMAGE challenge with a vision model using numbered-grid classification.

hCaptcha renders images on a <canvas>. Instead of slicing the canvas into tiles and
classifying each tile individually (which fails on reasoning-style challenges like
"click the flower the bee never lands on"), we overlay a numbered grid on the full
canvas and ask the vision model: "which cell number?" This turns an expensive
grounding problem into a simple classification problem — the VLM chooses from N²
labels instead of guessing pixel coordinates, and it costs ONE API call per page
instead of 16.

Grid-overlay also enables drag challenges: ask for two cells (source, target) and
execute a programmatic drag via page.mouse.
"""
import asyncio
import base64
import hashlib
import json
import io
import logging
import random
import re
import time

from pathlib import Path

from PIL import Image, ImageDraw

log = logging.getLogger(__name__)

_GRID = 4
_SKIP_SEL = ".button-submit"


# Extracted so the challenge-detection JS is assertable offline. Keep it a NON-RAW
# string: the drag boundary must stay `\\b` (a bare \b is Python backspace 0x08) and the
# '\\n' delimiter must stay literal, else the regex and newline-split silently break.
_CHALLENGE_META_JS = """() => {
        const txt = document.body.innerText;
        const lines = txt.split('\\n').filter(l => l.trim());
        const task = lines.find(l =>
            !l.includes('try again') && !l.match(/^(Skip|Verify|Next|EN)$/) && l.length > 5
        ) || '';
        const c = document.querySelector('canvas');
        const r = c ? c.getBoundingClientRect() : null;
        const hasDrag = /^drag\\b/i.test(task) || /help the (creature|monkey|robot|character)/i.test(task);
        const btn = document.querySelector('.button-submit');
        return {
            target: task,
            canvasRect: r ? {x: r.x, y: r.y, w: c.width, h: c.height, cssW: r.width, cssH: r.height} : null,
            isDrag: hasDrag,
            buttonText: btn ? btn.innerText.trim() : '',
        };
    }"""


async def _challenge_meta(fr) -> dict:
    """Extract challenge task text + detect layout.

    Frame boleh navigated/destroyed antara panggilan - satu evaluate gagal
    tidak boleh membunuh seluruh solve.
    """
    try:
        return await fr.evaluate(_CHALLENGE_META_JS)
    except Exception as e:
        msg = str(e).splitlines()[0][:70]
        if "destroyed" in msg.lower() or "closed" in msg.lower():
            log.info("_challenge_meta: frame mati (%s)", msg)
        else:
            log.warning("_challenge_meta gagal: %s", msg)
        return {}


async def _wait_challenge_ready(page, tries: int = 12, delay: float = 1.0):
    """Tunggu frame bercanvas sedia. hCaptcha render cabaran baru selepas
    cabaran lepas selesai - frame lama masih wujud tapi kosong.

    Pulang (frame, meta) atau (None, {}).
    """
    for i in range(1, tries + 1):
        try:
            fr = await _find_challenge_frame_async(page)
        except Exception:
            fr = None
        if fr is not None:
            meta = await _challenge_meta(fr)
            if meta.get("canvasRect") and (meta.get("target") or "").strip():
                if i > 1:
                    log.info("cabaran baru sedia selepas %ds", i)
                return fr, meta
        await asyncio.sleep(delay)
    return None, {}


async def _get_canvas_b64(fr) -> str:
    """Get the full canvas as a base64-encoded PNG data URL."""
    return await fr.evaluate("""() => {
        const c = document.querySelector('canvas');
        if (!c) return '';
        return c.toDataURL('image/png').split(',')[1];
    }""")


def _grid_overlay(b64: str, grid: int = _GRID) -> str:
    """Overlay a numbered grid on a base64 PNG, return base64 PNG.

    Each cell is labelled with its index (0..grid²-1) in a yellow box.
    Grid lines in red. Turns pixel-grounding into cell-classification.
    """
    img = Image.open(io.BytesIO(base64.b64decode(b64))).convert("RGB")
    w, h = img.size
    cw, ch = w // grid, h // grid
    draw = ImageDraw.Draw(img)
    n = 0
    for r in range(grid):
        for c in range(grid):
            x0, y0 = c * cw, r * ch
            draw.rectangle([x0, y0, x0 + cw, y0 + ch], outline="red", width=3)
            tx, ty = x0 + 6, y0 + 6
            draw.rectangle([tx - 2, ty - 2, tx + 38, ty + 26], fill="yellow")
            draw.text((tx, ty), str(n), fill="black")
            n += 1
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


async def _pick_cells(fr, keypool, target: str, grid: int = _GRID) -> list[int]:
    """Ask the vision model which numbered-grid cells satisfy *target*.

    ONE model call per page. The numbered grid turns grounding into
    classification — VLMs handle this far more accurately than pixel
    coordinates. Returns deduplicated cell indices or [].
    """
    b64 = await _get_canvas_b64(fr)
    if not b64:
        return []
    gridded = _grid_overlay(b64, grid)
    N = grid * grid
    prompt = (
        f"This image has a {grid}x{grid} numbered grid (0..{N - 1}, yellow label "
        f"top-left of each cell). Task: \"{target}\". "
        f"Reply ONLY the cell number(s) that satisfy the task, comma-separated "
        f"(e.g. `3` or `1,4,9`), or `none`."
    )
    text = await asyncio.to_thread(keypool.ask, gridded, prompt)
    log.info("_pick_cells(%r) -> %s", target, text[:120])
    nums = []
    for m in re.finditer(r"\d+", text):
        v = int(m.group())
        if 0 <= v < N:
            nums.append(v)
    seen = set()
    return [x for x in nums if not (x in seen or seen.add(x))]




async def _pick_cells_two_pass(fr, keypool, target: str, grid: int = _GRID) -> list[int]:
    """Dua-pass: terangkan sel dahulu, kemudian pilih berdasarkan keterangan.

    Model kecil tersilap bila terus diminta menjawab tugasan perbandingan.
    Bila diminta menerangkan dahulu, ia melihat setiap sel dengan teliti,
    dan pilihan kedua lebih tepat.
    """
    b64 = await _get_canvas_b64(fr)
    if not b64:
        return []
    gridded = _grid_overlay(b64, grid)
    N = grid * grid

    # PASS 1: keterangan setiap sel
    p1 = (
        f"This is a {grid}x{grid} numbered grid (cells 0..{N - 1}, yellow number "
        f"in the top-left of each cell). For EVERY cell, write one short line: "
        f"`<number>: <what you see>`. Be literal and specific about size, shape, "
        f"colour and position. Do not skip any cell."
    )
    desc = await asyncio.to_thread(keypool.ask, gridded, p1, 8, 60, 700)
    log.info("two-pass PASS1 -> %r", (desc or "")[:180])

    if not desc or len(desc) < 40:
        log.warning("two-pass: PASS1 gagal, jatuh ke satu-pass")
        return await _pick_cells(fr, keypool, target, grid)

    # PASS 2: pilih berdasarkan keterangan
    p2 = (
        f"A {grid}x{grid} numbered grid was described cell by cell as follows:\n"
        f"{desc[:2500]}\n\n"
        f"Task: \"{target}\"\n\n"
        f"Using ONLY the descriptions above, reply with the cell number(s) that "
        f"satisfy the task, comma-separated (e.g. `3` or `1,4,9`), or `none`. "
        f"Reply with numbers only."
    )
    text = await asyncio.to_thread(keypool.ask, gridded, p2, 8, 45, 120)
    log.info("two-pass PASS2(%r) -> %r", target[:40], (text or "")[:120])

    nums = []
    for m in re.finditer(r"\d+", text or ""):
        v = int(m.group())
        if 0 <= v < N and v not in nums:
            nums.append(v)

    if not nums:
        log.warning("two-pass: PASS2 tiada nombor, jatuh ke satu-pass")
        return await _pick_cells(fr, keypool, target, grid)
    return nums


async def _classify_tiles(fr, keypool, target: str) -> list[int]:
    """DEPRECATED — use _pick_cells instead."""
    return await _pick_cells(fr, keypool, target)


async def _click_tiles(page, fr, indices: list[int]) -> None:
    """Click matching tile positions on the canvas via page.mouse.

    page.mouse.click dispatches real OS-level events that hCaptcha respects.
    """
    cbox = await fr.locator("canvas").bounding_box()
    if not cbox:
        return
    tw = cbox["width"] / _GRID
    th = cbox["height"] / _GRID
    for idx in indices:
        col = idx % _GRID
        row = idx // _GRID
        x = cbox["x"] + (col + 0.5) * tw
        y = cbox["y"] + (row + 0.5) * th
        try:
            await page.mouse.click(x, y)
            await asyncio.sleep(0.3)
        except Exception as e:
            log.debug("tile %d click: %s", idx, str(e).splitlines()[0])


def _cell_center(cbox: dict, idx: int, grid: int = _GRID) -> tuple[float, float]:
    """Return (x, y) center pixel of cell `idx` within the canvas bounding box."""
    tw = cbox["width"] / grid
    th = cbox["height"] / grid
    col = idx % grid
    row = idx // grid
    return (cbox["x"] + (col + 0.5) * tw, cbox["y"] + (row + 0.5) * th)


async def _solve_drag(fr, page, keypool) -> bool:
    """Solve a drag challenge via ensemble: Gemini + Mistral, try each candidate.

    A single model guessing wrong was the whole failure. hCaptcha is the only
    judge: drag candidate A, check whether the challenge advanced, move to
    candidate B if not. Every attempt is logged as a training sample.
    """
    b64 = await _get_canvas_b64(fr)
    if not b64:
        return False
    cbox = await fr.locator("canvas").bounding_box()
    if not cbox:
        return False

    gridded = _grid_overlay(b64, _GRID)
    N = _GRID * _GRID

    prompt = (
        f"The image has a {_GRID}x{_GRID} numbered grid (cells 0..{N - 1}, yellow "
        f"label top-left of each cell). One cell holds a movable icon, one cell is "
        f"an empty gap in a circular arrangement. "
        f"Reply exactly: SOURCE=<cell with the icon> TARGET=<cell of the gap>. "
        f"Example: SOURCE=8 TARGET=10"
    )

    candidates = []

    # GEMINI FIRST - 9/9 accepted in the 2026-10-24 sample vs Mistral 0/9.
    gp = _gemini_pool()
    if gp is not None:
        try:
            txt = await asyncio.to_thread(gp.ask, gridded, prompt)
            parsed = _parse_src_tgt(txt or "")
            if parsed:
                candidates.append((parsed[0], parsed[1], "gemini"))
                log.info("drag gemini -> %s", parsed)
        except Exception as e:
            log.warning("drag gemini gagal: %s", str(e).splitlines()[0][:60])

    try:
        txt = await asyncio.to_thread(keypool.ask, gridded, prompt)
        parsed = _parse_src_tgt(txt or "")
        if parsed:
            candidates.append((parsed[0], parsed[1], "mistral"))
            log.info("drag mistral -> %s", parsed)
    except Exception as e:
        log.warning("drag mistral gagal: %s", str(e).splitlines()[0][:60])

    if not candidates:
        log.warning("drag: tiada cadangan")
        return False

    seen = set()
    uniq = []
    for s, t, m in candidates:
        if (s, t) in seen:
            continue
        seen.add((s, t))
        uniq.append((s, t, m))

    task = ""
    try:
        meta = await _challenge_meta(fr)
        task = (meta.get("target") or "")[:80]
    except Exception:
        pass

    await _human_pause(1.2, 2.8, "sebelum drag")

    for (src_cell, tgt_cell, model) in uniq:
        if not (0 <= src_cell < N and 0 <= tgt_cell < N):
            continue
        sx, sy = _cell_center(cbox, src_cell)
        tx, ty = _cell_center(cbox, tgt_cell)
        try:
            await page.mouse.move(sx, sy)
            await asyncio.sleep(0.2)
            await page.mouse.down()
            for i in range(1, 11):
                await page.mouse.move(sx + (tx - sx) * i / 10,
                                      sy + (ty - sy) * i / 10)
                await asyncio.sleep(0.05)
            await page.mouse.up()
            await asyncio.sleep(2.5)
        except Exception as e:
            log.warning("drag %s->%s gagal: %s", src_cell, tgt_cell,
                        str(e).splitlines()[0][:50])
            continue

        try:
            still = await fr.evaluate("""() => {
                const c = document.querySelector('canvas');
                if (!c) return false;
                const btn = document.querySelector('.button-submit');
                const t = btn ? btn.innerText.trim().toLowerCase() : '';
                return t !== 'verify';
            }""")
        except Exception:
            still = True

        accepted = not still
        log.info("drag %s: %d -> %d accepted=%s", model, src_cell, tgt_cell, accepted)
        _save_training(task, src_cell, tgt_cell, model, accepted)

        if accepted:
            return True

    return False

# ---------------------------------------------------------------------------
# DRAG ensemble (2026-10-24): Gemini + Mistral, cuba semua cadangan
# ---------------------------------------------------------------------------

_TRAIN_LOG = Path("/home/ubuntu/survey_automation/drag_training.jsonl")


def _gemini_pool():
    """KeyPool ke 9Router Gemini."""
    try:
        from solvers.common.mistral import KeyPool
        return KeyPool(api_key="9router",
                       endpoint="http://127.0.0.1:20128/v1/chat/completions",
                       model="ag/gemini-3.8-flash")
    except Exception as e:
        log.warning("gemini pool gagal: %s", str(e)[:80])
        return None


def _parse_src_tgt(text: str):
    """Parse SOURCE=<n> TARGET=<m> atau dua nombor."""
    if not text:
        return None
    m = re.search(r"source\D{0,8}(\d+).{0,40}?target\D{0,8}(\d+)", text, re.I | re.S)
    if m:
        return int(m.group(1)), int(m.group(2))
    nums = [int(x) for x in re.findall(r"\b(\d{1,2})\b", text) if int(x) < 16]
    if len(nums) >= 2:
        return nums[0], nums[1]
    return None


def _save_training(target: str, src: int, tgt: int, model: str,
                   accepted: bool) -> None:
    try:
        _TRAIN_LOG.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.time(), "task": (target or "")[:80],
               "src": src, "tgt": tgt, "model": model, "accepted": accepted}
        with _TRAIN_LOG.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Multi-type hCaptcha classifier + handlers (dipulihkan 2026-10-24)
# ---------------------------------------------------------------------------

_COUNT_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

_DRAG_RX = re.compile(
    r"\bdrag\b|help the (creature|monkey|robot|character|animal)", re.I)

_COUNT_RX = re.compile(
    r"\b(?:click|select|choose|pick|tap|find|mark)\b[^.!?]{0,40}?"
    r"\b(\d+|one|two|three|four|five|six|seven|eight|nine|ten)\b",
    re.I)


def _requested_count(target: str) -> int:
    """Berapa item diarahkan. Hanya nombor SELEPAS kata arahan dikira."""
    if not target:
        return 0
    m = _COUNT_RX.search(target)
    if not m:
        return 0
    raw = m.group(1).lower()
    if raw.isdigit():
        return int(raw)
    return _COUNT_WORDS.get(raw, 0)


def _classify_challenge_kind(target: str, is_drag: bool = False) -> str:
    """GROUP | DRAG | POINT | MULTI | GRID."""
    t = (target or "").lower()
    if is_drag or _DRAG_RX.search(t):
        return "DRAG"
    if "group" in t:
        return "GROUP"
    if re.search(r"\bpoints? where the line breaks?\b|\bclick (on )?the points?\b", t):
        return "POINT"
    # MULTI if counted: "click all", "select all", atau bilangan jelas +
    # kata klik/pilih ("Click the THREE matching shapes")
    if re.search(r"\bclick (on )?all\b|\bselect (all|the)\b|\bselect all\b", t):
        return "MULTI"
    if (_requested_count(t) >= 1
            and re.search(r"\b(click|select|choose|pick|tap|mark)\b", t)
            and not re.search(r"\bgroup\b", t)):
        return "MULTI"
    return "GRID"


async def _solve_point_click(fr, page, keypool, target: str) -> bool:
    """Klik titik tepat - model bagi koordinat ternormal 0..1."""
    b64 = await _get_canvas_b64(fr)
    if not b64:
        return False
    cbox = await fr.locator("canvas").bounding_box()
    if not cbox:
        return False
    want = _requested_count(target) or 2
    prompt = (
        f"hCaptcha puzzle. Instruction: \"{target}\"\n\n"
        f"Reply with the exact points to click as normalized coordinates "
        f"(x,y) where 0,0 is the TOP-LEFT and 1,1 is the BOTTOM-RIGHT. "
        f"Give {want} point(s), one per line, e.g.\n0.42,0.31\n0.77,0.68"
    )
    text = await asyncio.to_thread(keypool.ask, b64, prompt)
    log.info("_solve_point_click(%r) -> %r", target[:40], (text or "")[:180])

    pts = re.findall(r"(\d?\.\d+|\d+)\s*[,;]\s*(\d?\.\d+|\d+)", text or "")
    clicks = []
    for a, b in pts:
        try:
            x, y = float(a), float(b)
        except ValueError:
            continue
        if x > 1.0 or y > 1.0:
            x, y = x / 100.0, y / 100.0
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            continue
        clicks.append((x, y))
    if not clicks:
        log.warning("point: tiada koordinat")
        return False
    clicks = clicks[:want]

    for (x, y) in clicks:
        px = cbox["x"] + x * cbox["width"]
        py = cbox["y"] + y * cbox["height"]
        try:
            await page.mouse.click(px, py)
            await asyncio.sleep(0.4)
        except Exception as e:
            log.warning("point click gagal: %s", str(e).splitlines()[0])
    log.info("point: klik %d titik %s", len(clicks), clicks)
    return True


async def _solve_group_click(fr, page, keypool, target: str) -> bool:
    """Klik kumpulan: model bagi layout + posisi kumpulan."""
    b64 = await _get_canvas_b64(fr)
    if not b64:
        return False
    cbox = await fr.locator("canvas").bounding_box()
    if not cbox:
        return False
    want = _requested_count(target) or 2
    prompt = (
        f"hCaptcha puzzle. Instruction: \"{target}\"\n\n"
        f"The image is divided into GROUPS (not individual tiles). Reply with:\n"
        f"1) rows of groups\n2) columns of groups\n"
        f"Example for 2x2: `2,2`\n"
        f"Then on a new line list the group positions that satisfy the task as "
        f"row,col counted from 1 at the top-left, e.g. `1,1 2,2`."
    )
    text = await asyncio.to_thread(keypool.ask, b64, prompt)
    log.info("_solve_group_click(%r) -> %r", target[:40], (text or "")[:200])

    lay = re.search(r"(\d+)\s*[,x]\s*(\d+)", text or "")
    if not lay:
        log.warning("group: layout tak dapat diparse")
        return False
    rows, cols = int(lay.group(1)), int(lay.group(2))
    if not (1 <= rows <= 6 and 1 <= cols <= 6):
        log.warning("group: layout %dx%d tak munasabah", rows, cols)
        return False

    tail = (text or "")[lay.end():]
    pairs = re.findall(r"(\d)\s*[,:]\s*(\d)", tail)
    picks = []
    for a, b in pairs:
        r, c = int(a), int(b)
        if 1 <= r <= rows and 1 <= c <= cols and (r, c) not in picks:
            picks.append((r, c))
    if not picks:
        log.warning("group: tiada posisi")
        return False
    picks = picks[:want]

    cw = cbox["width"] / cols
    ch = cbox["height"] / rows
    for (r, c) in picks:
        x = cbox["x"] + cw * (c - 0.5)
        y = cbox["y"] + ch * (r - 0.5)
        try:
            await page.mouse.click(x, y)
            await asyncio.sleep(0.35)
        except Exception as e:
            log.warning("group click gagal: %s", str(e).splitlines()[0])
    log.info("group: layout %dx%d klik %s", rows, cols, picks)
    return True


# ---------------------------------------------------------------------------
# Logging menyeluruh (2026-10-24): rekod SETIAP jenis cabaran + outcome
# ---------------------------------------------------------------------------

_OUTCOME_LOG = Path("/home/ubuntu/survey_automation/hcaptcha_outcomes.jsonl")
_kind_streak = {"kind": None, "count": 0, "solved": 0}


def _log_outcome(kind: str, target: str, solved: bool, model: str = "",
                 detail: str = "") -> None:
    """Satu baris jsonl per cabaran. Kesan jenis mana yang menyekat."""
    try:
        _OUTCOME_LOG.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.time(), "kind": kind, "target": (target or "")[:90],
               "solved": bool(solved), "model": model, "detail": detail[:120]}
        with _OUTCOME_LOG.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
    except Exception:
        pass

    if _kind_streak["kind"] == kind:
        _kind_streak["count"] += 1
        if solved:
            _kind_streak["solved"] += 1
    else:
        _kind_streak["kind"] = kind
        _kind_streak["count"] = 1
        _kind_streak["solved"] = 1 if solved else 0

    if _kind_streak["count"] >= 4 and _kind_streak["solved"] == 0:
        log.warning("STALL: %s gagal %d kali berturut - jenis ini penyekat",
                    kind, _kind_streak["count"])
    else:
        log.info("outcome kind=%s solved=%s model=%s", kind, solved, model or "-")


# ---------------------------------------------------------------------------
# Signature guard (2026-10-24): frame lama mesti DIKESAN dan ditolak
# ---------------------------------------------------------------------------

async def _challenge_signature(fr) -> str:
    """Hash ringkas cabaran sekarang: task + saiz canvas + hash data-URL."""
    try:
        meta = await _challenge_meta(fr)
        task = (meta.get("target") or "").strip()
        rect = meta.get("canvasRect") or {}
        sig_parts = [task,
                     str(rect.get("width")), str(rect.get("height"))]
        data = await fr.evaluate("""() => {
            const c = document.querySelector('canvas');
            if (!c) return '';
            try { return c.toDataURL('image/png').slice(-160); } catch (e) { return ''; }
        }""")
        sig_parts.append(str(data or ""))
        return hashlib.sha1("|".join(sig_parts).encode()).hexdigest()[:16]
    except Exception:
        return ""


async def _wait_challenge_change(page, old_sig: str, tries: int = 12,
                                 delay: float = 1.0):
    """Tunggu cabaran BERUBAH dari old_sig. Tolak frame lama.

    Pulang (frame, meta, sig) atau (None, {}, "").
    """
    if not old_sig:
        return await _wait_challenge_ready(page, tries, delay)
    for i in range(1, tries + 1):
        try:
            fr = await _find_challenge_frame_async(page)
        except Exception:
            fr = None
        if fr is not None:
            sig = await _challenge_signature(fr)
            if sig and sig != old_sig:
                meta = await _challenge_meta(fr)
                if (meta.get("target") or "").strip() or meta.get("canvasRect"):
                    log.info("cabaran baru dikesan selepas %ds (sig %s -> %s)",
                             i, old_sig[:8], sig[:8])
                    return fr, meta, sig
        await asyncio.sleep(delay)
    log.warning("cabaran TIDAK berubah selepas %ds (sig kekal %s)",
                tries, old_sig[:8])
    return None, {}, ""

# ---------------------------------------------------------------------------
# Human pacing (2026-10-24): hCaptcha berhenti bagi cabaran bila solve
# terlalu cepat. Perlahankan rentak antara cabaran.
# ---------------------------------------------------------------------------

_cabaran_count = {"n": 0}


async def _human_pause(lo: float, hi: float, label: str = "") -> float:
    """Tidur rawak lo..hi saat. Rekod dalam log bila perlu."""
    wait = random.uniform(lo, hi)
    if label:
        log.info("pause %s: %.1fs", label, wait)
    await asyncio.sleep(wait)
    return wait


async def _pacing_between_challenges() -> None:
    """Jeda manusia antara cabaran. Setiap 3 cabaran, jeda lebih panjang."""
    _cabaran_count["n"] += 1
    n = _cabaran_count["n"]
    if n % 3 == 0:
        await _human_pause(12.0, 22.0, "berfikir (setiap 3)")
    else:
        await _human_pause(3.0, 8.0, "antara cabaran")


# ---------------------------------------------------------------------------
# MULTI handler + grid dinamik (2026-10-24)
# ---------------------------------------------------------------------------

def _detect_grid_size(cbox: dict) -> int:
    """Teka grid dari aspect/nisbah canvas. hCaptcha guna 3 atau 4 biasanya."""
    try:
        w, h = float(cbox.get("width") or 0), float(cbox.get("height") or 0)
    except Exception:
        return _GRID
    if w <= 0 or h <= 0:
        return _GRID
    side = min(w, h)
    # hCaptcha CSS: 3x3 ~300px, 4x4 ~500px (hCaptcha tak guna 5x5)
    if side <= 380:
        return 3
    return 4


async def _solve_multi_click(fr, page, keypool, target: str,
                             cbox: dict = None, grid: int = 0) -> bool:
    """Klik N sel yang padan. count dari _requested_count(target)."""
    b64 = await _get_canvas_b64(fr)
    if not b64:
        return False
    if cbox is None:
        cbox = await fr.locator("canvas").bounding_box()
    if not cbox:
        return False
    if not grid:
        grid = _detect_grid_size(cbox)

    want = _requested_count(target)
    if not want:
        want = 3  # 'matching shapes' lazimnya 3
    N = grid * grid

    gridded = _grid_overlay(b64, grid)
    prompt = (
        f"The image has a {grid}x{grid} numbered grid (cells 0..{N - 1}, yellow "
        f"label top-left of each cell). Task: \"{target}\"\n\n"
        f"Pick exactly {want} cell number(s) that satisfy the task. "
        f"Reply ONLY the numbers separated by commas, most confident first. "
        f"Example: 5, 9, 14"
    )
    text = await asyncio.to_thread(keypool.ask, gridded, prompt)
    log.info("_solve_multi_click(%r) grid=%dx%d want=%d -> %r",
             target[:45], grid, grid, want, (text or "")[:120])

    nums = []
    for tok in re.findall(r"\d{1,2}", text or ""):
        n = int(tok)
        if 0 <= n < N and n not in nums:
            nums.append(n)
    if not nums:
        log.warning("multi: tiada nombor sah (grid %dx%d)", grid, grid)
        return False

    picks = nums[:want]
    log.info("multi: grid %dx%d klik %s", grid, grid, picks)
    for idx in picks:
        x, y = _cell_center(cbox, idx, grid)
        try:
            await page.mouse.click(x, y)
            await asyncio.sleep(random.uniform(0.25, 0.55))
        except Exception as e:
            log.warning("multi click %d gagal: %s", idx, str(e).splitlines()[0])
    return True


async def _detect_a11y_text(fr) -> bool:
    """Detect a11y free-text-entry challenge: has text input + canvas."""
    try:
        return await fr.evaluate("""() => {
            const inp = document.querySelector('input[type="text"], textarea, input.text, input.task-text');
            const cv = document.querySelector('canvas');
            return !!(inp && cv);
        }""")
    except Exception:
        return False


async def _solve_a11y_text(fr, page, keypool) -> bool:
    """Solve a11y free-text-entry: OCR distorted text from canvas, type into input."""
    b64 = await _get_canvas_b64(fr)
    if not b64:
        log.warning("a11y: no canvas b64")
        return False
    prompt = (
        "This image contains distorted text characters (CAPTCHA). "
        "Read the text and reply ONLY with the characters shown. "
        "No spaces, no punctuation, just the raw characters."
    )
    text = await asyncio.to_thread(keypool.ask, b64, prompt)
    text = text.strip().replace(" ", "").replace("\n", "")
    log.info("a11y OCR -> %r", text)
    if not text:
        return False
    # type into the text input
    try:
        inp = await fr.query_selector(
            'input[type="text"], textarea, input.text, input.task-text')
        if not inp:
            log.warning("a11y: text input not found")
            return False
        await inp.fill("")
        await inp.type(text, delay=80)
    except Exception as e:
        log.warning("a11y type failed: %s", str(e).splitlines()[0])
        return False
    await asyncio.sleep(0.5)
    return await _click_submit(fr)



async def _click_submit(fr) -> bool:
    """Click the submit button (Skip/Next/Verify). Returns True if clicked."""
    try:
        btn = await fr.query_selector(_SKIP_SEL)
        if btn:
            text = await btn.inner_text()
            log.info("submit button: %r", text)
            await btn.click(timeout=5000)
            await asyncio.sleep(2)
            return True
    except Exception as e:
        log.warning("submit click: %s", str(e).splitlines()[0])
    return False


async def solve_hcaptcha_challenge(fr, page, keypool, max_pages: int = 5) -> bool:
    """Run the hCaptcha image challenge through all pages to completion.

    Returns True if Verify was pressed (caller checks for token).
    """
    fr, meta = await _wait_challenge_ready(page)
    if fr is None:
        log.info("no challenge canvas found (selepas 12s)")
        _log_outcome("NOCANVAS", "", False, detail="frame bercanvas tak muncul")
        return False
    # a11y free-text-entry: text input + distorted image (hCaptcha Enterprise)
    if await _detect_a11y_text(fr):
        log.info("a11y free-text-entry challenge detected")
        return await _solve_a11y_text(fr, page, keypool)
    target = meta.get("target") or ""
    kind = _classify_challenge_kind(target, bool(meta.get("isDrag")))
    log.info("challenge kind=%s target=%r btn=%r", kind, target[:60], meta.get("buttonText"))

    if kind == "DRAG":
        log.warning("drag challenge — best-effort via grid")
        solved = await _solve_drag(fr, page, keypool)
        _log_outcome("DRAG", target, solved,
                     model="gemini-first" if solved else "none",
                     detail="no submit" if not solved else "")
        if solved:
            await asyncio.sleep(1.5)
            await _click_submit(fr)  # Click Verify after drag succeeds
        else:
            await _click_submit(fr)  # Skip if drag fails
        return solved

    if kind == "POINT":
        done = await _solve_point_click(fr, page, keypool, target)
        _log_outcome("POINT", target, done, detail="point handler")
        if done:
            await asyncio.sleep(1.5)
            await _click_submit(fr)
            return True
        log.warning("point solve gagal, cuba grid")

    if kind == "GROUP":
        clicked = await _solve_group_click(fr, page, keypool, target)
        _log_outcome("GROUP", target, clicked, detail="group handler")
        if clicked:
            await _human_pause(1.8, 3.8, "sebelum submit group")
            sm = await _challenge_meta(fr)
            log.info("group done -> btn=%r", sm.get("buttonText"))
            await _click_submit(fr)
            return True
        # jatuh ke grid kalau group gagal
        log.warning("group solve gagal, cuba grid")

    if kind == "MULTI":
        cbox = meta.get("canvasRect")
        clicked = await _solve_multi_click(fr, page, keypool, target, cbox)
        _log_outcome("MULTI", target, clicked, model="multi-handler",
                     detail="want=%d" % _requested_count(target))
        if clicked:
            await _human_pause(1.5, 3.2, "sebelum submit multi")
            await _click_submit(fr)
            return True
        log.warning("multi solve gagal, cuba grid")

    if not target:
        log.info("no challenge target found")
        _log_outcome("NONE", "", False, detail="tiada target teks")
        return False

    prev_sig = await _challenge_signature(fr)

    for page_num in range(1, max_pages + 1):
        meta = await _challenge_meta(fr)
        target = meta.get("target", "")
        btn_text = meta.get("buttonText", "")

        log.info("page %d/%d: target=%r btn=%r", page_num, max_pages, target, btn_text)

        if btn_text.lower() in ("verify", "verifizieren", "verificar", "vahvista"):
            break  # already on verify page, just click

        # Jeda manusia dulu - hCaptcha flag solve terlalu cepat
        await _pacing_between_challenges()

        # Frame boleh jadi basi antara cabaran - tunggu yang BERCANVAS dan BERUBAH
        fr, _m, cur_sig = await _wait_challenge_change(page, prev_sig,
                                                       tries=10, delay=0.9)
        if fr is None:
            log.warning("cabaran tak berubah / frame kosong - tamat")
            _log_outcome("STALE", target, False, detail="frame tak berubah")
            return False
        prev_sig = cur_sig

        # Classify tiles and click matches
        if target:
            g = _detect_grid_size(meta.get("canvasRect") or {})
            positives = await _pick_cells_two_pass(fr, keypool, target, g)
            # hormati kiraan yang diminta ("click the THREE ..." -> max 3 sel)
            want = _requested_count(target)
            if want and len(positives) > want:
                log.info("grid pick %s -> hadkan ke %d", positives, want)
                positives = positives[:want]
            log.info("grid pick -> %s", positives)
            _log_outcome("GRID", target, bool(positives),
                         model="two-pass",
                         detail="picked %s" % (positives,))
            await _click_tiles(page, fr, positives)
            await _human_pause(1.5, 3.5, "sebelum submit")
        else:
            log.info("no target text on this page — clicking first tiles as guess")
            await _click_tiles(page, fr, list(range(_GRID)))

        await asyncio.sleep(2)

        # Click Next/Verify to advance
        clicked = await _click_submit(fr)
        if not clicked:
            return False
        # If positives were found, button advanced to Next/Verify (not skip)
        if not positives:
            return False  # no matches found, skipped
        await asyncio.sleep(2)

    # After last page, check for "Verify" button
    fr = await _find_challenge_frame_async(page)
    if fr:
        meta = await _challenge_meta(fr)
        log.info("final state: btn=%r", meta.get("buttonText"))
        if meta.get("buttonText", "").lower() != "skip":
            await _click_submit(fr)

    return True


async def _find_challenge_frame_async(page):
    """Return the hCaptcha challenge frame that actually holds the canvas.

    hCaptcha serves several frames under the same URL (a static asset frame
    plus the real challenge frame). Picking the first URL match lands on the
    static one, where `canvasRect` is null and every solve reports
    "no challenge canvas found". Prefer the frame with a real <canvas>.
    """
    fallback = None
    for fr in page.frames:
        u = fr.url or ""
        if "hcaptcha" not in u:
            continue
        if "#frame=challenge" not in u and "frame=challenge" not in u:
            continue
        if fallback is None:
            fallback = fr
        try:
            has = await fr.evaluate(
                "() => !!document.querySelector('canvas')")
        except Exception:
            has = False
        if has:
            return fr
    return fallback


def _find_challenge_frame(page):
    """Sync-compatible shim: prefer a frame whose URL marks it as challenge.

    Kept for callers that cannot await; async callers should use
    `_find_challenge_frame_async`, which verifies the canvas exists.
    """
    for fr in page.frames:
        u = fr.url or ""
        if "#frame=challenge" in u and "hcaptcha" in u:
            return fr
    return None
