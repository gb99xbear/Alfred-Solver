<div align="center">

# 🔐 Alfred Solver (Sizuk4 Edition)

**Self-hosted captcha-solving sidecar — 51 challenge types, zero per-solve cost.**

> ⚠️ **Educational purposes only.** This project exists to study how anti-bot
> challenges work and to test automation research in a controlled setting.
> You are responsible for complying with the terms of service of any site you
> point it at, and with all applicable laws. Do not use it for abuse,
> unauthorized access, or any illegal activity.

One FastAPI process. One JSON call. Tokens in seconds.

![types](https://img.shields.io/badge/types-51-blueviolet) ![python](https://img.shields.io/badge/python-3.11%2B-blue) ![license](https://img.shields.io/badge/license-MIT-green) ![no--docker](https://img.shields.io/badge/docker-not--needed-9cf)

**[⚡ Quick start](#-quick-start) · [🧩 Types](#-supported-types--51) · [✅ Verification](#-verification) · [📡 API](#-one-json-call-in-token-out)**

</div>

---

## ⚡ Quick start

```bash
git clone https://github.com/gb99xbear/Alfred-Solver.git && cd Alfred-Solver
./install.sh          # python deps + chromium + xvfb + node sidecar + ONNX models
./run.sh              # FastAPI on :8877
```

`run.sh` starts Xvfb automatically on a headless box, reads `$PORT`, and refuses
to start if the port is already busy. Check it is up:

```bash
curl -s http://localhost:8877/health
```

Then solve:

```bash
curl -s -X POST http://localhost:8877/solve -H 'Content-Type: application/json' \
  -d '{"type":"cloudflare","url":"https://nowsecure.nl/"}'
```

> **First run on a fresh box:** `install.sh` is idempotent and safe to re-run.
> Use `YES=1 ./install.sh` for a non-interactive install and
> `WITH_ARKOSE=1 ./install.sh` to also pull the optional 1.4 GB Arkose models.

---

## 🧩 Supported types — 51

Legend:

- ✅ **verified** — solved live with a real token/cookie during testing
- ⚙️ **needs input** — the solver is complete; it needs a real sitekey, URL, or image from your target
- 🔒 **needs IP** — works, but the vendor scores the network independently, so a datacenter IP is refused

### Browser-backed

| | type | result | notes |
|---|---|---|---|
| ✅ | `cloudflare` | `cf_clearance` cookie + UA | full interstitial |
| ✅ | `cloudflare_challenge` | `cf_clearance` cookie | alias path for the challenge endpoint |
| ✅ | `turnstile` | widget token | any sitekey, 3 solving modes |
| ✅ | `recaptcha` | `token` | v2 / v3 / Enterprise, ONNX tile classifier |
| ✅ | `hcaptcha` | `token` | checkbox + vision challenge path |
| ✅ | `hcaptcha_enterprise` | `token` | enterprise `rqdata` flow |
| ✅ | `datadome` | `datadome` cookie | IP-sensitive, see footnote 1 |
| ✅ | `imperva` | `visid_incap_*` + `incap_ses_*` | Incapsula session |
| ✅ | `perimeterx` | `_px3` cookie | press-and-hold flow |
| ✅ | `yandex` | SmartCaptcha token | |
| ✅ | `geetest` | v4 `captcha_output` + `pass_token` | slide |
| ✅ | `awswaf` | `aws-waf-token` cookie | verified against a live silent-WAF page |
| ⚙️ | `botguard` | Google `bgRequest` token | needs an account `email` to reach the token RPC |
| ⚙️ | `aliyun` | `{certifyId, deviceToken, data}` | needs `scene_id` + `prefix` |
| ✅ | `geetest_v3` | `validate` + `seccode` | click-type v3 (bilibili). Pass `gt`+`challenge`, a `register_url`, or nothing (auto-fetches a live pair). Binding builds via `scripts/build_geetest_v3.sh` |
| ✅ | `mtcaptcha` | `vt` token | verified with MTCaptcha's public demo sitekey |
| ⚙️ | `arkose` | `fc_token` | needs `public_key` + a clean IP |
| ⚙️ | `kasada` | `x-kpsdk-ct` headers | needs a classic `ips.js` site |
| ⚙️ | `cybersiara` | JWT token | needs the current `MasterUrlId` |
| ✅ | `x5sec` | `x5sec` cookie | needs a live punish URL |
| ⚙️ | `friendly` | challenge token | needs a live target |
| ⚙️ | `captchafox` | slide result | needs the puzzle piece |
| ✅ | `binance` | slide result | verified with the slide endpoint (`biz_id`) |
| ⚙️ | `basilisk` | slide + icon-click | needs `site_key` + `site_domain` |
| ⚙️ | `recaptcha_audio` | transcribed text | needs the audio clip URL (no browser) |
| ⚙️ | `steam` | solved text | needs the captcha image |
| ⚙️ | `vk` | solved text | needs the captcha image or `sid` |
| ✅ | `zhihu` | 4-char text | verified on a live zhihu.com captcha image |
| 🔒 | `akamai` | `_abck` cookie | datacenter IP is scored independently |

### No-browser (compute / image / PoW)

| | type | result | notes |
|---|---|---|---|
| ✅ | `cap` | PoW solution | verified with a generated challenge |
| ✅ | `anubis` | PoW solution | verified with a generated challenge |
| ✅ | `mcaptcha` | PoW solution | verified with a generated config |
| ✅ | `brave_pow` | PoW solution | Brave Search API dashboard (Argon2id) — end-to-end |
| ✅ | `goaway` | PoW solution | verified with a generated challenge |
| ✅ | `rotate` | rotation angle | ships its own ONNX model |
| ✅ | `image_to_text` | OCR text | ddddocr, raw base64 or data-URI |
| ✅ | `tspd` | F5/DDoS cookie | needs `url` |
| ✅ | `altcha` | PoW payload | official altcha-lib; verified with a generated challenge |
| ✅ | `tencent` | `ticket` + `randstr` | verified with the public test appid (pure-HTTP) |
| ⚙️ | `procaptcha` | PoW solution | needs the dapp `url` + `sitekey` |
| ✅ | `cerberus` | challenge solution (blake3) | verified with a generated challenge |
| ✅ | `yidun` | NetEase slider (v3 protocol 2.28.5) | verified with the demo captchaId |
| ⚙️ | `dingxiang` | Dingxiang v5 slider | needs an `app_id` (a demo default is included) |
| ✅ | `shumei` | Shumei click captcha | verified with the official trial org |
| ⚙️ | `douyin` | ByteDance slide puzzle | needs the puzzle image + a clean IP |
| ⚙️ | `vaptcha` | Vaptcha V4 gesture | needs the site's `vid` |
| ✅ | `grid` | grid selection | vision verified (pixtral); needs `image_b64` + `action` (the text instruction) |
| ✅ | `coordinates` | click coordinates | vision verified (pixtral); needs `image_b64` + `action` (the text instruction) |
| ✅ | `draw_around` | draw-around captcha | vision verified (pixtral); needs `image_b64` + `action` (the text instruction) |
| ✅ | `drag_drop` | drag & drop | vision verified (pixtral); needs `image_b64` + `action` (the text instruction) |
| ✅ | `bounding_box` | bounding-box selection | vision verified (pixtral); needs `image_b64` + `action` (the text instruction) |

**Footnotes**

1. **IP-sensitive (🔒)** — Akamai and DataDome score the network independently of
   the browser, so a residential or mobile egress is required. See the
   **Why IP matters** section below.
2. **Needs input (⚙️)** — the solver is complete; it needs a real sitekey, URL, or
   image from the target site. No library can invent these values.
4. **Arkose models** — 24 optional ONNX models (~1.4 GB) load with one command.
   See the **Arkose models** section below.

## 🚀 One JSON call in, token out

```bash
curl -s -X POST :8877/solve -H 'Content-Type: application/json' \
  -d '{"type":"cloudflare","url":"https://nowsecure.nl/"}'
```

```json
{"type":"cloudflare", "solved":true, "cf_clearance":{"value":"X0ujw..."}, "user_agent":"Mozilla/5.0 ..."}
```

<details>
<summary><b>📖 More examples</b></summary>

```bash
# Turnstile
curl -s -X POST :8877/solve -H 'Content-Type: application/json' \
  -d '{"type":"turnstile","sitekey":"0x4AAA...","url":"https://target.com"}'

# reCAPTCHA v3 Enterprise
curl -s -X POST :8877/solve -H 'Content-Type: application/json' \
  -d '{"type":"recaptcha","version":"v3","enterprise":true,"sitekey":"6Lc...","url":"https://target.com"}'

# Tencent — pure-HTTP, no browser, no sitekey
curl -s -X POST :8877/solve -H 'Content-Type: application/json' \
  -d '{"type":"tencent"}'

# Imperva Incapsula session
curl -s -X POST :8877/solve -H 'Content-Type: application/json' \
  -d '{"type":"imperva","url":"https://balance.vanillagift.com/"}'

# GeeTest v4 slide
curl -s -X POST :8877/solve -H 'Content-Type: application/json' \
  -d '{"type":"geetest","captcha_id":"54088bb0...","risk_type":"slide"}'

# OCR — raw base64, or a data-URI (the prefix is stripped server-side)
curl -s -X POST :8877/solve -H 'Content-Type: application/json' \
  -d '{"type":"image_to_text","image_b64":"...
```