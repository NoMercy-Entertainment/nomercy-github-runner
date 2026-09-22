"""Trusted service entry point, invoked by absolute path under ``python -I``.

The runner's working directory and Python environment must never select the
code that establishes its Job Object and verifies its protected limits.
"""
import sys


def main():
    if not sys.flags.isolated:
        raise RuntimeError("jobhost launcher requires Python isolated mode")
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from agent.jobhost import main as run
    return run()


if __name__ == "__main__":
    sys.exit(main())
