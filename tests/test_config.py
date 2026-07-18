"""Regression test for BUG-3: CORS must default to no allowed origins.

cors_allowed_origins_list is what backend/app.py hands straight to
CORSMiddleware's allow_origins -- it must default to an empty list (not a
wildcard, not None) unless CORS_ALLOWED_ORIGINS is explicitly configured
(e.g. this repo's own .env now sets it, for the frontend/ dev server).
"""

from pathlib import Path

from backend.core.config import Settings


def test_cors_allowed_origins_defaults_to_empty_list():
    # _env_file=None skips loading the real .env for this instance -- this
    # tests the class's declared default, not whatever CORS_ALLOWED_ORIGINS
    # happens to be set to in the developer's own local .env (this repo's
    # own .env sets one, since frontend/ needs it).
    settings = Settings(_env_file=None)
    assert settings.cors_allowed_origins_list == []


def test_cors_allowed_origins_parses_comma_separated_list():
    settings = Settings(cors_allowed_origins="http://127.0.0.1:5173,http://localhost:5173")
    assert settings.cors_allowed_origins_list == ["http://127.0.0.1:5173", "http://localhost:5173"]


def test_cors_allowed_origins_strips_whitespace_and_drops_empties():
    settings = Settings(cors_allowed_origins=" http://127.0.0.1:5173 , , http://localhost:5173,")
    assert settings.cors_allowed_origins_list == ["http://127.0.0.1:5173", "http://localhost:5173"]


def test_comfyui_http_url_combines_host_and_port():
    settings = Settings(comfyui_host="192.168.1.5", comfyui_port=9000)
    assert settings.comfyui_http_url == "http://192.168.1.5:9000"


def test_workflow_path_wraps_workflow_dir_as_a_path():
    settings = Settings(workflow_dir="some/workflows/dir")
    assert settings.workflow_path == Path("some/workflows/dir")
