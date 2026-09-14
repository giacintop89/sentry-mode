(() => {
  const $ = id => document.getElementById(id);
  let config, revision, status, video, editing = -1, busy = false, eventId = -1;
  const dirty = new Set();
  const num = id => Number($(id).value);
  function message(text, kind = '') { $('sentry-result').textContent = text; $('sentry-result').className = 'result ' + kind; }
  async function api(path, body, post = false) {
    const headers = post ? {'X-Sentry-Node-Control':'1'} : {};
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    const r = await fetch('/api/' + path, {method:post?'POST':'GET', headers,
      body:body===undefined?undefined:JSON.stringify(body)});
    const data = await r.json(); if (!r.ok) throw Error(data.error || 'Command failed.'); return data;
  }
  function locks() {
    const armed = !!status?.armed;
    for (const id of ['settings','rule-editor','ssh-editor','telegram-editor']) $(id).disabled = !config || busy || armed;
    $('new-rule').disabled = !config || busy || armed;
    $('arm').disabled = !config || busy || armed || dirty.size > 0 || !config.rules.some(r=>r.enabled);
    $('disarm').disabled = busy || !armed;
    $('delete-rule').disabled = editing < 0;
    for (const button of $('rules').querySelectorAll('button')) button.disabled = busy || armed;
    $('save-help').textContent = armed ? 'Disarm before changing saved rules or actions.' : dirty.size ? 'Unsaved changes: '+[...dirty].join(', ')+'. Save them before starting Sentry.' : '';
  }
  for (const [id, section] of [['settings','settings'],['rule-editor','rule'],['ssh-editor','command'],['telegram-editor','Telegram']]) {
    $(id).addEventListener('input', event => { if (event.target.id==='command-select') return; dirty.add(section); locks(); });
  }
  function options(select, entries, selected) {
    select.replaceChildren();
    for (const [value, label] of entries) { const o=document.createElement('option'); o.value=value;o.textContent=label;select.append(o); }
    if (selected !== undefined) select.value = selected;
  }
  function refreshCommands() {
    const current = $('rule-command').value;
    options($('rule-command'), Object.keys(config.ssh_commands).map(id=>[id,id]), current);
    options($('command-select'), [['','New command'],...Object.keys(config.ssh_commands).map(id=>[id,id])]);
  }
  function renderRules() {
    $('rules').replaceChildren();
    config.rules.forEach((rule, index) => {
      const row=document.createElement('button');row.type='button';row.className='rule'+(index===editing?' editing':'');const text=document.createElement('span');
      if(index===editing)row.setAttribute('aria-current','true');
      const title=document.createElement('strong');title.textContent=rule.name;
      const detail=document.createElement('small');detail.textContent=(rule.enabled?'Enabled':'Disabled')+' · '+rule.object+' · '+rule.actions.map(a=>a.type.toUpperCase()).join(' + ');
      text.append(title,detail);row.append(text);
      row.addEventListener('click',()=>{if(index===editing)return;if(dirty.has('rule'))return message('Save the current rule or reset the editor first.','error');loadRule(index);});$('rules').append(row);
    });locks();
  }
  function showActionOptions() {
    $('tts-options').hidden=!$('use-tts').checked;$('ssh-options').hidden=!$('use-ssh').checked;
    $('telegram-options').hidden=!$('use-telegram').checked;
    $('rule-telegram-text').required=$('use-telegram').checked;
    $('region-fields').hidden=!$('use-region').checked;
    $('rule-text').required=$('use-tts').checked;
    $('rule-command').required=$('use-ssh').checked;
  }
  for(const id of ['use-tts','use-ssh','use-telegram','use-region'])$(id).addEventListener('change',showActionOptions);
  function loadRule(index) {
    editing=index;const r=config.rules[index]||{name:'',enabled:true,object:'person',min_confidence:.7,min_count:1,consecutive_detections:3,rearm_after_absence_seconds:10,cooldown_seconds:60,region:null,actions:[{type:'tts',text:'Hello. Please wait here.',voice:'en',rate:175,effects:{preset:'natural',pitch:0,volume:60}}]};
    $('rule-heading').textContent=index<0?'New rule':'Edit rule · '+r.name;
    for(const [id,value] of Object.entries({'rule-name':r.name,'rule-object':r.object,'rule-confidence':r.min_confidence*100,'rule-count':r.min_count,'rule-hits':r.consecutive_detections,'rule-absence':r.rearm_after_absence_seconds,'rule-cooldown':r.cooldown_seconds}))$(id).value=value;
    $('rule-enabled').checked=r.enabled;$('use-region').checked=!!r.region;
    ['left','top','right','bottom'].forEach((side,i)=>$('region-'+side).value=(r.region||[0,0,1,1])[i]*100);
    const t=r.actions.find(a=>a.type==='tts'),ssh=r.actions.find(a=>a.type==='ssh');
    $('use-tts').checked=!!t;$('use-ssh').checked=!!ssh;
    voiceOption(t?.voice);$('rule-text').value=t?.text||'';$('rule-voice').value=t?.voice||'en';$('rule-rate').value=t?.rate||175;
    $('rule-preset').value=t?.effects?.preset||'natural';$('rule-pitch').value=t?.effects?.pitch||0;$('rule-volume').value=t?.effects?.volume??60;
    if(ssh)$('rule-command').value=ssh.command_id;
    const telegram=r.actions.find(a=>a.type==='telegram');
    $('use-telegram').checked=!!telegram;
    $('rule-telegram-text').value=telegram?.text||'Sentry detected a person at the entrance.';
    $('rule-telegram-silent').checked=telegram?.silent||false;
    syncSoundboard();dirty.delete('rule');showActionOptions();renderRules();
  }
  // Saved messages are copied into the rule, so deleting one from the soundboard never breaks a rule.
  let soundboard=[];
  const ttsFields=['rule-text','rule-voice','rule-rate','rule-preset','rule-pitch','rule-volume'];
  function voiceOption(id) {
    if(id&&![...$('rule-voice').options].some(o=>o.value===id))$('rule-voice').add(new Option(id,id));
  }
  function syncSoundboard() {
    const match=soundboard.find(m=>m.text===$('rule-text').value&&(m.voice==null||m.voice===$('rule-voice').value)
      &&(m.rate==null||m.rate===num('rule-rate'))&&(m.effects?.preset||'natural')===$('rule-preset').value
      &&(m.effects?.preset!=='custom'||Number(m.effects.pitch)===num('rule-pitch'))&&(m.effects?.volume==null||m.effects.volume===num('rule-volume')));
    $('rule-soundboard').value=match?match.id:'';
  }
  function useSoundboard(id) {
    const m=soundboard.find(item=>item.id===id);if(!m)return;
    voiceOption(m.voice);$('rule-text').value=m.text;
    if(m.voice!=null)$('rule-voice').value=m.voice;
    if(m.rate!=null)$('rule-rate').value=m.rate;
    $('rule-preset').value=m.effects?.preset||'natural';$('rule-pitch').value=m.effects?.pitch||0;
    if(m.effects?.volume!=null)$('rule-volume').value=m.effects.volume;
  }
  async function loadSoundboard() {
    try{soundboard=(await api('soundboard')).messages;}catch{soundboard=[];}
    options($('rule-soundboard'),[['','Custom message'],...soundboard.map(m=>[m.id,m.text.length>60?m.text.slice(0,59)+'…':m.text])]);
    $('rule-soundboard-help').textContent=soundboard.length?'Pick a saved message to copy its text, voice, speed and modification into this rule.':'No saved messages yet. Save one from the soundboard on the Voice page.';
    syncSoundboard();
  }
  $('rule-soundboard').addEventListener('change',()=>useSoundboard($('rule-soundboard').value));
  for(const id of ttsFields)$(id).addEventListener('input',syncSoundboard);
  // Free-text object type, checked against the detector's categories before saving.
  let objects=[];
  $('rule-object').addEventListener('input',()=>{const value=$('rule-object').value.trim().toLowerCase();
    $('rule-object').setCustomValidity(!value||objects.includes(value)?'':'Unknown object type. Start typing to see supported categories.');});
  function collectRule() {
    const actions=[];
    if($('use-tts').checked)actions.push({type:'tts',text:$('rule-text').value,voice:$('rule-voice').value,rate:num('rule-rate'),effects:{preset:$('rule-preset').value,pitch:num('rule-pitch'),volume:num('rule-volume')}});
    if($('use-ssh').checked)actions.push({type:'ssh',command_id:$('rule-command').value});
    if($('use-telegram').checked)actions.push({type:'telegram',text:$('rule-telegram-text').value,silent:$('rule-telegram-silent').checked});
    return {name:$('rule-name').value.trim(),enabled:$('rule-enabled').checked,object:$('rule-object').value.trim().toLowerCase(),min_confidence:num('rule-confidence')/100,min_count:num('rule-count'),consecutive_detections:num('rule-hits'),rearm_after_absence_seconds:num('rule-absence'),cooldown_seconds:num('rule-cooldown'),region:$('use-region').checked?['left','top','right','bottom'].map(s=>num('region-'+s)/100):null,actions};
  }
  async function save(next, section, clearTelegramToken = false) {
    if(busy)return false;busy=true;locks();message('Saving…');
    next.test_mode=$('test-mode').checked;next.detection_fps=num('detection-fps');
    try {
      const data=await api('sentry/config',{config:next,revision,clear_telegram_token:clearTelegramToken},true);config=data.config;revision=data.revision;
      if(section==='Telegram')loadTelegram(data.telegram_token_configured);
      dirty.delete(section);dirty.delete('settings');renderRules();message('Saved.','success');return true;
    }catch(error){message(error.message,'error');return false;}
    finally{busy=false;locks();}
  }
  $('save-settings').addEventListener('click',()=>save(structuredClone(config),'settings'));
  function loadTelegram(hasToken) {
    $('telegram-token').value='';$('telegram-clear').disabled=!hasToken;
    $('telegram-chat').value=config.telegram.chat_id;
    $('telegram-token').placeholder=hasToken?'Token saved — leave blank to keep':'Paste bot token from BotFather';
    $('telegram-state').textContent=hasToken?'Bot token saved on this node.':'No bot token saved yet.';
  }
  $('telegram-form').addEventListener('submit',event=>{
    event.preventDefault();const next=structuredClone(config);
    next.telegram={bot_token:$('telegram-token').value.trim(),chat_id:$('telegram-chat').value.trim()};
    save(next,'Telegram');
  });
  $('telegram-clear').addEventListener('click',async()=>{
    if(await save(structuredClone(config),'Telegram',true))message('Bot token removed.','success');
  });
  $('rule-form').addEventListener('submit',async event=>{event.preventDefault();const next=structuredClone(config);const rule=collectRule();if(editing<0)next.rules.push(rule);else next.rules[editing]=rule;if(await save(next,'rule'))loadRule(editing<0?config.rules.length-1:editing);});
  $('new-rule').addEventListener('click',()=>{if(dirty.has('rule'))return message('Save the current rule or reset the editor first.','error');loadRule(-1);});
  const reset=document.createElement('button');reset.type='button';reset.className='secondary';reset.textContent='Reset editor';reset.addEventListener('click',()=>loadRule(editing));$('delete-rule').after(reset);
  $('delete-rule').addEventListener('click',async()=>{if(editing<0)return;const next=structuredClone(config);next.rules.splice(editing,1);if(await save(next,'rule'))loadRule(config.rules.length?0:-1);});
  function loadCommand(id) {
    const c=config.ssh_commands[id]||{host:'',user:'',port:22,timeout_seconds:5,identity_file:'',command:''};
    $('command-id').value=id;$('command-id').readOnly=!!id;
    for(const [field,key]of [['host','host'],['user','user'],['port','port'],['timeout','timeout_seconds'],['key','identity_file'],['text','command']])$('command-'+field).value=c[key];
    dirty.delete('command');locks();
  }
  $('command-select').addEventListener('change',()=>loadCommand($('command-select').value));
  $('command-form').addEventListener('submit',async event=>{event.preventDefault();const id=$('command-id').value;const next=structuredClone(config);Object.defineProperty(next.ssh_commands,id,{enumerable:true,configurable:true,writable:true,value:{host:$('command-host').value,user:$('command-user').value,port:num('command-port'),timeout_seconds:num('command-timeout'),identity_file:$('command-key').value,command:$('command-text').value}});if(await save(next,'command')){refreshCommands();$('command-select').value=id;loadCommand(id);}});
  $('delete-command').addEventListener('click',async()=>{const id=$('command-select').value;if(!id)return;const next=structuredClone(config);delete next.ssh_commands[id];if(await save(next,'command')){refreshCommands();loadCommand('');}});
  async function action(path) {
    if(busy)return;busy=true;locks();message('Updating…');
    try{await api(path,undefined,true);await poll();message(path.endsWith('/start')?'Started.':'Stopped.','success');}
    catch(error){message(error.message,'error');}
    finally{busy=false;locks();}
  }
  $('arm').addEventListener('click',()=>action('sentry/start'));
  $('disarm').addEventListener('click',()=>action('sentry/stop'));
  async function poll() {
    try {
      [status,video]=await Promise.all([api('sentry/status'),api('video/status')]);
      $('armed-state').textContent=status.armed?(status.test_mode?'Armed · test mode':'Armed · actions enabled'):status.error?'Fault / disarmed':'Disarmed';
      $('armed-state').className='badge'+(status.armed?' armed':'');
      $('monitor-info').textContent=(video.capture_running?'Camera active':'Camera idle')+' · '+(video.running?'Preview enabled':'Preview hidden')+' · '+(status.active_action||status.pending_actions+' queued actions')+(status.error?' · '+status.error:'');
      const latest=status.events[0]?.id||0;
      if(latest!==eventId){eventId=latest;$('events').replaceChildren();for(const e of status.events){const row=document.createElement('div');row.className='event';row.dataset.kind=e.kind;const meta=document.createElement('small');meta.textContent=new Date(e.time).toLocaleTimeString()+' · '+e.kind+(e.rule?' · '+e.rule:'');const body=document.createElement('div');body.textContent=e.message;row.append(meta,body);$('events').append(row);}if(!status.events.length)$('events').textContent='No events yet.';}
      if(config&&status.revision!==revision&&!busy)message('Settings changed in another tab. Reload this page before editing.','error');
      locks();
    }catch(error){message(error.message,'error');}
  }
  async function init(){try{const data=await api('sentry/config');config=data.config;revision=data.revision;
    $('test-mode').checked=config.test_mode;$('detection-fps').value=config.detection_fps;
    loadTelegram(data.telegram_token_configured);
    objects=data.objects;options($('rule-objects'),objects.map(x=>[x,x]));refreshCommands();renderRules();loadRule(config.rules.length?0:-1);loadCommand('');await Promise.all([loadSoundboard(),poll()]);message(data.error||'',data.error?'error':'');
  }catch(error){message(error.message,'error');}finally{locks();}}
  init();setInterval(poll,1500);
})();
