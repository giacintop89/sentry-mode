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
  function ago(seconds) {
    if (seconds === null || seconds === undefined) return '—';
    if (seconds < 90) return Math.round(seconds) + ' s ago';
    if (seconds < 5400) return Math.round(seconds / 60) + ' min ago';
    return Math.round(seconds / 3600) + ' h ago';
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
    const meta = [node.zone && 'zone ' + node.zone, node.profile, node.agent_version && 'agent ' + node.agent_version,
      node.config_revision ? 'configuration ' + node.config_revision : 'installed configuration',
      node.last_seen ? 'heard ' + ago(Date.now() / 1000 - node.last_seen) : 'not heard yet',
      node.clock_status && 'clock ' + node.clock_status];
    f(item, 'meta').textContent = meta.filter(Boolean).join(' · ');
    f(item, 'caption').textContent = 'Sources of ' + node.display_name;
    f(item, 'table-region').setAttribute('aria-label', 'Sources of ' + node.display_name);
    const rows = node.sources.map(source => {
      const row = document.createElement('tr');
      const state = !source.declared ? 'no longer declared' : !source.enabled ? 'disabled'
        : !source.supported ? 'no driver yet' : source.error ? 'error' : source.state;
      const cells = [source.name, source.kind || source.role, state, reading(source), ago(source.last_reading_age_seconds)];
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
    f(item, 'drivers').textContent = 'Kinds a satellite can drive: ' + last.drivers.join(', ') + '. Set "enabled": false to keep a source in the list without reading it.';
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
