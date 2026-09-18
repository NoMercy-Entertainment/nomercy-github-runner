"""Runs the scenarios in suite.py against every runtime in HARNESSES.

The scenarios live in suite.py, named for what they are; this file exists
because pytest collects test_*.py, and it adds nothing of its own.
"""
from .suite import *  # noqa: F401,F403
