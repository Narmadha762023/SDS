"""Token usage + estimated cost tracking for the OpenAI API calls this app
makes.

Every chat completion response includes a `usage` object (prompt tokens,
completion tokens, total tokens) that this app was previously just
discarding after reading the answer. This module captures it so a batch
can show a running token total and an estimated dollar cost.

Pricing is a fixed table below, not fetched live -- OpenAI doesn't expose
pricing through the API, so this needs updating by hand if prices change
or a different model is used. Treat the dollar figure as an estimate, not
an invoice-accurate number: it doesn't account for prompt-caching
discounts or any account-specific pricing. For the real, authoritative
number, check platform.openai.com/usage.
"""

# USD per 1,000,000 tokens. Source: openai.com/api/pricing (checked Sep 2026).
MODEL_PRICING = {
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o": {"input": 2.50, "output": 10.00},
}


class UsageTracker:
    """Accumulates token usage across however many API calls make up one
    document -- or a whole batch, by merging per-document trackers into one.
    """

    def __init__(self):
        self.calls = []  # each: {model, prompt_tokens, completion_tokens, total_tokens}

    def record(self, response, model: str) -> None:
        """Pull the usage object off a chat.completions.create() response."""
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        self.calls.append({
            "model": model,
            "prompt_tokens": usage.prompt_tokens,
            "completion_tokens": usage.completion_tokens,
            "total_tokens": usage.total_tokens,
        })

    def merge(self, other: "UsageTracker") -> None:
        self.calls.extend(other.calls)

    @property
    def total_tokens(self) -> int:
        return sum(c["total_tokens"] for c in self.calls)

    @property
    def prompt_tokens(self) -> int:
        return sum(c["prompt_tokens"] for c in self.calls)

    @property
    def completion_tokens(self) -> int:
        return sum(c["completion_tokens"] for c in self.calls)

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def estimated_cost_usd(self) -> float:
        """Sum of (tokens / 1e6) * rate per call, using MODEL_PRICING.
        Calls for a model not in the table are silently excluded from the
        total (never guess a rate) -- `has_unpriced_calls` tells the
        caller whether that happened, so the UI can flag it.
        """
        cost = 0.0
        for c in self.calls:
            rates = MODEL_PRICING.get(c["model"])
            if not rates:
                continue
            cost += (c["prompt_tokens"] / 1_000_000) * rates["input"]
            cost += (c["completion_tokens"] / 1_000_000) * rates["output"]
        return cost

    @property
    def has_unpriced_calls(self) -> bool:
        return any(c["model"] not in MODEL_PRICING for c in self.calls)

    def summary(self) -> dict:
        return {
            "total_tokens": self.total_tokens,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "estimated_cost_usd": self.estimated_cost_usd(),
            "call_count": self.call_count,
            "has_unpriced_calls": self.has_unpriced_calls,
        }
