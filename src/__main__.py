"""Module entry point: ``python -m src``.

Keeps the console entry point and the background supervisor entry point on
the same controlled function, so a packaged install never loses the ability
to launch the worker.
"""

from src.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
