# claude-reflect (fork)

Capture hook for the reflect pipeline. Detects a correction in any repo
the hook runs in and posts one JSON item to the private queue issue in
`REFLECT_QUEUE_REPO`, where the aggregator turns pending items into a
reviewed proposal. See `PLAN.md`.

Forked from [`BayramAnnakov/claude-reflect`](https://github.com/BayramAnnakov/claude-reflect)
v3.1.0 (MIT, Copyright (c) 2025 Bayram Annakov). The retained detection
code (`detect_patterns` and its pattern tables in
`scripts/lib/reflect_utils.py`) is upstream's work under that license.

## How it works

`scripts/capture_learning.py` runs on `UserPromptSubmit`:

1. Read the prompt from stdin, run `detect_patterns`.
2. Run the secret deny list; on a hit, post nothing.
3. POST one comment with a single fenced JSON block to the queue issue
   (`reflect queue` / `reflect-queue`).

## Config

| Env var                 | Purpose                                              |
| ----------------------- | ---------------------------------------------------- |
| `REFLECT_QUEUE_REPO`    | Queue repo in `owner/name` form. Must be private.    |
| `REFLECT_CAPTURE_TOKEN` | GitHub token with `issues: write` on the queue repo. |

Fail-closed: missing config, non-private queue, or
API error means one stderr line and exit 0. No stdout — `UserPromptSubmit`
stdout is injected into the model context and there is nothing to act on.

## Tests

```bash
python3 -m pytest tests/ -v
```

## License

MIT
