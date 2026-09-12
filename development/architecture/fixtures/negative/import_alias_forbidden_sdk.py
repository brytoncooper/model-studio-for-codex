"""Fixture: import aliases must not hide forbidden SDK roots."""
import openai as vendor_sdk


def client():
    return vendor_sdk
