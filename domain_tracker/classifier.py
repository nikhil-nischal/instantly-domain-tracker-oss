"""Email classifier — human vs auto-reply detection.

Combines i_status and subject-line analysis to classify received emails
as human_reply, auto_reply, or unclassified.

Heuristics derived from live API samples:
  - "Automatic reply:" subject prefix → auto_reply
  - i_status in (-1, 1, 2) → human
  - Non-standard auto-reply keywords (retired, out of office, etc.) → auto_reply
  - i_status=0 is ambiguous → unclassified
  - i_status=-2 is mixed → unclassified
  - Emails with no i_status fall back to subject-only classification
"""

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AUTO_REPLY_KEYWORDS = [
    "out of office",
    "office changes",
    "retired",
    "no longer with",
    "auto-reply",
    "autoresponder",
    "automated reply",
    "has left",
    "is no longer",
]

# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


def classify_email(item: dict) -> str:
    """Classify a received email as human_reply, auto_reply, or unclassified.

    Args:
        item: A received email object from the Instantly API, expected to
              contain at minimum a ``subject`` key (str) and optionally an
              ``i_status`` key (int or None).

    Returns:
        One of:
          - ``"auto_reply"``   — confidently an automated/out-of-office reply
          - ``"human_reply"``  — confidently a human reply
          - ``"unclassified"`` — uncertain or missing signals
    """
    subject = (item.get("subject") or "").strip()
    i_status = item.get("i_status")

    # --- Rule 1: Standard auto-reply subject prefix ---
    # "Automatic reply:" is the most reliable single classifier (100% auto in sample)
    if subject.lower().startswith("automatic reply:"):
        return "auto_reply"

    # --- Rule 2: Non-standard auto-reply keywords ---
    # Covers retirement notices, out-of-office, departure, etc.
    if any(kw in subject.lower() for kw in AUTO_REPLY_KEYWORDS):
        return "auto_reply"

    # --- Rule 3: Confident human replies via i_status ---
    # i_status = -1, 1, 2 are 100% human in sampled data
    if i_status in (-1, 1, 2):
        return "human_reply"

    # --- Default: uncertain ---
    # i_status=0 (mixed), i_status=-2 (mixed), or no i_status at all
    return "unclassified"
