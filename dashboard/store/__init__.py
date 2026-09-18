"""The control plane's state: what runners are meant to exist, and where.

Separate from `history.py`, which records what already happened. History is
hot - a sample every five seconds per running job - while this is written only
when someone changes an intention. Keeping them in one file would put rare,
important writes behind a busy one.

They are also separate in kind. History is a fact that cannot be wrong; a spec
is a wish that reality is reconciled against. The one place they meet is
`runs.runner_id`, which T-0204 backfills. That is a reference rather than a
foreign key precisely because the two live in different files, and the test
that every historical row resolves to a spec is what stands in for the
constraint SQLite cannot enforce across them.
"""
