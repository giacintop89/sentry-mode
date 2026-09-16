# Phone push-to-talk

Hold a button on a phone and your voice plays live through the node speaker.

## Using it

Open the main page on the phone, tap **Enable mic**, allow access, then hold **Hold to
talk**. Release to finish; the microphone stays enabled for the next press until you turn it
off or leave the page. Each press is limited to 60 seconds. Touch cancellation, leaving the
page and connection loss all stop the transmission. The speaker is shared with speech and
the hardware audio tests, so a concurrent action reports busy. Video can run during a
transmission, and the **Voice modification** controls are locked while one is in progress.

**Record**, beside Hold to talk, records the same phone microphone and saves the message to
the [Captures](captures.md) view instead of playing it, up to 120 seconds. It lights
red while recording; press it again to stop and save. The browser's recording (WebM, Ogg or
MP4) is uploaded to POST `/api/captures/message` as `application/octet-stream` and converted
to an AAC `.m4a` file of at most 5 MB.

## Loudness

A phone sends speech well below full scale: its own automatic gain aims at a safe recording
level, not a loud one, so playing those samples untouched is much quieter than synthesized
speech, which is rendered at full scale. The node lifts them on the way to the speaker, by up
to `speech.talk_gain_db` decibels — 15 by default.

The setting is a ceiling, not a fixed gain. The lift stops once the audio reaches the peak,
so a phone that already sends a strong signal is barely touched and a quiet one is brought
most of the way up; near-silence between words is left where it is rather than being raised
into audible hiss. Set it to `0` to play exactly what the phone sent. Raising it costs
nothing but headroom against room noise.

If it is still quiet with the lift in place, the attenuation is below this software. The
**Output level** slider on `/hardware` is the speaker's own gain, which caps everything the
node plays through it ([hardware and devices](hardware-and-devices.md)); a sink sitting at a
tenth of unity loses 20 dB, more than the lift puts back.

The **Volume** slider in Voice modification applies after the lift and is a plain
attenuation: 100% is unmodified, and it starts at `speaker.volume`. FFmpeg does the work, in
the same pass as a pitch preset.

## HTTPS is required

Phone browsers only grant microphone access over a trusted HTTPS connection; a LAN HTTP
address cannot ask for it. The app serves HTTP and HTTPS together with shared controls:

```bash
python3 scripts/setup_phone_https.py 192.168.11.240 pi5 pi5.local
sentry-mode serve --port 8083 --https-port 8443 \
  --tls-cert .local/tls/server.crt --tls-key .local/tls/server.key --tls-ca .local/tls/ca.crt
```

On the phone, open the HTTP page and expand **Set up this phone** to download the local CA
certificate and follow the iPhone/Android trust instructions. Where HTTP is bound to
loopback, fetch `/local-ca.crt` from the HTTPS address instead, accepting the browser warning
once, or copy `.local/tls/ca.crt` across by other means. Then open the HTTPS link and allow
microphone access.

Trusting the CA is a one-time phone setup; the helper changes no device's trust settings. It
keeps private keys in ignored `.local/tls/`, reuses the CA on reruns, and issues a one-year
server certificate. Rerun it and restart after the Pi's address changes or before the
certificate expires. Only the public CA is served, at `/local-ca.crt`. A certificate your
phone already trusts can be supplied through the same TLS options without `--tls-ca`.

Inside an embedded frame, microphone use additionally requires a secure parent and delegated
microphone permission; the standalone HTTPS Voice view always remains available.

## Transport

AudioWorklet captures mono 48 kHz PCM16. POST `/api/talk/start` returns a session token,
sample rate and duration limit; `/api/talk/chunk`, `/api/talk/stop` and `/api/talk/cancel`
carry that token in `X-Sentry-Mode-Talk`. Chunks are `application/octet-stream` with
consecutive `X-Audio-Sequence` values from zero and at most 9600 bytes each; the browser
limits a chunk to 100 ms and uploads them in order. All four endpoints need the same-origin
control header. `/api/talk/start` accepts the effects object as its optional JSON body.

One player process runs for a whole press, and no audio file is retained on the server. A
second process filters the stream whenever a gain or a pitch preset is in force.
Leading and trailing padding reuses the speech settings, capped at 1000/750 ms for live
transmission. A bounded packet queue rejects a connection that falls behind, and a
three-second idle timeout releases an abandoned session. Effects add a small processing
delay while keeping one continuous playback stream.

Current Android Chrome and iPhone Safari expose the required APIs over HTTPS; device-specific
permission and audio behavior still needs testing on real phones.

See also: [text to speech](text-to-speech.md), [configuration](configuration.md),
[security model](security.md).
