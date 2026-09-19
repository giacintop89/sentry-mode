# Cose pendenti — sottosistema satelliti Pico

Aggiornato il 19 settembre 2026.

Questo file è operativo: dice cosa manca, cosa serve per farlo, il comando esatto e come
si vede che è riuscito. Il perché di ogni riga sta in [`firmware/pico/README.md`](../firmware/pico/README.md)
sotto *What is left to do*; l'ordine in cui conviene affrontarle sta nel piano,
[§12.3](sentry-mode-pico-implementation-plan.md). Qui non si spiega niente due volte.

## 1. Serve una mano fisica

Sono le uniche righe che bloccano altre righe. In ordine di resa.

### 1.1 Rimettere in linea `pico-ingresso` — sblocca tre prove

**Serve:** la `pico2_w`, un cavo USB abbastanza lungo da tenere premuto BOOTSEL, un minuto.
La versione 2 dei record ha reso illeggibile ciò che quella scheda teneva: è il percorso di
aggiornamento come è scritto, e costa un riprovisioning.

    # scheda in BOOTSEL, poi copiare:
    build/pico2_w/sentry_firmware.uf2
    # poi, con il bridge sulla console:
    python scripts/pico_bridge.py --config .local/pico-bridge.json --console pico-ingresso
    provision <json>       # nome, hub, radice topic, Wi-Fi
    credentials <json>     # chiavi: ca, cert, key — non authority/certificate/key

**Riuscita:** il nodo compare `online` in `/api/satellites` con `reached_by` diverso da
`bridge`, e `status` sulla console dice radio invece di cavo.

### 1.2 `T14`, una coda che si riempie davvero

**Dipende da 1.1.** Su una scheda cablata la consegna è il frame sul cavo: `queued` resta a
zero comunque, ed è un limite dichiarato, non un difetto. Con il nodo radio in linea:
spegnere il broker, guardare la spool riempirsi, il `link_lost`, la coalescenza, e la
spazzata dopo uno scatto d'orologio.

**Riuscita:** `queue.events` diverso da zero nel nodo, e i conteggi di `drops` che si
muovono quando la coda è piena.

### 1.3 `T06`/`T07` dalla scheda stessa

**Dipende da 1.1.** Quattro certificati sono già stati offerti a questo broker e rifiutati o
accettati apposta, ma tutti da un client Python. Manca l'handshake della scheda rifiutato:
`forget certificate` sulla console, poi provisionarne uno che non deve passare.

### 1.4 Un sensore vero sui morsetti — `T18`/`T19`

**Serve:** un PIR, un contatto reed, un DS18B20 con la sua resistenza da 4,7 kΩ, un
microfono I²S. Finora ogni percorso è stato esercitato dalla scheda che pilota il proprio
pad o legge un bus vuoto.

### 1.5 `T39`, due schede sullo stesso bridge

**Serve:** una seconda scheda. Il bridge porta già una lista; ce n'è sempre stata una sola.

### 1.6 `PICO-10`, lo snapshot

**Serve:** un modulo SPI Arducam con FIFO JPEG. Finché non c'è resta `not built` nel
manifest, e con esso `T32` e `T33`. `video.start` è rifiutato per nome.

### 1.7 Un'interruzione di alimentazione vera

**Serve:** un'alimentazione che si possa togliere a metà di una scrittura. Il watchdog
resetta il chip senza togliergli corrente, e `tear` scrive mezzo record apposta: nessuno dei
due è la tensione che cala durante una cancellazione.

Il riavvio del Pi del 19 settembre è stato una mancanza di corrente vera — la scheda è
tornata con `reset power`, provisioning e configurazione intatti — ma a flash ferma. Conta
come prova che il vault regge uno spegnimento qualunque; non come prova di uno a metà
cancellazione, che è quello che questa riga chiede.

## 2. In corso, senza bisogno di nessuno

### 2.1 `T40`, quarantotto ore

**Primo tentativo perso.** È partito dal flash del 18 settembre alle 10:44 ed è arrivato a
circa 25 ore: il 19 settembre alle 12:11 il Pi si è riavviato, e con lui sono andati il
bridge, il broker (che non parte da solo: `mosquitto` è `disabled`), la corrente sul cavo
della scheda — che è tornata con `reset power` — e i log, che stavano in `/tmp`.

**Secondo tentativo in corso** dal 19 settembre alle 12:11, e scade il **21 settembre verso
le 12:15**. Il campionatore scrive una riga ogni cinque minuti in `.local/long-run.log`, e il
bridge scrive in `.local/bridge.log`: entrambi fuori da `/tmp`, quindi un riavvio non se li
porta più via.

**Riuscita:** 48 ore di righe senza un `reset` e senza buchi.

**Lo rompe:** riflashare la scheda, staccare il cavo, riavviare il Pi. Dopo un riavvio del
Pi, rimettere in piedi a mano, in quest'ordine:

    /usr/sbin/mosquitto -c .local/mosquitto-sentry.conf -d
    # il bridge, con la FIFO della console tenuta aperta da uno scrittore
    python scripts/pico_bridge.py --config .local/pico-bridge.json --console pico-cablato
    curl -s -X POST localhost:8083/api/runtime/start -H 'X-Sentry-Mode-Control: 1'
    setsid .local/long_run.sh &

L'hub riparte da solo come servizio, ma con il runtime fermo: senza l'ultima `POST` il
broker risulta `stopped` anche quando è su.

## 3. Deciso di non fare

- **La soglia dei due secondi, misurata altrove.** Il 18 settembre 2026 è stata guardata su
  questa rete — 101.235 letture, 99,53% sotto i 50 ms, 0,35% oltre i due secondi, 118 in
  mezzo — e si rifà con `python scripts/satellite_admin.py journal --waits`. Quello che
  manca è un'altra casa e mesi invece di giorni, che non è una riga di lavoro ma del tempo.
- **I due secondi che distinguono uno scatto d'orologio da una correzione.** Non hanno
  nessuna misura dietro, e per averne una serve una scheda lasciata in pace un giorno con la
  deriva fra due risposte annotata.

## 4. Chiuso il 18 settembre 2026 — non rifarlo

- Un nodo riapprovato mentre è già connesso: l'hub tiene lo `state` rifiutato e lo rilegge.
- `T16` intero: il salto UTC in entrambi i versi e le tre ore senza risposte, viste al
  minuto. Con dentro un difetto trovato e corretto — il verbo `time` della console non
  registrava che una risposta era arrivata.
- Il broker riavviato che lasciava invisibile un nodo cablato.
- `queued_ms` letto dall'hub, mostrato sotto `queue.waited` e detto nel log alle due soglie.

Le trascrizioni di tutte e quattro sono in [`firmware/pico/README.md`](../firmware/pico/README.md).
