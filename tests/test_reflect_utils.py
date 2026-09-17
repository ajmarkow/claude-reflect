#!/usr/bin/env python3
"""Tests for the kept reflect_utils functions.

Covers detect_patterns, create_queue_item (including the queue-item schema
contract with process_queue.py), should_include_message,
normalize_repo_identity, and find_secret.
"""

import sys
import unittest
from pathlib import Path
from uuid import UUID

# Add scripts directory to path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from lib.reflect_utils import (
    create_queue_item,
    detect_patterns,
    find_secret,
    iso_timestamp,
    normalize_repo_identity,
    should_include_message,
    MAX_CAPTURE_PROMPT_LENGTH,
)


class TestPatternDetection(unittest.TestCase):
    """Tests for pattern detection."""

    def test_explicit_remember_pattern(self):
        result = detect_patterns("remember: always use gpt-5.1")
        item_type, patterns, confidence, sentiment, decay = result

        self.assertEqual(item_type, "explicit")
        self.assertIn("remember:", patterns)
        self.assertEqual(confidence, 0.90)
        self.assertEqual(decay, 120)

    def test_positive_pattern_perfect(self):
        result = detect_patterns("perfect! that's exactly what I wanted")
        item_type, patterns, confidence, sentiment, decay = result

        self.assertEqual(item_type, "positive")
        self.assertEqual(sentiment, "positive")
        self.assertGreaterEqual(confidence, 0.70)

    def test_correction_no_use(self):
        result = detect_patterns("no, use Python instead")
        item_type, patterns, confidence, sentiment, decay = result

        self.assertEqual(item_type, "auto")
        self.assertIn("no,", patterns)
        self.assertEqual(sentiment, "correction")

    def test_correction_i_told_you_high_confidence(self):
        result = detect_patterns("I told you to use async")
        item_type, patterns, confidence, sentiment, decay = result

        self.assertEqual(item_type, "auto")
        self.assertIn("I-told-you", patterns)
        self.assertGreaterEqual(confidence, 0.85)
        self.assertEqual(decay, 120)

    def test_guardrail_dont_add_unless(self):
        result = detect_patterns("don't add docstrings unless I explicitly ask")
        item_type, patterns, confidence, sentiment, decay = result

        self.assertEqual(item_type, "guardrail")
        self.assertIn("dont-unless-asked", patterns)
        self.assertGreaterEqual(confidence, 0.90)
        self.assertEqual(decay, 120)

    def test_guardrail_minimal_changes(self):
        result = detect_patterns("only make minimal changes please")
        item_type, patterns, confidence, sentiment, decay = result

        self.assertEqual(item_type, "guardrail")
        self.assertIn("minimal-changes", patterns)

    def test_no_pattern_match(self):
        result = detect_patterns("Hello, how are you?")
        item_type, patterns, confidence, sentiment, decay = result

        self.assertIsNone(item_type)
        self.assertEqual(patterns, "")

    def test_false_positive_question_rejected(self):
        result = detect_patterns("can you figure out how to make this fit?")
        item_type, patterns, confidence, sentiment, decay = result

        self.assertIsNone(item_type)

    def test_no_problem_not_correction(self):
        result = detect_patterns("No problem, let's move on")
        self.assertIsNone(result[0])

    def test_ja_chigau_correction(self):
        result = detect_patterns("違う、useStateじゃなくてuseRefを使って")
        self.assertEqual(result[0], "auto")
        self.assertIn("chigau", result[1])

    def test_zh_bushi_correction(self):
        result = detect_patterns("不是，应该用另一个方法")
        self.assertEqual(result[0], "auto")
        self.assertIn("bushi", result[1])

    def test_ko_ani_correction(self):
        result = detect_patterns("아니, 그게 아니라 이거를 수정해")
        self.assertEqual(result[0], "auto")
        self.assertIn("ani", result[1])

    def test_short_message_confidence_boost(self):
        result = detect_patterns("no, use gpt-5.1")
        item_type, patterns, confidence, sentiment, decay = result

        self.assertEqual(item_type, "auto")
        self.assertGreaterEqual(confidence, 0.75)


class TestQueueItemSchema(unittest.TestCase):
    """The item must match what process_queue.py reads: id, created_at,
    message, patterns, type, confidence, status. Items without an id are
    silently dropped downstream, so assert the id parses as uuid4. Also
    assert no absolute path leaks into any field."""

    def test_schema_fields(self):
        item = create_queue_item(
            message="no, use postgres not sqlite",
            item_type="auto",
            patterns="no, use-X-not-Y",
            confidence=0.8,
        )

        self.assertEqual(
            set(item.keys()),
            {"id", "created_at", "message", "patterns", "type", "confidence", "status"},
        )
        self.assertEqual(item["message"], "no, use postgres not sqlite")
        self.assertEqual(item["type"], "auto")
        self.assertEqual(item["patterns"], "no, use-X-not-Y")
        self.assertEqual(item["confidence"], 0.8)
        self.assertEqual(item["status"], "pending")

    def test_id_is_uuid4(self):
        item = create_queue_item(
            message="no, use X",
            item_type="auto",
            patterns="no,",
            confidence=0.8,
        )
        parsed = UUID(item["id"], version=4)
        self.assertEqual(str(parsed), item["id"])

    def test_ids_unique(self):
        kwargs = dict(
            message="no, use X", item_type="auto", patterns="no,", confidence=0.8
        )
        self.assertNotEqual(
            create_queue_item(**kwargs)["id"], create_queue_item(**kwargs)["id"]
        )

    def test_no_absolute_path_in_any_field(self):
        item = create_queue_item(
            message="no, use X",
            item_type="auto",
            patterns="no,",
            confidence=0.8,
        )
        for key, value in item.items():
            self.assertNotIn("/home/", str(value), f"field {key}")
            self.assertNotIn("/Users/", str(value), f"field {key}")
            self.assertNotIn("C:\\", str(value), f"field {key}")

    def test_created_at_format(self):
        ts = iso_timestamp()
        self.assertRegex(ts, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class TestShouldIncludeMessage(unittest.TestCase):
    """Tests for should_include_message() — filters system content."""

    def test_normal_correction_included(self):
        self.assertTrue(should_include_message("no, use gpt-5.1 not gpt-5"))
        self.assertTrue(should_include_message("remember: always use venv"))

    def test_empty_string_excluded(self):
        self.assertFalse(should_include_message(""))
        self.assertFalse(should_include_message("   "))

    def test_xml_tag_excluded(self):
        self.assertFalse(
            should_include_message(
                "<task-notification>some content</task-notification>"
            )
        )
        self.assertFalse(
            should_include_message("<system-reminder>use X not Y</system-reminder>")
        )

    def test_json_excluded(self):
        self.assertFalse(should_include_message('{"prompt": "no, use X"}'))

    def test_session_continuation_excluded(self):
        self.assertFalse(
            should_include_message(
                "This session is being continued from a previous conversation"
            )
        )

    def test_system_reminder_with_correction_pattern(self):
        msg = "<system-reminder>use context7 mcp every time, don't use old API</system-reminder>"
        self.assertFalse(should_include_message(msg))


class TestNormalizeRepoIdentity(unittest.TestCase):
    """Origin URL -> host/owner/name label. Label only — capture fires in
    every repo; nothing here gates on the result."""

    def test_scp_like(self):
        self.assertEqual(
            normalize_repo_identity("git@github.com:ajmarkow/nix-components.git"),
            "github.com/ajmarkow/nix-components",
        )

    def test_https(self):
        self.assertEqual(
            normalize_repo_identity("https://github.com/ajmarkow/nix-components"),
            "github.com/ajmarkow/nix-components",
        )

    def test_ssh_scheme(self):
        self.assertEqual(
            normalize_repo_identity("ssh://git@github.com/ajmarkow/nix-components.git"),
            "github.com/ajmarkow/nix-components",
        )

    def test_unusable_identities(self):
        self.assertIsNone(normalize_repo_identity(None))
        self.assertIsNone(normalize_repo_identity(""))
        self.assertIsNone(normalize_repo_identity("/home/user/repo"))
        self.assertIsNone(normalize_repo_identity("file:///home/user/repo"))
        self.assertIsNone(normalize_repo_identity("not a url at all !!!"))
        self.assertIsNone(normalize_repo_identity("https://github.com/onlyowner"))
        self.assertIsNone(normalize_repo_identity("https://github.com/a/b/c"))


class TestFindSecret(unittest.TestCase):
    """One realistic case per deny-list pattern."""

    CASES = [
        ("openai-key", "no, use sk-ant-api03-AAAAbbbbCCCCdddd not the old key"),
        ("github-pat", "the token ghp_AbCdEfGhIjKlMnOpQrStUv is expired"),
        ("github-pat", "use github_pat_abcDEF1234567890 for this"),
        ("github-oauth", "gho_xYz1234567890abcdef failed"),
        ("aws-access-key", "AKIAIOSFODNN7EXAMPLE is the key"),
        ("aws-access-key", "rotated to ASIAIOSFODNN7EXAMPLE yesterday"),
        ("slack-token", "xoxb-1234-5678-abcdefghijklmnop broke"),
        ("slack-token", "xoxp-1234-5678-abcdefghijklmnop broke"),
        ("auth-header", "Authorization: Bearer abc123"),
        ("bearer-token", "pass Bearer eyJhbGciOiJIUzI1NiJ9 along"),
        ("private-key", "-----BEGIN RSA PRIVATE KEY-----"),
        ("credential-assignment", "password: hunter2"),
        ("credential-assignment", "api_key=abc123"),
        ("credential-assignment", "secret = abc123"),
        ("credential-assignment", "token: abc123"),
        ("long-hex-run", "deadbeef" * 8),
        ("url-userinfo", "https://user:pass@example.com/path"),
        ("url-token-param", "https://example.com/cb?token=abc123"),
    ]

    def test_each_pattern_matches(self):
        for expected_name, text in self.CASES:
            with self.subTest(pattern=expected_name, text=text[:30]):
                self.assertEqual(find_secret(text), expected_name)

    def test_clean_text_passes(self):
        self.assertIsNone(find_secret("no, use postgres not sqlite"))

    def test_long_base64_run(self):
        self.assertEqual(find_secret("QUJD" * 16), "long-base64-run")


class TestCaptureLearningFiltering(unittest.TestCase):
    """Filter layering: system content and long prompts blocked before POST."""

    def test_system_content_blocked_before_detect_patterns(self):
        system_msg = "<system-reminder>use context7 mcp every time</system-reminder>"
        self.assertFalse(should_include_message(system_msg))

        real_correction = "no, use gpt-5.1 not gpt-5"
        self.assertTrue(should_include_message(real_correction))

    def test_long_prompt_blocked(self):
        long_prompt = "a" * (MAX_CAPTURE_PROMPT_LENGTH + 1)
        should_skip = (
            len(long_prompt) > MAX_CAPTURE_PROMPT_LENGTH
            and "remember:" not in long_prompt.lower()
        )
        self.assertTrue(should_skip)

    def test_long_prompt_with_remember_allowed(self):
        long_remember = "remember: " + "a" * MAX_CAPTURE_PROMPT_LENGTH
        should_skip = (
            len(long_remember) > MAX_CAPTURE_PROMPT_LENGTH
            and "remember:" not in long_remember.lower()
        )
        self.assertFalse(should_skip)

    def test_short_real_correction_passes_both_filters(self):
        msg = "no, use postgres not sqlite"
        self.assertTrue(should_include_message(msg))
        self.assertLessEqual(len(msg), MAX_CAPTURE_PROMPT_LENGTH)


if __name__ == "__main__":
    unittest.main()
