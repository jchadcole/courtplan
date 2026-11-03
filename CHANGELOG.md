# Changelog

All notable changes to CourtPlan are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). See "Releasing" in the README.

## [Unreleased]

### Added
- Single-elimination brackets from pool standings (Brackets tab, public bracket view) and a
  drag-and-drop schedule grid with game locks, conflict waivers and re-optimize (PR #2).
- Release versioning: `VERSION` file, this changelog, `scripts/release.py`, a tag-triggered
  GitHub release workflow, `GET /api/version`, and the version shown in the app footer.

## [1.0.0] - 2026-11-03

First version (PR #1).

### Added
- Multi-tournament, multi-venue AAU scheduler: pool round-robins, slot grid, greedy construction
  with restarts, ejection repair and simulated annealing; hard rules never relaxed.
- Coach conflict protection with travel buffers, per-coach rule profiles, rest windows, venue
  zoning by division, 85/15 catch-up slots, referee zoning, RPI-balanced pools.
- Team preferences, including free-text requests read by Claude (with a rule-based fallback).
- Live control for site directors: court delays with slot shifting, scores, announcements,
  auto-repair, and a Claude-written director briefing.
- Public mobile live schedule with QR code, Server-Sent Events updates, notifications, standings
  and tie-breakers, CSV import and export.
- Secure access: scrypt passwords, server-side sessions, CSRF tokens, login throttling, role checks.

[Unreleased]: https://github.com/jchadcole/courtplan/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/jchadcole/courtplan/releases/tag/v1.0.0
