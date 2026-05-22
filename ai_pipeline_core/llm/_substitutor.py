"""Content shortener for URLs and high-entropy strings.

Two-tier substitution system:
- Tier 1: Crypto/encoded values (hex, base58, base64) → prefix...suffix (10+10)
- Tier 2: URLs > 80 chars after Tier 1 → prefix...suffix (50+15) with hard cap

Uses `...` (three dots) as truncation marker. LLMs preserve this format naturally
because it matches standard truncation in block explorers and documentation.

Round-trip: prepare() → substitute() → LLM → restore().
"""

import logging
import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ── Zero-width / invisible characters ──────────────────────────────────────────
_INVISIBLE_CHARS = frozenset("\u200b\u200c\u200d\ufeff\u2060\u180e")

# ── Tier 1 config ─────────────────────────────────────────────────────────────
_T1_PREFIX = 10
_T1_SUFFIX = 10
_T1_MIN_LENGTH = 66

# ── Tier 2 URL config ─────────────────────────────────────────────────────────
_URL_THRESHOLD = 80
_URL_PREFIX_LEN = 50
_URL_SUFFIX_LEN = 15
_URL_TARGET_LEN = _URL_PREFIX_LEN + 3 + _URL_SUFFIX_LEN  # 68
_URL_MAX_COLLISION_ATTEMPTS = (_URL_THRESHOLD - _URL_TARGET_LEN) // 2  # 6

# ── Tier 1 detection patterns (most specific → least specific) ─────────────────
_T1_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("hex_prefixed", re.compile(r"\b0x[a-fA-F0-9]{64,}\b")),
    ("hex", re.compile(r"\b[a-fA-F0-9]{66,}\b")),
    ("base64_padded", re.compile(r"\b[A-Za-z0-9+/]{64,}={1,2}\b")),
    ("base64_unpadded", re.compile(r"\b[A-Za-z0-9+/]{66,}\b")),
    ("base58", re.compile(r"\b[123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz]{66,}\b")),
    ("high_entropy", re.compile(r"\b[A-Za-z0-9]{66,}\b")),
]

# Per-pattern entropy and diversity thresholds
_T1_THRESHOLDS: dict[str, tuple[float, int]] = {
    "hex_prefixed": (3.0, 8),
    "hex": (3.0, 8),
    "base64_padded": (3.5, 10),
    "base64_unpadded": (3.5, 12),
    "base58": (3.5, 12),
    "high_entropy": (3.5, 12),
}

# ── Other patterns ─────────────────────────────────────────────────────────────
_URL_PATTERN = re.compile(r"https?://[^\s<>\"'`\[\]{}|\\^]+", re.IGNORECASE)
_PATH_PATTERN = re.compile(r"/[a-zA-Z0-9_\-\./]+")
_HEX_IN_BROADER = re.compile(r"0x[a-fA-F0-9]{40,}")

# ── Protection patterns ───────────────────────────────────────────────────────
_JWT_PATTERN = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_CONTENT_ADDRESSED_SCHEMES = ("ipfs://", "ipns://", "magnet:", "data:")
_CODE_KEYWORDS = ("install ", "npm ", "yarn ", "pnpm ", "pip ", "uv ", "uvx ", "require(", "import ")
_DELIMITER_PAIRS: dict[str, str] = {"(": ")", "[": "]", '"': '"', "'": "'", ">": "<"}

# ── Fuzzy restore config ─────────────────────────────────────────────────────
_FUZZY_BEFORE_CONTEXT = _URL_PREFIX_LEN + _URL_MAX_COLLISION_ATTEMPTS * 2 + 5  # 67
_FUZZY_AFTER_CONTEXT = 20  # > max suffix (_URL_SUFFIX_LEN = 15)
_FUZZY_MIN_PREFIX_MATCH = 8
_FUZZY_BOUNDARY_CHARS = frozenset(" \t\n\r\"'`),;:!?]}>\\&#/")
_FUZZY_DOTS_RE = re.compile(r"\.{3,}")

# ── Helper functions ───────────────────────────────────────────────────────────


def _normalize(s: str) -> str:
    """Remove invisible chars for pattern matching (not from source text)."""
    if not any(c in s for c in _INVISIBLE_CHARS):
        return s
    return "".join(c for c in s if c not in _INVISIBLE_CHARS)


def _build_normalized_view(text: str) -> tuple[str, list[int] | None]:
    """Build normalized text and index map for pattern detection.

    Returns (normalized_text, index_map) where index_map[norm_idx] = orig_idx.
    If no invisible chars, returns (text, None) for fast path.
    """
    if not any(c in text for c in _INVISIBLE_CHARS):
        return text, None
    chars: list[str] = []
    mapping: list[int] = []
    for i, c in enumerate(text):
        if c not in _INVISIBLE_CHARS:
            chars.append(c)
            mapping.append(i)
    mapping.append(len(text))
    return "".join(chars), mapping


def _map_span(index_map: list[int] | None, start: int, end: int) -> tuple[int, int]:
    if index_map is None:
        return start, end
    return index_map[start], index_map[end]


def _entropy(s: str) -> float:
    if not s:
        return 0.0
    counts = Counter(s)
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def _trim_url(url: str) -> str:
    """Trim trailing punctuation from URL, preserving balanced brackets."""
    while url and url[-1] in ".,;:!?)]}'\"'`":
        if url[-1] == ")" and url.count("(") >= url.count(")"):
            break
        if url[-1] == "]" and url.count("[") >= url.count("]"):
            break
        url = url[:-1]
    return url


def _overlaps_any(start: int, end: int, claimed: list[tuple[int, int]]) -> bool:
    return any(start < ce and end > cs for cs, ce in claimed)


def _is_in_content_addressed_url(context_before: str) -> bool:
    lower = context_before.lower()
    for scheme in _CONTENT_ADDRESSED_SCHEMES:
        if scheme in lower:
            pos = lower.rfind(scheme)
            if not any(c in context_before[pos:] for c in " \t\n\r"):
                return True
    return False


def _is_valid_path(value: str, ctx_before: str, ctx_after: str) -> bool:
    if value.startswith("//") or len(value) < 30 or value.count("/") < 3:
        return False
    ctx = _normalize(ctx_before)
    ctx_a = _normalize(ctx_after)
    if not ctx or not ctx_a:
        return False
    return ctx[-1] in _DELIMITER_PAIRS and _DELIMITER_PAIRS[ctx[-1]] == ctx_a[0]


def _is_valid_t1_pattern(value: str, kind: str, ctx_before: str, ctx_after: str) -> bool:
    """Validate a Tier 1 pattern using entropy, diversity, and context checks."""
    if _is_in_content_addressed_url(ctx_before):
        return False
    if kind not in _T1_THRESHOLDS:
        return True
    min_ent, min_div = _T1_THRESHOLDS[kind]
    if _entropy(value) < min_ent or len(set(value)) < min_div:
        return False

    ctx_b = _normalize(ctx_before)
    ctx_a = _normalize(ctx_after)

    if kind == "high_entropy":
        if ctx_b.rstrip().endswith("@"):
            return False
        if any(kw in ctx_b.lower() for kw in _CODE_KEYWORDS):
            return False
        if ctx_b.endswith(".") or ctx_a.startswith("."):
            return False
        if value.isalpha() and len(value) < 40:
            return False
    if kind == "base64_unpadded" and "/" in value and "+" not in value:
        return False
    if kind in {"base64_unpadded", "base64_padded", "base58"}:
        hi = _HEX_IN_BROADER.search(value)
        if hi and hi.group() != value:
            return False
    return not (kind == "base64_padded" and (ctx_b.endswith(".") or ctx_a.startswith(".")))


# ── URLSubstitutor ─────────────────────────────────────────────────────────────


@dataclass(slots=True)
class URLSubstitutor:
    """Two-tier content shortener for LLM context.

    Tier 1: High-entropy strings (addresses, hashes, base64) → prefix...suffix
    Tier 2: Long URLs (> 80 chars after Tier 1) → prefix...suffix with hard cap
    """

    # Unified forward/reverse for all mapping types
    _forward: dict[str, str] = field(default_factory=dict)
    _reverse: dict[str, str] = field(default_factory=dict)

    # Tier 1 collision state
    _t1_canonical: dict[str, str] = field(default_factory=dict)
    _t1_pairs: set[tuple[str, str]] = field(default_factory=set)

    # Tier 2 URL collision state
    _url_prefixes: set[str] = field(default_factory=set)
    _url_suffixes: set[str] = field(default_factory=set)

    # Fuzzy restore entries: (prefix_lower, suffix_lower, original)
    _fuzzy_entries: list[tuple[str, str, str]] = field(default_factory=list)

    _prepared: bool = False

    @property
    def is_prepared(self) -> bool:
        return self._prepared

    @property
    def pattern_count(self) -> int:
        return len(self._forward)

    def get_mappings(self) -> dict[str, str]:
        """Return a copy of all original → shortened mappings."""
        return dict(self._forward)

    # ── Tier 1 ──────────────────────────────────────────────────────────────

    def _t1_has_conflict(self, abbr: str) -> bool:
        """Check if abbreviation collides with an existing one."""
        parts = abbr.lower().split("...")
        if len(parts) != 2:
            return True
        return abbr.lower() in self._reverse or tuple(parts) in self._t1_pairs

    def _t1_register(self, canonical: str, original: str, abbr: str) -> None:
        parts = abbr.lower().split("...")
        self._t1_canonical[canonical] = abbr
        self._t1_pairs.add((parts[0], parts[1]))
        self._forward[original] = abbr
        if abbr.lower() not in self._reverse:
            self._reverse[abbr.lower()] = original
            self._fuzzy_entries.append((parts[0], parts[1], original))

    def _abbreviate_t1(self, original: str) -> str | None:
        """Abbreviate a high-entropy value. Returns abbreviated form or None."""
        normalized = _normalize(original)
        if len(normalized) < _T1_MIN_LENGTH:
            return None

        canonical = normalized.lower()
        if canonical in self._t1_canonical:
            abbr = self._t1_canonical[canonical]
            if original not in self._forward:
                self._forward[original] = abbr
            return abbr

        # Widening collision resolution: try prefix/suffix = 10/10, 11/11, 12/12, 13/13
        for extra in range(4):
            p, s = _T1_PREFIX + extra, _T1_SUFFIX + extra
            if p + 3 + s >= len(normalized):
                return None
            abbr = f"{normalized[:p]}...{normalized[-s:]}"
            if not self._t1_has_conflict(abbr):
                self._t1_register(canonical, original, abbr)
                return abbr

        return None

    # ── Tier 2 (URLs) ──────────────────────────────────────────────────────

    def _apply_tier1_to_url(self, url: str) -> str:
        """Find Tier 1 patterns in URL, abbreviate them, return modified URL."""
        found: list[tuple[str, str]] = []
        for kind, pattern in _T1_PATTERNS:
            for m in pattern.finditer(url):
                value = m.group()
                normalized = _normalize(value)
                if len(normalized) < _T1_MIN_LENGTH:
                    continue
                canonical = normalized.lower()
                if canonical not in self._t1_canonical:
                    if kind in _T1_THRESHOLDS:
                        min_ent, min_div = _T1_THRESHOLDS[kind]
                        if _entropy(normalized) < min_ent or len(set(normalized)) < min_div:
                            continue
                    if kind == "base64_unpadded" and "/" in normalized and "+" not in normalized:
                        continue
                    if kind in {"base64_unpadded", "base64_padded", "base58"}:
                        hi = _HEX_IN_BROADER.search(value)
                        if hi and hi.group() != value:
                            continue
                    abbr = self._abbreviate_t1(value)
                    if abbr:
                        found.append((canonical, abbr))
                else:
                    found.append((canonical, self._t1_canonical[canonical]))

        if not found:
            return url

        result = url
        for canonical, abbr in sorted(found, key=lambda x: len(x[0]), reverse=True):
            lower = result.lower()
            idx = lower.find(canonical)
            while idx >= 0:
                result = result[:idx] + abbr + result[idx + len(canonical) :]
                lower = result.lower()
                idx = lower.find(canonical, idx + len(abbr))
        return result

    def _url_has_conflict(self, abbr: str) -> bool:
        parts = abbr.lower().split("...")
        if len(parts) != 2:
            return True
        p, s = parts
        return abbr.lower() in self._reverse or (p in self._url_prefixes and s in self._url_suffixes)

    def _shorten_url_tier2(self, url: str) -> str | None:
        """Tier 2: prefix...suffix with hard cap at _URL_THRESHOLD."""
        if len(url) <= _URL_TARGET_LEN:
            return None

        for attempt in range(_URL_MAX_COLLISION_ATTEMPTS + 1):
            adj_prefix = _URL_PREFIX_LEN + attempt * 2
            if adj_prefix + 3 + _URL_SUFFIX_LEN >= len(url):
                return None

            abbr = f"{url[:adj_prefix]}...{url[-_URL_SUFFIX_LEN:]}"
            if not self._url_has_conflict(abbr):
                parts = abbr.lower().split("...")
                self._url_prefixes.add(parts[0])
                self._url_suffixes.add(parts[1])
                self._forward[url] = abbr
                self._reverse[abbr.lower()] = url
                self._fuzzy_entries.append((parts[0], parts[1], url))
                return abbr

        return None

    def _shorten_url(self, original: str) -> str:
        """Full URL pipeline: Tier 1 inside URL → threshold check → Tier 2."""
        if original in self._forward:
            return self._forward[original]

        if len(_normalize(original)) < 40:
            return original

        # Apply Tier 1 abbreviations inside the URL
        modified = self._apply_tier1_to_url(original)

        if modified != original and len(modified) <= _URL_THRESHOLD:
            self._forward[original] = modified
            self._reverse[modified.lower()] = original
            return modified

        # Still too long — Tier 2 on the ORIGINAL (avoids nested ... patterns)
        result = self._shorten_url_tier2(original)
        if result:
            return result

        # Tier 2 cap hit — fall back to Tier 1 form if it helped at all
        if modified != original:
            self._forward[original] = modified
            self._reverse[modified.lower()] = original
            return modified

        return original

    # ── Paths ──────────────────────────────────────────────────────────────

    def _shorten_path(self, original: str) -> str:
        if original in self._forward:
            return self._forward[original]

        normalized = _normalize(original)
        segments = [s for s in normalized.split("/") if s]
        if len(segments) < 2:
            return original

        last = segments[-1]
        for prefix_count in range(1, len(segments)):
            prefix_segs = segments[:prefix_count]
            short = f"/{'/'.join(prefix_segs)}/.../{last}"
            if short.lower() not in self._reverse:
                self._forward[original] = short
                self._reverse[short.lower()] = original
                return short

        return original

    # ── Public API ─────────────────────────────────────────────────────────

    def prepare(self, texts: Sequence[str]) -> None:
        """Pre-process texts to build substitution mappings."""
        for text in texts:
            if text:
                self._scan(text)
        self._prepared = True

    def _scan(self, text: str) -> None:
        """Pre-scan text to populate mappings. Same logic as substitute(), result discarded."""
        self.substitute(text)

    def substitute(self, text: str) -> str:
        """Replace patterns with shortened forms."""
        if not text:
            return text

        normalized, index_map = _build_normalized_view(text)
        jwt_spans = [(m.start(), m.end()) for m in _JWT_PATTERN.finditer(normalized)]
        replacements: list[tuple[int, int, str]] = []

        # Phase 1: URLs
        url_spans: list[tuple[int, int]] = []
        for m in _URL_PATTERN.finditer(normalized):
            url_text = _trim_url(m.group())
            url_start, url_end = m.start(), m.start() + len(url_text)
            orig_s, orig_e = _map_span(index_map, url_start, url_end)
            original = text[orig_s:orig_e]

            if original not in self._forward:
                self._shorten_url(original)
            if original in self._forward and self._forward[original] != original:
                replacements.append((orig_s, orig_e, self._forward[original]))
                url_spans.append((url_start, url_end))

        # Phase 2: Tier 1 patterns
        claimed: list[tuple[int, int]] = list(url_spans)
        for kind, pattern in _T1_PATTERNS:
            for m in pattern.finditer(normalized):
                s, e = m.start(), m.end()
                if _overlaps_any(s, e, claimed):
                    continue
                if any(js <= s < je for js, je in jwt_spans):
                    continue

                orig_s, orig_e = _map_span(index_map, s, e)
                original = text[orig_s:orig_e]

                if original not in self._forward:
                    ctx_b = text[max(0, orig_s - 200) : orig_s]
                    ctx_a = text[orig_e : min(len(text), orig_e + 30)]
                    if _is_valid_t1_pattern(m.group(), kind, ctx_b, ctx_a):
                        self._abbreviate_t1(original)

                if original in self._forward and self._forward[original] != original:
                    replacements.append((orig_s, orig_e, self._forward[original]))
                    claimed.append((s, e))

        # Phase 3: Paths
        for m in _PATH_PATTERN.finditer(normalized):
            s, e = m.start(), m.end()
            if _overlaps_any(s, e, url_spans):
                continue
            orig_s, orig_e = _map_span(index_map, s, e)
            original = text[orig_s:orig_e]
            if original not in self._forward:
                ctx_b = text[max(0, orig_s - 30) : orig_s]
                ctx_a = text[orig_e : min(len(text), orig_e + 30)]
                if _is_valid_path(m.group(), ctx_b, ctx_a):
                    self._shorten_path(original)
            if original in self._forward and self._forward[original] != original:
                replacements.append((orig_s, orig_e, self._forward[original]))

        return self._apply_replacements(text, replacements)

    @staticmethod
    def _apply_replacements(text: str, replacements: list[tuple[int, int, str]]) -> str:
        if not replacements:
            return text
        replacements.sort(key=lambda x: (x[0], -(x[1] - x[0])))
        filtered: list[tuple[int, int, str]] = []
        last_end = 0
        for start, end, short in replacements:
            if start >= last_end:
                filtered.append((start, end, short))
                last_end = end
        result = text
        for start, end, short in reversed(filtered):
            result = result[:start] + short + result[end:]
        return result

    def _fuzzy_restore(self, text: str) -> str:
        """Fuzzy fallback: restore when LLM drops suffix or truncates prefix/suffix by 1-2 chars."""
        if not self._fuzzy_entries:
            return text

        replacements: list[tuple[int, int, str]] = []

        for m in _FUZZY_DOTS_RE.finditer(text):
            dot_start = m.start()
            dot_end = m.end()

            before = text[max(0, dot_start - _FUZZY_BEFORE_CONTEXT) : dot_start]
            before_lower = before.lower()
            after = text[dot_end : min(len(text), dot_end + _FUZZY_AFTER_CONTEXT)]
            after_lower = after.lower()

            candidates: list[tuple[int, int, str]] = []

            for prefix_lower, suffix_lower, original in self._fuzzy_entries:
                # ── Prefix check: exact, -1 char, or -2 chars ──
                prefix_match_len = 0
                if before_lower.endswith(prefix_lower):
                    prefix_match_len = len(prefix_lower)
                elif len(prefix_lower) >= _FUZZY_MIN_PREFIX_MATCH + 1 and before_lower.endswith(prefix_lower[:-1]):
                    prefix_match_len = len(prefix_lower) - 1
                elif len(prefix_lower) >= _FUZZY_MIN_PREFIX_MATCH + 2 and before_lower.endswith(prefix_lower[:-2]):
                    prefix_match_len = len(prefix_lower) - 2

                if prefix_match_len < _FUZZY_MIN_PREFIX_MATCH:
                    continue

                # ── Suffix check: exact, -1 char, -2 chars, or suffix dropped ──
                suffix_match_len = 0
                suffix_matched = False

                if after_lower.startswith(suffix_lower):
                    suffix_match_len = len(suffix_lower)
                    suffix_matched = True
                elif len(suffix_lower) >= 2 and after_lower.startswith(suffix_lower[1:]):
                    suffix_match_len = len(suffix_lower) - 1
                    suffix_matched = True
                elif len(suffix_lower) >= 3 and after_lower.startswith(suffix_lower[2:]):
                    suffix_match_len = len(suffix_lower) - 2
                    suffix_matched = True
                elif not after or after[0] in _FUZZY_BOUNDARY_CHARS:
                    suffix_matched = True  # suffix dropped by LLM

                if not suffix_matched:
                    continue

                repl_start = dot_start - prefix_match_len
                repl_end = dot_end + suffix_match_len
                candidates.append((repl_start, repl_end, original))

            if len(candidates) == 1:
                replacements.append(candidates[0])
            elif len(candidates) > 1:
                ctx = text[max(0, dot_start - 20) : min(len(text), dot_end + 20)]
                logger.warning(
                    "Ambiguous fuzzy restore: %d candidates at position %d: '%s'", len(candidates), dot_start, ctx
                )

        return self._apply_replacements(text, replacements)

    def restore(self, text: str) -> str:
        """Restore shortened forms to originals. Case-insensitive, handles ... and … .

        First pass: exact match of full shortened forms (case-insensitive).
        Second pass: fuzzy fallback for LLM-mangled forms — suffix dropped,
        prefix/suffix truncated by 1-2 chars, or extra dots (4+).
        """
        if not text or not self._reverse:
            return text

        result = text.replace("\u2026", "...")  # Unicode ellipsis → three dots

        lower_result = result.lower()
        for short in sorted(self._reverse, key=len, reverse=True):
            if short not in lower_result:
                continue
            original = self._reverse[short]
            escaped = re.escape(short)
            result = re.sub(escaped, original.replace("\\", "\\\\"), result, flags=re.IGNORECASE)
            lower_result = result.lower()

        return self._fuzzy_restore(result)
