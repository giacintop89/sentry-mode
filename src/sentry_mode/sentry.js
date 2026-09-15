(() => {
  const $ = id => document.getElementById(id);
  let config, revision, status, video, editing = -1, busy = false, eventId = -1;
  const dirty = new Set();
  const num = id => Number($(id).value);
  function message(text, kind = '') { $('sentry-result').textContent = text; $('sentry-result').title = text; $('sentry-result').className = 'result ' + kind; }
  // Rule editor notices sit beside its buttons, where the user is looking.
  function ruleMessage(text, kind = '') { $('rule-result').textContent = text; $('rule-result').title = text; $('rule-result').className = 'result ' + kind; }
  // A cross-origin frame ignores window.confirm and answers it "no" without asking, and the
  // dashboard embeds this app, so buttons that act for real confirm in the page: the first
  // click arms the button, the second one runs it, and it disarms itself after a few seconds.
  const arming = new Map();
  function armButton(button, label = 'Click again') {
    const pending = arming.get(button);
    if (pending) { clearTimeout(pending); arming.delete(button); button.textContent = button.dataset.label; return true; }
    button.dataset.label = button.textContent;
    button.textContent = label;
    arming.set(button, setTimeout(() => {
      arming.delete(button);
      button.textContent = button.dataset.label;
    }, 6000));
    return false;
  }
  async function api(path, body, post = false) {
    const headers = post ? {'X-Sentry-Mode-Control':'1'} : {};
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
    $(id).addEventListener('input', event => { if (event.target.id==='command-select'||event.target.dataset.f==='sound-file') return; dirty.add(section); locks(); });
  }
  function options(select, entries, selected) {
    select.replaceChildren();
    for (const [value, label] of entries) { const o=document.createElement('option'); o.value=value;o.textContent=label;select.append(o); }
    if (selected !== undefined) select.value = selected;
  }
  function refreshCommands() {
    for (const step of steps()) if (step.dataset.type === 'ssh') fillCommands(step, f(step,'command').value);
    options($('command-select'), [['','New command'],...Object.keys(config.ssh_commands).map(id=>[id,id])]);
  }
  // A rule keeps the command id, so a command deleted from Integrations stays visible here
  // as a missing entry instead of silently becoming another command.
  function fillCommands(step, selected) {
    const entries = Object.keys(config.ssh_commands).map(id=>[id,id]);
    if (selected && !config.ssh_commands[selected]) entries.unshift([selected, 'Missing command: ' + selected]);
    if (!entries.length) entries.push(['', 'No saved commands yet — add one in Integrations']);
    options(f(step,'command'), entries, selected || entries[0][0]);
  }
  // One line for the sequence: steps that start together are joined with +, the rest with an arrow.
  function sequence(actions) {
    const groups=[];
    for(const action of actions){
      if(groups.length&&action.with_previous)groups[groups.length-1].push(action.type.toUpperCase());
      else groups.push([action.type.toUpperCase()]);
    }
    return groups.map(group=>group.join('+')).join(' → ');
  }
  function renderRules() {
    $('rules').replaceChildren();
    config.rules.forEach((rule, index) => {
      const row=document.createElement('button');row.type='button';row.className='rule'+(index===editing?' editing':'');const text=document.createElement('span');
      if(index===editing)row.setAttribute('aria-current','true');
      const title=document.createElement('strong');title.textContent=rule.name;
      const detail=document.createElement('small');detail.textContent=(rule.enabled?'Enabled':'Disabled')+' · '+rule.object+' · '+sequence(rule.actions);
      text.append(title,detail);row.append(text);
      row.addEventListener('click',()=>{if(index===editing)return;if(dirty.has('rule'))return ruleMessage('Save the current rule or reset the editor first.','error');loadRule(index);});$('rules').append(row);
    });locks();
  }
  // A rule's actions are an ordered list of steps: each one is a copy of its type's
  // template, so the same kind of action can appear as often as the sequence needs it.
  // Ids inside a copy are suffixed to stay unique for their labels; the code reaches a
  // step's own fields through data-f instead.
  const STEP_LABELS={photo:'Take a picture',audio:'Record audio',video:'Record a video',tts:'Speak a message',tune:'Play a tune',sound:'Play an audio file',telegram:'Send a Telegram message',ssh:'Run a saved SSH command',wait:'Wait'};
  const MAX_STEPS=16;
  let stepSeq=0;
  const f=(step,name)=>step.querySelector('[data-f="'+name+'"]');
  const val=(step,name)=>Number(f(step,name).value);
  const steps=()=>[...$('steps').children];
  const ttsSteps=()=>steps().filter(step=>step.dataset.type==='tts');
  function touched(){dirty.add('rule');locks();}
  function makeStep(type) {
    const n=++stepSeq;
    const step=$('step-frame').content.firstElementChild.cloneNode(true);
    const body=$('step-'+type).content.cloneNode(true);
    for(const el of body.querySelectorAll('[id]'))el.id+='-'+n;
    for(const el of body.querySelectorAll('label[for]'))el.htmlFor+='-'+n;
    step.dataset.type=type;
    step.querySelector('.step-title').textContent=STEP_LABELS[type];
    step.querySelector('.step-body').append(body);
    return step;
  }
  function fillStep(step,a) {
    step.querySelector('.step-together').checked=!!a.with_previous;
    const set=(name,value)=>{f(step,name).value=value;};
    if(a.type==='photo'){set('photo-count',a.count??1);set('photo-interval',a.interval_seconds??2);f(step,'photo-interval').disabled=(a.count??1)<=1;}
    else if(a.type==='audio')set('audio-duration',a.duration_seconds??10);
    else if(a.type==='video'){set('video-duration',a.duration_seconds??10);f(step,'video-audio').checked=a.audio!==false;}
    else if(a.type==='tts'){
      set('text',a.text??'Hello. Please wait here.');renderVoices(step,a.voice||'en');set('rate',a.rate||175);
      set('preset',a.effects?.preset||'natural');set('pitch',a.effects?.pitch||0);set('volume',a.effects?.volume??60);
      fillSoundboard(step);
    }
    else if(a.type==='tune'){set('tune',a.tune||'chime');set('tune-repeat',a.repeat||1);set('tune-volume',a.volume??60);set('tune-pitch',a.pitch??0);}
    else if(a.type==='sound'){renderSounds(step,a.sound||'');set('sound-repeat',a.repeat||1);set('sound-volume',a.volume??80);}
    else if(a.type==='telegram'){set('telegram-text',a.text??'Sentry detected a person at the entrance.');f(step,'telegram-silent').checked=!!a.silent;}
    else if(a.type==='ssh')fillCommands(step,a.command_id||'');
    else if(a.type==='wait')set('wait-seconds',a.seconds??2);
  }
  function readStep(step) {
    const type=step.dataset.type,a={type,with_previous:step.querySelector('.step-together').checked};
    if(type==='photo')return {...a,count:val(step,'photo-count'),interval_seconds:val(step,'photo-interval')};
    if(type==='audio')return {...a,duration_seconds:val(step,'audio-duration')};
    if(type==='video')return {...a,duration_seconds:val(step,'video-duration'),audio:f(step,'video-audio').checked};
    if(type==='tts')return {...a,text:f(step,'text').value,voice:f(step,'voice').value,rate:val(step,'rate'),
      effects:{preset:f(step,'preset').value,pitch:val(step,'pitch'),volume:val(step,'volume')}};
    if(type==='tune')return {...a,tune:f(step,'tune').value,repeat:val(step,'tune-repeat'),volume:val(step,'tune-volume'),pitch:val(step,'tune-pitch')};
    if(type==='sound')return {...a,sound:f(step,'sound').value,repeat:val(step,'sound-repeat'),volume:val(step,'sound-volume')};
    if(type==='telegram')return {...a,text:f(step,'telegram-text').value,silent:f(step,'telegram-silent').checked};
    if(type==='ssh')return {...a,command_id:f(step,'command').value};
    return {...a,seconds:val(step,'wait-seconds')};
  }
  // A step can be closed to keep a long sequence readable; its header then says what it
  // does, so the order stays legible without opening anything.
  function openStep(step,open) {
    step.querySelector('.step-body').hidden=!open;
    step.querySelector('.step-toggle').setAttribute('aria-expanded',String(open));
    step.querySelector('.step-caret').textContent=open?'▾':'▸';
    if(!open)stepSummary(step);
  }
  const shorten=text=>text.trim().length>48?text.trim().slice(0,47)+'…':text.trim();
  const label=(step,name)=>f(step,name).selectedOptions[0]?.textContent||'';
  // "Italiano · Paola · Natural" is the speaker's name to a reader of the summary line.
  const voiceName=step=>label(step,'voice').split(' · ')[1]||label(step,'voice');
  function stepSummary(step) {
    const type=step.dataset.type,say=text=>{step.querySelector('.step-summary').textContent=text;};
    if(type==='photo')return say(val(step,'photo-count')>1?val(step,'photo-count')+' pictures, every '+val(step,'photo-interval')+' s':'one picture');
    if(type==='audio')return say(val(step,'audio-duration')+' s of sound');
    if(type==='video')return say(val(step,'video-duration')+' s'+(f(step,'video-audio').checked?' with sound':', silent'));
    if(type==='tts')return say(shorten(f(step,'text').value)+' · '+voiceName(step));
    if(type==='tune')return say(label(step,'tune')+(val(step,'tune-repeat')>1?' ×'+val(step,'tune-repeat'):'')
      +(val(step,'tune-pitch')?' at '+(val(step,'tune-pitch')>0?'+':'')+val(step,'tune-pitch')+' st':''));
    if(type==='sound')return say(shorten(label(step,'sound')));
    if(type==='telegram')return say(shorten(f(step,'telegram-text').value));
    if(type==='ssh')return say(shorten(label(step,'command')));
    say(val(step,'wait-seconds')+' s');
  }
  // Nothing runs alongside the first step, and a rule always keeps at least one step.
  function renderSteps() {
    const all=steps();
    all.forEach((step,index)=>{
      step.querySelector('.step-with').hidden=index===0;
      if(index===0)step.querySelector('.step-together').checked=false;
      const [up,down]=step.querySelectorAll('.step-move');
      up.disabled=index===0;down.disabled=index===all.length-1;
      step.querySelector('.step-remove').disabled=all.length<2;
    });
    $('add-step').disabled=all.length>=MAX_STEPS;
    $('steps-help').textContent=all.length>=MAX_STEPS?'A rule runs at most '+MAX_STEPS+' steps. Remove one to add another.'
      :'A rule runs up to '+MAX_STEPS+' steps. A step that fails is logged and the sequence carries on.';
  }
  function setSteps(actions) {
    $('steps').replaceChildren();
    for(const action of actions){const step=makeStep(action.type);$('steps').append(step);fillStep(step,action);}
    for(const step of ttsSteps())syncSoundboard(step);
    // One step is the whole rule, so it opens; a sequence opens closed and is read as a list.
    for(const step of steps()){stepSummary(step);openStep(step,actions.length<2);}
    renderSteps();
  }
  $('add-step').addEventListener('click',()=>{
    if(steps().length>=MAX_STEPS)return;
    const step=makeStep($('new-step').value);
    $('steps').append(step);fillStep(step,{type:$('new-step').value});
    stepSummary(step);renderSteps();touched();ruleMessage('');
    step.scrollIntoView({block:'nearest'});
  });
  $('steps').addEventListener('click',event=>{
    const button=event.target.closest('button');if(!button)return;
    const step=button.closest('.step');
    if(button.classList.contains('step-move')) {
      const other=Number(button.dataset.move)<0?step.previousElementSibling:step.nextElementSibling;
      if(!other)return;
      if(other===step.previousElementSibling)other.before(step);else other.after(step);
      renderSteps();touched();return;
    }
    if(button.classList.contains('step-toggle')) {
      openStep(step,step.querySelector('.step-body').hidden);return;
    }
    if(button.classList.contains('step-remove')) {
      if(steps().length<2)return;
      step.remove();renderSteps();touched();return;
    }
    const test=button.dataset.test;
    if(test==='message')testMessage(step,button);
    else if(test==='tune')testTune(step,button);
    else if(test==='telegram'||test==='ssh')testAction(readStep(step),button);
    else if(test==='sound-upload')f(step,'sound-file').click();
    else if(test==='sound-preview')previewSound(step);
    else if(test==='sound-delete')deleteSound(step,button);
  });
  $('steps').addEventListener('change',event=>{
    const step=event.target.closest('.step');if(!step)return;
    const name=event.target.dataset.f;
    if(name==='soundboard')useSoundboard(step,event.target.value);
    else if(name==='sound')renderSounds(step);
    else if(name==='sound-file')uploadSound(step);
    stepSummary(step);
  });
  $('steps').addEventListener('input',event=>{
    const step=event.target.closest('.step');if(!step)return;
    stepSummary(step);
    if(step.dataset.type==='tts')syncSoundboard(step);
    if(event.target.dataset.f==='photo-count')f(step,'photo-interval').disabled=val(step,'photo-count')<=1;
  });
  $('rule-form').addEventListener('invalid',event=>{
    const step=event.target.closest?.('.step');
    if(step)openStep(step,true);
  },true);
  function showRegion(){$('region-fields').hidden=!$('use-region').checked;}
  $('use-region').addEventListener('change',showRegion);
  // Speaking the draft announcement uses the same speaker endpoint as the Voice view,
  // so it is heard exactly as a trigger would say it, without saving or arming the rule.
  async function testMessage(step,button) {
    if(!f(step,'text').reportValidity())return;
    button.disabled=true;button.textContent='Speaking…';message('');
    try{const data=await api('speech',{text:f(step,'text').value,voice:f(step,'voice').value,rate:val(step,'rate'),
      effects:{preset:f(step,'preset').value,pitch:val(step,'pitch'),volume:val(step,'volume')}},true);message(data.message,'success');}
    catch(error){message(error.message,'error');}
    finally{button.disabled=false;button.textContent='Test message';}
  }
  async function testTune(step,button) {
    button.disabled=true;button.textContent='Playing…';message('');
    try{const data=await api('tunes/play',{tune:f(step,'tune').value,repeat:val(step,'tune-repeat'),volume:val(step,'tune-volume'),pitch:val(step,'tune-pitch')},true);message(data.message,'success');}
    catch(error){message(error.message,'error');}
    finally{button.disabled=false;button.textContent='Test tune';}
  }
  // Telegram and SSH have nothing local to rehearse with, so their test runs through the
  // Sentry executor as a rule of one action: the draft is validated, refused while armed,
  // logged in the event log, and logged only when test mode is on, exactly like a trigger.
  // The button says what it does, so it acts on the first click; Test rule, which can fire
  // several of these at once without saying so, is the one that still asks.
  async function testAction(action,button) {
    if(!$('rule-form').reportValidity())return;
    button.disabled=true;message('Testing this step on the node…');
    try{message((await api('sentry/rules/test',{...collectRule(),actions:[{...action,with_previous:false}]},true)).message,'success');}
    catch(error){message(error.message,'error');}
    finally{button.disabled=false;}
  }
  function loadRule(index) {
    editing=index;const r=config.rules[index]||{name:'',enabled:true,object:'person',min_confidence:.7,min_count:1,consecutive_detections:3,rearm_after_absence_seconds:10,cooldown_seconds:60,region:null,actions:[{type:'tts',text:'Hello. Please wait here.',voice:'en',rate:175,effects:{preset:'natural',pitch:0,volume:60}}]};
    // The rule library already highlights the rule being edited, so the heading only
    // appears for a new rule, which is highlighted nowhere.
    $('rule-heading').textContent=index<0?'New rule':'Edit rule · '+r.name;$('rule-heading').hidden=index>=0;
    for(const [id,value] of Object.entries({'rule-name':r.name,'rule-object':r.object,'rule-confidence':r.min_confidence*100,'rule-count':r.min_count,'rule-hits':r.consecutive_detections,'rule-absence':r.rearm_after_absence_seconds,'rule-cooldown':r.cooldown_seconds}))$(id).value=value;
    $('rule-enabled').checked=r.enabled;$('use-region').checked=!!r.region;
    ['left','top','right','bottom'].forEach((side,i)=>$('region-'+side).value=(r.region||[0,0,1,1])[i]*100);
    setSteps(r.actions);
    ruleMessage('');dirty.delete('rule');showRegion();renderRules();
  }
  // Saved messages are copied into the rule, so deleting one from the soundboard never breaks a rule.
  let soundboard=[];
  // Every voice installed on the node is offered per step, so two steps can answer in
  // different voices; a rule keeps the voice id, so one that is no longer installed stays
  // visible as a missing entry instead of quietly speaking with another voice.
  let voices=[];
  function renderVoices(step,selected=f(step,'voice').value) {
    const select=f(step,'voice');
    if(!voices.length){
      if(selected&&![...select.options].some(o=>o.value===selected))select.add(new Option(selected,selected));
      if(selected)select.value=selected;
      return;
    }
    const entries=voices.map(v=>[v.id,v.label+' · '+v.quality]);
    if(selected&&!voices.some(v=>v.id===selected))entries.unshift([selected,'Missing voice: '+selected]);
    options(select,entries,selected||entries[0][0]);
  }
  async function loadVoices() {
    try{voices=(await api('speech/voices')).voices;}catch{voices=[];}
    // A closed step names its voice, so its summary is rewritten once the names are known.
    for(const step of ttsSteps()){renderVoices(step);stepSummary(step);}
  }
  function syncSoundboard(step) {
    const match=soundboard.find(m=>m.text===f(step,'text').value&&(m.voice==null||m.voice===f(step,'voice').value)
      &&(m.rate==null||m.rate===val(step,'rate'))&&(m.effects?.preset||'natural')===f(step,'preset').value
      &&(m.effects?.preset!=='custom'||Number(m.effects.pitch)===val(step,'pitch'))&&(m.effects?.volume==null||m.effects.volume===val(step,'volume')));
    f(step,'soundboard').value=match?match.id:'';
  }
  function useSoundboard(step,id) {
    const m=soundboard.find(item=>item.id===id);if(!m)return;
    f(step,'text').value=m.text;
    if(m.voice!=null)renderVoices(step,m.voice);
    if(m.rate!=null)f(step,'rate').value=m.rate;
    f(step,'preset').value=m.effects?.preset||'natural';f(step,'pitch').value=m.effects?.pitch||0;
    if(m.effects?.volume!=null)f(step,'volume').value=m.effects.volume;
  }
  function fillSoundboard(step) {
    options(f(step,'soundboard'),[['','Custom message'],...soundboard.map(m=>[m.id,m.text.length>60?m.text.slice(0,59)+'…':m.text])],'');
    f(step,'soundboard-help').textContent=soundboard.length?'Copies the saved text, voice, speed and modification into this rule.':'Nothing saved yet. Save a message from the soundboard on the Voice page.';
  }
  async function loadSoundboard() {
    try{soundboard=(await api('soundboard')).messages;}catch{soundboard=[];}
    for(const step of ttsSteps()){fillSoundboard(step);syncSoundboard(step);}
  }
  // Audio files live on the node and are shared by every rule; a rule stores only the file id.
  let sounds=[];
  function renderSounds(step,selected=f(step,'sound').value) {
    const entries=sounds.map(s=>[s.id,s.name+' · '+s.seconds+' s']);
    if(!entries.length)entries.push(['','No audio files yet — upload one']);
    if(selected&&!sounds.some(s=>s.id===selected))entries.unshift([selected,'Missing file: '+selected]);
    options(f(step,'sound'),entries,selected||entries[0][0]);
    const known=sounds.some(s=>s.id===f(step,'sound').value);
    step.querySelector('[data-test=sound-preview]').disabled=step.querySelector('[data-test=sound-delete]').disabled=!known;
  }
  async function loadSounds() {
    try{sounds=(await api('sounds')).sounds;
      for(const step of steps())if(step.dataset.type==='sound'){renderSounds(step);stepSummary(step);}}
    catch(error){ruleMessage(error.message,'error');}
  }
  async function uploadSound(step) {
    const input=f(step,'sound-file'),file=input.files[0];input.value='';if(!file)return;
    if(file.size>10*1024*1024)return ruleMessage('Audio files are limited to 10 MB.','error');
    const upload=step.querySelector('[data-test=sound-upload]');
    ruleMessage('Uploading and converting '+file.name+'…');upload.disabled=true;
    try{
      const r=await fetch('/api/sounds/upload',{method:'POST',headers:{'X-Sentry-Mode-Control':'1','Content-Type':'application/octet-stream','X-Sound-Name':encodeURIComponent(file.name)},body:file});
      const data=await r.json();if(!r.ok)throw Error(data.error||'Upload failed.');
      sounds=data.sounds;renderSounds(step,data.saved);touched();ruleMessage('Uploaded. Save the rule to use it.','success');
    }catch(error){ruleMessage(error.message,'error');}
    finally{upload.disabled=false;}
  }
  async function previewSound(step) {
    const button=step.querySelector('[data-test=sound-preview]');
    button.disabled=true;ruleMessage('Playing on the node…');
    try{ruleMessage((await api('sounds/play',{id:f(step,'sound').value},true)).message,'success');}
    catch(error){ruleMessage(error.message,'error');}
    finally{renderSounds(step);}
  }
  async function deleteSound(step,button) {
    const id=f(step,'sound').value;if(!id)return;
    if(!armButton(button)){ruleMessage('This deletes the audio file from the node. Click again to delete.','error');return;}
    try{
      sounds=(await api('sounds/delete',{id},true)).sounds;
      for(const other of steps())if(other.dataset.type==='sound')renderSounds(other,other===step?'':undefined);
      ruleMessage('Audio file deleted.','success');
    }catch(error){ruleMessage(error.message,'error');}
  }
  // Free-text object type, checked against the detector's categories before saving.
  let objects=[];
  $('rule-object').addEventListener('input',()=>{const value=$('rule-object').value.trim().toLowerCase();
    $('rule-object').setCustomValidity(!value||objects.includes(value)?'':'Unknown object type. Start typing to see supported categories.');});
  function collectRule() {
    const actions=steps().map(readStep);
    if(actions.length)actions[0].with_previous=false;
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
  $('new-rule').addEventListener('click',()=>{if(dirty.has('rule'))return ruleMessage('Save the current rule or reset the editor first.','error');loadRule(-1);});
  const reset=document.createElement('button');reset.type='button';reset.className='secondary';reset.textContent='Reset editor';reset.addEventListener('click',()=>loadRule(editing));$('test-rule').after(reset);
  const ruleResult=document.createElement('p');ruleResult.id='rule-result';ruleResult.className='result';ruleResult.setAttribute('role','status');reset.after(ruleResult);
  // Testing runs the editor's actions once, without saving or waiting for a detection.
  $('test-rule').addEventListener('click',async()=>{
    if(!$('rule-form').reportValidity())return;
    const rule=collectRule();
    // A test always runs for real now, so the warning no longer depends on test mode.
    if(rule.actions.some(a=>a.type==='ssh'||a.type==='telegram')
      &&!armButton($('test-rule'))){ruleMessage('This runs the actions for real, including SSH commands and Telegram messages. Click again to run.','error');return;}
    $('test-rule').disabled=true;ruleMessage('Testing this rule on the node…');
    try{ruleMessage((await api('sentry/rules/test',rule,true)).message,'success');}
    catch(error){ruleMessage(error.message,'error');}
    finally{$('test-rule').disabled=false;}
  });
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
      if(latest!==eventId){if(status.events.some(e=>e.id>eventId&&/^(Photo|Video|Audio|Message) saved/.test(e.message)))loadCaptures();eventId=latest;$('events').replaceChildren();for(const e of status.events){const row=document.createElement('div');row.className='event';row.dataset.kind=e.kind;const meta=document.createElement('small');meta.textContent=new Date(e.time).toLocaleTimeString()+' · '+e.kind+(e.rule?' · '+e.rule:'');const body=document.createElement('div');body.textContent=e.message;row.append(meta,body);$('events').append(row);}if(!status.events.length)$('events').textContent='No events yet.';}
      if(config&&status.revision!==revision&&!busy)message('Settings changed in another tab. Reload this page before editing.','error');
      locks();
    }catch(error){message(error.message,'error');}
  }
  async function loadCaptures() {
    let captures=[];
    try{captures=(await api('captures')).captures;}catch(error){$('captures-result').className='result error';$('captures-result').textContent=error.message;return;}
    $('captures-empty').hidden=captures.length>0;
    const cards=captures.map(capture=>{
      const card=document.createElement('li');card.className='capture-card';
      const url='/captures/'+encodeURIComponent(capture.name);
      const media=document.createElement(capture.kind==='photo'?'img':capture.kind);
      media.src=url;
      if(capture.kind==='video'){media.controls=true;media.preload='metadata';media.playsInline=true;}
      else if(capture.kind==='audio'){media.controls=true;media.preload='metadata';}
      else{media.loading='lazy';media.alt='Photo from rule '+capture.rule;}
      if(capture.kind!=='photo')card.append(media);
      else{const open=document.createElement('a');open.href=url;open.target='_blank';open.rel='noopener';open.append(media);card.append(open);}
      const meta=document.createElement('div');meta.className='capture-meta';
      const title=document.createElement('strong');title.textContent={photo:'Photo · ',video:'Video · ',audio:'Audio · '}[capture.kind]+capture.rule.replaceAll('-',' ');
      const when=document.createElement('small');when.textContent=new Date(capture.time).toLocaleString()+' · '+(capture.size<1048576?Math.max(1,Math.round(capture.size/1024))+' KB':(capture.size/1048576).toFixed(1)+' MB');
      const remove=document.createElement('button');remove.type='button';remove.className='danger';remove.textContent='Delete';
      remove.addEventListener('click',async()=>{remove.disabled=true;
        try{await api('captures/delete',{name:capture.name},true);await loadCaptures();}
        catch(error){remove.disabled=false;$('captures-result').className='result error';$('captures-result').textContent=error.message;}});
      meta.append(title,when,remove);card.append(meta);return card;
    });
    $('captures').replaceChildren(...cards);$('captures-result').textContent='';
  }
  $('tab-captures').addEventListener('click',loadCaptures);
  async function init(){try{const data=await api('sentry/config');config=data.config;revision=data.revision;
    $('test-mode').checked=config.test_mode;$('detection-fps').value=config.detection_fps;
    loadTelegram(data.telegram_token_configured);
    objects=data.objects;options($('rule-objects'),objects.map(x=>[x,x]));refreshCommands();renderRules();loadRule(config.rules.length?0:-1);loadCommand('');await Promise.all([loadVoices(),loadSoundboard(),loadSounds(),loadCaptures(),poll()]);message(data.error||'',data.error?'error':'');
  }catch(error){message(error.message,'error');}finally{locks();}}
  init();setInterval(poll,1500);
})();
