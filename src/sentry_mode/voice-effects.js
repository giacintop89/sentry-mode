for (const prefix of ['tts','ptt']) {
  const preset=document.getElementById(prefix+'-preset'),pitch=document.getElementById(prefix+'-pitch');
  const volume=document.getElementById(prefix+'-volume');
  preset.addEventListener('change',()=>{
    if(preset.value!=='custom')pitch.value={natural:0,demon:-7,chipmunk:7}[preset.value];
    document.getElementById(prefix+'-pitch-value').textContent=pitch.value;
  });
  pitch.addEventListener('input',()=>{preset.value='custom';document.getElementById(prefix+'-pitch-value').textContent=pitch.value;});
  volume.addEventListener('input',()=>{document.getElementById(prefix+'-volume-value').textContent=volume.value;});
}
window.getVoiceEffects=prefix=>({preset:document.getElementById(prefix+'-preset').value,
  pitch:Number(document.getElementById(prefix+'-pitch').value),volume:Number(document.getElementById(prefix+'-volume').value)});
window.setVoiceVolume=(prefix,value)=>{
  document.getElementById(prefix+'-volume').value=value;document.getElementById(prefix+'-volume-value').textContent=value;
};
