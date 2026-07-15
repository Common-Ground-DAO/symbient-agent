# Symbient deployment

This deployment runs Hermes as Common Ground's unprivileged platform agent.
The container receives only one persistent data directory. It does not mount a
source repository, Docker socket, SSH directory, browser profile, or developer
credential store, and it exposes no inbound host port.

## Runtime boundary

- Common Ground Bot API v1 is the only community identity.
- The adapter responds only to structured mentions and direct replies in the
  channels granted by the bot's live Common Ground installations and roles.
- Hermes' sender allowlist remains the final inbound authorization gate.
- The configured tool surface is research, memory, session recall, planning,
  and clarification. Code execution and filesystem access are disabled.
- GitHub/developer handoff is intentionally absent until its workflow and
  authorization policy are designed.

## Install

1. Create a private data directory owned by the runtime UID.
2. Copy `config.yaml`, `SOUL.md`, and `hermes.env.example` into it; rename the
   last file to `.env` and fill in the secrets and sender UUID allowlist.
3. Set `.env` mode to `0600` and the directory mode to `0700`.
4. Run `docker compose -f deploy/docker-compose.symbient.yml up -d --build`.

When credentials already exist in private dotenv files, `bootstrap_local.py`
can copy only the required values without placing secrets in shell arguments:

```sh
python3 deploy/bootstrap_local.py \
  --data-dir /path/to/private-data \
  --cg-env /path/to/common-ground-bot.env \
  --openai-env /path/to/openai.env \
  --allowed-user <common-ground-user-uuid> \
  --uid "$(id -u)" --gid "$(id -g)"
```

For a public community deployment, do not merely set
`COMMONGROUND_ALLOW_ALL_USERS=true`. First separate per-user untrusted context
from the trusted organizational/developer handoff path and define who may cause
external side effects.
