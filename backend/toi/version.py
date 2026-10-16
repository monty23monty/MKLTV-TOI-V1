"""Public build metadata; container builds inject the immutable Git revision."""

import os

APP_VERSION = os.getenv("TOI_APP_VERSION", "2.0.1")
APP_REVISION = os.getenv("TOI_APP_REVISION", "development")


def build_metadata():
    return {"version": APP_VERSION, "revision": APP_REVISION}
