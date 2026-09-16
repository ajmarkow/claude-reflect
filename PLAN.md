# claude-reflect fork: capture hook → queue issue → reviewed PR on nix-components

## Status

**The aggregator runs** in
[`ajmarkow/reflect-queue-test`](https://github.com/ajmarkow/reflect-queue-test):
a GitHub issue holds the queue, `daily-reflect-pr.yml` runs `process_queue.py`
on a cron, an OpenAI-compatible model rewrites one target file, and a PR opens
against the target repo. It is not ready to turn on — it publishes to a public
repo in the same run that generates the content, with no filtering and no
review. See Approval flow.

**The capture half does not exist.** Nothing writes items to the queue issue;
`scripts/capture_learning.py` still appends to a local
`~/.claude/learnings-queue.json`.

This plan covers the capture hook, the approval flow, and the nix-components
changes both depend on.

## Provenance

This repo is not original work. Commit `da7d58e` copied
[`BayramAnnakov/claude-reflect`](https://github.com/BayramAnnakov/claude-reflect)
v3.1.0 — MIT, Copyright (c) 2025 Bayram Annakov — without forking, so there is
no `upstream` remote and no shared history.

That matters because the one substantial thing surviving the deletions below
is `detect_patterns` and its pattern tables, which are upstream's work.
`LICENSE` stays. `.claude-plugin/plugin.json` holds the only machine-readable
link to upstream and is being deleted, so the work moves to a real fork, where
GitHub carries the link structurally.

## Step 0: fork upstream

Do this first. Use `gh repo fork` — a `git init` plus file copy recreates the
attribution problem being fixed.

```bash
rtk gh repo fork BayramAnnakov/claude-reflect --clone=false --default-branch-only
rtk gh repo clone ajmarkow/claude-reflect /var/lib/paseo/projects/claude-reflect
cd /var/lib/paseo/projects/claude-reflect && git checkout -b reflect/declarative
```

Work on `reflect/declarative`, never on `main` — keeping `main` at upstream's
state is what makes cherry-picking later upstream fixes possible. Move this
`PLAN.md` across as the first commit. Record upstream's version in the README,
since the fork will not track upstream after this point.

Then archive `ajmarkow/reflect-skill-declarative`. It is an unattributed copy
of MIT code and should not stay live beside the fork. **Confirm before
archiving** — outward-facing and not trivially undone.

## Architecture

| Repo                        | Owns                                                                       |
| --------------------------- | -------------------------------------------------------------------------- |
| `claude-reflect` (the fork) | The capture hook. Detects a correction, posts one item to the queue issue. |
| `reflect-queue-test`        | The queue issue, the model call, the proposal PR, and the publish step.    |
| `nix-components`            | The target file the PR edits, and the module that deploys the hook.        |

Flow:

1. **Capture** — a correction in an allowlisted repo becomes one JSON item on
   the private queue issue.
2. **Propose** (daily cron) — the model turns pending items into replacement
   content, committed to a branch in the **private** queue repo and opened as
   a PR there. Nothing public is touched.
3. **Approve** — merging that private PR is the approval.
4. **Publish** — the merge triggers a PR against public `nix-components`
   carrying the merged bytes.

## Prerequisite: consolidate the CLAUDE.md source into one file

`process_queue.py` edits exactly one `REFLECT_TARGET_FILE` and hard-fails if
the model touches anything else. The current nix-components layout has
`modules/lib/claude-md-content.nix` as a thin wrapper over
`modules/lib/claude-md/default.nix`, which concatenates 29 per-rule files —
so adding a rule means touching two files, which the guard rejects.

Fold it back, as a separate PR landed first:

1. Inline every `modules/lib/claude-md/*.nix` rule into
   `modules/lib/claude-md-content.nix`, preserving order.
2. Delete `modules/lib/claude-md/`.
3. Leave `modules/claude-code-claude-md.nix` alone — it already imports
   `./lib/claude-md-content.nix`, so `codex.nix` and `opencode.nix` keep
   working. The generated prose already names this path, so it stays correct.
4. `nix flake check --no-build`, then ship through CI.

`REFLECT_TARGET_FILE` is then `modules/lib/claude-md-content.nix`.

## Privacy boundary

The hook copies a slice of the user's prompt off the machine, and a regex
decides which slice. Prompts routinely contain credentials, customer names,
proprietary code, and paths. "No, use the staging key `sk-...` not the prod
one" is a textbook correction and a textbook leak.

Verified visibility of each hop:

| Repo                         | Visibility                                     |
| ---------------------------- | ---------------------------------------------- |
| `reflect-queue-test` (queue) | **private**                                    |
| `nix-components` (target)    | **public**                                     |
| `claude-reflect` (the fork)  | public, since forks of public repos are public |

The target being public is the hop that matters. Anything surviving capture
lands in a public diff, in notifications and email, in GitHub's search index,
and finally in the generated `~/.claude/CLAUDE.md`. A force-push does not
retract it.

Note what repo visibility does **not** tell you: a public repo's source says
nothing about what a prompt typed in its session contains. No repo is safe to
capture from on the grounds that its code is already published.

Four controls, all fail-closed. The first three reduce what gets captured and
stored; none of them can recognise sensitive content with no distinctive shape
— a customer name, an internal hostname. The fourth is what covers that case.

### 1. Opt in per repo, keyed on remote identity

`REFLECT_CAPTURE_REPOS` is a comma-separated allowlist managed in the
home-manager module. Empty or unset captures nothing, so a host that gets the
module without a list is inert.

**Entries are `host/owner/name`, never a directory basename.** A basename is
not an identity: any clone named `nix-components` — someone else's fork, a
scratch copy — would match and start publishing its prompts.

Derive the identity from the `origin` remote and normalize:

| Origin URL                                         | Identity                             |
| -------------------------------------------------- | ------------------------------------ |
| `git@github.com:ajmarkow/nix-components.git`       | `github.com/ajmarkow/nix-components` |
| `https://github.com/ajmarkow/nix-components`       | `github.com/ajmarkow/nix-components` |
| `ssh://git@github.com/ajmarkow/nix-components.git` | `github.com/ajmarkow/nix-components` |

`urllib.parse.urlsplit` handles the `https://` and `ssh://` forms; only the
scp-like `git@host:owner/name` needs a regex. Strip userinfo, port, trailing
`/`, and `.git`; lowercase. Keep the host — without it `github.com/o/n` and an
internal GitLab `o/n` collide. Compare as exact full strings: no globs, no
prefix matching, no bare `owner/name`.

Fail closed on every ambiguity, each exiting 0 without posting: not inside a
git work tree, no `origin` remote, an `origin` URL that does not parse to
exactly one `host/owner/name`, or a local-path / `file://` origin.

A renamed or transferred repo stops matching and capture stops. Re-add the new
identity deliberately.

### 2. Refuse a public queue, at runtime

Before the first POST of a session, `GET /repos/{REFLECT_QUEUE_REPO}` and
abort unless `private` is `true`. One request per session. No override flag —
a public queue is never correct.

### 3. Drop on any secret match, do not mask

Run the message through a deny list before building the item. On a hit, exit 0
and post nothing. Masking is more code and leaves a regex deciding how much of
an unrecognised secret to keep; losing one correction costs nothing.

The list covers at minimum: key prefixes (`sk-`, `ghp_`, `github_pat_`,
`gho_`, `AKIA`, `ASIA`, `xoxb-`, `xoxp-`), `Authorization:` and `Bearer `
headers, `-----BEGIN * PRIVATE KEY-----`,
`(password|passwd|secret|token|api[_-]?key)\s*[:=]`, any run of 32+ base64 or
hex characters, and any URL carrying userinfo or a token-shaped query
parameter. (`secretty` cannot be reused — it is a PTY wrapper, not a
stdin filter.)

**Run the same list again in `process_queue.py`**, over every item before the
model sees it. This is not a second boundary — same detector, same shapes — it
only catches a pattern that slipped past capture through a code path
difference. A hit there means the capture filter regressed, so treat it as an
incident:

1. `DELETE /repos/{repo}/issues/comments/{id}` on the source comment first, so
   the raw text stops being stored. `PATCH` is not enough: GitHub keeps
   comment edit history.
2. Only after the delete returns 204, post the `skipped` status comment,
   naming the item id and the matched pattern — never the text.
3. If the delete fails, abort the run and leave the item pending. Never mark
   an item skipped while its raw text is still on the issue.

This requires a code change: `extract_items` records `_source_url` but not the
comment id, so carry `_source_comment_id` through as well.

Deletion limits exposure; it does not undo it. The comment already triggered
notifications when posted. Capture-side filtering is the primary control.

Finally, scan the model's replacement content against the same list before
committing it. A hit aborts with no commit and no PR.

### 4. No public write without human approval of the exact bytes

Opening a PR **is** the disclosure — a branch on a public repo is public the
moment it exists, and closing the PR retracts nothing. "Never auto-merge" is a
correctness control, not a privacy one.

So approval happens entirely inside the private queue repo, and it is a
**merge**, not a marker in a comment. See Approval flow.

## Capture hook

Trim `scripts/capture_learning.py` to: check the allowlist, read the prompt
from stdin, run `detect_patterns`, run the deny list, and on a clean hit POST
one comment to the queue issue containing a single fenced JSON block.

**The item schema must match what `process_queue.py` reads.** The current
`create_queue_item` output has no `id`, and `extract_items` silently drops any
item without one — so every captured correction would vanish with no error
anywhere. Emit:

```json
{
  "id": "<uuid4>",
  "created_at": "<ISO 8601 Z>",
  "message": "<the correction>",
  "patterns": "<short label>",
  "type": "auto",
  "confidence": 0.8,
  "status": "pending"
}
```

Drop `sentiment`, `decay_days`, and the absolute-path `project` field. Drop
`source_project` too: only allowlisted repos can appear, and nothing downstream
reads it.

**Config:**

| Env var                 | Purpose                                                                |
| ----------------------- | ---------------------------------------------------------------------- |
| `REFLECT_QUEUE_REPO`    | Queue repo in `owner/name` form. Must be private; checked at runtime.  |
| `REFLECT_CAPTURE_TOKEN` | GitHub token with `issues: write` on the queue repo only.              |
| `REFLECT_CAPTURE_REPOS` | Allowlist of exact `host/owner/name`. Empty or unset captures nothing. |

The queue issue title and label are constants (`reflect queue`,
`reflect-queue`), not config.

**Failure posture.** The hook runs synchronously on `UserPromptSubmit`, before
the prompt reaches the model, so it must never stall or block a session:

- Missing required config → one stderr line, exit 0. Not exit 2: blocking a
  prompt over a config problem the user cannot fix mid-session is worse than
  being quiet. Verification step 2 covers the silence.
- Network or API error → stderr, exit 0. A dropped item is acceptable.
- `urllib` with an explicit `timeout=3`. No new dependency.
- No stdout. `UserPromptSubmit` stdout is injected into the model's context,
  and with no `/reflect` command left there is nothing to act on.

**Delete the `SessionStart` reminder.** The proposal PR is the review surface
now, and `session_start_reminder.py` depends on `load_queue`, which is going.

## Approval flow

Phase 1 proposes inside the private repo; merging that proposal publishes.

The queue repo keeps a mirror of the target file at
`proposals/<basename of REFLECT_TARGET_FILE>` on `main`.

**Phase 1 — daily cron. Touches nothing public.**

1. Read pending items; run the deny list and the deletion path above.
2. Fetch the current target file from nix-components and commit it to the
   mirror path on `main` if it drifted. The proposal diff is only meaningful
   against current upstream state.
3. Run the model, then scan its output.
4. Commit the replacement to branch `proposal/<YYYY-MM-DD>` and open a PR
   **in the queue repo**, body listing the item ids it covers.
5. Comment the PR link on the queue issue; mark items `awaiting approval`.
6. If an unmerged proposal PR already exists, close it first — that is
   supersession, and it is visible rather than inferred.

**Phase 2 — `pull_request: [closed]`, guarded on
`merged == true` and a `proposal/*` head branch.**

1. Read the target content from the mirror path **at the merge commit**. Never
   re-run the model; that would publish bytes nobody read.
2. Push branch `reflect/proposal-<PR number>` to nix-components and open a PR
   there.
3. Comment the public PR URL back on the queue issue; mark items `published`.

**Why a merge and not a marker.** An earlier draft used HTML-comment markers
(`reflect-approve:<id>`) parsed out of comment bodies, with a SHA-256 digest,
a TTL, an approver allowlist, and a live permission check. All of that is
GitHub functionality reimplemented badly — and it had a hole no identity check
closes: the capture hook posts with the owner's PAT, so a captured correction
containing the approve marker would pass every author check and self-approve.

A merge gives the same properties natively:

| Property       | Native mechanism                                                     |
| -------------- | -------------------------------------------------------------------- |
| Immutability   | The merge commit SHA                                                 |
| Addressability | The PR number                                                        |
| Supersession   | Closing the previous proposal PR                                     |
| Authorization  | Who can merge in the private repo                                    |
| Idempotency    | `merged == true` fires once; the public PR either exists or does not |
| Audit          | PR merge history                                                     |
| Injection      | Impossible — approval is an event, not text                          |

**Recovery.** The public branch name is derived from the proposal PR number,
so a re-run pushes to the same branch rather than accumulating one per
attempt. Before publishing, query whether a PR already exists for that branch;
if so, the run is a no-op. A crash between push and PR creation leaves an
orphan branch — harmless, visible, deleted by hand in seconds. That is the
accepted cost of not building reconciliation logic that would run once a year.

**Concurrency.** Both phases share one workflow `concurrency` group with
`cancel-in-progress: false`, keyed to the queue repo rather than `github.ref`.

**Credentials split.** Phase 1 needs only `issues: write` plus `contents:
write` on the private queue. Only phase 2 gets a token that can write to
nix-components, so the daily cron holds nothing that can publish.

## Deletions

| Path                                                | Lines | Why                                                             |
| --------------------------------------------------- | ----- | --------------------------------------------------------------- |
| `commands/reflect.md`                               | 1512  | Local-review flow, replaced by the proposal PR                  |
| `tests/test_semantic_detector.py`                   | 728   | Tests deleted code                                              |
| `scripts/lib/semantic_detector.py`                  | 561   | Parallel AI-detection system, never imported by the wired hooks |
| `tests/test_integration.py`                         | 486   | Integration tests for the file-queue and command flow           |
| `tests/test_memory_hierarchy.py`                    | 404   | Tests the multi-target routing being deleted                    |
| `scripts/compare_detection.py`                      | 366   | Orphaned dev CLI                                                |
| `commands/reflect-skills.md`                        | 362   | Same as `reflect.md`                                            |
| `tests/test_tool_errors.py`                         | 326   | Tests `extract_tool_errors.py`                                  |
| `scripts/legacy/*.sh`                               | 312   | 5 unwired bash duplicates of the Python hooks                   |
| `scripts/extract_tool_errors.py`                    | 172   | Only reachable from `reflect.md`                                |
| `commands/view-queue.md`                            | 103   | Same as `reflect.md`                                            |
| `SKILL.md`                                          | 68    | Documents six commands that are all going away                  |
| `scripts/post_commit_reminder.py`                   | 65    | `PostToolUse` hook, dropped                                     |
| `scripts/session_start_reminder.py`                 | 64    | `SessionStart` hook, dropped                                    |
| `scripts/check_learnings.py`                        | 51    | `PreCompact` hook, dropped                                      |
| `scripts/extract_session_learnings.py`              | 43    | Only reachable from `reflect.md`                                |
| `scripts/extract_tool_rejections.py`                | 42    | Same                                                            |
| `.github/workflows/test.yml`                        | 40    | Invokes deleted scripts; rewrite, do not keep                   |
| `commands/skip-reflect.md`                          | 37    | Same as `reflect.md`                                            |
| `scripts/read_queue.py`                             | 11    | Pass-through wrapper                                            |
| `DISTRIBUTION.md` / `RELEASING.md` / `CHANGELOG.md` | 554   | Plugin-marketplace release docs                                 |
| `.claude-plugin/`                                   | 33    | Not distributed as a marketplace plugin                         |
| `assets/`, `.github/FUNDING.yml`                    | —     | Upstream artifacts                                              |

From `scripts/lib/reflect_utils.py` (1173 lines), delete `find_claude_files`,
`suggest_claude_file`, `_parse_rule_frontmatter`, `get_auto_memory_path`,
`read_auto_memory`, `suggest_auto_memory_topic`, `read_all_memory_entries`
(multi-target routing, one caller); `get_global_queue_path`,
`migrate_global_queue`, `append_to_queue`, `get_queue_path`, `load_queue`,
`save_queue` (file-queue I/O); and the session-file utilities
(`extract_user_messages` and friends) that only served `--scan-history`.

Keep `detect_patterns` and its pattern tables, `create_queue_item` (reshaped
above), `should_include_message`, `MAX_CAPTURE_PROMPT_LENGTH` — all pure logic
with no file I/O.

`hooks/hooks.json` keeps only `UserPromptSubmit`. It is redundant once
home-manager registers the hook directly, but keep it so the repo still works
as a plain Claude Code plugin for anyone who clones it.

`LICENSE` is **not** on the delete list and must not be.

Rewrite `README.md` short, documenting the env var contract and crediting
`BayramAnnakov/claude-reflect` for the retained detection code. Update
`CLAUDE.md` (dev docs) to match.

## Tests

Trim `tests/test_reflect_utils.py` from 1074 to roughly 250 lines, covering
the kept functions plus a test that the emitted item matches the schema
`process_queue.py` accepts — that one prevents the silent-drop bug.

Add `tests/test_privacy.py`, with the network call stubbed so a regression
fails a test rather than posting for real:

- Every deny-list pattern, one realistic case each → no POST.
- Unset `REFLECT_CAPTURE_REPOS` → no POST. Identity off the list → no POST.
  Identity on the list → POST.
- **Identity collision** — two repos whose directories share a basename but
  whose `origin` remotes differ; only the allowlisted one posts.
- **Normalization** — scp-like, `https://`, and `ssh://` forms of one remote
  resolve to one identity, with `.git`, userinfo, port, trailing slash, and
  case all normalized away.
- **Unusable identity** → no POST: outside a work tree, no `origin`,
  unparseable URL, `file://` origin.
- **Near misses** → no POST: a fork under another owner, the same `owner/name`
  on another host, a name that is a prefix or suffix of an allowlisted one.
- Queue repo reporting `private: false` → no POST, no override.
- The emitted item carries no absolute path in any field.

In `reflect-queue-test`, with the GitHub API and model client stubbed:

- **Detection deletes the source comment**, and the `skipped` status is posted
  only after the delete succeeds.
- **Delete failure aborts the run** — no `skipped` comment, item stays
  pending. The state to prove impossible is "marked handled, text still
  there".
- **The status comment never echoes the matched text.**
- **Phase 1 touches nothing public** — stub the git layer and assert no push,
  branch, or PR call against nix-components.
- **Phase 2 publishes the merge commit's bytes**, never a fresh model call.
- **Phase 2 ignores a closed-unmerged proposal PR**, and ignores merges from
  branches outside `proposal/*`.
- **Republish is a no-op** when the public PR already exists for that branch.
- **`status_ids` carve-outs** — `**claimed**` and `awaiting approval` must not
  count as seen, in one test, so the next status string added does not
  reintroduce the bug.

## nix-components changes

1. **Consolidation PR** (prerequisite above).
2. **Hook deployment**, two files:
   - `modules/claude-code/hooks.nix` — the hook script, written the way
     `block-ssh-rg-cd.sh` and `rtk-rewrite.sh` are.
   - `modules/claude-code/settings.nix` — add a `UserPromptSubmit` block to
     `programs.claude-code.settings.hooks`. Only `PreToolUse` exists today.
3. **The module ships inert.** `REFLECT_CAPTURE_REPOS` defaults to empty.
   Populate it with one entry and leave it a week before adding more;
   `github.com/ajmarkow/nix-components` is a reasonable first choice, because
   its sessions concern Nix config rather than customer or production data.
   The entry is the full identity, not `nix-components`.
4. **No workflow file here.** Both phases live in the queue repo.
5. **Deployment is not automatic.** nix-components is a component flake; its
   `check.yml` runs `nix flake check` and builds packages, it does not deploy.
   Shipping the hook needs the `deploy-nix-components` flow — bump the input
   in nix-server, nix-mac, and nix-pixelbook, and let each host's CI deploy.
   Decide which of the three should run the hook before bumping.

**Secret delivery.** `REFLECT_CAPTURE_TOKEN` cannot go in
`home.sessionVariables` — that writes it into the world-readable Nix store.
Use the runtime-read pattern from `modules/paseo-remote.nix:34`, which reads
`$HOME/.config/infisical-token` at invocation. The non-secret vars are fine as
session variables.

## Known defects in reflect-queue-test

Real, not hypothetical. Fix before the cron runs.

1. **`nix flake check` needs Nix on the runner.** The check command fails
   immediately today — the workflow installs Python and nothing else. Add a
   Nix install step, or the check never protects anything. Hardcode the
   command rather than keeping `REFLECT_CHECK_COMMAND` configurable; there is
   one target and one correct check.
2. **Nix string escaping.** The target is a `.nix` file whose body is a `''…''`
   literal. A rule containing `${` or `''` breaks evaluation, and the model has
   no instruction about it. Add the escaping rules to
   `prompts/apply-insights.md`.
3. **A closed PR loses its items forever.** `process_queue.py` marks items
   `in review` once a PR opens, and `status_ids` then excludes them from every
   future run. Either mark items only on merge, or add a way to reopen them.
4. **No deny list, and no approval step.** Both are new work — see Privacy
   boundary and Approval flow. The propose/approve split is the largest single
   change in this plan.
5. **Nothing removes captured text from the queue issue.** Even private, it
   accumulates raw prompt slices indefinitely. Immediate deletion covers
   deny-list hits; routine cleanup — deleting item comments once their public
   PR merges — still needs building.

## Open decisions

1. **Model provider.** `process_queue.py` defaults to `meta` at
   `https://api.meta.ai/v1` with `muse-spark-1.2-contributor`. OpenRouter is
   already supported and already wired into this environment. Pick one. This
   blocks the aggregator running at all.
2. **Queue repo name.** `reflect-queue-test` reads as a throwaway.
3. **GitHub secret naming.** nix-components' `check.yml` consumes
   `secrets.INFISICAL_*`, an Infisical-synced set. The queue repo's secrets are
   hand-set and outside that sync. Confirm that is intended.

## Verification

Steps 1 and 2 gate everything downstream.

1. `python3 -m pytest tests/` passes, including all of `tests/test_privacy.py`.
2. **Leak drill against the real queue, before any host deploys.** With the
   allowlist naming only the current repo:
   - `echo '{"prompt":"no, use sk-ant-api03-AAAA... not the old key"}' | python3 scripts/capture_learning.py`
     → exit 0 and **no new comment**. Confirm by reading the issue, not by
     trusting the exit code.
   - From a repo not on the allowlist → no comment.
   - `REFLECT_QUEUE_REPO` pointed at any public repo → no comment, plus an
     explicit stderr line.
3. With a clean prompt → a fenced JSON comment appears with an `id` and no
   absolute path. Unset `REFLECT_QUEUE_REPO` → one stderr line, exit 0, no
   comment, session unaffected.
4. **Phase 1 drill.** Seed one clean item and one carrying a fake key, then run
   the workflow:
   - A proposal PR opens **in the queue repo**, and
     `gh api repos/ajmarkow/nix-components/branches` shows no new branch. This
     is the check that disclosure has not happened.
   - The fake-key item's comment is gone from the issue — confirm the comment
     id returns 404 — and its `skipped` status does not quote the text.
5. **Phase 2 drill.** Merge the proposal PR → a PR opens on nix-components
   editing only `modules/lib/claude-md-content.nix`, byte-identical to the
   merged mirror file. Closing a proposal PR without merging publishes
   nothing.
6. `nix flake check --no-build` passes on that PR branch. Merge by hand. This
   is a **correctness** gate — the content is already public by now; step 5's
   merge was the privacy gate.
7. After `deploy-nix-components` and each host's CI, a real correction in an
   allowlisted repo lands on the queue issue. Read the first week's items by
   hand before widening the allowlist, and check that week's proposals for
   sensitive content carrying no secret shape — the failure mode the deny
   lists cannot catch.
