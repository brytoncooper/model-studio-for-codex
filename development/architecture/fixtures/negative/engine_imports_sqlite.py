"""Fixture: engine must not import concrete storage."""
import sqlite3


def connect():
    return sqlite3.connect(":memory:")
