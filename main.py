"""assignment_uspto — script entry point.

Replace `summarize` with your real USPTO patent-assignment logic. The shape to keep:
small typed pure functions (easy to test) + a thin `main()` that wires argument parsing,
logging, and output. See the workspace `python-project-setup` skill for conventions.
"""

from __future__ import annotations

import argparse
import logging

logger = logging.getLogger(__name__)


def summarize(values: list[int]) -> dict[str, float]:
    """Return basic summary statistics for a non-empty list of numbers.

    Args:
        values: The numbers to summarize.

    Returns:
        A mapping with count, total, mean, min, and max.

    Raises:
        ValueError: If ``values`` is empty.
    """
    if not values:
        raise ValueError("values must not be empty")
    return {
        "count": len(values),
        "total": sum(values),
        "mean": sum(values) / len(values),
        "min": min(values),
        "max": max(values),
    }


def main() -> None:
    """Parse arguments, run the logic, and print results."""
    parser = argparse.ArgumentParser(description="assignment_uspto script")
    parser.add_argument("numbers", nargs="*", type=int, help="numbers to summarize")
    parser.add_argument("-v", "--verbose", action="store_true", help="enable debug logging")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    numbers: list[int] = args.numbers or [1, 2, 3, 4, 5]
    logger.info("Summarizing %d value(s)", len(numbers))
    for key, value in summarize(numbers).items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
