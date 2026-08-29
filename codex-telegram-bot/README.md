# Codex Telegram Bot

Standalone, single-owner Telegram frontend for continuing Codex CLI threads.

`bot.py` owns Telegram polling, native draft progress, commands, process
lifecycle, and state persistence. Its only source-level dependency on the
parent Claude bridge is `../telegram_format.py`.

Runtime state is stored in `state.json` and is ignored by git.

Install the example systemd unit after supplying the real Telegram token and
owner ID. The service launches a fresh `codex exec` process for every turn.
