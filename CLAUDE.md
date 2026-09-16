# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

claude-reflect (fork of BayramAnnakov/claude-reflect v3.1.0) is the capture
half of the reflect pipeline: `scripts/capture_learning.py` runs on
`UserPromptSubmit`, detects a correction in an allowlisted repo, and POSTs
one JSON item to the private queue issue. The aggregator (queue issue,
model call, proposal PR, publish step) lives in a separate repo. See
`PLAN.md`.

## Architecture

```
hooks/hooks.json          → Hook definition (UserPromptSubmit only)
scripts/capture_learning.py → Allowlist check, detect, deny list, one POST
scripts/lib/reflect_utils.py → detect_patterns + tables, create_queue_item,
                               should_include_message, repo identity, deny list
tests/                    → pytest suite
```

### Data flow

1. User prompt → `capture_learning.py` → one fenced-JSON comment on the
   queue issue (`reflect queue` / `reflect-queue`).
2. Queue item schema: `id` (uuid4), `created_at`, `message`, `patterns`,
   `type`, `confidence`, `status: pending`. Items without an `id` are
   silently dropped downstream — never omit it.

### Detection code provenance

`detect_patterns` and its pattern tables are retained upstream work (MIT,
Copyright (c) 2025 Bayram Annakov). `LICENSE` stays.

## Development Commands

```bash
# Test capture hook with simulated input (allowlist + token required for a POST;
# without them the hook exits 0 having posted nothing)
echo '{"prompt":"no, use gpt-5.1 not gpt-5"}' | python3 scripts/capture_learning.py

# Run tests
python -m pytest tests/ -v
```

## Hook events

| Hook | Script | Purpose |
|------|--------|---------|
| UserPromptSubmit | `capture_learning.py` | Detect corrections and POST to queue issue |

## Detection

`scripts/lib/reflect_utils.py` defines pattern detection:

- **Corrections**: "no, use X", "don't use", "stop using", "that's wrong", "actually", "use X not Y"
- **Positive**: "perfect!", "exactly right", "great approach", "nailed it"
- **Explicit**: "remember:" prefix (highest confidence)

Confidence scores range 0.60-0.90 based on pattern strength and count.

## Privacy boundary

Four controls, all fail-closed:

1. **Opt in per repo** — `REFLECT_CAPTURE_REPOS` allowlist of exact
   `host/owner/name` identities derived from the `origin` remote.
2. **Refuse a public queue at runtime** — abort unless the queue repo
   reports `private: true`.
3. **Deny list** — drop on any secret-shaped match, do not mask.
4. **Approval is a merge in the private queue repo** — never auto-publish.

## Platform Support

- **macOS**: Fully supported
- **Linux**: Fully supported
- **Windows**: Fully supported (native Python, no WSL required)

Requires Python 3.6+.
