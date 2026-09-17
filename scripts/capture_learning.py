#!/usr/bin/env python3
"""Capture corrections from UserPromptSubmit and POST one item to the queue issue.

Flow: read prompt -> detect_patterns -> deny list -> one POST to the
private queue issue. Fail-closed throughout: any config, privacy, or API
problem exits 0 without posting.

Env (see PLAN.md):
  REFLECT_QUEUE_REPO     queue repo in `owner/name` form; must be private.
  REFLECT_CAPTURE_TOKEN  GitHub token with `issues: write` on the queue repo.

Fires in every repo the hook runs in. The `repo_identity` field labels
the item with the repo the correction was typed in; it is never a
capture gate. The secret deny lists (client and server) are the privacy
control; there is deliberately no per-repo allowlist here.

No stdout: UserPromptSubmit stdout is injected into the model context, and
with no /reflect command there is nothing to act on. One stderr line on
the paths that stay silent.
"""

import json
import os
import subprocess
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from lib.reflect_utils import (
    create_queue_item,
    detect_patterns,
    find_secret,
    normalize_repo_identity,
    should_include_message,
    MAX_CAPTURE_PROMPT_LENGTH,
)

try:
    from urllib.error import HTTPError
    from urllib.request import Request, urlopen
except ImportError:  # pragma: no cover - urllib is stdlib
    HTTPError = None  # type: ignore
    Request = None  # type: ignore
    urlopen = None  # type: ignore

QUEUE_ISSUE_TITLE = "reflect queue"
QUEUE_LABEL = "reflect-queue"
HTTP_TIMEOUT = 3


class TokenExpiredError(Exception):
    """Raised when GitHub answers 401: the capture token is expired or
    revoked. This fails LOUD (exit 2, blocks the prompt) so a dead token
    can never pass as quiet capture. Everything else stays silent."""


def _fail(reason: str) -> int:
    print(f"reflect: capture skipped: {reason}", file=sys.stderr)
    return 0


def _fail_loud(reason: str) -> int:
    print(f"reflect: CAPTURE TOKEN INVALID: {reason}", file=sys.stderr)
    print("reflect: fix REFLECT_CAPTURE_TOKEN, then retry the prompt.", file=sys.stderr)
    return 2


def _origin_url() -> "str | None":
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if proc.returncode != 0 or proc.stdout.strip() != "true":
            return None
        proc = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout.strip() or None
    except Exception:
        return None


def _github(path: str, token: str, payload: "object | None" = None) -> object:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        f"https://api.github.com{path}",
        data=body,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        method="POST" if payload is not None else "GET",
    )
    try:
        with urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        status = getattr(exc, "code", None)
        if HTTPError is not None and isinstance(exc, HTTPError) and status == 401:
            raise TokenExpiredError(
                f"GitHub API {path} returned 401 — token expired or revoked."
            ) from exc
        raise


def _find_queue_issue(repo: str, token: str) -> "str | None":
    page = 1
    while True:
        try:
            issues = _github(
                f"/repos/{repo}/issues?state=open&labels={QUEUE_LABEL}&per_page=100&page={page}",
                token,
            )
        except TokenExpiredError:
            raise
        except Exception as exc:
            print(f"reflect: queue issue lookup failed: {exc}", file=sys.stderr)
            return None
        if not isinstance(issues, list):
            return None
        for issue in issues:
            if isinstance(issue, dict) and issue.get("title") == QUEUE_ISSUE_TITLE:
                number = issue.get("number")
                return str(number) if number is not None else None
        if len(issues) < 100:
            return None
        page += 1


def _post_item(repo: str, token: str, issue_number: str, item: dict) -> bool:
    comment = "```json\n" + json.dumps(item, indent=2) + "\n```"
    try:
        _github(
            f"/repos/{repo}/issues/{issue_number}/comments",
            token,
            {"body": comment},
        )
    except TokenExpiredError:
        raise
    except Exception as exc:
        print(f"reflect: queue POST failed: {exc}", file=sys.stderr)
        return False
    return True


def main() -> int:
    queue_repo = os.environ.get("REFLECT_QUEUE_REPO", "").strip()
    capture_token = os.environ.get("REFLECT_CAPTURE_TOKEN", "").strip()
    if not queue_repo or not capture_token:
        return _fail("missing REFLECT_QUEUE_REPO or REFLECT_CAPTURE_TOKEN")

    try:
        repo_info = _github(f"/repos/{queue_repo}", capture_token)
    except TokenExpiredError as exc:
        return _fail_loud(str(exc))
    except Exception as exc:
        print(f"reflect: queue repo check failed: {exc}", file=sys.stderr)
        return 0
    if not isinstance(repo_info, dict) or repo_info.get("private") is not True:
        return _fail(f"queue repo {queue_repo} is not private")

    try:
        input_data = sys.stdin.read()
    except Exception:
        input_data = ""
    if not input_data:
        return 0
    try:
        data = json.loads(input_data)
    except json.JSONDecodeError:
        return 0

    prompt = data.get("prompt") or data.get("message") or data.get("text")
    if not prompt or not isinstance(prompt, str):
        return 0

    if not should_include_message(prompt):
        return 0

    # Skip very long prompts — real user corrections are short.
    # Exception: explicit "remember:" markers are always processed.
    if len(prompt) > MAX_CAPTURE_PROMPT_LENGTH and "remember:" not in prompt.lower():
        return 0

    item_type, patterns, confidence, _sentiment, _decay_days = detect_patterns(prompt)
    if not item_type:
        return 0

    secret = find_secret(prompt)
    if secret is not None:
        return _fail(f"deny-list hit ({secret})")

    try:
        issue_number = _find_queue_issue(queue_repo, capture_token)
    except TokenExpiredError as exc:
        return _fail_loud(str(exc))
    if issue_number is None:
        return _fail("queue issue not found")
    item = create_queue_item(
        message=prompt,
        item_type=item_type,
        patterns=patterns,
        confidence=confidence,
    )
    item["repo_identity"] = normalize_repo_identity(_origin_url())
    try:
        _post_item(queue_repo, capture_token, issue_number, item)
    except TokenExpiredError as exc:
        return _fail_loud(str(exc))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"Warning: capture_learning.py error: {exc}", file=sys.stderr)
        sys.exit(0)
