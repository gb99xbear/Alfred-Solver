# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Storage-state support for reCAPTCHA Enterprise
- Mistral SSE parsing fix
- Single-key mode for Mistral (api_key + endpoint)
- hCaptcha/recaptcha solver refactor
- Storage-state support for reCAPTCHA Enterprise

### Fixed
- Mistral SSE parsing for streaming gateways
- Mistral single-key mode support

### Removed
- Debug/test files (heycash_*, ysense_*, hc_survey_*, probe_*, etc.)
- Backup files and broken files

## [1.0.0] - 2026-10-27

### Added
- Initial release of Alfred Solver (Sizuk4 Edition)
- 51 captcha solver types
- Storage-state support for reCAPTCHA Enterprise
- Mistral AI integration with SSE parsing
- Brave Search Argon2id PoW solver
- Geetest v3 click-type solver

### Changed
- Rebranded from CF-Solver to Alfred Solver (Sizuk4 Edition)
- Updated branding and documentation

### Fixed
- Mistral SSE parsing for streaming responses
- Repository cleanup and .gitignore update

## [0.9.0] - 2026-10-23

### Added
- Brave Search Argon2id PoW solver
- Geetest v3 click-type solver with auto-fetch
- Retry-After handling and 429 ladder

### Fixed
- Model missing errors

## [0.8.0] - 2026-10-20

### Added
- Arkose Labs solver (optional ONNX models)
- Brave Search API integration

### Changed
- Improved installation script

## [0.7.0] - 2026-10-15

### Added
- reCAPTCHA v3 Enterprise support
- Storage-state support for session persistence

### Changed
- Refactored browser context handling

## [0.6.0] - 2026-10-10

### Added
- reCAPTCHA v2/v3 support
- hCaptcha enterprise support

## [0.5.0] - 2026-10-05

### Added
- Cloudflare Turnstile solver
- hCaptcha checkbox solver

## [0.4.0] - 2026-10-01

### Added
- Cloudflare interstitial solver
- Tencent captcha (pure HTTP)

## [0.3.0] - 2026-09-25

### Added
- Basic Cloudflare challenge solver
- reCAPTCHA v2 support

## [0.2.0] - 2026-09-20

### Added
- Basic project structure
- FastAPI server skeleton

## [0.1.0] - 2026-09-15

### Added
- Initial project setup