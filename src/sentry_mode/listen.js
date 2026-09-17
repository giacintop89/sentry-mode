(() => {
  // Live microphone audio from the node. The stream is raw 16-bit mono PCM, scheduled
  // block by block through the Web Audio API so the page — not a media element's own
  // buffering — decides how far behind the picture the sound is allowed to fall.
  // Listening follows the video: the node is only audible while the preview is running.
  // A satellite microphone is heard the same way; the hub asks the node for sound only
  // while somebody listens.
  const RATE = 16000, LEAD = 0.12, MAX_LEAD = 0.4;
  const toggle = document.getElementById('listen-audio'), result = document.getElementById('video-result');
  const image = document.getElementById('video'), choice = document.getElementById('listen-source');
  let context = null, abort = null, playAt = 0, notice = null;
  function hint(text) {
    if (notice === text) return;
    notice = text;
    if (text) { result.textContent = text; result.className = 'result'; }
    else if (result.className === 'result') result.textContent = '';
  }
  function schedule(bytes) {
    const samples = new Int16Array(bytes.buffer, bytes.byteOffset, bytes.byteLength >> 1);
    const buffer = context.createBuffer(1, samples.length, RATE);
    const channel = buffer.getChannelData(0);
    for (let i = 0; i < samples.length; i++) channel[i] = samples[i] / 32768;
    const source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(context.destination);
    // Behind live, or drifted too far ahead after a stall: rejoin the stream.
    if (playAt < context.currentTime || playAt > context.currentTime + MAX_LEAD) {
      playAt = context.currentTime + LEAD;
    }
    source.start(playAt);
    playAt += buffer.duration;
  }
  async function start() {
    if (abort) return;
    abort = new AbortController();
    playAt = 0;
    try {
      context = context || new AudioContext();
      await context.resume();
      if (context.state !== 'running') throw new Error('blocked');
      const url = choice.value ? '/api/audio/monitor?source_id=' + encodeURIComponent(choice.value) : '/api/audio/monitor';
      const response = await fetch(url, {signal: abort.signal});
      if (!response.ok) throw new Error('unavailable');
      hint('');
      window.nodeRefresh?.();  // The node is now streaming audio; light the nav mark.
      const reader = response.body.getReader();
      // An odd byte count would split a sample, so carry the stray byte to the next block.
      let carry = new Uint8Array(0);
      for (;;) {
        const {done, value} = await reader.read();
        if (done) break;
        let block = value;
        if (carry.length) {
          block = new Uint8Array(carry.length + value.length);
          block.set(carry); block.set(value, carry.length);
        }
        carry = block.length % 2 ? block.slice(block.length - 1) : new Uint8Array(0);
        if (block.length > 1) schedule(block.subarray(0, block.length & ~1));
      }
    } catch (error) {
      if (error.name === 'AbortError') return;
      hint(context && context.state !== 'running'
        ? 'Click anywhere on the page to hear the node.'
        : choice.value && choice.selectedIndex > 0 ? 'That microphone cannot be heard right now.' : 'Live audio is unavailable on this node.');
    }
    abort = null;
    if (wanted()) setTimeout(() => { if (wanted()) start(); }, 1000);
  }
  function stop() {
    if (abort) { abort.abort(); abort = null; window.nodeRefresh?.(); }
    if (context) context.suspend();
    hint('');
  }
  // The preview image is only visible while video runs, so it tracks the camera state.
  function wanted() { return toggle.checked && !image.hidden && !document.hidden; }
  function sync() { if (wanted()) start(); else stop(); }
  async function microphones() {
    try {
      const response = await fetch('/api/microphones');
      if (!response.ok) return;
      const listed = (await response.json()).microphones || [];
      let saved = '';
      try { saved = localStorage.getItem('sentry-microphone') || ''; } catch {}
      choice.replaceChildren(...listed.map(m => new Option(m.display_name + (m.remote ? ' · satellite' : ''), m.remote ? m.source_id : '')));
      choice.value = listed.some(m => m.remote && m.source_id === saved) ? saved : '';
      choice.hidden = listed.length < 2;
    } catch {}
  }
  choice.addEventListener('change', () => {
    try { localStorage.setItem('sentry-microphone', choice.value); } catch {}
    stop(); sync();
  });
  toggle.addEventListener('change', sync);
  new MutationObserver(sync).observe(image, {attributeFilter: ['hidden']});
  document.addEventListener('visibilitychange', sync);
  // Browsers keep audio silent until the visitor interacts with the page, and the block
  // stays until then, so every interaction retries instead of only the first one.
  for (const event of ['click', 'keydown', 'touchend']) {
    document.addEventListener(event, () => { if (wanted() && (!context || context.state !== 'running')) { stop(); start(); } });
  }
  window.addEventListener('pagehide', stop);
  microphones().then(sync);
})();
