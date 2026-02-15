import threading
from dataclasses import dataclass, field


@dataclass
class Budget:
    """Tracks LLM usage for a single sandbox session.

    Thread-safe: the bridge server may handle concurrent requests
    from the container (though typical agents are sequential).
    """

    max_calls: int
    max_tokens: int

    _calls: int = field(default=0, init=False, repr=False)
    _prompt_tokens: int = field(default=0, init=False, repr=False)
    _completion_tokens: int = field(default=0, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def check(
        self,
        planned_prompt_tokens: int = 0,
        planned_completion_tokens: int = 0,
    ) -> str | None:
        """Return error message if budget exhausted, None if OK."""
        with self._lock:
            if self._calls >= self.max_calls:
                return (
                    f"Budget exhausted: {self.max_calls} LLM calls used. "
                    "Produce your final answer now."
                )
            total = self._prompt_tokens + self._completion_tokens
            if total >= self.max_tokens:
                return (
                    f"Budget exhausted: {self.max_tokens} tokens used. "
                    "Produce your final answer now."
                )
            planned_prompt = max(0, planned_prompt_tokens)
            planned_completion = max(0, planned_completion_tokens)
            planned_total = planned_prompt + planned_completion
            if total + planned_total > self.max_tokens:
                return (
                    f"Budget exhausted: requested up to {planned_total} planned tokens "
                    f"but only {self.max_tokens - total} tokens remain."
                )
            return None

    def record(
        self,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        calls: int = 1,
    ) -> None:
        """Record usage from one LLM call."""
        with self._lock:
            self._calls += max(0, calls)
            self._prompt_tokens += max(0, prompt_tokens)
            self._completion_tokens += max(0, completion_tokens)

    def summary(self) -> dict:
        """Return current usage stats."""
        with self._lock:
            return {
                "calls": self._calls,
                "max_calls": self.max_calls,
                "prompt_tokens": self._prompt_tokens,
                "completion_tokens": self._completion_tokens,
                "total_tokens": self._prompt_tokens + self._completion_tokens,
                "max_tokens": self.max_tokens,
            }

    def remaining(self) -> dict:
        """Return remaining call/token budget."""
        with self._lock:
            used_tokens = self._prompt_tokens + self._completion_tokens
            return {
                "calls": max(0, self.max_calls - self._calls),
                "tokens": max(0, self.max_tokens - used_tokens),
            }
