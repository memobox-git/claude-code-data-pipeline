"""Tests that need a database skip unless the local URLs are set (see spotify/README.md)."""
import os

import pytest

needs_db = pytest.mark.skipif(not os.environ.get("SPOTIFY_DATABASE_URL"), reason="SPOTIFY_DATABASE_URL not set")
needs_agent_db = pytest.mark.skipif(not os.environ.get("SPOTIFY_AGENT_DATABASE_URL"),
                                    reason="SPOTIFY_AGENT_DATABASE_URL not set")
