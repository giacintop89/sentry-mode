(() => {
  const enable = document.getElementById('enable-mic'), hold = document.getElementById('hold-talk');
  const result = document.getElementById('talk-result'), setup = document.getElementById('phone-setup');
  const speakButton = document.getElementById('speak');
  const recordButton = document.getElementById('record-message');
  const recordResult = document.getElementById('message-result');
  let stream, context, source, processor, muted, current, pressed = false, enabling = false;
  let recorder, recordTimer, recordStarted;
  const MAX_MESSAGE_SECONDS = 120;
  function status(message, kind = '') { result.textContent = message; result.className = 'result ' + kind; }
  async function api(path, session, data, sequence) {
    const headers = {'X-Sentry-Node-Control':'1'};
    if (path === 'start') {headers['Content-Type']='application/json';data=JSON.stringify(getVoiceEffects('ptt'));}
    if (session) headers['X-Sentry-Node-Talk'] = session.token;
    if (data && path !== 'start') { headers['Content-Type'] = 'application/octet-stream'; headers['X-Audio-Sequence'] = String(sequence); }
    const response = await fetch('/api/talk/' + path, {method:'POST', headers, body:data,
      signal: AbortSignal.timeout(8000)});
    const body = await response.json();
    if (!response.ok) throw Error(body.error || 'Transmission failed.');
    return body;
  }
  function releaseMicrophone() {
    stream?.getTracks().forEach(track => track.stop()); stream = undefined;
    source?.disconnect(); processor?.disconnect(); muted?.disconnect();
    source = processor = muted = undefined;
    context?.close().catch(() => {}); context = undefined;
    hold.disabled = true; enable.disabled = false; enable.textContent = 'Enable phone microphone';
    recordButton.disabled = true;
  }
  function messageStatus(message, kind = '') {
    recordResult.textContent = message; recordResult.className = 'result ' + kind;
  }
  function messageFormat() {
    // Browsers differ: Chrome and Firefox record WebM, Safari MP4. ffmpeg reads both.
    return ['audio/webm', 'audio/mp4', 'audio/ogg'].find(type =>
      window.MediaRecorder?.isTypeSupported(type)) || '';
  }
  function showRecording() {
    const seconds = Math.round((Date.now() - recordStarted) / 1000);
    messageStatus('Recording · ' + seconds + ' s of ' + MAX_MESSAGE_SECONDS + ' s');
  }
  function startMessage() {
    const type = messageFormat();
    if (!stream || !type) { messageStatus('This browser cannot record messages.', 'error'); return; }
    const chunks = [];
    recorder = new MediaRecorder(stream, {mimeType: type});
    recorder.addEventListener('dataavailable', event => { if (event.data.size) chunks.push(event.data); });
    recorder.addEventListener('stop', async () => {
      clearInterval(recordTimer); recorder = undefined;
      recordButton.classList.remove('recording');
      hold.disabled = !stream; enable.disabled = false;
      try {
        const blob = new Blob(chunks, {type});
        if (!blob.size) throw Error('The recording is empty.');
        const response = await fetch('/api/captures/message', {method:'POST',
          headers:{'X-Sentry-Node-Control':'1', 'Content-Type':'application/octet-stream'},
          body: blob, signal: AbortSignal.timeout(60000)});
        const info = await response.json();
        if (!response.ok) throw Error(info.error || 'The message could not be saved.');
        messageStatus(info.message + ' Open the Sentry Captures tab to play it.', 'success');
      } catch (error) { messageStatus(error.message, 'error'); }
      finally { recordButton.disabled = !stream; }
    });
    recorder.start();
    recordStarted = Date.now();
    recordButton.classList.add('recording');
    hold.disabled = true; enable.disabled = true;
    showRecording();
    recordTimer = setInterval(() => {
      showRecording();
      if (Date.now() - recordStarted >= MAX_MESSAGE_SECONDS * 1000) stopMessage();
    }, 1000);
  }
  function stopMessage() {
    if (!recorder || recorder.state === 'inactive') return;
    recordButton.disabled = true; messageStatus('Saving the message…');
    recorder.stop();
  }
  recordButton.addEventListener('click', () => {
    if (recorder) stopMessage(); else startMessage();
  });
  async function cancel(session, message) {
    if (session.failed) return;
    document.getElementById('ptt-effects').disabled=false;
    session.failed = true; pressed = false; clearTimeout(session.timer);
    releaseMicrophone();
    if (session.token) api('cancel', session).catch(() => {});
    if (current === session) current = undefined;
    hold.textContent = 'Hold to talk'; hold.classList.remove('transmitting');
    speakButton.disabled = false; recordButton.disabled = true; status(message, 'error');
  }
  async function enableMicrophone() {
    if (enabling || current) return;
    enabling = true; enable.disabled = true;
    try {
      // Create/resume on the user gesture, including on iOS.
      context = new AudioContext({sampleRate:48000});
      await context.resume();
      stream = await navigator.mediaDevices.getUserMedia({audio:{channelCount:1, echoCancellation:true,
        noiseSuppression:true, autoGainControl:true}, video:false});
      if (document.hidden) throw Error('Keep this page open while using the microphone.');
      if (context.sampleRate !== 48000) throw Error('This browser cannot capture at 48 kHz. Try another browser.');
      await context.audioWorklet.addModule('/pcm-worklet.js');
      stream.getAudioTracks()[0].addEventListener('ended', () => {
        if (current) cancel(current, 'Phone microphone disconnected.'); else releaseMicrophone();
      });
      hold.disabled = false; enable.disabled = false; enable.textContent = 'Turn microphone off';
      recordButton.disabled = !messageFormat();
      status('Microphone ready. Hold to transmit; release to stop.');
      messageStatus(messageFormat() ? 'Ready to record a message to Captures.'
        : 'This browser cannot record messages.', messageFormat() ? '' : 'error');
    } catch (error) {
      releaseMicrophone();
      status(error.name === 'NotAllowedError' ? 'Microphone permission denied. Allow it in your browser’s site settings.' : error.message, 'error');
    } finally { enabling = false; }
  }
  enable.addEventListener('click', () => {
    if (stream) { releaseMicrophone(); status('Microphone is off.'); } else enableMicrophone();
  });
  function send(session, data) {
    if (session.failed) return;
    if (++session.pending > 12) { cancel(session, 'Connection is too slow. Hold to try again.'); return; }
    const sequence = session.sequence++;
    session.chain = session.chain.then(async () => {
      if (!session.failed) await api('chunk', session, data, sequence);
    }).catch(error => cancel(session, error.message)).finally(() => { session.pending--; });
  }
  async function begin() {
    if (current || !stream || hold.disabled) return;
    pressed = true;
    const session = {chain:Promise.resolve(), pending:0, sequence:0, failed:false}; current = session;
    document.getElementById('ptt-effects').disabled=true;
    enable.disabled = true; speakButton.disabled = true; recordButton.disabled = true;
    hold.textContent = 'Connecting…'; status('Opening the speaker. Keep holding…');
    try {
      await context.resume();
      const info = await api('start'); session.token = info.token;
      if (!pressed || session.failed) {
        await api('cancel', session);
        if (current === session) status('Transmission cancelled.');
        reset(session); return;
      }
      processor = new AudioWorkletNode(context, 'phone-pcm');
      source = context.createMediaStreamSource(stream); muted = context.createGain(); muted.gain.value = 0;
      // A silent destination keeps processing active without playing into the phone.
      source.connect(processor); processor.connect(muted); muted.connect(context.destination);
      session.flushed = new Promise(resolve => { session.flushDone = resolve; });
      processor.port.onmessage = event => {
        if (event.data === 'stopped') session.flushDone(); else send(session, event.data);
      };
      hold.textContent = 'Talking · release to stop'; hold.classList.add('transmitting');
      status('Transmitting to the node speaker…');
      session.timer = setTimeout(() => finish(), info.max_seconds * 1000);
    } catch (error) { cancel(session, error.message); }
  }
  function reset(session) {
    if (current !== session) return;
    document.getElementById('ptt-effects').disabled=false;
    current = undefined; pressed = false; clearTimeout(session.timer);
    source?.disconnect(); processor?.disconnect(); muted?.disconnect();
    source = processor = muted = undefined;
    hold.textContent = 'Hold to talk'; hold.classList.remove('transmitting');
    hold.disabled = !stream; enable.disabled = false; speakButton.disabled = false;
    recordButton.disabled = !stream || !messageFormat();
  }
  async function finish() {
    pressed = false;
    const session = current;
    if (!session || !processor || session.finishing) return;
    session.finishing = true; clearTimeout(session.timer); hold.disabled = true;
    status('Finishing playback…');
    try {
      processor.port.postMessage('stop');
      await Promise.race([session.flushed, new Promise((_, reject) => setTimeout(() => reject(Error('Microphone stopped responding.')), 2000))]);
      source.disconnect(); processor.disconnect(); muted.disconnect();
      await session.chain;
      if (session.failed) return;
      const response = await api('stop', session); status(response.message, 'success'); reset(session);
    } catch (error) { cancel(session, error.message); }
  }
  hold.addEventListener('pointerdown', event => {
    if (event.button !== 0) return;
    event.preventDefault(); hold.setPointerCapture(event.pointerId); begin();
  });
  hold.addEventListener('pointerup', finish);
  hold.addEventListener('pointercancel', () => { if (current) cancel(current, 'Transmission cancelled.'); });
  hold.addEventListener('lostpointercapture', finish);
  hold.addEventListener('contextmenu', event => event.preventDefault());
  hold.addEventListener('keydown', event => {
    if ([' ', 'Enter'].includes(event.key)) { event.preventDefault(); if (!event.repeat) begin(); }
  });
  hold.addEventListener('keyup', event => { if ([' ', 'Enter'].includes(event.key)) { event.preventDefault(); finish(); } });
  function leave() {
    pressed = false;
    if (recorder) { stopMessage(); return; }
    if (current) cancel(current, 'Transmission stopped because the page lost focus.');
    else releaseMicrophone();
  }
  window.addEventListener('pagehide', leave);
  window.addEventListener('blur', () => { if (current) leave(); });
  document.addEventListener('visibilitychange', () => { if (document.hidden) leave(); });
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    enable.disabled = true; setup.hidden = false;
    status('Open the HTTPS address below to enable your phone microphone.');
    fetch('/api/talk/config').then(r => r.json()).then(config => {
      if (config.https_port) {
        const url = new URL(location.href); url.protocol = 'https:'; url.port = config.https_port;
        const link = document.getElementById('phone-https'); link.href = url.href; link.textContent = url.href;
      } else document.getElementById('phone-https').textContent = 'HTTPS is not configured on this server.';
      document.getElementById('phone-ca').hidden = !config.local_ca;
    }).catch(() => status('Could not load phone setup. Reload the page.', 'error'));
  } else if (!window.AudioWorkletNode) {
    enable.disabled = true; status('This browser does not support live microphone streaming.', 'error');
  }
})();
