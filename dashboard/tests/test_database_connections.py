"""Transactions must release database handles without waiting for GC."""
import gc
import os
from pathlib import Path
import sqlite3

import pytest

import history
from store import schema


@pytest.fixture(params=['control', 'history'])
def database(request, tmp_path, monkeypatch):
    path = tmp_path / (request.param + '.db')
    if request.param == 'control':
        connect = lambda: schema.connect(path)
    else:
        monkeypatch.setattr(history, 'DB_PATH', str(path))
        connect = history._conn
    with connect() as connection:
        connection.execute('CREATE TABLE entries (value TEXT)')
    return connect, path


def test_success_commits_and_closes_connection(database):
    connect, path = database
    with connect() as connection:
        connection.execute("INSERT INTO entries VALUES ('saved')")
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        connection.execute('SELECT 1')
    reopened = sqlite3.connect(path)
    try:
        assert reopened.execute('SELECT value FROM entries').fetchall() == [('saved',)]
    finally:
        reopened.close()


def test_failed_transaction_rolls_back_and_closes_connection(database):
    connect, path = database
    with pytest.raises(RuntimeError, match='aborted'):
        with connect() as connection:
            connection.execute("INSERT INTO entries VALUES ('discarded')")
            raise RuntimeError('aborted')
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        connection.execute('SELECT 1')
    with connect() as reopened:
        assert reopened.execute('SELECT value FROM entries').fetchall() == []


def test_commit_failure_also_releases_connection(database):
    connect, path = database
    with connect() as setup:
        setup.executescript('CREATE TABLE parent (id INTEGER PRIMARY KEY);'
                           'CREATE TABLE child (parent_id INTEGER REFERENCES parent(id)'
                           ' DEFERRABLE INITIALLY DEFERRED);')
    with pytest.raises(sqlite3.IntegrityError):
        with connect() as connection:
            connection.execute('INSERT INTO child VALUES (123)')
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        connection.execute('SELECT 1')
    with connect() as reopened:
        assert reopened.execute('SELECT * FROM child').fetchall() == []


@pytest.mark.skipif(not Path('/proc/self/fd').is_dir(), reason='Linux handle accounting')
def test_repeated_reads_do_not_accumulate_handles_with_gc_disabled(database):
    connect, path = database
    enabled = gc.isenabled()
    gc.disable()
    before = len(os.listdir('/proc/self/fd'))
    try:
        for _ in range(500):
            with connect() as connection:
                assert connection.execute('SELECT count(*) FROM entries').fetchone()[0] == 0
        assert len(os.listdir('/proc/self/fd')) <= before + 2
    finally:
        if enabled:
            gc.enable()
