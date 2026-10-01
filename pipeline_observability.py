from __future__ import annotations

from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Callable


def _format_fields(fields: dict[str, Any]) -> str:
    if not fields:
        return ""
    return " | " + " | ".join(f"{key}={value}" for key, value in fields.items())


@dataclass
class PipelineProgress:
    """Bounded stage logging for GitHub Actions, Colab, and local runs."""

    total: int
    label: str = "PIPELINE"
    emit: Callable[[str], None] = print
    current: int = 0
    started_at: float = field(default_factory=perf_counter)

    def run(self, name: str, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        self.current += 1
        step = f"[{self.current:02d}/{self.total:02d}]"
        started = perf_counter()
        self.emit(f"{step} START {name}")
        try:
            result = func(*args, **kwargs)
        except Exception as exc:
            self.emit(
                f"{step} FAIL  {name} | elapsed={perf_counter() - started:.2f}s "
                f"| error={type(exc).__name__}"
            )
            raise
        self.emit(f"{step} DONE  {name} | elapsed={perf_counter() - started:.2f}s")
        return result

    def diagnostic(self, name: str, **fields: Any) -> None:
        self.emit(f"[DIAGNOSTIC] {name}{_format_fields(fields)}")

    def progress(self, name: str, completed: int, total: int, **fields: Any) -> None:
        self.emit(f"[PROGRESS] {name} | {completed}/{total}{_format_fields(fields)}")

    def finish(self, status: str, **fields: Any) -> None:
        self.emit(
            f"[{self.label}] {status} | total_elapsed={perf_counter() - self.started_at:.2f}s"
            f"{_format_fields(fields)}"
        )
