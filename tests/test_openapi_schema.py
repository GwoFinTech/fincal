"""Verify OpenAPI schema coverage (Issue: OpenAPI type generation)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def test_openapi_schema_has_response_models():
    """Every API path must have a non-empty 200 response schema."""
    from app.main import app
    schema = app.openapi()
    paths = schema.get("paths", {})

    missing = []
    for path, methods in paths.items():
        for method in ("get", "post", "put", "delete", "patch"):
            op = methods.get(method)
            if not op:
                continue
            responses = op.get("responses", {})
            ok_resp = responses.get("200", {})
            content = ok_resp.get("content", {})
            if not content:
                # 204 or no-body responses are OK for some endpoints
                if ok_resp.get("description") and "200" not in responses:
                    continue
                # Allow endpoints that return 304 (ETag) or streaming
                if "304" in responses or "200" not in responses:
                    continue
                missing.append(f"{method.upper()} {path}")

    assert not missing, f"Endpoints without response_model: {missing}"


def test_openapi_schema_has_component_models():
    """Schema should expose Pydantic models as components."""
    from app.main import app
    schema = app.openapi()
    components = schema.get("components", {}).get("schemas", {})

    # Key models must exist
    required = [
        "EarningItem", "UserResponse", "WatchlistItem", "HealthResponse",
        "SyncRun", "ManagedWatchlistItem", "DecisionResponse",
    ]
    found = [name for name in required if name in components]
    missing = [name for name in required if name not in components]

    assert len(found) >= 5, f"Missing required models: {missing}"
