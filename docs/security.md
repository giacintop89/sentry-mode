# Security model

This is a local-first node for a trusted network. It has no login, so bind it to loopback or
keep it on a network you trust.

## The web surface

Every POST requires the same-origin header `X-Sentry-Mode-Control: 1`, which a cross-origin
page cannot set without the node's consent. There are no arbitrary shell or filesystem-path
controls: paths served from `/captures/` are validated against the captures directory, and
uploads are converted through fixed pipelines. The server listens on all interfaces only
when `serve` is told to; `run` starts no web server at all. Status probes are cached for ten
seconds, and the same hardware adapters as the CLI serialize camera and audio access.

Embedding is blocked by default. `web_frame_origins` lists the exact origins allowed to
frame the app, and framing grants the parent no access to control APIs.

## HTTPS for phones

`scripts/setup_phone_https.py` issues a local CA and a one-year server certificate into
ignored `.local/tls/`. Only the public CA is served, at `/local-ca.crt`; private keys never
leave the node. Trusting the CA is a manual, one-time step on each phone — the helper
changes no device's trust settings. A certificate a phone already trusts can be supplied
through the TLS options without `--tls-ca`.

## Secrets

The Telegram bot token lives in the private Sentry settings file. It is masked in general
configuration output, omitted from Sentry API responses and event logs, and passed to the
sending child process through stdin — never process arguments or logged URLs. Provider
errors are sanitized so a token-bearing URL cannot reach the log. A blank token field
preserves the stored token; **Remove bot token** clears it.

## Commands

Saved SSH commands are fixed configuration strings, never assembled from detection data and
never passed through a shell built by the node. OpenSSH runs in batch mode with strict
host-key checking: no password prompts, no automatic trust of unknown hosts. Standard output
is discarded and stderr is bounded in the log.

## Actions and uncertainty

Nothing is retried automatically. A Telegram request that timed out or was cancelled has an
unknown outcome and may still have been delivered; an SSH command already running on the
remote machine may continue after the local process is stopped. Disarming cancels local work
and discards the queue.

Test mode holds back what a detection would do, not what a button does: every test button
runs for real, which is why a Telegram test needs credentials a logged-only rule can go
without.

See also: [HTTP API](http-api.md), [Telegram](telegram.md), [SSH commands](ssh-commands.md).
