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

Le mancanze di corrente del 19 settembre — una al riavvio del Pi e 267 per la porta che va
in over-current, punto 1.8 — sono state vere: la scheda è tornata ogni volta con `reset
power`, provisioning e configurazione intatti. Ma erano tutte a flash ferma. Contano come
prova che il vault regge uno spegnimento qualunque; non come prova di uno a metà
cancellazione, che è quello che questa riga chiede.

### 1.8 La porta USB va in over-current, e la scheda si riavvia da sola

**Comparso dopo il riavvio del Pi del 19 settembre, ed è la cosa più urgente qui.** La
scheda ha ripreso corrente e poi l'ha persa in continuazione: **267 `boot_id` diversi in due
ore**, ognuno con `reset=power`, e il bridge che vede la porta smettere di rispondere
(`device reports readiness to read but returned no data`). Il kernel dice perché:

    usb usb3-port2: over-current change #292
    usb 3-2: can't read configurations, error -71

Non è l'alimentazione del Pi — `vcgencmd get_throttled` risponde `0x0` — è la porta che va
in protezione. Sullo stesso bus ci sono una webcam Logitech e un disco USB.

**Serve:** un hub USB alimentato per la scheda, o un'altra porta e un altro cavo. Finché non
è risolto, nessuna corsa lunga sta in piedi e ogni misura di durata è falsata.

**Riuscita:** `comings.restarts` in `/api/satellites` che smette di salire, e nessun
`over-current change` nuovo in `dmesg`.

**Di buono:** la scheda ha retto 267 mancanze di corrente vere tornando ogni volta con
provisioning, credenziali e revisione 109 intatti. Non è la prova che chiede il punto 1.7 —
quella vuole la corrente tolta *durante* una scrittura in flash — ma è la stessa cosa a
flash ferma, 267 volte.

## 2. La corsa lunga

### 2.1 `T40` — chiusa, su 25 ore invece di 48

Chiusa il 19 settembre 2026 per decisione del proprietario: la corsa è durata 25,39 ore e
si è interrotta per il riavvio del Pi, e si tengono buone quelle. Quello che c'è, per chi
legge il verdetto:

    boot 645b07cf   18 set 10:45:25 → 19 set 12:08:43   25,39 h
    3.048 eventi: 2.712 live, 333 historic, 3 baseline
    un solo boot_id; pausa massima fra due eventi 90 s

Un solo `boot_id` vuol dire che non si è mai resettata; i 333 `historic` sono le tre ore di
`time_uncertain` fatte apposta. L'evidenza è il giornale, `.local/satellites.sqlite3`,
perché il log del campionatore stava in `/tmp` e il riavvio l'ha cancellato. Da oggi
campionatore e bridge scrivono in `.local/long-run.log` e `.local/bridge.log`.

**Se si volesse rifarla davvero** — 48 ore di fila — prima va risolto il punto 1.8, perché
allo stato la scheda non resta accesa.

## 3. Deciso di non fare

- **La soglia dei due secondi, misurata altrove.** Il 18 settembre 2026 è stata guardata su
  questa rete — 101.235 letture, 99,53% sotto i 50 ms, 0,35% oltre i due secondi, 118 in
  mezzo — e si rifà con `python scripts/satellite_admin.py journal --waits`. Quello che
  manca è un'altra casa e mesi invece di giorni, che non è una riga di lavoro ma del tempo.
- **I due secondi che distinguono uno scatto d'orologio da una correzione.** Non hanno
  nessuna misura dietro, e per averne una serve una scheda lasciata in pace un giorno con la
  deriva fra due risposte annotata.

## 4. Chiuso — non rifarlo

Il 19 settembre 2026:

- `T40`, la corsa lunga, su 25 ore invece di 48 e per decisione: vedi 2.1.

Il 18 settembre 2026:

- Un nodo riapprovato mentre è già connesso: l'hub tiene lo `state` rifiutato e lo rilegge.
- `T16` intero: il salto UTC in entrambi i versi e le tre ore senza risposte, viste al
  minuto. Con dentro un difetto trovato e corretto — il verbo `time` della console non
  registrava che una risposta era arrivata.
- Il broker riavviato che lasciava invisibile un nodo cablato.
- `queued_ms` letto dall'hub, mostrato sotto `queue.waited` e detto nel log alle due soglie.

Le trascrizioni di tutte e quattro sono in [`firmware/pico/README.md`](../firmware/pico/README.md).
