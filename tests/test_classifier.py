
import pytest
from pydantic import ValidationError

from classifier import Classification


def test_valid_classification():
    result = Classification.model_validate({
        "category": "billing",
        "priority": "medium",
        "summary": "The customer requests a refund."
    })

    assert result.category == "billing"
    assert result.priority == "medium"


@pytest.mark.parametrize("payload", [
    {
        "category": "sales",
        "priority": "medium",
        "summary": "The customer needs help."
    },
    {
        "category": "billing",
        "priority": "urgent",
        "summary": "The customer reports a charge."
    },
    {
        "category": "billing",
        "priority": "medium"
    },
    {
        "category": "billing",
        "priority": "medium",
        "summary": "The customer requests a refund.",
        "approved": True
    }
])
def test_invalid_classifications_are_rejected(payload):
    with pytest.raises(ValidationError):
        Classification.model_validate(payload)


def test_malformed_json_is_rejected():
    with pytest.raises(ValidationError):
        Classification.model_validate_json("not valid JSON")



@pytest.mark.parametrize("summary", [
    "",
    "   ",
    "\n\t "
])
def test_blank_summaries_are_rejected(summary):
    with pytest.raises(ValidationError):
        Classification.model_validate({
            "category": "billing",
            "priority": "medium",
            "summary": summary
        })
