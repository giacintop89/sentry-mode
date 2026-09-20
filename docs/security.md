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
through the TLS options without `--tls-ca`. Until the CA is trusted the browser objects,
and some apps turn that objection into "cannot connect" — a trust problem wearing a
network problem's clothes ([web dashboard](web-dashboard.md#when-it-cannot-be-reached)).

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

## What the satellite network can reach

The satellites live on their own network, and only two things on this hub belong on it:
the broker they publish to and the media gateway they send pictures and sound to. The
dashboard is administrative — it arms and disarms, edits rules and opens the microphone —
so a node that has been taken over should not be able to knock on its door.

Set `satellites.network` to that subnet and the hub says, once at startup, whether an
administrative listener answers on it:

    WARNING dashboard listens on every interface and is reachable from the satellite
    network 192.168.11.0/24 at 192.168.11.10. Bind it to the house network instead.

It is a warning and not a refusal: the hub keeps serving, because the person who set the
interfaces up is the one who decides. `serve --host` takes the address to bind to, and
binding the dashboard to the house address is the fix.

## Taking a node away

Revoking a node takes back all four of the things it was given, at once and on this hub's
side, without waiting for the broker to notice:

| Channel | What happens |
|---|---|
| Events | the session and its grant end, and anything that arrives afterwards is refused as `not_approved` |
| Commands | the node is told once, and nothing else is ever sent to it |
| Video | an open publication is closed with *the node was revoked*, and its ticket cannot be used again |
| Audio | the same, on the same gateway |

Its retained snapshot is erased, its sources are marked disabled, and its health entry is
dropped. Reloading the broker's own configuration does not close connections that are
already open, which is exactly why the hub does not rely on it.

An agent that speaks a protocol this hub does not is refused in the same breath rather
than left looking healthy: no session opens, nothing it publishes is acted on, and the
node's card says `unsupported_protocol`. A node whose events are being dropped one at a
time while its card stays green is the failure this prevents.

## Actions and uncertainty

Nothing is retried automatically. A Telegram request that timed out or was cancelled has an
unknown outcome and may still have been delivered; an SSH command already running on the
remote machine may continue after the local process is stopped. Disarming cancels local work
and discards the queue.

Test mode holds back what a detection would do, not what a button does: every test button
runs for real, which is why a Telegram test needs credentials a logged-only rule can go
without.

See also: [HTTP API](http-api.md), [Telegram](telegram.md), [SSH commands](ssh-commands.md).
