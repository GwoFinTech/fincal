"""Regression coverage for response-model shapes found in production."""
from datetime import date


def test_user_response_accepts_integer_portal_user_id():
    from app.schemas import UserResponse

    user = UserResponse.model_validate({
        "id": 42,
        "portal_user_id": 7,
        "email": "u@example.com",
        "name": "User",
        "role": "user",
        "is_admin": False,
        "ical_token": "token",
        "ical_url": "https://example.test/ical/token",
    })
    assert user.portal_user_id == 7


def test_decision_response_accepts_revision_summary_object():
    from app.schemas import DecisionResponse

    result = DecisionResponse.model_validate({
        "status": "available",
        "revision_trend": {
            "status": "available",
            "sample_count": 1,
            "eps": {"direction": "flat"},
        },
    })
    assert result.revision_trend is not None
    assert result.revision_trend["status"] == "available"
