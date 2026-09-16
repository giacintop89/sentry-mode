(() => {
  // App-level views only: the host dashboard owns the surrounding frame.
  const tabs = [...document.querySelectorAll('[data-tab]')];
  function selectTab(selected, focus = false) {
    const group = selected.dataset.tabGroup;
    for (const tab of tabs.filter(item => item.dataset.tabGroup === group)) {
      const active = tab === selected;
      tab.setAttribute('aria-selected', String(active));
      tab.tabIndex = active ? 0 : -1;
      document.getElementById(tab.getAttribute('aria-controls')).hidden = !active;
    }
    if (focus) selected.focus({preventScroll: true});
  }
  function fromHash() {
    const key = location.hash.slice(1);
    if (document.body.dataset.page === 'monitor') {
      const voice = ['speech', 'push-to-talk'].includes(key);
      if (!voice && document.body.dataset.view === 'voice') {
        document.getElementById('hold-talk').dispatchEvent(new Event('pointercancel'));
      }
      document.body.dataset.view = voice ? 'voice' : 'video';
      document.querySelector('.comms-column').hidden = !voice;
      document.querySelector('.visual-column').hidden = voice;
      const telemetry = document.querySelector('.telemetry-grid');
      if (telemetry) telemetry.hidden = voice;
      for (const link of document.querySelectorAll('.app-views a')) {
        const active = voice ? link.hasAttribute('data-voice-link') : link.getAttribute('href') === '/';
        if (active) link.setAttribute('aria-current', 'page');
        else link.removeAttribute('aria-current');
      }
    }
    const selected = tabs.find(tab => tab.dataset.tab === key) || tabs[0];
    if (selected) selectTab(selected);
    if (document.body.dataset.page === 'sentry') {
      // Captures is a view of the navigation, not a tab of the workspace below it.
      const captures = key === 'captures';
      if (captures) for (const panel of document.querySelectorAll('main>[role=tabpanel]')) panel.hidden = true;
      document.querySelector('.sentry-control').hidden = captures;
      document.querySelector('main>.workspace-tabs').hidden = captures;
      document.getElementById('panel-captures').hidden = !captures;
      for (const link of document.querySelectorAll('.app-views a[href^="/sentry"]')) {
        if (link.getAttribute('href').includes('#captures') === captures) link.setAttribute('aria-current', 'page');
        else link.removeAttribute('aria-current');
      }
    }
  }
  for (const tab of tabs) {
    tab.addEventListener('click', () => {
      selectTab(tab);
      history.replaceState(null, '', '#' + tab.dataset.tab);
    });
    tab.addEventListener('keydown', event => {
      const group = tabs.filter(item => item.dataset.tabGroup === tab.dataset.tabGroup);
      let index = group.indexOf(tab);
      if (event.key === 'ArrowRight') index = (index + 1) % group.length;
      else if (event.key === 'ArrowLeft') index = (index - 1 + group.length) % group.length;
      else if (event.key === 'Home') index = 0;
      else if (event.key === 'End') index = group.length - 1;
      else return;
      event.preventDefault();
      selectTab(group[index], true);
      history.replaceState(null, '', '#' + group[index].dataset.tab);
    });
  }
  window.addEventListener('hashchange', fromHash);
  // A view link that stays on this page switches the view in place instead of reloading.
  for (const link of document.querySelectorAll('.app-views a')) {
    const href = link.getAttribute('href');
    if (new URL(href, location.href).pathname !== location.pathname) continue;
    link.addEventListener('click', event => {
      event.preventDefault();
      history.replaceState(null, '', href);
      fromHash();
    });
  }
  document.getElementById('rule-form')?.addEventListener('invalid', event => {
    const panel = event.target.closest('[role=tabpanel]');
    const tab = panel && tabs.find(item => item.getAttribute('aria-controls') === panel.id);
    if (tab) selectTab(tab);
  }, true);
  fromHash();

  const stat = (name, value) => {
    for (const node of document.querySelectorAll('[data-stat="' + name + '"]')) node.textContent = value;
  };
  // The mark pulses while the node streams video or audio, so any page shows it at a glance.
  function connection(online, streaming = false) {
    for (const node of document.querySelectorAll('.connection-status')) {
      const label = online ? streaming ? 'Node connected · streaming' : 'Node connected' : 'Node unreachable';
      node.setAttribute('aria-label', label);
      node.title = label;
    }
    for (const node of document.querySelectorAll('[data-connection-dot]')) {
      node.classList.toggle('online', online);
      node.classList.toggle('offline', !online);
      node.classList.toggle('streaming', online && streaming);
    }
  }
  // A rotated copy of the mark closes the frame on the left of a narrow navigation. It is
  // decorative, so it is cloned here instead of being repeated in every page's markup.
  const status = document.querySelector('.connection-status');
  if (status) {
    const mirror = status.querySelector('.connection-mark').cloneNode(true);
    mirror.classList.add('connection-mirror');
    status.parentElement.prepend(mirror);
  }
  async function get(path) {
    const response = await fetch(path, {signal: AbortSignal.timeout(5000)});
    if (!response.ok) throw Error('Node unavailable');
    return response.json();
  }
  let pending = false;
  async function refresh() {
    if (pending || document.hidden) return;
    pending = true;
    try {
      const [video, sentry, streams] = await Promise.all([get('/api/video/status'), get('/api/sentry/status'), get('/api/streams')]);
      connection(true, streams.video || streams.audio);
      const detection = video.detection;
      stat('camera', video.capture_running ? 'Capturing' : 'Idle');
      stat('camera-detail', video.running ? 'Live preview enabled' : video.monitoring ? 'Monitoring · preview hidden' : 'Preview is hidden');
      stat('detection', detection.error ? 'Error' : detection.enabled ? 'Enabled' : 'Disabled');
      stat('detection-detail', detection.error ? 'Check detection controls' : detection.enabled ? 'YOLOX Nano' : 'Object recognition is off');
      stat('sentry', sentry.error ? 'Error' : sentry.armed ? 'Armed' : 'Disarmed');
      stat('sentry-detail', sentry.armed ? sentry.test_mode ? 'Test mode · actions simulated' : 'Watching · actions enabled' : 'Monitoring stopped');
      stat('queue', String(sentry.pending_actions));
      stat('queue-detail', sentry.active_action || 'No action in progress');
      stat('inference', detection.inference_ms === null ? 'Not available' : detection.inference_ms + ' ms');
      const readout = document.getElementById('detection-readout');
      if (readout) {
        readout.replaceChildren();
        if (detection.enabled && video.capture_running && !detection.error && detection.objects.length) {
          for (const object of detection.objects) {
            const chip = document.createElement('div'); chip.className = 'object-chip';
            const label = document.createElement('span'); label.textContent = object.label;
            const confidence = document.createElement('small'); confidence.textContent = Math.round(object.confidence * 100) + '%';
            chip.append(label, confidence); readout.append(chip);
          }
        } else {
          const empty = document.createElement('p');
          empty.textContent = detection.error || (!detection.enabled ? 'Enable object detection to inspect the scene.' : !video.capture_running ? 'Detection ready. Start video or arm Sentry.' : 'No objects detected in the latest frame.');
          readout.append(empty);
        }
      }
    } catch {
      connection(false);
      for (const name of ['camera', 'detection', 'sentry', 'queue']) {
        stat(name, '—'); stat(name + '-detail', 'Connection unavailable');
      }
      stat('inference', 'Connection unavailable');
      const readout = document.getElementById('detection-readout');
      if (readout) readout.textContent = 'Node connection unavailable.';
    } finally { pending = false; }
  }
  // The satellites view exists only on a hub that has them switched on.
  const satellites = [...document.querySelectorAll('[data-satellites-link]')];
  if (satellites.some(link => link.hidden)) {
    get('/api/satellites').then(data => {
      for (const link of satellites) link.hidden = !data.enabled && !link.hasAttribute('aria-current');
    }).catch(() => {});
  }
  refresh();
  window.nodeRefresh = refresh;
  if (document.body.dataset.page !== 'hardware') {
    document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
    setInterval(refresh, 3000);
  }
})();
