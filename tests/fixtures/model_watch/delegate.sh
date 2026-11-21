#!/usr/bin/env bash
# Hand fixture for model_watch: only the model lines matter.
FREE="opencode/deepseek-v4.1-flash-free opencode/deepseek-v4.1-flash"
GO="opencode-go/kimi-k3.1-pro opencode-go/kimi-k3.2-pro"
case "$MODE" in
  max)
    MAX="opencode/deepseek-v4.1-flash opencode/deepseek-v4.1-flash"
    ;;
esac
ZEN="opencode/deepseek-v4.2-flash"
OTHER="keep   spacing  exactly"
# FREE="opencode/deepseek-v4.1-flash"
