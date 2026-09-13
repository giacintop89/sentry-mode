// Send fixed 100 ms PCM16 packets; the server accepts mono at 48 kHz.
class PhonePCM extends AudioWorkletProcessor {
  constructor() {
    super(); this.samples = new Float32Array(4800); this.used = 0; this.active = true;
    this.port.onmessage = event => {
      if (event.data === 'stop') { this.active = false; this.flush(); this.port.postMessage('stopped'); }
    };
  }
  flush() {
    if (!this.used) return;
    const data = new ArrayBuffer(this.used * 2), view = new DataView(data);
    for (let i = 0; i < this.used; i++) {
      const value = Math.max(-1, Math.min(1, this.samples[i]));
      view.setInt16(i * 2, value * (value < 0 ? 32768 : 32767), true);
    }
    this.port.postMessage(data, [data]); this.used = 0;
  }
  process(inputs) {
    const channel = inputs[0]?.[0];
    if (this.active && channel) for (const value of channel) {
      this.samples[this.used++] = value;
      if (this.used === this.samples.length) this.flush();
    }
    return true;
  }
}
registerProcessor('phone-pcm', PhonePCM);
