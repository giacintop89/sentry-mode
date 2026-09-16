(() => {
  // Saved messages live on the node, so every phone and browser sees the same board.
  const grid = document.getElementById('soundboard-cards'), empty = document.getElementById('soundboard-empty');
  const result = document.getElementById('soundboard-result'), saveButton = document.getElementById('save-message');
  const text = document.getElementById('speech-text'), voice = document.getElementById('voice');
  const rate = document.getElementById('rate'), speechResult = document.getElementById('speech-result');
  const presets = {natural: 'Natural', demon: 'Demon', chipmunk: 'Chipmunk', custom: 'Custom pitch'};
  let messages = [], playing = null, busy = false, preview = null;

  function status(message, kind = '') { result.textContent = message; result.className = 'result ' + kind; }
  async function call(path, body) {
    const options = body === undefined ? {} : {method: 'POST', headers: {'X-Sentry-Mode-Control': '1', 'Content-Type': 'application/json'}, body: JSON.stringify(body)};
    const response = await fetch(path, options);
    const data = await response.json();
    if (!response.ok) throw Error(data.error || 'Command failed');
    return data;
  }
  function voiceLabel(id) {
    const option = [...voice.options].find(item => item.value === id);
    return option ? option.dataset.label || option.textContent.split(' · ')[0] : (id || 'Default voice');
  }
  function details(message) {
    const parts = [voiceLabel(message.voice), (message.rate ?? rate.defaultValue) + ' wpm'];
    const effects = message.effects || {};
    if (effects.preset && effects.preset !== 'natural') {
      parts.push(effects.preset === 'custom' ? (effects.pitch > 0 ? '+' : '') + effects.pitch + ' st' : presets[effects.preset]);
    }
    return parts.join(' · ');
  }
  // A saved file keeps the words it was made from, so a download is recognisable later.
  function fileName(message) {
    const slug = message.text.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
    return 'sentry-mode-' + (slug.slice(0, 40).replace(/-$/, '') || 'message') + '.mp3';
  }
  function render() {
    grid.replaceChildren();
    empty.hidden = messages.length > 0;
    for (const message of messages) {
      const card = document.createElement('li');
      card.className = 'sound-card';
      card.dataset.message = message.id;
      const title = document.createElement('span');
      title.className = 'sound-title'; title.textContent = message.text;
      const meta = document.createElement('span');
      meta.className = 'sound-meta'; meta.textContent = details(message);
      const play = document.createElement('button');
      play.type = 'button'; play.className = 'sound-play';
      play.disabled = busy;
      play.textContent = playing === message.id ? 'PLAYING…' : 'PLAY';
      play.setAttribute('aria-label', 'Play “' + message.text + '” on the node');
      play.addEventListener('click', () => playMessage(message));
      const save = document.createElement('a');
      save.className = 'sound-download'; save.textContent = 'DOWNLOAD';
      save.href = '/soundboard/' + message.id + '.mp3'; save.download = fileName(message);
      save.setAttribute('aria-label', 'Download “' + message.text + '” as an mp3');
      const listen = document.createElement('button');
      listen.type = 'button'; listen.className = 'sound-preview';
      const label = document.createElement('span');
      label.textContent = 'PREVIEW';
      listen.append(label);
      listen.setAttribute('aria-label', 'Preview “' + message.text + '” through this device');
      listen.addEventListener('click', () => previewMessage(message));
      const remove = document.createElement('button');
      remove.type = 'button'; remove.className = 'sound-delete';
      remove.textContent = '×'; remove.disabled = busy;
      remove.setAttribute('aria-label', 'Delete “' + message.text + '” from the soundboard');
      remove.addEventListener('click', () => deleteMessage(message));
      const actions = document.createElement('div');
      actions.className = 'sound-actions';
      actions.append(play, save, listen);
      card.append(title, meta, actions, remove);
      if (playing === message.id) card.classList.add('playing');
      grid.append(card);
    }
  }
  async function refresh() {
    try {
      const data = await call('/api/soundboard');
      messages = data.messages;
      if (data.error) status(data.error, 'error');
      render();
    } catch (error) { status(error.message, 'error'); }
  }
  async function playMessage(message) {
    if (busy) return;
    busy = true; playing = message.id; render();
    status('Playing on the node…');
    try { status((await call('/api/soundboard/play', {id: message.id})).message, 'success'); }
    catch (error) { status(error.message, 'error'); }
    finally { busy = false; playing = null; render(); }
  }
  // The node speaker is not always where you are: preview plays the stored sample here.
  function previewMessage(message) {
    if (preview) { preview.pause(); preview = null; }
    const player = new Audio('/soundboard/' + message.id + '.mp3');
    preview = player;
    player.addEventListener('ended', () => { if (preview === player) preview = null; });
    player.play()
      .then(() => status('Playing through this device\u2026'))
      .catch(error => { if (preview === player) preview = null; status(error.message, 'error'); });
  }
  async function deleteMessage(message) {
    if (busy) return;
    busy = true; render();
    if (preview) { preview.pause(); preview = null; }
    try { await call('/api/soundboard/delete', {id: message.id}); status('Message deleted.', 'success'); }
    catch (error) { status(error.message, 'error'); }
    finally { busy = false; await refresh(); }
  }
  saveButton.addEventListener('click', async () => {
    const message = text.value.trim();
    if (!message) { speechResult.className = 'result error'; speechResult.textContent = 'Enter a message first.'; return; }
    saveButton.disabled = true;
    try {
      const data = await call('/api/soundboard', {text: message, voice: voice.value, rate: Number(rate.value), effects: getVoiceEffects('tts')});
      speechResult.className = 'result success';
      speechResult.textContent = data.created ? 'Saved to the soundboard.' : 'This message is already on the soundboard.';
      await refresh();
      grid.querySelector('[data-message="' + data.saved + '"] .sound-play')?.scrollIntoView({block: 'nearest'});
    } catch (error) { speechResult.className = 'result error'; speechResult.textContent = error.message; }
    finally { saveButton.disabled = false; }
  });
  // Voice names arrive after the page loads; relabel the cards once they do.
  new MutationObserver(render).observe(voice, {childList: true});
  refresh();
})();
