import pytest
from pydantic import ValidationError
from log_analyzer import LogAnalysis


def test_valid_log_analysis():
    data = {
        "severity": "HIGH",
        "likely_cause": "Database connection timeout",
        "suggested_fix": "Check DB credentials and network policies.",
        "confidence": 0.85,
    }
    model = LogAnalysis(**data)
    assert model.severity == "HIGH"
    assert model.confidence == 0.85


def test_invalid_severity():
    data = {
        "severity": "WARNING",
        "likely_cause": "OOM Kill",
        "suggested_fix": "Increase memory limits.",
        "confidence": 0.9,
    }
    with pytest.raises(ValidationError) as exc:
        LogAnalysis(**data)
    assert "severity" in str(exc.value)


def test_invalid_confidence_out_of_bonds():
    data = {
        "severity": "LOW",
        "likely_cause": "Transient network blip",
        "suggested_fix": "Ignore, system self-healed.",
        "confidence": 1.5,
    }
    with pytest.raises(ValidationError):
        LogAnalysis(**data)


def test_missing_required_field():
    data = {"severity": "CRITICAL", "likely_cause": "Disk failure", "confidence": 0.99}
    with pytest.raises(ValidationError):
        LogAnalysis(**data)
