#!/bin/sh
# claude-cursor-wrapper.sh - claudeCode.claudeProcessWrapper for Cursor's Claude extension.
#
# The extension runs every Claude process as: <wrapper> <bundled claude binary> <args...>,
# with CLAUDE_CONFIG_DIR taken from the claudeCode.environmentVariables setting. That
# setting can only hold an absolute path, never "unset", but the default account must run
# with CLAUDE_CONFIG_DIR unset or it loses its Keychain login. So
# accounts/cursor-claude-account.py writes the default account as ~/.claude and this
# wrapper drops it. Any other dir (~/.claude.x) passes through unchanged.
case "${CLAUDE_CONFIG_DIR%/}" in
    "$HOME/.claude") unset CLAUDE_CONFIG_DIR ;;
esac
exec "$@"
