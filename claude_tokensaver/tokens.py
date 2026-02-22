"""Token estimation and formatting."""

try:
    import tiktoken
    _enc = tiktoken.get_encoding("cl100k_base")

    def estimate_tokens(text: str) -> int:
        return len(_enc.encode(text))
except ImportError:
    def estimate_tokens(text: str) -> int:
        return max(1, len(text) // 4)


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)
