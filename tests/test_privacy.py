#!/usr/bin/env python3
"""Privacy-boundary tests for scripts/capture_learning.py.

The network call is stubbed: a regression fails a test instead of
posting for real. Every path that must not post asserts the stub saw
no POST; the one path that must post asserts exactly one POST with an
item carrying an id and no absolute path.
"""
import io
import json
import os
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

# Add scripts directory to path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import capture_learning
from capture_learning import main
from lib.reflect_utils import DENY_PATTERNS

ALLOW_IDENTITY = "github.com/ajmarkow/nix-components"
ALLOW_ORIGIN = "git@github.com:ajmarkow/nix-components.git"

BASE_ENV = {
    "REFLECT_QUEUE_REPO": "ajmarkow/reflect-queue-test",
    "REFLECT_CAPTURE_TOKEN": "test-token",
    "REFLECT_CAPTURE_REPOS": ALLOW_IDENTITY,
}

CLEAN_PROMPT = "no, use postgres not sqlite"


class StubAPI:
    """Records GET/POST calls; queue repo is private unless told otherwise."""

    def __init__(self, private=True, issue_number=7):
        self.private = private
        self.issue_number = issue_number
        self.posts = []

    def __call__(self, path, token, payload=None):
        if payload is None:
            if path.endswith(f"/repos/{BASE_ENV['REFLECT_QUEUE_REPO']}"
                             ) or path == f"/repos/{BASE_ENV['REFLECT_QUEUE_REPO']}":
                return {"private": self.private}
            if "/issues?" in path:
                if self.issue_number is None:
                    return []
                return [{"title": "reflect queue", "number": self.issue_number}]
            raise AssertionError(f"unexpected GET {path}")
        self.posts.append((path, payload))
        return {"id": 1}


@contextmanager
def hook_env(stub, origin, prompt, env_overrides=None):
    env = dict(BASE_ENV)
    if env_overrides:
        env.update(env_overrides)
    for key in list(os.environ):
        if key.startswith("REFLECT_"):
            del os.environ[key]
    os.environ.update({k: v for k, v in env.items() if v is not None})
    stdin = io.StringIO(json.dumps({"prompt": prompt}))
    with patch.dict(os.environ, {}, clear=False), \
            patch("capture_learning._origin_url", return_value=origin), \
            patch("capture_learning._github", side_effect=stub), \
            patch.object(capture_learning.sys, "stdin", stdin), \
            patch.object(capture_learning.sys, "stderr", io.StringIO()):
        yield stub


def run_hook(stub, origin, prompt, env_overrides=None):
    with hook_env(stub, origin, prompt, env_overrides):
        code = main()
    assert code == 0
    return stub


class TestDenyList(unittest.TestCase):
    """Every deny-list pattern, one realistic case each -> no POST."""

    CASES = [
        "no, use sk-ant-api03-AAAAbbbbCCCCdddd not the old key",
        "the token ghp_AbCdEfGhIjKlMnOpQrStUv is expired",
        "use github_pat_abcDEF1234567890 for this",
        "gho_xYz1234567890abcdef failed",
        "AKIAIOSFODNN7EXAMPLE is the key",
        "rotated to ASIAIOSFODNN7EXAMPLE yesterday",
        "xoxb-1234-5678-abcdefghijklmnop broke",
        "xoxp-1234-5678-abcdefghijklmnop broke",
        "Authorization: Bearer abc123",
        "pass Bearer eyJhbGciOiJIUzI1NiJ9 along",
        "-----BEGIN RSA PRIVATE KEY-----",
        "password: hunter2",
        "api_key=abc123",
        "secret = abc123",
        "token: abc123",
        "deadbeef" * 8,
        "QUJD" * 16,
        "https://user:pass@example.com/path",
        "https://example.com/cb?token=abc123",
    ]

    def test_deny_list_coverage_matches_module(self):
        """If a pattern is added to DENY_PATTERNS without a case here,
        this fails — the point is one realistic case per pattern."""
        names_in_module = {name for name, _ in DENY_PATTERNS}
        self.assertEqual(names_in_module, {
            "openai-key", "github-pat", "github-oauth", "aws-access-key",
            "slack-token", "auth-header", "bearer-token", "private-key",
            "credential-assignment", "long-hex-run", "long-base64-run",
            "url-userinfo", "url-token-param",
        })

    def test_every_case_posts_nothing(self):
        for text in self.CASES:
            with self.subTest(text=text[:40]):
                stub = run_hook(StubAPI(), ALLOW_ORIGIN, text)
                self.assertEqual(stub.posts, [])


class TestAllowlist(unittest.TestCase):
    """Unset list -> no POST. Off-list identity -> no POST. On-list -> POST."""

    def _no_reflect_keys(self):
        return {k: None for k in BASE_ENV}

    def test_unset_allowlist_no_post(self):
        stub = run_hook(StubAPI(), ALLOW_ORIGIN, CLEAN_PROMPT,
                        {"REFLECT_CAPTURE_REPOS": None})
        self.assertEqual(stub.posts, [])

    def test_empty_allowlist_no_post(self):
        stub = run_hook(StubAPI(), ALLOW_ORIGIN, CLEAN_PROMPT,
                        {"REFLECT_CAPTURE_REPOS": "  "})
        self.assertEqual(stub.posts, [])

    def test_off_list_identity_no_post(self):
        stub = run_hook(StubAPI(),
                        "git@github.com:otherowner/nix-components.git",
                        CLEAN_PROMPT)
        self.assertEqual(stub.posts, [])

    def test_on_list_identity_posts(self):
        stub = run_hook(StubAPI(), ALLOW_ORIGIN, CLEAN_PROMPT)
        self.assertEqual(len(stub.posts), 1)

    def test_missing_queue_repo_no_post(self):
        stub = run_hook(StubAPI(), ALLOW_ORIGIN, CLEAN_PROMPT,
                        {"REFLECT_QUEUE_REPO": None})
        self.assertEqual(stub.posts, [])

    def test_missing_token_no_post(self):
        stub = run_hook(StubAPI(), ALLOW_ORIGIN, CLEAN_PROMPT,
                        {"REFLECT_CAPTURE_TOKEN": None})
        self.assertEqual(stub.posts, [])


class TestIdentityCollision(unittest.TestCase):
    """Two repos share a directory basename but have different origins;
    only the allowlisted one posts."""

    def test_only_allowlisted_origin_posts(self):
        stub = run_hook(StubAPI(), ALLOW_ORIGIN, CLEAN_PROMPT)
        self.assertEqual(len(stub.posts), 1)

        stub = run_hook(StubAPI(),
                        "git@github.com:someone-else/nix-components.git",
                        CLEAN_PROMPT)
        self.assertEqual(stub.posts, [])


class TestNormalization(unittest.TestCase):
    """scp-like, https://, and ssh:// forms of one remote resolve to one
    identity, with .git, userinfo, port, trailing slash, and case
    normalized away."""

    FORMS = [
        "git@github.com:ajmarkow/nix-components.git",
        "https://github.com/ajmarkow/nix-components",
        "ssh://git@github.com/ajmarkow/nix-components.git",
        "https://github.com/ajmarkow/nix-components.git",
        "https://user@github.com:443/ajmarkow/nix-components/",
        "GIT@GITHUB.COM:AJMARKOW/NIX-COMPONENTS.GIT",
        "ssh://git@github.com:22/ajmarkow/nix-components.git",
    ]

    def test_all_forms_post(self):
        for origin in self.FORMS:
            with self.subTest(origin=origin):
                stub = run_hook(StubAPI(), origin, CLEAN_PROMPT)
                self.assertEqual(len(stub.posts), 1, origin)


class TestUnusableIdentity(unittest.TestCase):
    """Outside a work tree, no origin, unparseable URL, file:// -> no POST."""

    def test_no_work_tree_no_post(self):
        stub = run_hook(StubAPI(), None, CLEAN_PROMPT)
        self.assertEqual(stub.posts, [])

    def test_local_path_origin_no_post(self):
        stub = run_hook(StubAPI(), "/home/user/nix-components", CLEAN_PROMPT)
        self.assertEqual(stub.posts, [])

    def test_file_origin_no_post(self):
        stub = run_hook(StubAPI(), "file:///home/user/nix-components",
                        CLEAN_PROMPT)
        self.assertEqual(stub.posts, [])

    def test_unparseable_origin_no_post(self):
        stub = run_hook(StubAPI(), "not a url at all !!!", CLEAN_PROMPT)
        self.assertEqual(stub.posts, [])


class TestNearMisses(unittest.TestCase):
    """A fork under another owner, the same owner/name on another host,
    and prefix/suffix names must not match."""

    def test_fork_other_owner_no_post(self):
        stub = run_hook(StubAPI(),
                        "git@github.com:forkowner/nix-components.git",
                        CLEAN_PROMPT)
        self.assertEqual(stub.posts, [])

    def test_same_name_other_host_no_post(self):
        stub = run_hook(StubAPI(),
                        "git@gitlab.example.com:ajmarkow/nix-components.git",
                        CLEAN_PROMPT)
        self.assertEqual(stub.posts, [])

    def test_prefix_name_no_post(self):
        stub = run_hook(StubAPI(),
                        "git@github.com:ajmarkow/nix-components-extra.git",
                        CLEAN_PROMPT)
        self.assertEqual(stub.posts, [])

    def test_suffix_name_no_post(self):
        stub = run_hook(StubAPI(),
                        "git@github.com:ajmarkow/my-nix-components.git",
                        CLEAN_PROMPT)
        self.assertEqual(stub.posts, [])


class TestPublicQueueRefused(unittest.TestCase):
    """Queue repo reporting private:false -> no POST, no override."""

    def test_public_queue_no_post(self):
        stub = run_hook(StubAPI(private=False), ALLOW_ORIGIN, CLEAN_PROMPT)
        self.assertEqual(stub.posts, [])


class TestEmittedItem(unittest.TestCase):
    """The posted item carries an id and no absolute path in any field."""

    def test_item_has_id_and_no_paths(self):
        stub = run_hook(StubAPI(), ALLOW_ORIGIN, CLEAN_PROMPT)
        self.assertEqual(len(stub.posts), 1)
        path, payload = stub.posts[0]
        self.assertIn("/issues/7/comments", path)
        body = payload["body"]
        self.assertTrue(body.startswith("```json\n"))
        item = json.loads(body[len("```json\n"):].split("```")[0])
        self.assertIn("id", item)
        self.assertEqual(item["status"], "pending")
        for key, value in item.items():
            self.assertNotIn("/home/", str(value), f"field {key}")
            self.assertNotIn("/Users/", str(value), f"field {key}")
            self.assertNotIn(os.getcwd(), str(value), f"field {key}")

    def test_non_correction_posts_nothing(self):
        stub = run_hook(StubAPI(), ALLOW_ORIGIN, "Hello, how are you?")
        self.assertEqual(stub.posts, [])


if __name__ == "__main__":
    unittest.main()
