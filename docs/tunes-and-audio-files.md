# Tunes and audio files

Two ways for a rule to make a sound that is not speech: a built-in tune synthesized on the
spot, or an audio file uploaded to the node.

## Built-in tunes

Ten tunes are shipped: Chime, Doorbell, Alert beeps, Siren, Fanfare, and five that lean on
the intervals a doorbell avoids — Glitch, Neon drift, Uplink, Sentinel and Blackout — for a
node that sounds like a machine.

A tune is written as 16-bit mono WAV, played on the node speaker, and deleted. Each note is
a frequency, a length and a decay, and short linear ramps at both ends avoid clicks between
notes.

Settings are **Repeat** (1–5), **Volume** (0–100%) and **Pitch (semitones)** from −24 to
+24. Pitch moves every note by the same ratio, so the tune keeps its intervals and its
length is untouched. **Test tune** plays the editor's settings on the node immediately;
POST `/api/tunes/play` does the same.

## Uploaded audio files

**Upload file…** in an audio-file step accepts MP3, WAV, OGG, M4A or FLAC up to 60 seconds
and 10 MB, converts it on the node, and stores it in the shared library, so any rule can use
it. A rule keeps only the file id: a file deleted from the node shows in the editor as a
missing entry rather than silently becoming another file, and arming refuses a rule whose
file is gone.

**Play on node** previews the selected file and **Delete file** removes it from the node —
that one asks for a second click, because it deletes for every rule. Settings are **Repeat**
(1–5) and **Volume** (0–100%). GET `/api/sounds` lists the library, POST
`/api/sounds/upload`, `/api/sounds/play` and `/api/sounds/delete` manage it.

## Sharing the speaker

Tunes, audio files, announcements, push-to-talk and the hardware tone test all use one
speaker under a shared lock, so they take turns rather than overlapping — inside a parallel
group too. A queued audio step that waits too long for the speaker expires and says so.

Test mode logs a tune or file without playing it.

See also: [action sequencer](action-sequencer.md), [text to speech](text-to-speech.md).
