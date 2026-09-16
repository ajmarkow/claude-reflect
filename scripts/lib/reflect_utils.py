#!/usr/bin/env python3
"""Shared utilities for the claude-reflect capture hook.

The pattern tables and `detect_patterns` below are retained from
BayramAnnakov/claude-reflect (MIT, Copyright (c) 2025 Bayram Annakov),
upstream v3.1.0. Everything else from upstream (file queue, memory
routing, session scanning, semantic detection) was removed; see PLAN.md.

Kept: `detect_patterns` and its pattern tables, `create_queue_item`,
`should_include_message`, `MAX_CAPTURE_PROMPT_LENGTH` — plus the new
repo-identity normalization and secret deny list the capture hook needs.
All pure logic, no file I/O, no network.
"""
import re
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlsplit


# =============================================================================
# Timestamp utilities
# =============================================================================

def iso_timestamp() -> str:
    """Get current UTC timestamp in ISO 8601 format."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# =============================================================================
# Pattern definitions (from capture-learning.sh)
# =============================================================================

# Explicit marker patterns (highest confidence)
EXPLICIT_PATTERNS = [
    (r"remember:", "remember:", 0.90, 120),  # pattern, name, confidence, decay_days
]

# Positive feedback patterns
POSITIVE_PATTERNS = [
    (r"perfect!|exactly right|that's exactly", "perfect", 0.70, 90),
    (r"that's what I wanted|great approach", "great-approach", 0.70, 90),
    (r"keep doing this|love it|excellent|nailed it", "keep-doing", 0.70, 90),
]

# Correction patterns (conservative set to minimize false positives)
# Format: (regex_pattern, pattern_name, is_strong)
#
# DESIGN NOTES:
# - These patterns are English-centric as a FAST first-pass filter
# - Non-English corrections are caught by semantic filtering during /reflect
# - We use STRUCTURAL signals (length, questions, task requests) for language-agnostic filtering
# - Users can use explicit markers like "remember:" in any language
#
CORRECTION_PATTERNS = [
    (r"^no[,. ]+", "no,", True),  # Starts with "no," - common correction opener
    (r"^don't\b|^do not\b", "don't", True),  # Starts with don't/do not
    (r"^stop\b|^never\b", "stop/never", True),  # Starts with stop/never
    (r"that's (wrong|incorrect)|that is (wrong|incorrect)", "that's-wrong", True),
    (r"^actually[,. ]", "actually", False),  # Starts with "actually"
    (r"^I meant\b|^I said\b", "I-meant/said", True),  # Clarification
    (r"^I told you\b|^I already told\b", "I-told-you", True),  # Higher confidence
    (r"use .{1,30} not\b", "use-X-not-Y", True),  # "use X not Y" - limited gap
]

# Guardrail patterns - "don't do X unless" constraints (highest confidence for corrections)
# These detect user frustrations about Claude making unwanted changes
# Format: (regex_pattern, pattern_name, confidence, decay_days)
GUARDRAIL_PATTERNS = [
    (r"don't (?:add|include|create) .{1,40} unless", "dont-unless-asked", 0.90, 120),
    (r"only (?:change|modify|edit|touch) what I (?:asked|requested|said)", "only-what-asked", 0.90, 120),
    (r"stop (?:refactoring|changing|modifying|editing) (?:unrelated|other|surrounding)", "stop-unrelated", 0.90, 120),
    (r"don't (?:over-engineer|add extra|be too|make unnecessary)", "dont-over-engineer", 0.85, 90),
    (r"don't (?:refactor|reorganize|restructure) (?:unless|without)", "dont-refactor-unless", 0.85, 90),
    (r"leave .{1,30} (?:alone|unchanged|as is)", "leave-alone", 0.85, 90),
    (r"don't (?:add|include) (?:comments|docstrings|type hints|annotations) (?:unless|to code)", "dont-add-annotations", 0.85, 90),
    (r"(?:minimal|minimum|only necessary) changes", "minimal-changes", 0.80, 90),
]

# Structural patterns indicating FALSE POSITIVES (language-agnostic)
# These focus on MESSAGE STRUCTURE rather than specific words
FALSE_POSITIVE_PATTERNS = [
    r"[?\uff1f]$",  # Ends with question mark (ASCII ? or full-width ？)
    r"[\u55ce\u5417\u5462\u304b\uae4c]$",  # Ends with CJK question particle (嗎吗呢か까)
    r"^(please|can you|could you|would you|help me)\b",  # Task request openers
    r"(help|fix|check|review|figure out|set up)\s+(this|that|it|the)\b",  # Task verbs
    r"(error|failed|could not|cannot|can't|unable to)\s+\w+",  # Error descriptions
    r"(is|was|are|were)\s+(not|broken|failing)",  # Bug reports
    r"^I (need|want|would like)\b",  # Task requests
    r"^(ok|okay|alright)[,.]?\s+(so|now|let)",  # Task continuations
]

# English phrases that look like correction openers but are NOT corrections
# Especially important for CJK-mixed text where these appear naturally
NON_CORRECTION_PHRASES = [
    r"^no\s+problem",        # "No problem" - agreement
    r"^no\s+worries",        # "No worries" - agreement
    r"^no\s+need\b",         # "No need" - acknowledgment
    r"^no\s+way\b",          # "No way!" - surprise/exclamation
    r"^don't\s+worry",       # "Don't worry" - reassurance
    r"^don't\s+mind",        # "Don't mind" - agreement
    r"^don't\s+bother",      # "Don't bother" - polite decline
    r"^never\s+mind",        # "Never mind" - dismissal
    r"^stop\s+worrying",     # "Stop worrying" - reassurance
]

# CJK correction patterns (parallel to English CORRECTION_PATTERNS)
# These detect explicit corrections in CJK languages
# Format: (regex_pattern, pattern_name, is_strong)
CJK_CORRECTION_PATTERNS = [
    # Japanese
    (r"^いや[、,.\s]|^いや違", "iya", True),       # いや、〜 / いや違う - "no, ..."
    (r"^違う[、，,.\s！!。]|^ちがう[、,.\s]", "chigau", True),  # 違う、〜 - "wrong, ..."
    (r"そうじゃなく[てけ]|そっちじゃなく[てけ]", "souja-nakute", True),  # "not that"
    (r"間違[いえっ]て", "machigatte", True),       # 間違ってる - "it's wrong"
    (r"じゃなくて.{0,30}にして", "janakute-nishite", True),  # 〜じゃなくて〜にして
    (r"^やめて[。！!]?\s*$", "yamete", True),      # やめて - "stop"
    (r"^そうじゃない", "souja-nai", True),          # そうじゃない - "that's not right"
    (r"って言った[のよでじゃ]", "tte-itta", True),   # って言ったのに - "I told you"
    # Chinese
    (r"^不是[，,. ]", "bushi", True),              # 不是、〜 - "no, ..."
    (r"^错了|^錯了", "cuole", True),               # 错了 - "wrong"
    (r"不要.{0,20}要", "buyao-yao", True),         # 不要X要Y - "don't X, use Y"
    # Korean
    (r"^아니[,. ]", "ani", True),                  # 아니, - "no, ..."
    (r"틀렸", "teullyeoss", True),                 # 틀렸 - "wrong"
]

# Maximum prompt length for live capture (UserPromptSubmit hook)
# Prompts longer than this are almost certainly system content, not user corrections.
# Exception: explicit "remember:" markers are always processed regardless of length.
MAX_CAPTURE_PROMPT_LENGTH = 500

# Maximum message length for weak patterns (structural heuristic)
# Long messages are more likely to be context/tasks than corrections
MAX_WEAK_PATTERN_LENGTH = 150

# Very short messages without question marks are more likely corrections
MIN_SHORT_CORRECTION_LENGTH = 80


def detect_patterns(text: str) -> Tuple[Optional[str], str, float, str, int]:
    """
    Detect patterns in text and return classification.

    Returns:
        Tuple of (type, matched_patterns, confidence, sentiment, decay_days)
        type: "explicit", "positive", "auto", "guardrail", or None
        matched_patterns: Space-separated pattern names
        confidence: 0.0 to 1.0
        sentiment: "correction" or "positive"
        decay_days: Number of days until decay
    """
    # Too short to be actionable (e.g. "OK", "好", "yes")
    # CJK characters carry more meaning per char, so use a lower threshold
    stripped = text.strip()
    has_cjk = bool(re.search(r'[\u3000-\u9fff\uf900-\ufaff\uac00-\ud7af]', stripped))
    short_threshold = 2 if has_cjk else 4
    if len(stripped) <= short_threshold:
        return (None, "", 0.0, "correction", 90)

    # Check for explicit "remember:" - always highest priority
    for pattern, name, confidence, decay in EXPLICIT_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return ("explicit", name, confidence, "correction", decay)

    # Check for guardrail patterns - "don't do X unless" constraints
    # These are high-confidence corrections about unwanted behavior
    for pattern, name, confidence, decay in GUARDRAIL_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return ("guardrail", name, confidence, "correction", decay)

    # Check for FALSE POSITIVE patterns - skip these messages
    for fp_pattern in FALSE_POSITIVE_PATTERNS:
        if re.search(fp_pattern, text, re.IGNORECASE):
            return (None, "", 0.0, "correction", 90)

    # Check for non-correction English phrases (before correction patterns)
    # Prevents "No problem", "Don't worry" etc. from being caught as corrections
    for nc_pattern in NON_CORRECTION_PHRASES:
        if re.search(nc_pattern, text, re.IGNORECASE):
            return (None, "", 0.0, "correction", 90)

    # Check for positive patterns
    matched_positive = []
    for pattern, name, confidence, decay in POSITIVE_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            matched_positive.append(name)

    if matched_positive:
        return ("positive", " ".join(matched_positive), 0.70, "positive", 90)

    # Skip long messages for weak patterns (likely task requests)
    text_length = len(text)

    # Check for CJK correction patterns (language-specific)
    # Use stripped text for anchor patterns (^/$) to handle leading/trailing whitespace
    matched_cjk = []
    cjk_strong = False
    for pattern, name, is_strong in CJK_CORRECTION_PATTERNS:
        if re.search(pattern, stripped):
            matched_cjk.append(name)
            if is_strong:
                cjk_strong = True

    if matched_cjk:
        confidence = 0.75 if cjk_strong else 0.60
        decay_days = 90 if cjk_strong else 60
        if text_length < MIN_SHORT_CORRECTION_LENGTH:
            confidence = min(0.90, confidence + 0.10)
        elif text_length > 300:
            confidence = max(0.50, confidence - 0.15)
        return ("auto", " ".join(matched_cjk), confidence, "correction", decay_days)

    # Check for English correction patterns
    matched_corrections = []
    pattern_count = 0
    has_strong_pattern = False
    has_i_told_you = False

    for pattern, name, is_strong in CORRECTION_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            # Skip weak patterns in long messages
            if not is_strong and text_length > MAX_WEAK_PATTERN_LENGTH:
                continue
            matched_corrections.append(name)
            pattern_count += 1
            if is_strong:
                has_strong_pattern = True
            if name == "I-told-you":
                has_i_told_you = True

    if matched_corrections:
        # Calculate confidence based on pattern count, type, and length
        if has_i_told_you:
            confidence = 0.85
            decay_days = 120
        elif pattern_count >= 3:
            confidence = 0.85
            decay_days = 120
        elif pattern_count >= 2:
            confidence = 0.75
            decay_days = 90
        elif has_strong_pattern:
            confidence = 0.70
            decay_days = 60
        else:
            confidence = 0.55  # Reduced for weak single patterns
            decay_days = 45

        # Adjust confidence based on message length (structural signal)
        # Short messages are more likely to be direct corrections
        if text_length < MIN_SHORT_CORRECTION_LENGTH:
            confidence = min(0.90, confidence + 0.10)  # Boost for short messages
        elif text_length > 300:
            confidence = max(0.50, confidence - 0.15)  # Reduce for long messages
        elif text_length > 150:
            confidence = max(0.55, confidence - 0.10)

        return ("auto", " ".join(matched_corrections), confidence, "correction", decay_days)

    return (None, "", 0.0, "correction", 90)


def create_queue_item(
    message: str,
    item_type: str,
    patterns: str,
    confidence: float,
) -> Dict[str, object]:
    """Create a queue item matching the schema `process_queue.py` reads.

    Items without an `id` are silently dropped downstream, so the id is
    generated here and never omitted. No absolute paths, no sentiment,
    no decay fields — nothing downstream reads them.
    """
    return {
        "id": str(uuid.uuid4()),
        "created_at": iso_timestamp(),
        "message": message,
        "patterns": patterns,
        "type": item_type,
        "confidence": confidence,
        "status": "pending",
    }


def should_include_message(text: str) -> bool:
    """Check if a message should be included in learning detection.

    Filters out system content like XML tags, JSON, tool results, and
    session continuations that should never be treated as user corrections.

    Used by the live capture (UserPromptSubmit hook).
    """
    # Skip empty lines
    if not text.strip():
        return False

    # Skip lines starting with certain patterns
    skip_patterns = [
        r"^<",              # XML tags (<task-notification>, <system-reminder>, etc.)
        r"^\[",             # Brackets
        r"^\{",             # JSON
        r"tool_result",
        r"tool_use_id",
        r"<command-",
        r"<task-notification>",
        r"<system-reminder>",
        r"This session is being continued",
        r"^Analysis:",
        r"^\*\*",           # Bold text
        r"^   -",           # Indented lists
    ]

    for pattern in skip_patterns:
        if re.search(pattern, text):
            return False

    return True


# =============================================================================
# Repo identity: origin URL -> host/owner/name
# =============================================================================

# scp-like syntax: [user@]host:owner/name — urlsplit cannot parse this form.
_SCP_LIKE_RE = re.compile(r"^(?:[^@/]+@)?([^:/\s]+):(.+)$")


def normalize_repo_identity(origin_url: Optional[str]) -> Optional[str]:
    """Normalize a git `origin` URL to an exact `host/owner/name` identity.

    Returns None for anything that does not parse to exactly one
    host/owner/name: local paths, `file://` origins, unparseable URLs,
    non-Git URL schemes.

    Normalization: `urllib.parse.urlsplit` for `https://`/`ssh://` (and
    `http://`/`git://`) forms, a regex for scp-like `git@host:owner/name`.
    Userinfo, port, trailing `/`, and `.git` are stripped; everything is
    lowercased. The host is kept — without it `github.com/o/n` and an
    internal GitLab `o/n` collide.
    """
    if not origin_url or not isinstance(origin_url, str):
        return None
    url = origin_url.strip()
    if not url:
        return None
    if url.lower().startswith("file://"):
        return None

    host: Optional[str] = None
    path = ""
    scp_match = _SCP_LIKE_RE.match(url)
    if scp_match and "://" not in url:
        host = scp_match.group(1).lower()
        path = scp_match.group(2)
    else:
        try:
            parts = urlsplit(url)
        except ValueError:
            return None
        if parts.scheme not in ("http", "https", "ssh", "git"):
            return None
        if not parts.hostname:
            return None
        host = parts.hostname.lower()
        path = parts.path or ""

    path = path.lstrip("/").rstrip("/")
    if path.lower().endswith(".git"):
        path = path[:-4]
    path = path.rstrip("/")
    segments = [seg for seg in path.split("/") if seg]
    if len(segments) != 2:
        return None
    owner, name = segments[0].lower(), segments[1].lower()
    if not host or not owner or not name:
        return None
    return f"{host}/{owner}/{name}"


# =============================================================================
# Secret deny list: drop on any match, do not mask
# =============================================================================

# Each entry is (pattern_name, compiled_regex). `find_secret` returns the
# first matching name, or None. On a hit the hook posts nothing.
DENY_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9_-]{8,}")),
    ("github-pat", re.compile(r"\b(?:ghp_|github_pat_)[A-Za-z0-9_]{8,}")),
    ("github-oauth", re.compile(r"\bgho_[A-Za-z0-9_]{8,}")),
    ("aws-access-key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{8,}")),
    ("slack-token", re.compile(r"\bxox[bpa]-.+[A-Za-z0-9-]{4,}")),
    ("auth-header", re.compile(r"(?i)\bauthorization\s*:")),
    ("bearer-token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/=]{8,}")),
    ("private-key", re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")),
    ("url-userinfo", re.compile(r"(?i)\bhttps?://[^/\s]+@")),
    ("url-token-param",
     re.compile(r"(?i)[?&](?:token|access_token|api_key|apikey|secret|key)=[^&\s]+")),
    ("credential-assignment",
     re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key)\s*[:=]")),
    ("long-hex-run", re.compile(r"\b[0-9a-fA-F]{32,}\b")),
    ("long-base64-run", re.compile(r"[A-Za-z0-9+/]{32,}={0,2}")),
]


def find_secret(text: str) -> Optional[str]:
    """Return the name of the first deny-list pattern matching `text`.

    A hit means the message may carry a credential: the caller must drop
    the message, not mask it. Returns None when the text is clean.
    """
    for name, pattern in DENY_PATTERNS:
        if pattern.search(text):
            return name
    return None
