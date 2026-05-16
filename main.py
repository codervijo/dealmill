"""Entry shim for `uv run python main.py` (the central Makefile.python's default
`run` target). The real `make run` target is overridden in Makefile.local to call
`python -m dealmill` directly; this file is here for parity with the builder's
Python stack convention."""

from dealmill.cli import app

if __name__ == "__main__":
    app()
