"""Validation for a user-supplied research topic."""

MAX_TOPIC_LENGTH = 200


def validate_topic(value: str) -> str:
    """Return a trimmed topic or raise ``ValueError`` for invalid input."""
    if not isinstance(value, str):
        raise ValueError("topic must be text")

    topic = value.strip()
    if not topic:
        raise ValueError("topic cannot be blank")
    if len(topic) > MAX_TOPIC_LENGTH:
        raise ValueError(f"topic must be {MAX_TOPIC_LENGTH} characters or fewer")
    return topic
