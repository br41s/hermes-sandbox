"""Schema tests for the Site Launch questionnaire.

The questionnaire is the whole boundary of the product: everything the buyer
can influence has to fit in these fields, and anything that doesn't fit has to
be rejected loudly rather than quietly turning into a scoping conversation.
"""

import pytest

from scripts.bl_site_setup import SECTORS, SiteSetupError, validate_answers

MINIMAL = {
    "company_name": "Fontanería García",
    "sector": "Instalaciones",
    "notify_email": "hola@garcia.example",
}


def test_minimal_answers_validate():
    cleaned = validate_answers(dict(MINIMAL))
    assert cleaned["company_name"] == "Fontanería García"


def test_blank_and_none_values_are_dropped_not_written():
    cleaned = validate_answers({**MINIMAL, "biz_city": "   ", "legal_id": None})
    assert "biz_city" not in cleaned
    assert "legal_id" not in cleaned


@pytest.mark.parametrize("missing", sorted(MINIMAL))
def test_required_fields_are_required(missing):
    answers = {k: v for k, v in MINIMAL.items() if k != missing}
    with pytest.raises(SiteSetupError) as exc:
        validate_answers(answers)
    assert exc.value.code == "invalid_questionnaire"


def test_free_text_sector_is_rejected():
    # A free-text sector is the first crack through which "tell us about your
    # business" scoping comes back — it has to be one of the wizard's options.
    with pytest.raises(SiteSetupError) as exc:
        validate_answers({**MINIMAL, "sector": "Una cosa que hacemos nosotros"})
    assert exc.value.code == "invalid_questionnaire"


def test_every_declared_sector_is_accepted():
    for sector in SECTORS:
        assert validate_answers({**MINIMAL, "sector": sector})["sector"] == sector


def test_unknown_field_fails_loudly():
    # Silently dropping it would ship a site missing data the buyer paid for.
    with pytest.raises(SiteSetupError) as exc:
        validate_answers({**MINIMAL, "custom_page_request": "quiero una tienda"})
    assert exc.value.code == "invalid_questionnaire"
    assert "custom_page_request" in str(exc.value)


def test_bad_biz_type_is_rejected():
    with pytest.raises(SiteSetupError):
        validate_answers({**MINIMAL, "biz_type": "MiNegocioEspecial"})


def test_non_string_answer_is_rejected():
    with pytest.raises(SiteSetupError):
        validate_answers({**MINIMAL, "biz_phone": 600123456})
