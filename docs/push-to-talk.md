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

One player process runs for a whole press, and no audio file is retained on the server.
Leading and trailing padding reuses the speech settings, capped at 1000/750 ms for live
transmission. A bounded packet queue rejects a connection that falls behind, and a
three-second idle timeout releases an abandoned session. Effects add a small processing
delay while keeping one continuous playback stream.

Current Android Chrome and iPhone Safari expose the required APIs over HTTPS; device-specific
permission and audio behavior still needs testing on real phones.

See also: [text to speech](text-to-speech.md), [security model](security.md).
