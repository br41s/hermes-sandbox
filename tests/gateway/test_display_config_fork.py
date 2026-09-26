"""Fork's own tests for gateway.display_config, kept out of upstream's file so upstream merges do not conflict."""


# ---------------------------------------------------------------------------
# turn_start_ack — immediate "message received" acknowledgment (ours)
# ---------------------------------------------------------------------------

class TestTurnStartAck:
    """``turn_start_ack`` is on by default for interactive chat platforms,
    off for non-interactive tiers, and fully overrideable per profile."""

    def test_on_by_default_for_interactive_platforms(self):
        from gateway.display_config import resolve_display_setting

        for plat in ("telegram", "discord", "slack", "whatsapp"):
            assert resolve_display_setting({}, plat, "turn_start_ack") is True, plat

    def test_off_by_default_for_non_interactive_platforms(self):
        from gateway.display_config import resolve_display_setting

        for plat in ("email", "sms", "webhook", "homeassistant", "api_server"):
            assert resolve_display_setting({}, plat, "turn_start_ack") is False, plat

    def test_default_text_is_spanish(self):
        from gateway.display_config import resolve_display_setting

        text = resolve_display_setting({}, "telegram", "turn_start_ack_text")
        assert text == "✅ Mensaje recibido, te escribo cuando tenga novedades."

    def test_text_overrideable_per_profile(self):
        from gateway.display_config import resolve_display_setting

        config = {
            "display": {
                "platforms": {"telegram": {"turn_start_ack_text": "Recibido, ya te aviso."}}
            }
        }
        assert (
            resolve_display_setting(config, "telegram", "turn_start_ack_text")
            == "Recibido, ya te aviso."
        )

    def test_global_disable(self):
        from gateway.display_config import resolve_display_setting

        config = {"display": {"turn_start_ack": False}}
        assert resolve_display_setting(config, "telegram", "turn_start_ack") is False

    def test_yaml_off_string_normalises_to_false(self):
        from gateway.display_config import resolve_display_setting

        config = {"display": {"platforms": {"telegram": {"turn_start_ack": "off"}}}}
        assert resolve_display_setting(config, "telegram", "turn_start_ack") is False
