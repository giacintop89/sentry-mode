(() => {
  const $ = id => document.getElementById(id);
  // Cards are rebuilt on every poll, except that an open editor keeps what is being typed.
  const drafts = new Map();
  const open = new Set();
  let last = null, busy = false;
  function message(text, kind = '') { $('satellite-result').textContent = text; $('satellite-result').className = 'result ' + kind; }
  async function api(path, body) {
    const post = body !== undefined;
    const r = await fetch('/api/' + path, {method: post ? 'POST' : 'GET',
      headers: post ? {'X-Sentry-Mode-Control': '1', 'Content-Type': 'application/json'} : {},
      body: post ? JSON.stringify(body) : undefined});
    const data = await r.json(); if (!r.ok) throw Error(data.error || 'Request failed.'); return data;
  }
  const f = (card, name) => card.querySelector('[data-f="' + name + '"]');
  function span(seconds) {
    if (seconds === null || seconds === undefined) return '—';
    if (seconds < 90) return Math.round(seconds) + ' s';
    if (seconds < 5400) return Math.round(seconds / 60) + ' min';
    return Math.round(seconds / 3600) + ' h';
  }
  function ago(seconds) {
    if (seconds === null || seconds === undefined) return '—';
    return span(seconds) + ' ago';
  }
  // Why the board is running this time, in words rather than in the wire's. A node that
  // did not say is left out: silence is not a clean start, and showing one would be
  // inventing the most reassuring of the answers it could have given.
  const RESET = {power: 'came back on power', brownout: 'came back after a brown-out',
    button: 'came back on the reset button', watchdog: 'came back on the watchdog',
    software: 'came back on a software reboot', debugger: 'came back on a debugger'};
  // Where a source reads from, in the options it was configured with. A pin is the one
  // thing about a microcontroller source that decides whether it is wired to anything, and
  // it was only visible by opening the editor and reading the JSON.
  function where(source) {
    const options = source.options || {};
    const bits = [];
    if (typeof options.pin === 'number') bits.push('GPIO ' + options.pin);
    if (options.device) bits.push(String(options.device));
    if (options.measure) bits.push(String(options.measure));
    if (typeof options.interval_seconds === 'number') bits.push('every ' + span(options.interval_seconds));
    return bits.join(' · ') || '—';
  }
  function reading(source) {
    const last = source.last;
    if (!last) return source.error ? 'Unavailable' : '—';
    if (last.quality !== 'valid' || last.value === null) return 'Unavailable';
    const value = typeof last.value === 'boolean' ? (last.value ? 'active' : 'idle') : String(last.value);
    return value + (last.unit ? ' ' + last.unit : '');
  }
  // What the node file would say, so the editor opens on the sources as they are.
  function asEntries(node) {
    return node.sources.filter(s => s.declared).map(s => ({id: s.name, kind: s.kind, ...(s.enabled ? {} : {enabled: false}), ...s.options}));
  }
  function configurationLine(node) {
    const c = node.configuration;
    if (!c) return ['', ''];
    const label = 'Configuration ' + c.revision + ': ';
    if (c.state === 'sent') return [label + 'sent, waiting for the node…', ''];
    if (c.state === 'received') return [label + 'received, starting the drivers…', ''];
    if (c.state === 'applied') return [label + 'applied.', 'success'];
    return [label + 'not applied — ' + (c.detail || 'no reason given'), 'error'];
  }
  function card(node) {
    const item = $('satellite-card').content.firstElementChild.cloneNode(true);
    item.dataset.node = node.node_id;
    f(item, 'name').textContent = node.display_name + (node.display_name !== node.node_id ? ' · ' + node.node_id : '');
    const freshness = node.status !== 'approved' ? node.status : node.online ? node.freshness : 'offline';
    f(item, 'freshness').textContent = freshness;
    f(item, 'freshness').classList.toggle('armed', freshness === 'fresh');
    const platform = node.platform || {};
    const machine = platform.name && (platform.name + (platform.experimental ? ' (experimental)' : ''));
    const meta = [node.zone && 'zone ' + node.zone, node.profile, machine, node.agent_version && 'agent ' + node.agent_version,
      node.config_revision ? 'configuration ' + node.config_revision : 'installed configuration',
      node.last_seen ? 'heard ' + ago(Date.now() / 1000 - node.last_seen) : 'not heard yet',
      node.clock_status && 'clock ' + node.clock_status];
    f(item, 'meta').textContent = meta.filter(Boolean).join(' · ');
    // The second line is what the node measured about itself, and only what this kind of
    // board can measure: the hub leaves out what it cannot, rather than showing a zero.
    const board = node.board || {}, queue = node.queue || {}, comings = node.comings || {};
    const limits = [platform.streams && platform.streams.length ? null : 'no camera or microphone',
      platform.manual_tests === false ? 'no on-demand test' : null];
    const numbers = [
      board.uptime_seconds != null && 'up ' + span(board.uptime_seconds),
      board.temperature_c != null && board.temperature_c.toFixed(1) + ' °C',
      board.memory_available_kb != null && board.memory_available_kb + ' kB free',
      board.load1 != null && 'load ' + board.load1.toFixed(2),
      board.throttled,
      board.reset && (RESET[board.reset] || 'came back: ' + board.reset),
      comings.restarts ? comings.restarts + ' restart' + (comings.restarts === 1 ? '' : 's')
        + (comings.restarted_seconds_ago != null ? ', last ' + ago(comings.restarted_seconds_ago) : '') : null,
      queue.events != null && queue.events + ' queued',
      queue.drops && queue.drops.total ? queue.drops.total + ' dropped' : null,
      ...limits];
    f(item, 'board').textContent = numbers.filter(Boolean).join(' · ');
    f(item, 'caption').textContent = 'Sources of ' + node.display_name;
    f(item, 'table-region').setAttribute('aria-label', 'Sources of ' + node.display_name);
    const rows = node.sources.map(source => {
      const row = document.createElement('tr');
      const state = !source.declared ? 'no longer declared' : !source.enabled ? 'disabled'
        : !source.supported ? 'no driver yet' : source.error ? 'error' : source.state;
      const cells = [source.name, source.kind || source.role, where(source), state,
        reading(source), ago(source.last_reading_age_seconds)];
      for (const text of cells) { const cell = document.createElement('td'); cell.textContent = text; row.append(cell); }
      row.children[0].setAttribute('scope', 'row');
      if (source.error) row.title = source.error;
      row.dataset.state = state;
      return row;
    });
    f(item, 'sources').replaceChildren(...rows);
    f(item, 'empty').hidden = rows.length > 0;
    f(item, 'table-region').hidden = rows.length === 0;
    f(item, 'errors').replaceChildren(...node.errors.map(text => { const li = document.createElement('li'); li.textContent = text; return li; }));
    const [text, kind] = configurationLine(node);
    f(item, 'configuration').textContent = text;
    f(item, 'configuration').className = 'result ' + kind;
    const area = f(item, 'sources-json'), id = 'sources-' + node.node_id;
    area.id = id; f(item, 'label').htmlFor = id;
    area.value = drafts.get(node.node_id) ?? JSON.stringify(asEntries(node), null, 2);
    // What this node can drive, not what some satellite could: a board with four drivers
    // should not be offered nine and be told `failed` after the round trip.
    const drivers = platform.drivers || last.drivers;
    const room = platform.max_sources ? ' Up to ' + platform.max_sources + ' sources.' : '';
    f(item, 'drivers').textContent = 'Kinds this node can drive: ' + drivers.join(', ') + '.' + room
      + ' Set "enabled": false to keep a source in the list without reading it.';
    const editor = f(item, 'editor');
    editor.open = open.has(node.node_id);
    const approved = node.status === 'approved' && node.online;
    for (const control of f(item, 'form').elements) control.disabled = !approved || busy;
    if (!approved) f(item, 'drivers').textContent = 'The node has to be approved and online before its sources can change.';
    editor.addEventListener('toggle', () => { if (editor.open) open.add(node.node_id); else open.delete(node.node_id); });
    area.addEventListener('input', () => drafts.set(node.node_id, area.value));
    f(item, 'reset').addEventListener('click', () => { drafts.delete(node.node_id); area.value = JSON.stringify(asEntries(node), null, 2); });
    f(item, 'form').addEventListener('submit', event => { event.preventDefault(); send(node, area); });
    return item;
  }
  async function send(node, area) {
    let sources;
    try { sources = JSON.parse(area.value); }
    catch { area.setCustomValidity('This is not valid JSON.'); area.reportValidity(); area.setCustomValidity(''); return; }
    if (!Array.isArray(sources)) { area.setCustomValidity('Write a list: [ {"id": …, "kind": …}, … ]'); area.reportValidity(); area.setCustomValidity(''); return; }
    busy = true; message('Sending to ' + node.node_id + '…');
    try {
      const data = await api('satellites/configure', {node_id: node.node_id, sources});
      drafts.delete(node.node_id);
      message(data.message, 'success');
      setTimeout(() => { if ($('satellite-result').textContent === data.message) message(''); }, 8000);
    } catch (error) { message(error.message, 'error'); }
    finally { busy = false; await poll(); }
  }
  function render(data) {
    last = data;
    $('broker-state').textContent = !data.enabled ? 'Off' : data.broker === 'connected' ? 'Broker connected' : 'Broker ' + (data.broker || 'unknown').replaceAll('_', ' ');
    $('broker-state').classList.toggle('armed', data.enabled && data.broker === 'connected');
    $('satellites-off').hidden = data.enabled;
    // Rebuilding under the focus would drop the cursor or the keyboard position; the
    // next poll after the focus leaves the list catches up.
    if ($('satellite-list').contains(document.activeElement)) return;
    $('satellite-list').replaceChildren(...data.nodes.map(card));
    if (!busy && !$('satellite-result').classList.contains('success')) {
      message(data.error || (data.enabled && !data.nodes.length ? 'No node is registered yet.' : ''), data.error ? 'error' : '');
    }
  }
  async function poll() {
    if (document.hidden) return;
    try { render(await api('satellites')); }
    catch (error) { message(error.message, 'error'); }
  }
  poll();
  setInterval(poll, 3000);
})();
