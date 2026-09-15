# Telegram

A rule can send a plain-text Telegram message when it triggers.

## Setup

In `/sentry` → **Integrations**, save the bot token from
[BotFather](https://t.me/BotFather) and the destination: a numeric chat ID, including the
minus sign for groups, or a public channel `@username`. Start a conversation with the bot
first, or add it to the destination and allow it to send. A numeric chat ID can be read from
`message.chat.id` in an update from Telegram's
[getUpdates API](https://core.telegram.org/bots/api#getupdates). Use the app's HTTPS address
when entering the token.

Then add a **Send a Telegram message** step to the rules that need it, enter the message,
optionally choose silent delivery, and save each rule. Bot settings are shared by every
rule; nothing is configured by default.

## Sending

Messages use Telegram's
[sendMessage API](https://core.telegram.org/bots/api#sendmessage) over HTTPS, plain text up
to 4096 characters, with no template substitution and no image uploads. The same
confirmation, cooldown, absence rearming, queue limit and expiry apply as to any other step.

A short-lived Python child process makes exactly one request; credentials reach it through
stdin, never process arguments or logged URLs. The parent enforces a five-second total
deadline and cancels and reaps the child on disarm. Nothing is retried, because an
interrupted request may already have delivered: after a timeout or cancellation delivery is
unknown, and a submitted message may still arrive. Provider errors are sanitized so no
token-bearing URL is logged.

Test mode logs `Telegram: ...` without contacting Telegram, and works without credentials.
Arming a rule whose messages will really be sent requires both token and destination.
**Test send** in the step runs that one step through Sentry, so it is refused while armed,
appears in the event log, and — like every test button — really sends.

## The token

It is stored in the private Sentry settings file, masked in general configuration output,
and omitted from Sentry API responses and event logs. A blank token field preserves the
saved token; the red **Remove bot token** button clears it immediately.

See also: [action sequencer](action-sequencer.md), [security model](security.md).
