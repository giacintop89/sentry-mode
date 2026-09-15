# SSH commands

A rule can run a command that was saved in advance on another machine. Nothing about the
command is ever assembled from detection data.

## Saving a command

In `/sentry` → **Integrations**, a saved command has an id, host or SSH alias, optional
user, port, the remote command, an optional identity-file path on the node, and a timeout of
1–60 seconds. Up to 32 commands can be saved. A rule's step refers to one by id; deleting a
command a rule uses shows in the editor as a missing entry, and arming refuses the rule.

Set up key-based access and verify host keys first, as the normal user that runs Sentry
Node. Sentry uses OpenSSH batch mode and strict host-key checking, as documented in
[ssh_config](https://man.openbsd.org/ssh_config): it never accepts a password and never
trusts an unknown host key automatically.

## Running

The saved command runs exactly as entered, with no detection-data interpolation and no
shell assembled by the node. Standard output is discarded; a failure and bounded stderr
appear in the event log. Nothing is retried.

Disarming stops the local SSH process; a command already executing on the remote machine may
continue. Test mode logs the command without running it, and **Test command** in the step
runs that one step through Sentry for real, refused while armed and visible in the event
log.

No SSH command is configured by default.

See also: [action sequencer](action-sequencer.md), [security model](security.md).
