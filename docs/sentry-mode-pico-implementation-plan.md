# Sentry Mode — Satelliti Raspberry Pi Pico
## Piano implementativo per Pico W, Pico 2 W e Pico cablati

**Data:** 17 settembre 2026  
**Repository:** `giacintop89/sentry-mode`  
**Baseline verificata:** `b597535d23d077f91cab84b9ad48727d943868c5` su `main`  
**Stato:** implementato. Il firmware è scritto, costruito per quattro schede e provato su hardware; `PICO-00`…`PICO-09` e `PICO-11` sono chiusi, `PICO-10` (snapshot JPEG) non è stato costruito perché non c'è una camera SPI da collegare, ed è dichiarato `not built` nel manifest. Le trascrizioni di ciò che è stato visto accadere — e l'elenco esplicito di ciò che non lo è stato — sono in [`firmware/pico/README.md`](../firmware/pico/README.md); il manifest di un'immagine lo produce `make pico-release`. Aggiornamento del 18 settembre 2026, a valle della prova live sull'hub di casa; i limiti dichiarati sono in fondo al README del firmware e le cose che restano da fare in [§12.3](#123-azioni-residue).  
**Continuità:** estensione del sistema satelliti già presente, non porting dell'app Linux sul microcontrollore.

> Primo risultato utile: un Pico W con PIR e contatto porta comunica con l'hub già esistente, riceve un grant, pubblica eventi validi e attiva una regola Sentry. La camera può rimanere sul Pi 5 o su uno Zero W. Audio e immagini non bloccano questo rilascio.

I percorsi descritti come nuovi, i profili e i comandi di build del firmware sono da implementare. I riferimenti [R…] indicano codice della baseline; [E…] documentazione primaria esterna. Le cifre di budget e i criteri prestazionali sono obiettivi di collaudo, non misure già ottenute.

## 1. Scelta di piattaforma e confine del lavoro

Il Pico è un microcontrollore programmato tramite firmware: non esegue Raspberry Pi OS/Linux. Le varianti senza suffisso W non hanno Wi-Fi/Bluetooth integrati. Questo cambia il software del satellite, non l'applicazione hub. [E1]

| Scheda | Risorse documentate | Destinazione proposta |
|---|---|---|
| Pico / Pico H | RP2040, due Cortex-M0+, fino a 133 MHz; 264 kB SRAM, 2 MB flash; nessuna radio integrata | Sensori collegati tramite USB a un bridge Linux |
| Pico W / WH | Risorse RP2040; Wi-Fi 2,4 GHz e Bluetooth | Baseline wireless per sensori; BLE dopo collaudo |
| Pico 2 | RP2350, 520 kB SRAM, 4 MB flash; nessuna radio integrata | Variante cablata con maggiore margine |
| Pico 2 W | RP2350, fino a 150 MHz, 520 kB SRAM, 4 MB flash; Wi-Fi e Bluetooth | Variante preferita per sperimentare audio e combinazioni più impegnative |

Specifiche da documentazione Raspberry Pi. Per RP2350 questo piano usa il target Arm Cortex-M33; non introduce un secondo porting RISC-V. [E1]

### 1.1 I quattro ruoli, senza promettere equivalenza allo Zero

| Ruolo | Rilascio di riferimento | Estensione possibile | Esclusione esplicita |
|---|---|---|---|
| Sensori GPIO, I²C, 1-Wire, ADC | Sì: funzione principale | Altri driver tipizzati e diagnosticabili | Collegamenti diretti a segnali industriali o fuori specifica elettrica |
| Presenza BLE/Wi-Fi | BLE su modelli W, dopo prova di coesistenza | Dati Wi-Fi letti dal router sull'hub | Identificazione certa di persone o disarmo basato solo su beacon |
| Microfono remoto | Prima attività acustica locale e solo eventi | PCM su rete tramite gateway esistente, subordinato ai gate | Copiare ALSA/arecord, usare un generico microfono USB come periferica Linux |
| Camera satellite | Non nel rilascio base | Snapshot JPEG con modulo SPI dotato di codifica e buffer propri | CSI/rpicam-vid/H.264 del percorso Zero, webcam USB plug-and-play |

L'interfaccia camera SPI non è una supposizione astratta: il produttore Arducam pubblica esempi Pico per camere Mini 2MP/5MP. Gli esempi non costituiscono però un collaudo del trasporto Sentry, del Wi-Fi o del carico TLS. [E7]

Non si dichiara impossibile ogni inferenza su microcontrollore. TinyML è fuori da questo piano: detection e decisioni restano sull'hub per conservare un solo motore di regole.

### 1.2 Profili di prodotto

- `pico-w-sensor`: GPIO + ADC/I²C/1-Wire; MQTT e diagnostica. Primo profilo da qualificare.
- `pico-w-presence`: sensori leggeri + scansione BLE filtrata. Nessun media continuo.
- `pico-audio-experimental`: microfono I²S, attività acustica; invio PCM abilitato solo dopo qualifica della singola scheda.
- `pico-usb-sensor`: sensori via USB CDC e bridge sul Pi 5/Zero.
- `pico-jpeg-experimental`: camera SPI con JPEG/FIFO, snapshot richiesti dall'hub; non attivo di default.

Le capacità disponibili dipendono da firmware, cablaggio e qualifica. Non abilitare tutte le funzioni perché la scheda ha abbastanza pin.

## 2. Baseline effettiva: cosa è già presente

La baseline precedente `4febced…` non è più la base corretta per questo lavoro. Nella versione verificata sono già presenti contratti, agente Zero, identità, ingress, sessioni, storage, gestione sorgenti e gateway media. [R1–R9]

| Componente esistente | Evidenza nel codice | Decisione per Pico |
|---|---|---|
| `contracts/satellite/v1/` | Envelope generato da Pydantic; fixture valide/invalide; formato audio generato | Riutilizzare; non scrivere a mano una versione MCU incompatibile |
| `satellites/protocol.py` | `event` + `delivery`, UUID, timestamp con timezone, qualità e clock | Serializzatore C++ conforme allo stesso envelope |
| `satellites/mqtt.py` | Paho MQTT 5, identità dal topic, client certificate, TLS minimo 1.2 | Mantenere l'hub; verificare client MCU MQTT 3.1.1 sullo stesso broker |
| `satellites/ingress.py` | Deduplica, classificazione iniziale/storico/live, rifiuto di tempo incerto | Non aggirare per far sembrare operativo un Pico senza orario |
| `satellites/service.py` | Sessione da `state`, grant, sorgenti, comandi e collegamento media | Estendere profili/limiti/driver riconosciuti, non duplicare il servizio |
| `sources/models.py` | Sensori, presenza, microfono e camera già tipizzati | Mantenere SourceKind; aggiungere proprietà di capacità dove necessario |
| `satellite/.../commands.py` | Grant/renew/revoke/stop, configure, comandi audio/video | Implementare un sottoinsieme dichiarato e risposte esplicite per il resto |
| `vision/media_gateway.py` | mTLS **1.3**, identità approvata, stream, token, scadenza; kind audio/video | Riuso per PCM; JPEG richiede estensione, non invio come H.264 |
| `contracts/.../audio.json` | Blocchi `SMA1`, PCM mono 16 kHz | Riutilizzare il formato, non inventare un protocollo audio MCU |
| Agente Linux `satellite/` | Python, Paho, driver e processi Linux | Rimane indipendente dal nuovo firmware |

**Correzione rispetto allo stack suggerito in precedenza:** l'implementazione ora presente ha scelto H.264 e PCM su un gateway mTLS proprio. Non reinserire MediaMTX, FFmpeg sul satellite o WebSocket per adattare il Pico. Le ADR descrivono questa scelta; l'audio è indicato come testato con capture simulata, non qualificato sul microfono reale. [R7–R9]

## 3. Stack del firmware

### ADR-P01 — C/C++ con SDK ufficiale

Usare C17/C++17, Raspberry Pi Pico SDK, CMake e Ninja/Make. La release SDK **2.3.1**, risultata nella verifica, è il candidato iniziale: registrare SHA e submodule esatti dopo il primo build. Non scaricare `latest` a ogni compilazione. [E2]

| Responsabilità | Scelta |
|---|---|
| GPIO, ADC, I²C, PIO, DMA, timer, watchdog | API Pico SDK |
| Wi-Fi | CYW43 via integrazione SDK |
| TCP/IP | lwIP |
| MQTT | `pico_lwip_mqtt`, profilo MQTT 3.1.1 |
| TLS | Mbed TLS integrato nell'SDK, validazione server e certificato client |
| BLE | BTstack, solo BLE scanner nel primo profilo |
| USB device | TinyUSB CDC |
| Dati | JSON conforme ai contratti, parser a token/buffer fissi per i comandi |
| Configurazione persistente | Record versionati in partizioni flash dedicate |
| Test | CTest su host, fixture Python dell'hub, test hardware separati |

Le librerie SDK includono integrazioni lwIP, MQTT, Mbed TLS e BTstack; gli esempi ufficiali includono client MQTT/TLS e Bluetooth insieme al Wi-Fi. Sono componenti di partenza, non dimostrazioni automatiche del profilo Sentry. [E3, E4]

MicroPython può servire a provare un sensore. Non è il firmware di riferimento per questa integrazione: preferiamo controllare esplicitamente buffer, callback di rete, DMA e memoria TLS. Non è necessario introdurre FreeRTOS, Zephyr o un framework Arduino nel primo incremento.

### ADR-P02 — Un loop applicativo, nessuna allocazione incontrollata

Primo profilo: un solo core per applicazione e rete; `pico_cyw43_arch_lwip_threadsafe_background` con disciplina prevista dall'SDK. I callback di rete copiano messaggi in code finite e tornano; il loop applicativo valida ed esegue le transizioni. Le chiamate raw lwIP fuori dal contesto previsto sono protette con le API `cyw43_arch_lwip_begin/end`. [E3]

Gli interrupt GPIO registrano stato e timestamp monotono, non fanno JSON, TLS o MQTT. Un ISR DMA segnala un buffer completo e basta. L'eventuale secondo core sarà introdotto per l'audio soltanto dopo misure; non dovrà chiamare autonomamente lo stack di rete.

Usare strutture a capacità fissa. Vietare crescita non limitata di stringhe, liste, code e cache; controllare tutte le allocazioni delle librerie. Disabilitare eccezioni/RTTI nel firmware se non richiesti dai componenti effettivi. Un errore di memoria degrada il ruolo interessato, non disabilita i controlli TLS.

### ADR-P03 — Stesso protocollo applicativo, versione MQTT diversa

Il client lwIP consultato costruisce CONNECT con protocol level 4 e clean session. L'hub usa MQTT 5, ma Mosquitto supporta entrambe le versioni. Il ponte è il broker: `schema_version=1` del payload resta indipendente da MQTT. [E5, E6, R3]

Confermare sullo **SHA lwIP incluso nell'SDK bloccato** la stessa semantica. Non introdurre proprietà MQTT 5 obbligatorie nel percorso MCU: grant, epoca, identificativi e scadenze devono essere nel payload già previsto.

### ADR-P04 — Non abbassare la sicurezza per far entrare il profilo

MQTT deve mantenere mTLS e verifica del server. L'audio deve superare anche la prova mTLS 1.3, richiesta dal gateway corrente. Se il profilo RP2040 non sostiene la seconda connessione o la configurazione TLS necessaria, pubblicare il profilo solo sensori/attività acustica e usare Pico 2 W o il bridge per l'audio. Nessun downgrade automatico del gateway. [R3, R9]

## 4. Organizzazione nella stessa repository

```text
firmware/pico/                           # NUOVO, non pacchetto Python
  CMakeLists.txt
  CMakePresets.json
  sdk.lock
  README.md
  include/{lwipopts,mbedtls_config,btstack_config}.h
  src/
    main.cpp
    app/{lifecycle,config,capabilities,health,watchdog}.cpp
    protocol/{event_json,command_parser,control_json,identity,lease}.cpp
    net/{wifi,mqtt,tls,clock_sync}.cpp
    storage/{layout,config_slots,provisioning}.cpp
    drivers/{gpio,i2c_bme280,onewire_ds18b20,adc}.cpp
    presence/ble_scanner.cpp
    audio/{i2s_capture,activity,pcm_blocks,media_client}.cpp
    camera/{spi_jpeg,snapshot_client}.cpp
    usb/{descriptors,framing,bridge_client}.cpp
  pio/i2s_rx.pio
  profiles/*.json                        # esempi pubblici senza credenziali
  tests/{unit,fixtures,hardware}/
  tools/{build,report_size}.py

contracts/satellite/v1/                  # ESISTENTE
  event.schema.json                      # invariato, generato
  audio.json                             # invariato, generato
  fixtures/...
  control/...                            # NUOVO: formalizzazione wire già in uso

scripts/
  pico_provision.py                      # NUOVO: tool host, non gira sulla scheda
  pico_bridge.py                         # NUOVO: solo profilo cablato

src/sentry_mode/satellites/
  capabilities.py                        # NUOVO: profilo amministrativo + limiti
  service.py                             # ESTENDERE
  identity.py                            # ESTENDERE metadati/versioni, se necessari
  api.py                                 # ESTENDERE configurazione per tipo nodo
src/sentry_mode/vision/
  media_gateway.py                       # RIUSO audio; JPEG solo fase opzionale
  snapshot_source.py                     # NUOVO soltanto per JPEG

.github/workflows/pico.yml               # NUOVO
```

Non creare tutti i moduli vuoti nella prima PR. Ogni incremento introduce i file che utilizza. Non spostare o rinominare il pacchetto Linux `satellite/`.

## 5. Contratto wire: compatibilità prima delle periferiche

### 5.1 Topics

Riutilizzare la radice configurata dall'hub, senza hardcodificarla nel firmware:

```text
<prefix>/nodes/<node_id>/state       Pico → hub, QoS 1, retained
<prefix>/nodes/<node_id>/health      Pico → hub, QoS 0, non retained
<prefix>/nodes/<node_id>/events      Pico → hub, QoS 1, non retained
<prefix>/nodes/<node_id>/acks        Pico → hub, QoS 1, non retained
<prefix>/nodes/<node_id>/commands    hub → Pico, QoS 1, non retained
```

Il nodo non legge wildcard di altri nodi e non pubblica su `commands`. ACL e certificato identificano chi sta parlando; un `node_id` nel JSON non è autenticazione. [R3, R5]

### 5.2 Envelope invariato

Esempio illustrativo conforme alla forma attuale; timestamp e UUID sono dati di esempio, non da copiare come costanti:

```json
{
  "schema_version": 1,
  "event": {
    "event_id": "11f6e610-4fc7-4d56-8c85-7c8399e8a201",
    "node_id": "pico-ingresso",
    "source_id": "pir-1",
    "boot_id": "156ea82a-047e-4de6-8c67-2a3cf67f4959",
    "sequence": 42,
    "kind": "sensor.motion",
    "occurred_at": "2026-09-17T09:30:00Z",
    "clock_status": "synced",
    "value": true,
    "unit": null,
    "quality": "valid"
  },
  "delivery": {
    "connection_id": "63469f65-d7bd-4ed0-a647-9de95befc829",
    "hub_epoch": 7,
    "grant_id": "025a1b1f-2969-48c2-b14e-079c8b955f7c",
    "queued_ms": 12,
    "replayed": false,
    "initial_state": false
  }
}
```

`source_id` è locale al nodo: `pir-1`, non `pico-ingresso.pir-1`. È l'hub a comporre l'identità completa. `event_id` identifica la lettura; una ritrasmissione non ne genera uno nuovo. `boot_id` cambia a ogni boot; `connection_id` a ogni nuova connessione; `sequence` cresce per sorgente nello stesso boot. [R2]

Implementare interi a 64 bit dove il protocollo li richiede. Non affidare contatori monotoni a un millisecond timer a 32 bit. Generare UUID corretti, con campi versione/variant coerenti e una strategia che impedisca la ripetizione al riavvio. Identificativo hardware e MAC non sono segreti né credenziali.

### 5.3 Handshake e grant

```text
BOOT → CONFIG_VALID → WIFI_CONNECTING → CLOCK_READY → MQTT_CONNECTING
     → COMMAND_SUBSCRIBED → STATE_SENT → WAIT_GRANT → ACTIVE
                                      ↘ errore → BACKOFF / DEGRADED
```

Preparare e confermare la subscription ai comandi prima di dichiarare `online=true`, evitando di perdere il primo grant. Pubblicare stato e health è consentito prima del grant; eventi operativi no. Al grant valido inviare prima la baseline dei sensori (`initial_state=true`), quindi nuove transizioni. Il baseline non deve causare un'azione.

Usare timer monotoni per grant e rinnovi. Rifiutare un rinnovo con sequenza vecchia, un comando per altro nodo o altra epoca, un duplicato che tenti di estendere la durata. Scollegamento, revoca o scadenza fermano i media e invalidano la capacità di pubblicare eventi live. Il ripristino riparte con nuova sessione e baseline.

### 5.4 LWT compatto: un dettaglio da non saltare

Il client lwIP consultato usa lunghezze a 8 bit per topic e messaggio Will. Il pieno `state` Linux contiene lista sorgenti e opzioni e non va riutilizzato ciecamente come LWT MCU. [E5, R12]

Usare un LWT minimo con `schema_version`, `node_id`, `boot_id`, `connection_id`, `online=false`; verificarne a build/runtime la dimensione entro il limite della libreria bloccata. L'hub corrente tratta il messaggio offline prima di adottare le sorgenti: non occorre inviare tutta la configurazione. Verificare anche il topic completo con la radice configurata. [R5]

### 5.5 Consegna, duplicati e arresti

Una chiamata `mqtt_publish` riuscita non è un PUBACK. Mantenere un piccolo insieme di messaggi in-flight fino al relativo callback; dopo timeout usare lo stesso `event_id`. Distinguere messaggio accettato nella coda locale, consegnato al broker e registrato dal servizio.

Il PUBACK del broker non dimostra che l'hub abbia completato una regola. Il clean session del client MCU non conserva comandi durante una disconnessione. Il rilascio iniziale non garantisce assenza di perdita in caso di spegnimento, né esecuzione exactly-once. La persistenza/journal dell'hub resta quella esistente; un eventuale ACK end-to-end richiederebbe un contratto dedicato, non il riuso ambiguo degli ACK comando.

Primo profilo: coda RAM limitata, non storico flash. Durante interruzioni coalescere letture periodiche, contare le transizioni perse e non ripresentarle come nuove al ripristino. Un evento conservato conserva timestamp e origine; se è storico va marcato `replayed`, oppure scartato con contatore.

### 5.6 Schemi controllo e limiti MCU

L'envelope eventi e l'audio hanno già contratti generati. Formalizzare anche `state`, `health`, `commands` e `acks` con fixture prima del porting C++; partire dal comportamento corrente, non da un vecchio esempio nel piano Zero. [R1, R6, R12]

Il firmware non deve contenere un interprete completo JSON Schema. Il decoder controllo implementa la grammatica chiusa; i test host verificano compatibilità con fixture condivise. I messaggi prodotti dal serializer C++ vengono validati da Pydantic e dallo schema generato nei test Python.

Aggiungere limiti espliciti per lunghezza, profondità, stringhe, numero di sorgenti; rifiutare chiavi duplicate, NUL, UTF-8 invalido, NaN/infinito e numeri fuori intervallo. Assemblare messaggi MQTT frammentati prima del parsing e non leggere oltre il payload completo.

## 6. Provisioning, orologio e limiti di memoria

### 6.1 Identità e credenziali

Il tool host `scripts/pico_provision.py` deve predisporre un nodo tramite USB, con modalità manutenzione attivata fisicamente. Deve usare l'emissione/approvazione già prevista per i satelliti, senza creare una CA parallela o importare le credenziali amministrative dell'hub nel firmware.

Materiale provisionato: nome stabile, indirizzo hub, radice topic, Wi-Fi, CA pubblica, certificato client individuale e relativa chiave. Il certificato deve essere associato al nodo approvato e alle ACL esistenti. Il firmware di distribuzione è identico per un profilo: i segreti sono una configurazione privata separata, non costanti committate o file in un artefatto CI pubblico.

Il tool verifica dimensioni, algoritmo della chiave, catena, identificativo e compatibilità del layout flash prima di scrivere; non sovrascrive un'identità esistente senza un'operazione amministrativa esplicita. Un factory reset elimina le credenziali del dispositivo e richiede anche revoca lato hub.

La chiave privata salvata sulla flash di un RP2040 non va presentata come protetta dall'accesso fisico. Non chiamare “cifratura sicura” un file cifrato con una chiave contenuta nello stesso firmware. Per un prodotto con resistenza alla manomissione servono un requisito e una soluzione hardware dedicati.

### 6.2 TLS: validare davvero il server

Bloccare configurazione Mbed TLS e catena di certificati insieme all'SDK. Il primo test deve verificare certificato client, CA del broker, hostname o IP SAN, scadenza e origine dell'entropia. Un esempio che effettua il handshake ma non verifica il nome del server non supera il gate.

Non presumere che l'esempio lwIP MQTT abiliti automaticamente tutti i controlli richiesti. Qualora l'adapter scelto non esponga la verifica del nome, aggiungere una piccola estensione verificabile all'adapter TLS, con test negativo per nome sbagliato; non saltare il controllo. Per certificati con IP SAN verificare il comportamento della versione Mbed TLS bloccata.

Le chiavi client vengono generate sul computer di provisioning con strumenti crittografici standard. Per nonce TLS e casualità di sessione utilizzare il percorso di entropia previsto dall'integrazione SDK/Mbed TLS, verificandone l'inizializzazione; non usare MAC, uptime o un seed fisso come sorgente crittografica. La presenza di un generatore casuale nell'SDK non basta a dimostrare una corretta integrazione TLS. [E9]

L'avvio senza credenziali rimane in manutenzione; una rete indisponibile causa backoff, non collegamento in chiaro. L'audio richiede una configurazione capace di mTLS 1.3 sul gateway corrente. Nessuna modifica globale a TLS 1.2 per aggirare un limite del firmware. [R9]

### 6.3 Il Pico deve sapere quando è successo l'evento

L'hub corrente non usa come eventi live le letture con `clock_status=unsynced/unknown`. Non cambiare questa regola per il Pico. [R4]

Implementare due orologi distinti:

- Timer monotono a 64 bit per debounce, campionamento, attese, lease e ordine locale.
- Stima UTC, con stato di sincronizzazione e incertezza, per `occurred_at` e verifica temporale dei certificati.

La prima implementazione usa SNTP verso un server configurato della LAN, preferibilmente l'hub opportunamente predisposto. A ogni accensione l'ora deve essere acquisita di nuovo; un valore memorizzato è solo un limite di plausibilità, non prova che il clock sia sincronizzato. Gli eventi nati prima della sincronizzazione non diventano freschi cambiando il loro timestamp a posteriori.

SNTP senza autenticazione non fornisce da solo garanzie crittografiche sul tempo. Il profilo iniziale assume una rete di gestione controllata e mantiene tutte le verifiche della catena TLS. Un requisito di resistenza a manipolazione attiva dell'orologio richiede un bootstrap temporale autenticato dedicato; non si risolve disabilitando la validità dei certificati.

Dopo salto significativo del clock, timeout oltre il budget di deriva o perdita di fiducia: dichiarare tempo incerto, non generare azioni live, risincronizzare e inviare una nuova baseline. Misurare il budget di deriva prima di decidere quanto mantenere `synced` senza un nuovo campione. Un timestamp `1970-...` con qualità `synced` è sempre un errore.

### 6.4 Flash e aggiornamenti

Usare un layout definito dal linker, con aree distinte per codice, provisioning, due slot di configurazione e storage richiesto dalle librerie. L'SDK/BTstack può già usare settori finali per lo storage Bluetooth: non collocare la configurazione negli “ultimi due settori” senza verificare la mappa. [E3]

Ogni record configurazione contiene versione, lunghezza, revisione monotona, checksum e marcatore di commit scritto per ultimo. Il boot sceglie l'ultimo record valido; il vecchio rimane utilizzabile se manca il commit. Il checksum rileva corruzione, non autentica il contenuto.

Il firmware valida tutta una nuova configurazione prima di applicarla. Un limite superato, un pin riservato, una collisione PIO/DMA o un driver non compilato fanno fallire l'intero aggiornamento senza applicazione parziale. Scrivere solo su modifiche effettive: niente heartbeat, campioni o contatori sulla flash a ogni evento.

Le operazioni di erase/program devono seguire le API di sicurezza flash dell'SDK, tenendo conto di codice eseguito da flash, interrupt e radio. La modalità manutenzione può fermare temporaneamente l'acquisizione; non dichiarare continuità durante una riprogrammazione se non è stata dimostrata.

Per il primo rilascio: UF2 via BOOTSEL/USB e ritorno manuale a una build nota. I due slot di configurazione non costituiscono un aggiornamento firmware A/B. OTA, secure boot e rollback automatico del firmware sono progetti separati, non capacità implicite. [E1, E9]

### 6.5 Budget proposto e diagnostica

Valori iniziali da verificare con linker map e prove sul dispositivo, non prestazioni certificate:

| Risorsa | Budget iniziale proposto | Comportamento al limite |
|---|---|---|
| Sorgenti configurate | 8 | Rifiutare configurazione, non troncarla |
| Evento JSON | 2 KiB | Errore esplicito e contatore |
| Stato/configurazione/comando | 4 KiB | Limite annunciato e validato anche dall'hub |
| Buffer MQTT di uscita | 8 KiB come candidato | Verificare spazio per payload, topic e header; backpressure |
| Eventi in coda | 32 record nativi e massimo 8 KiB complessivi | Coalescere misure periodiche; scartare il più vecchio con contatore |
| In-flight QoS 1 | 2 iniziali | Attendere callback senza bloccare il loop |
| Cache comandi | 64 ID e relativi esiti | Espulsione controllata; epoche/lease continuano a proteggere dai replay |
| Audio sperimentale | 4 blocchi da 100 ms, circa 12,9 KiB serializzati | Scartare blocchi interi e segnalare gap |
| Margine memoria RP2040 | Obiettivo di almeno 48 KiB nel peggior punto misurato | Non abilitare il profilo se handshake/stack lo consumano |

Il limite di una coda è sia per numero sia per byte. Non allocare 32 buffer da 2 KiB solo perché 2 KiB è il massimo dell'envelope: mantenere letture strutturate e serializzare al momento della pubblicazione. Il limite reale dell'output ring MQTT va verificato nella libreria bloccata.

Registrare memoria minima disponibile, massimo uso stack, allocazioni fallite, massimo ritardo loop, reset reason, riconnessioni, pacchetti/eventi scartati, errori sensore e qualità orologio. Le misure assenti sono `unknown`, non zero. Non riprodurre statistiche Linux fittizie come load average, RSS o spazio SD.

Il watchdog viene alimentato quando loop e task essenziali progrediscono, non quando Internet risponde. Un access point spento non deve provocare un ciclo infinito di reset. Il primo profilo è alimentato continuamente: modalità a batteria e deep sleep richiedono una diversa politica di disponibilità, non sono promesse di questo rilascio.

## 7. Driver sensori e presenza

### 7.1 GPIO: primo percorso completo

Realizzare prima PIR e contatto porta con driver `gpio`, compatibile con il kind già riconosciuto dall'hub. Il mapping pin è espresso come numero GPIO della scheda, non numero fisico del connettore; UI e file devono dirlo esplicitamente.

Le letture hanno stato logico, polarità configurata, timestamp, qualità e flag baseline. Debounce e tempo di assestamento del PIR sono configurabili entro limiti. La prima lettura dopo boot, grant o riconfigurazione è uno stato iniziale, mai una nuova intrusione.

Il circuito di un contatto deve definire anche il significato di ingresso aperto e pull-up/pull-down. Il software non può distinguere automaticamente un filo tagliato da un contatto aperto se l'elettronica non offre tale informazione. Non pubblicare qualità “guasto rilevato” senza una misura che lo giustifichi.

Gli ingressi vanno progettati per i livelli elettrici della scheda; non collegare direttamente 24 V, 0–10 V o segnali di moduli incompatibili. La selezione di alimentazione e adattamento dei livelli resta una verifica del cablaggio prima del test funzionale.

### 7.2 I²C, 1-Wire e ADC

Per I²C scegliere un primo sensore identificato, per esempio BME280, e conservare il kind già in uso. Includere identificazione del dispositivo, coefficienti di calibrazione, unità, timeout di bus e range plausibili. Un NACK o bus bloccato rende indisponibile quella lettura; non produce temperatura zero.

Per DS18B20 implementare conversione e lettura come macchina a stati: nessuna lunga attesa bloccante nel loop di rete. Verificare CRC, identificativo del sensore e valori di avvio/errore; scegliere inizialmente alimentazione normale, non modalità parassita. Rilevare un sensore disconnesso entro un timeout documentato.

Il Pico offre ADC a bordo: per una LDR si può quindi valutare una lettura analogica con un circuito appropriato, diversamente da un GPIO puramente digitale. Il valore iniziale sarà ADC/volt o luminosità relativa, non lux senza calibrazione. La temperatura interna del chip non va etichettata come temperatura ambiente. [E1]

La configurazione deve rilevare conflitti tra pin, bus, canali DMA e state machine PIO prima di avviare i driver. L'elenco dei pin riservati dipende da `PICO_BOARD`: non copiare quello del Pico non wireless sul modello W.

### 7.3 Adattamento dell'hub senza un secondo motore

Introdurre un catalogo amministrativo di profili MCU: architettura, driver consentiti, massimo sorgenti, dimensione configurazione, supporto a stream e test manuali. La capacità utilizzabile è l'intersezione tra profilo approvato, build dichiarata, configurazione locale e qualifica del dispositivo.

Non fidarsi di una dichiarazione `supports_audio=true` come autorizzazione. La dichiarazione consente all'hub di proporre la funzione; il registro approvato e la sessione continuano ad autorizzarla. Le capacità non cambiano `SourceKind` e non introducono un nuovo tipo di evento “pico”. [R5, R10]

L'API di configurazione filtra le opzioni secondo il profilo. Non inviare a un Pico percorsi `/dev/...`, argomenti ALSA, nomi di processi o configurazioni CSI. Una richiesta non supportata riceve `failed` con motivo, non un ACK `applied` seguito da nessun comportamento.

Conservare regole, correlazioni, code azioni e scelta esplicita della sorgente già presenti. Esempio di primo uso: `pico-ingresso.pir-1` attiva una foto di una camera Zero/locale già registrata. Non serve aggiungere una camera al Pico per ottenere questo risultato.

### 7.4 BLE

Usare BTstack come scanner di advertising BLE. Il profilo conserva una allowlist di dispositivi/beacon configurati e un numero massimo di candidati; non pubblica ogni pacchetto radio. Raccogliere osservazioni, filtrare RSSI e produrre transizioni secondo isteresi e finestre temporali. Le API BLE e l'integrazione CYW43 sono disponibili nell'SDK. [E3]

Allineare i payload al kind e alle fixture di presenza già usati dall'agente Linux. Gli stati sono `present`, `absent` e `unknown`: `absent` richiede che lo scanner sia funzionante per tutta la finestra; radio in errore o nodo offline significa `unknown`. Non confrontare RSSI tra stanze come una misura di distanza calibrata.

Qualificare Wi-Fi e BLE contemporanei mentre arrivano comandi e vengono pubblicati eventi. Se il carico di scansione fa perdere heartbeat, ridurre duty cycle e rate aggregati oppure separare i profili. Un beacon noto non autentica una persona e non autorizza il disarmo automatico.

Per la presenza Wi-Fi mantenere l'eventuale lettura autorizzata dell'access point/router sull'hub. Il firmware Pico non deve implementare un secondo inventario di rete per simulare la presenza umana.

## 8. Audio: due risultati separati

### 8.1 Livello A — Attività acustica, senza inviare audio

Collegare un microfono I²S identificato e realizzare un ricevitore tramite PIO e DMA. Non usare ALSA/arecord e non assumere che un esempio SDK di uscita I²S implementi anche l'ingresso.

Validare allineamento dei bit, canale L/R, segno e conversione dalla parola nativa del microfono a PCM16. Per il calcolo RMS usare accumulazione con ampiezza sufficiente; limitare frequenza delle misure e carico del loop. La soglia produce un evento compatibile con l'attuale `audio.loud_noise`, con isteresi e cooldown locali. La soglia è attività acustica, non riconoscimento di voce, spari o vetri.

I valori dBFS, se esposti come diagnostica, sono relativi al segnale digitale; non sono dB SPL calibrati. L'attività acustica è opt-in, con indicazione in UI anche quando non viene trasmesso PCM.

Questo livello può essere rilasciato anche quando la connessione audio cifrata non supera il gate. Non dichiarare disponibile il live audio solo perché il DMA funziona.

### 8.2 Livello B — PCM remoto compatibile con Sentry

Formato proposto identico al contratto esistente: 16 kHz, 16 bit, mono. Il traffico utile è `16000 × 16 = 256000 bit/s`, ossia 32 kB/s, prima di framing e TLS. Un blocco da 100 ms ha 1600 campioni, 3200 byte PCM e 30 byte di header. Sono calcoli di volume dati, non misure di throughput.

Riutilizzare esattamente il framing SMA1. [R7]

| Campo | Codifica |
|---|---|
| magic | 4 byte `SMA1` |
| version, flags | u8, u8; bit 0 per campioni persi prima del blocco |
| reserved | u16, zero |
| sequence | u32, da zero per connessione |
| first_sample | u64, contatore acquisizione |
| captured_ns | u64, orologio del nodo, diagnostico |
| samples | u16; 1600 nel profilo iniziale |
| payload | campioni PCM16 little-endian |

Non serializzare direttamente una `struct` C++ con padding dipendente dal compilatore: scrivere i campi little-endian a offset definiti. Un test deve confrontare i byte prodotti dal firmware con `contracts/satellite/v1/audio.json` e il decoder Python dell'hub.

Flusso di autorizzazione: grant eventi valido → comando `audio_start` accettato → mTLS 1.3 con il certificato del nodo → header gateway con kind audio, stream, sorgente e token → risposta `ok` → blocchi PCM. Destinazione host solo dalla configurazione provisionata; il comando può scegliere la porta nei limiti consentiti, non un host esterno. [R6, R9, R11]

La sequenza del flusso riparte a ogni nuova connessione; il contatore campioni segue l'acquisizione. Lo stop/revoke/scadenza deve chiudere il socket e scartare buffer pendenti. Nessun invio del passato dopo riconnessione, nessuna registrazione nascosta sulla flash, nessuna ripartenza del microfono remoto al boot senza nuova autorizzazione.

Usare ring DMA e coda di blocchi finiti; le scritture TLS parziali vengono completate senza intercalare altri blocchi. Quando si perde terreno, scartare solo blocchi completi non ancora inviati e segnalare il gap. La connessione in errore viene chiusa, non riempita di frame parziali. Il traffico audio non deve impedire i rinnovi MQTT o l'esecuzione di `stop`.

L'hub esistente allinea le registrazioni usando l'arrivo dei blocchi, non il timestamp del nodo: mantenere questa semantica. Conservare anche la soppressione degli eventi audio durante il parlato dell'hub e la finestra successiva già prevista; non chiamarla cancellazione d'eco. [R7]

### 8.3 Gate audio, per ciascuna scheda

Misurare insieme acquisizione, connessione MQTT, seconda connessione TLS, rinnovi, code e diagnostica. Il massimo uso memoria può verificarsi durante il handshake, non nello streaming stabile. Richiedere test ripetuti di avvio/stop e perdita Wi-Fi, non un singolo file ricevuto.

Pico 2 W è il candidato con maggior margine di RAM; non una garanzia di funzionamento. Se la build SDK scelta non offre il profilo TLS 1.3 richiesto o la memoria non basta, il firmware rifiuta il live audio e mantiene il livello A. Non si cambia la sicurezza dell'hub per far superare il test.

## 9. Camera: snapshot sperimentali, non porting del video Zero

### 9.1 Hardware e memoria

Usare esclusivamente un modulo esterno concreto con codifica JPEG e buffer/FIFO propri, raggiungibile via SPI. La documentazione del produttore offre esempi Pico da cui verificare le operazioni di acquisizione e lettura, non garanzie di compatibilità con Sentry. [E7]

Un frame RGB565 640×480 occuperebbe `640 × 480 × 2 = 614400 byte`: più della SRAM totale documentata di Pico W e Pico 2 W. Il progetto deve leggere il JPEG dal buffer esterno a chunk, non allocare un'immagine RGB intera nel microcontrollore. [E1]

Profilo iniziale proposto: snapshot 320×240 a richiesta, chunk SPI/TLS da 2–4 KiB e limite iniziale di 128 KiB per immagine compressa. Se la dimensione supera il limite, rifiutare la cattura con errore; non tagliare il JPEG. Risoluzione e limite sono parametri di test da adeguare al modulo, non specifiche già qualificate.

### 9.2 Estensione minima e dichiarata del gateway

Il gateway attuale distingue audio e video H.264: non accetta genericamente qualsiasi immagine. Non dichiarare il modulo SPI come driver `csi`, perché l'hub avvierebbe il percorso sbagliato. [R5, R9]

Solo quando il prototipo SPI è verificato, aggiungere un profilo media snapshot versionato e negoziato, con:

- Capacità `snapshot`, distinta da `live_video` e registrata amministrativamente.
- Comando chiuso `image_capture`, identificativo richiesta, sorgente, scadenza e token; niente URL arbitrari.
- Kind gateway `image`, lunghezza massima, formato JPEG e dimensioni attese; lettura limitata del payload.
- Sink sull'hub che valida formato/dimensioni, limita la decodifica e produce evidenza con la sorgente corretta.

Formalizzare layout e messaggi in un nuovo contratto dedicato, con fixture; non modificare l'envelope eventi V1 o SMA1 per inserirvi byte d'immagine. La risposta al comando e il completamento dell'evidenza sono distinti.

La prima integrazione deve sostenere una foto manuale o una foto comandata da PIR. Una sorgente snapshot non diventa automaticamente una camera monitorabile con tre detection consecutive: il planner deve rifiutare regole incompatibili oppure utilizzare un percorso di analisi singola esplicitamente progettato.

Qualora TLS 1.3 e buffering non superino il gate, il modulo resta solo prototipo locale/USB. Il rilascio dei sensori non dipende da questa fase. La soluzione di riferimento per video continuo rimane il percorso Pi 5/Zero già esistente.

## 10. Pico senza W: bridge USB opzionale

Pico e Pico 2 senza W non hanno radio integrata. Per usarli senza cambiare scheda, il percorso proposto è USB device CDC tramite TinyUSB, collegato a un Pi 5 o Zero Linux già in rete. TinyUSB offre la classe CDC; il trasporto applicativo e il bridge restano da implementare. [E1, E8]

```text
Pico → USB CDC → bridge Linux → MQTT/mTLS → SatelliteService esistente
          comandi chiusi ←           ← grant/configure/stop
```

Il bridge mantiene un'identità logica per ciascun Pico autorizzato. L'associazione avviene con configurazione amministrativa della porta/dispositivo; un identificativo USB dichiarato dal dispositivo non è un'autenticazione crittografica. Solo hardware fisicamente fidato può essere collegato al bridge.

Sul collegamento USB usare frame con magic, versione, lunghezza limitata, contatore e checksum. Tenere diagnostica testuale fuori dal canale dati. Il checksum rileva frame danneggiati, non impedisce a un dispositivo ostile di falsificare dati.

Il bridge può usare il trasporto Linux già esistente, senza importarlo nel firmware. Detiene credenziali MQTT individuali con ACL limitate, non quelle amministrative dell'hub. Non pubblica come nodo scelto liberamente dal payload USB.

Il firmware conserva boot ID, identità della lettura e tempo monotono; il bridge associa le letture a UTC solo dopo una sincronizzazione USB con incertezza accettabile. Letture vecchie o di tempo incerto non vengono ritimbrate come nuove. Il bridge dichiara la propria mediazione nelle informazioni del nodo.

USB scollegata, bridge arrestato o firmware non responsivo chiudono la sessione e portano i sensori a stato sconosciuto. Comandi e rinnovi scadono comunque. Non è prevista un'automazione locale che continui a far credere operativo un nodo scollegato.

La prima versione cablata trasferisce sensori, non webcam o audio USB generico. TinyUSB permette altri profili, ma svilupparli non è necessario per il primo risultato.

## 11. Piano per incrementi implementativi

La numerazione PICO evita confusione con le PR del precedente piano Zero. Sono unità di lavoro proposte, non pull request già aperte. Ogni incremento deve lasciare funzionante l'hub con firmware Pico assente.

### PICO-00 — Baseline, contratti e verifica di fattibilità TLS

**Dipendenze:** nessuna. **File:** documenti di compatibilità, fixture aggiuntive, script temporanei di qualificazione; nessuna riscrittura di `SatelliteService`.

Bloccare commit hub e SDK candidato; registrare compiler/submodule. Eseguire e registrare i test attuali prima di modificarli. Inventariare schema eventi, stato, health, comandi e audio, includendo comportamento dei grant. Fare un primo build MQTT+mTLS sul dispositivo reale; verificare versione TLS, nome certificato, RAM e dimensione firmware.

**Uscita:** ADR con matrice Pico W/Pico 2 W, SDK/lwIP/Mbed TLS reali, limiti Will e verdetto sul profilo eventi. Audio/JPEG possono restare non qualificati. Il solo successo di compilazione non dimostra il funzionamento TLS.

### PICO-01 — Progetto firmware e test host

**Dipende da:** PICO-00. **File:** `firmware/pico/CMakeLists.txt`, main, HAL timer, lifecycle, watchdog, test CTest, workflow CI.

Aggiungere target `pico_w`, `pico2_w` e target cablati. Separare funzioni pure dal codice SDK per testare parser, lease e serializer su host. Introdurre heartbeat locale USB/LED, allocatori e contatori; nessun sensore obbligatorio. Prodotti di build UF2/ELF/map senza segreti.

**Uscita:** build ripetibile, test host verdi, firmware base avviato e watchdog provato. Ogni target compila solo i moduli necessari al suo profilo.

### PICO-02 — Provisioning, storage e orologio

**Dipende da:** PICO-01. **File:** storage, identity, clock sync, `scripts/pico_provision.py`.

Implementare provisioning individuale, layout flash versionato, due slot configurazione, recovery USB e timer a 64 bit. Integrare SNTP e stato clock senza automatismi di sicurezza al ribasso. Testare mancanza CA, chiavi sbagliate, reset e configurazione troncata.

**Uscita:** l'identità resta stabile tra boot; boot ID cambia; una scrittura interrotta lascia disponibile una configurazione valida. Nessun evento di ora incerta risulta live.

### PICO-03 — MQTT, controllo e conformità con il broker attuale

**Dipende da:** PICO-02. **File:** net MQTT/TLS, protocol serializer/parser/lease, controllo fixture, piccoli adattamenti hub se necessari.

Implementare MQTT 3.1.1, mTLS, LWT compatto, subscribe confermato prima di online, grant/renew/revoke/stop/configure e ACK. Aggiungere mock sensor, coda limitata, replay classification e deduplica degli ID comando. Verificare insieme nodo Linux MQTT 5 e MCU 3.1.1 sul broker con ACL effettive.

**Uscita:** stato riconosciuto, grant ricevuto, baseline e nuova lettura registrate correttamente; comando duplicato non prolunga la sessione, vecchio LWT non spegne una nuova connessione. Fallimenti TLS non danno fallback plaintext.

### PICO-04 — Driver sensori reali

**Dipende da:** PICO-03. **File:** driver GPIO, I²C, 1-Wire, ADC; profili/pin map e test hardware.

Integrare prima PIR e contatto, poi gli altri driver come sottoincrementi. Allineare opzioni e payload al catalogo hub. Aggiungere timeout, baseline, qualità e errori; impedire conflitti risorse.

**Uscita:** un PIR reale fa scattare una regola già presente nell'hub; il contatto aperto al boot non genera una falsa nuova transizione. La camera non è necessaria per una regola solo sensore. Un guasto I²C non blocca MQTT e GPIO.

### PICO-05 — Profili MCU nell'hub e UI

**Dipende da:** PICO-03; completamento con PICO-04. **File:** nuovo catalogo capacità, `satellites/service.py`, API e pagina satelliti esistenti; test regressione.

Registrare tipo scheda, firmware, trasporto, limiti e capacità approvate. Mostrare pin, qualità clock, memoria, reset, driver e limitazioni; filtrare campi Linux non pertinenti. Configurazioni troppo grandi o ruoli non compilati vengono rifiutati prima dell'invio.

**Uscita:** installazione e configurazione del profilo sensori dall'hub senza terminale permanente. **MVP sensori completato**: PICO-00…05, indipendente dalle fasi media.

### PICO-06 — Presenza BLE

**Dipende da:** PICO-05. **File:** scanner BTstack, aggregazione, configurazione allowlist, test coesistenza.

Implementare filtro beacon, isteresi, scadenze e stato sconosciuto quando lo scanner non funziona. Documentare quali identificativi del beacon sono utilizzati; non derivare presenza certa da qualunque dispositivo scoperto.

**Uscita:** presenza filtrata nello stesso modello Sentry; Wi-Fi/MQTT rimangono reattivi durante scansione. Il test comprende radio guasta e rete congestionata.

### PICO-07 — Ingresso I²S e attività acustica

**Dipende da:** PICO-05. **File:** PIO RX, DMA, conversione PCM, activity driver, test con segnale sintetico.

Portare l'acquisizione sul microcontrollore; verificare bit, canale, clipping e sequenza campioni. Pubblicare solo eventi di attività con soglie configurate, senza inviare audio.

**Uscita:** rilevamento stabile con buffer limitati e privacy esplicita. Il firmware dichiara attività acustica ma non live audio se quest'ultimo non è qualificato.

### PICO-08 — Audio remoto SMA1

**Dipende da:** PICO-07 e gate TLS media. **File:** media client, packing SMA1, golden fixtures, prove gateway corrente.

Integrare i comandi audio già presenti, mTLS 1.3, token/stream e scadenze. Testare due connessioni TLS, scritture parziali, gap e stop; verificare la compatibilità con ascolto browser e registrazione hub senza introdurre un nuovo server audio.

**Uscita:** qualifica separata per Pico W/Pico 2 W. Il mancato superamento non blocca sensori o attività acustica; la capacità streaming resta disabilitata.

### PICO-09 — Bridge per Pico cablato

**Dipende da:** contratti PICO-03 e firmware base PICO-01/02; può procedere in parallelo a BLE/audio. **File:** USB CDC/framing, `scripts/pico_bridge.py`, mapping amministrativo e test.

Integrare sensori via USB, mapping di identità, sincronizzazione con il bridge e revoca su scollegamento. Il bridge non diventa un secondo motore di regole.

**Uscita:** Pico non W utilizzabile come sorgente autorizzata, con gli stessi eventi e senza radio esterna obbligatoria.

### PICO-10 — Snapshot JPEG, opzionale

**Dipende da:** PICO-05, prova camera SPI e gate TLS media; può riutilizzare il media client PICO-08 senza dipendere dalla qualifica del microfono. **File:** driver camera, nuovo contratto snapshot, gateway image sink, SourceManager/API per snapshot.

Verificare prima camera e FIFO in locale, poi trasporto a chunk. Implementare comando e capacità negoziati; limitare lunghezza, tempo e pixel decodificati. Conservare la sorgente nell'evidenza.

**Uscita:** foto a richiesta con modulo identificato. Nessuna dichiarazione di video continuo o supporto a regole visuali temporali senza test dedicato. Un fallimento resta confinato al profilo sperimentale.

### PICO-11 — Consolidamento e rilascio per profilo

**Dipende da:** PICO-05 per sensori; solo dalle fasi effettivamente pubblicate per le altre capacità. **File:** test integrazione, documentazione installazione/recovery, manifest release e CI.

Eseguire prove prolungate, interruzioni alimentazione/rete, rinnovi e revoche, regressione dell'hub. Pubblicare esclusivamente profili qualificati, con scheda e periferiche esplicite. La release sensori non attende JPEG/audio.

**Uscita:** manifest con SHA firmware/SDK/submodule, mapping pin, capacità abilitate, risultati test e limitazioni. Tutte le build “experimental” sono marcate in UI e documentazione.

## 12. Matrice di accettazione

I test seguenti erano da implementare/eseguire quando questo piano è stato scritto. Lo stato di ciascuno al 17 settembre 2026 è in [12.2](#122-stato-della-matrice-al-17-settembre-2026).

| ID | Prova | Risultato richiesto |
|---|---|---|
| T01 | Build RP2040 e RP2350 | Target corretti, map/manifest prodotti, nessun segreto negli artefatti |
| T02 | Fixture eventi valide/invalide | Stesso verdetto tra serializer/parser MCU, schema e Pydantic |
| T03 | JSON malformato/oversize/frammentato | Rifiuto senza overflow, leak o arresto loop |
| T04 | UUID, numeri, duplicate keys | Tipi/range coerenti; nessuna conversione silenziosa |
| T05 | Nome nodo/sorgente falso | Rifiuto identità mismatch o ACL; nessuna sorgente su un altro nodo |
| T06 | CA, SAN, scadenza o certificato errati | Connessione rifiutata, nessun downgrade |
| T07 | Nessuna credenziale/entropia TLS non pronta | Manutenzione/errore esplicito, nessun collegamento insicuro |
| T08 | LWT con nomi massimi | Payload/topic entro i limiti della libreria bloccata |
| T09 | Nuova connessione e primo grant | Subscription pronta prima di online; grant non perso |
| T10 | Grant assente/scaduto/revocato | Nessun evento live o media non autorizzato |
| T11 | Comando duplicato e rinnovo vecchio | Stesso ACK, durata non estesa |
| T12 | Vecchia epoca e vecchio LWT | Nessun effetto sulla sessione attuale |
| T13 | Client MQTT 3.1.1 e 5 insieme | Stessi eventi/regole attraverso il broker configurato |
| T14 | Coda piena/disconnessione/PUBACK perso | Memoria limitata, drop contati, ID lettura stabile |
| T15 | Evento storico dopo riconnessione | Journal o scarto, mai nuovo allarme |
| T16 | Ora non sincronizzata o salto UTC | Evento non live; nuova baseline dopo recupero |
| T17 | Baseline al boot/PIR in assestamento | Nessuna falsa transizione attiva |
| T18 | Debounce, polarità e contatto scollegato | Stato coerente con cablaggio, niente diagnosi inventate |
| T19 | I²C bloccato/DS18B20 CRC errato | Guasto confinato alla sorgente, rete e altri ingressi reattivi |
| T20 | ADC fuori range o dati sensore invalidi | Qualità corretta, nessun valore plausibile fabbricato |
| T21 | Aggiornamento configurazione interrotto | Recupero ultimo slot valido, identità preservata |
| T22 | Pin/PIO/DMA in conflitto | Configurazione rifiutata prima dell'applicazione |
| T23 | Rete assente a lungo | Backoff e memoria stabili, nessun reset storm |
| T24 | Watchdog e reset ripetuti | Causa visibile; boot/connessione non riusano ID |
| T25 | Scanner BLE in errore | Presenza sconosciuta, non assente |
| T26 | Coesistenza BLE + MQTT | Heartbeat/comandi entro target; nessuna fame del loop |
| T27 | I²S campioni noti | Canale, segno, allineamento e PCM16 corretti |
| T28 | Framing SMA1 e letture parziali | Decoder hub compatibile, niente struct padding |
| T29 | Audio + due handshake TLS | Margine RAM misurato e controllo MQTT reattivo |
| T30 | Audio stop/revoke/perdita hub | Stop entro lease; buffer svuotati, niente replay audio |
| T31 | Parlato hub e attività audio | Politica anti-loop esistente conservata |
| T32 | Snapshot JPEG grande/corrotto | Byte/pixel/tempo limitati, nessun falso percorso H.264 |
| T33 | Foto comandata mentre cambia preview | Evidenza dalla sorgente richiesta, non dalla preview |
| T34 | USB scollegata/dispositivo non autorizzato | Nodo sconosciuto/offline; nessuna identità arbitraria |
| T35 | Bridge riavviato e letture in attesa | Nessun ritimestamp che trasformi vecchi eventi in live |
| T36 | Hub senza Pico e nodo Zero esistente | Funzionalità precedenti e schema V1 invariati |
| T37 | Test mode, test manuale, disarmo | Semantica precedente mantenuta e side effect espliciti |
| T38 | Configurazione unsupported/oltre capacità | ACK failed e UI chiara, mai “applicato” fittizio |
| T39 | Più MCU contemporanei | Rate limit per nodo, equità e nessun accumulo non limitato |
| T40 | Sessione prolungata del profilo rilasciato | Nessun incremento di memoria non spiegato, contatori e guasti tracciati |

### 12.1 Soglie iniziali di collaudo

Obiettivi proposti per rete LAN in buone condizioni, da approvare dopo la prima misura: p95 sensore→ingress entro 500 ms dopo debounce; p95 comando stop gestito entro 500 ms quando il collegamento è sano; nessuna crescita non limitata delle code; margine RAM conforme alla sezione 6.5.

La scadenza locale della lease resta la protezione durante un'interruzione di rete, anche quando il target di latenza non è raggiungibile. Separare latenza del sensore, trasporto, decisione e azione: una richiesta di foto o un annuncio possono aggiungere latenza propria.

Per ogni profilo da pubblicare eseguire una prova continuativa proposta di almeno 48 ore, oltre ai test di guasto. Questa è la durata del test richiesto, non una stima di consegna. Registrare scheda, build, alimentazione, periferiche, rete, RAM minima anche durante handshake, stack massimo, latenze, riconnessioni, drop, errori TLS e risultato della revoca. Non trasferire la qualifica di Pico 2 W al Pico W originale.

### 12.2 Stato della matrice al 18 settembre 2026

Verdetti conservativi: dove il README del firmware non mostra una trascrizione di ciò che è
accaduto, la riga resta *non eseguita* anche se il codice che la riguarda esiste ed è
testato. «Hardware» significa una Pico 2 W — e per le righe del bridge una Pico 2 senza
radio, che è la stessa scheda con la radio mai accesa — contro l'hub in esercizio.

| ID | Esito | Dove |
|---|---|---|
| T01 | Hardware | Quattro target costruiti, manifest prodotto, `image_check` non trova segreti nell'immagine |
| T02 | Host | `check_against_contracts.py`: gli stessi eventi scritti dal firmware, letti da schema e Pydantic |
| T03 | Host | Suite `json`/`event`/`command`: malformato, oltre misura, frammentato |
| T04 | Host | Suite `names`/`json`: uuid, numeri, chiavi ripetute |
| T05 | Hardware | Una scheda che annuncia il nome di un altro nodo: niente pubblicato, e l'ACL del broker la rifiuterebbe comunque |
| T06 | Parziale | Quattro certificati offerti al broker e rifiutati o accettati di proposito; nessuno di essi *dalla scheda* |
| T07 | Parziale | Come sopra: il rifiuto è del broker, e il caso «nessuna credenziale» è provato solo dal lato del nodo che non parte |
| T08 | Host | `control.cpp`: il congedo entro i 255 byte del will con i nomi più lunghi ammessi |
| T09 | Hardware | Subscribe confermato prima di `online`, grant ricevuto e applicato |
| T10 | Hardware | Lease scaduta, revocata e assente: nessun evento live e nessun audio |
| T11 | Hardware | Comando ripetuto, stesso ack, durata non estesa |
| T12 | Parziale | Ogni evento porta epoca e grant sotto cui è stato pubblicato; la metà dell'hub — un vecchio will che non chiude una sessione nuova — è provata dall'hub, non dalla scheda |
| T13 | Hardware | Nodo Zero in MQTT 5 e microcontrollore in 3.1.1 sullo stesso broker |
| T14 | Parziale | Coda e PUBACK perso su host; la disconnessione su hardware. Una coda piena non si ottiene su una scheda cablata: è il bridge a possedere la connessione, e una lettura consegnata al cavo esce subito dalla coda — `queued=0` con il broker spento per quaranta secondi |
| T15 | Parziale | Le letture trattenute arrivano dopo l'interruzione; la classificazione a journal è dell'hub e provata dai suoi test |
| T16 | Superato | Ora non sincronizzata: nessun evento live. Il salto UTC, eseguito il 18 settembre 2026, ha trovato un difetto — un'ora avanti e poi indietro, marche non monotone, tutte dichiarate `synced` — e ora un passo oltre i due secondi è distinto da una correzione e ciò che è in coda smette di dichiararsi sincronizzato. La metà lenta è stata vista lo stesso giorno: tre ore al minuto senza risposte e la prima lettura `unknown`/`time_uncertain`, nodo ancora online. Anche questa ha trovato un difetto, nel verbo `time` della console, corretto e riprovato. Resta scelta, non misurata, la soglia dei due secondi |
| T17 | Hardware | Baseline al boot e finestra di assestamento: nessuna falsa transizione |
| T18 | Parziale | Debounce e polarità provati con la scheda che pilota il proprio pad; nessun PIR e nessun contatto reale |
| T19 | Parziale | CRC e 85 °C su host, bus vuoto su hardware, nessuna sonda che risponda; I²C non è implementato |
| T20 | Parziale | Conteggi impossibili rifiutati su host; su scheda il pin è flottante, e il rumore è etichettato per quello che è |
| T21 | Hardware | Scrittura interrotta con il verbo `tear`: torna l'ultimo slot valido, l'identità resta |
| T22 | Hardware | Conflitto di pin rifiutato prima di applicare, e l'hub riceve un ack `failed` |
| T23 | Hardware | Rete assente per minuti, non per ore: backoff e memoria stabili, nessun reset storm |
| T24 | Hardware | Watchdog provato, causa del reset visibile, boot e connessione con identificativi nuovi |
| T25 | Hardware | Scanner spento: presenza `unknown` subito, non `absent` |
| T26 | Hardware | BLE e MQTT insieme: heartbeat e comandi reattivi, `missed` fermo dopo l'handshake |
| T27 | Hardware | I²S con segnale sintetico su host e cattura su filo vuoto sulla scheda |
| T28 | Hardware | Framing SMA1 letto dal gateway dell'hub, e da `audio_check.py` su host |
| T29 | Hardware | Due handshake TLS insieme su Pico 2 W, con il margine di RAM misurato |
| T30 | Hardware | Stop, revoca e perdita dell'hub entro la lease, senza replay audio |
| T31 | Non eseguito | La politica anti-loop è dell'hub e non è stata riprovata con un microcontrollore |
| T32 | Non eseguito | `PICO-10`: niente camera |
| T33 | Non eseguito | Come sopra |
| T34 | Hardware | USB scollegata e nodo non mappato: offline, e nessuna identità arbitraria |
| T35 | Hardware | Bridge riavviato con letture in attesa: consegnate, non ritimestampate |
| T36 | Host | 760 test dell'hub verdi senza alcun Pico collegato |
| T37 | Parziale | Una regola ha scattato in test mode; le azioni reali restano decisione di chi abita la casa |
| T38 | Hardware | Configurazione oltre le capacità: ack `failed` e messaggio chiaro, mai «applicato» |
| T39 | Non eseguito | C'è una sola scheda |
| T40 | Parziale | Trentacinque minuti di esercizio con heap e code stabili, non le 48 ore che questa sezione chiede |

Le tre righe più pesanti che restano sono `T40` (la durata), `T18`/`T19` (un sensore vero
sui morsetti) e l'interruzione di alimentazione vera, che non è una riga della matrice ma è
il limite dichiarato in testa al README del firmware.

Il 18 settembre 2026 la matrice è stata ripresa su hardware e ha prodotto tre correzioni,
che è il motivo per cui una riga non eseguita non si dichiara superata: il salto UTC di
`T16`; le tre ore senza risposte della stessa riga, che hanno scoperto un verbo `time` di
console incapace di far tornare indietro l'orologio; e — fuori matrice — un broker riavviato
che lasciava un nodo cablato invisibile all'hub mentre continuava a pubblicare. Tutte e tre
sono nel README del firmware con la trascrizione di ciò che è stato visto.

### 12.3 Azioni residue

Ciò che i verdetti qui sopra lasciano aperto, come elenco di cose da fare e non di cose da
sapere. La versione lunga, con il perché di ognuna, è in [`firmware/pico/README.md`](../firmware/pico/README.md)
sotto *What is left to do*; questa è la stessa lista nell'ordine in cui conviene affrontarla.

1. **Rimettere in linea `pico-ingresso`.** Una `pico2_w`, `build/pico2_w/sentry_firmware.uf2`
   via BOOTSEL e un nuovo provisioning: la versione 2 dei record ha reso illeggibile tutto
   ciò che teneva, che è il percorso di aggiornamento scritto nel piano e costa un cavo e un
   minuto. Da questa dipendono le tre righe successive.
2. **`T14`, una coda piena su scheda.** Solo il nodo con radio possiede la propria coda: su
   una cablata la consegna è il frame sul cavo e `queued` resta a zero. Con il nodo radio in
   linea, spegnere il broker e guardare riempirsi la spool, il `link_lost`, la coalescenza e
   la spazzata dopo un salto d'orologio.
3. **`T06`/`T07` dalla scheda.** Quattro certificati sono già stati offerti a questo broker e
   rifiutati o accettati di proposito, ma tutti da un client Python. Manca l'handshake della
   scheda stessa rifiutato: `forget certificate` e poi uno che non deve passare.
4. ~~**`T16`, la metà lenta.**~~ Fatta il 18 settembre 2026: ultima ora ricevuta alle
   07:41:27 con `scripts/pico_bridge.py --time-every 0`, e alle 10:41:27 — tre ore al minuto
   — la prima lettura `unknown`, `historic`, `time_uncertain`, con il nodo ancora online e
   38.220 secondi di uptime ininterrotto. L'attesa ha trovato un difetto: il verbo `time`
   della console risincronizzava l'orologio senza registrare che una risposta era arrivata,
   così la regola lo riportava a `unknown` al giro dopo. Corretto e riprovato sulla scheda.
   Resta non osservata la deriva fra due risposte su una scheda lasciata in pace un giorno:
   ogni orologio qui è stato spostato apposta.
5. **`T18`/`T19`, un sensore vero sui morsetti.** Un PIR, un contatto reed, un DS18B20 con
   la sua resistenza da 4,7 kΩ, un microfono I²S. Finora la scheda ha pilotato il proprio pad
   e letto un bus vuoto.
6. **`T40`, quarantotto ore.** La corsa più lunga qui è di trentacinque minuti.
7. **Un'interruzione di alimentazione vera.** Il watchdog resetta il chip senza togliergli
   corrente e `tear` scrive mezzo record di proposito: nessuno dei due è la tensione che cala
   durante una cancellazione.
8. **`T39`, due schede insieme.** Il bridge porta una lista di schede e finora ce n'è sempre
   stata una.
9. **`PICO-10`.** Serve un modulo SPI Arducam; finché non c'è resta `not built` nel manifest,
   e con esso `T32` e `T33`.
10. **Decisioni, non hardware.** Le due aperte sono chiuse. `queued_ms`: l'hub lo legge, lo
    mostra sotto `queue.waited` del nodo e dice una riga quando un nodo supera i due secondi
    e una quando rientra — visto il 18 settembre 2026 su tre nodi insieme. Il nodo
    riapprovato mentre è già connesso: l'hub tiene lo `state` che ha rifiutato — uno per
    nodo, e solo per un nome che il registro conosce già — e lo rilegge appena quel nodo
    smette di essere rifiutato. Approvando dall'API non si aspetta nulla; approvando da
    `satellite_admin.py`, che scrive il file e non passa dall'hub, si aspetta il prossimo
    heartbeat: quindici secondi al peggio, undici il 18 settembre 2026 sulla scheda cablata.
    Resta da misurare la soglia dei due secondi, che per ora è scelta, come i due secondi
    che distinguono uno scatto d'orologio da una correzione.

## 13. Build, release e regole di consegna

Esempi dei comandi da rendere disponibili dopo PICO-01; le opzioni `SENTRY_PICO_PROFILE` sono parte del progetto proposto, non comandi già presenti nella repository:

```bash
# Profilo sensori wireless, RP2040
cmake -S firmware/pico -B build/pico-w-sensor -G Ninja \
  -DPICO_BOARD=pico_w -DSENTRY_PICO_PROFILE=sensor
cmake --build build/pico-w-sensor

# Stesso protocollo su RP2350 Arm
cmake -S firmware/pico -B build/pico2-w-sensor -G Ninja \
  -DPICO_BOARD=pico2_w -DSENTRY_PICO_PROFILE=sensor
cmake --build build/pico2-w-sensor
```

Il build deve rifiutare un profilo wireless sul target `pico`/`pico2` e un profilo non previsto. La variante cablata usa un target/profilo proprio. Il blocco dell'SDK riguarda anche i submodule: `main` aggiornato automaticamente non è un pin riproducibile.

Artefatti pubblicabili: UF2, ELF, map, checksum, manifest, note di compatibilità, licenze delle dipendenze. Artefatti privati: Wi-Fi, chiavi, certificati individuali, bundle provisioning. Una nuova build non deve includere inavvertitamente la configurazione flash letta da una scheda già provisionata.

La release indica separatamente: `sensor: qualified`, `ble: qualified/not-tested`, `audio_activity`, `audio_stream`, `snapshot`, `usb_bridge`. Non usare una sola etichetta “Pico supportato” per nascondere le differenze. Le qualifiche sono inizialmente tutte non eseguite finché non esiste un report.

Per tornare indietro, usare UF2 noto e configurazione compatibile salvata privatamente. Il firmware vecchio deve rifiutare un layout nuovo che non sa leggere. La procedura non cancella o reidentifica automaticamente i nodi nel registro dell'hub.

### 13.1 Vincoli per chi implementa

La prima PR deve fissare baseline, contratti e prova MQTT/mTLS, non creare contemporaneamente sensori, audio e camera. Nessuna duplicazione del motore regole. Nessun import di librerie Linux nel firmware. Nessuna modifica incompatibile dell'envelope V1. Nessuna esecuzione di shell o URL media decisi dal payload. Nessun bypass TLS/clock per far passare un test.

Quando un gate hardware non è verificabile nel proprio ambiente, consegnare build, test host e una procedura ripetibile, marcando il gate come non eseguito. Non sostituire misure con stime presentate come risultati.

**Decisione di rilascio:** completare prima PICO-00…05. Aggiungere BLE come secondo profilo; poi attività acustica. Live audio, bridge cablato e JPEG sono incrementi distinti. Pi 5 e Zero continuano a svolgere i ruoli multimediali già operativi, mentre il Pico amplia la copertura dei sensori.


## 14. Riferimenti e tracciabilità

Verifica delle fonti: 17 settembre 2026. I riferimenti alla repository sono fissati al commit analizzato; gli upstream esterni non fissati a release vanno bloccati durante PICO-00. Le ADR riportano prove effettuate nel progetto, non test eseguiti per redigere questo documento.

### Repository

- **[R1]** [Contratti condivisi e generazione schema](https://github.com/giacintop89/sentry-mode/blob/b597535d23d077f91cab84b9ad48727d943868c5/contracts/satellite/v1/README.md)
- **[R2]** [Envelope eventi e delivery](https://github.com/giacintop89/sentry-mode/blob/b597535d23d077f91cab84b9ad48727d943868c5/src/sentry_mode/satellites/protocol.py)
- **[R3]** [Trasporto MQTT hub e TLS](https://github.com/giacintop89/sentry-mode/blob/b597535d23d077f91cab84b9ad48727d943868c5/src/sentry_mode/satellites/mqtt.py)
- **[R4]** [EventIngress, clock, deduplica e classificazione](https://github.com/giacintop89/sentry-mode/blob/b597535d23d077f91cab84b9ad48727d943868c5/src/sentry_mode/satellites/ingress.py)
- **[R5]** [SatelliteService, stato, grant e adozione sorgenti](https://github.com/giacintop89/sentry-mode/blob/b597535d23d077f91cab84b9ad48727d943868c5/src/sentry_mode/satellites/service.py)
- **[R6]** [Comandi supportati dall’agente e ACK](https://github.com/giacintop89/sentry-mode/blob/b597535d23d077f91cab84b9ad48727d943868c5/satellite/src/sentry_satellite/commands.py)
- **[R7]** [ADR audio: SMA1, mTLS e stato di qualifica](https://github.com/giacintop89/sentry-mode/blob/b597535d23d077f91cab84b9ad48727d943868c5/docs/adr/satellite-audio-profile.md)
- **[R8]** [ADR video: H.264 su gateway mTLS](https://github.com/giacintop89/sentry-mode/blob/b597535d23d077f91cab84b9ad48727d943868c5/docs/adr/satellite-video-profile.md)
- **[R9]** [Gateway media e obbligo TLS 1.3](https://github.com/giacintop89/sentry-mode/blob/b597535d23d077f91cab84b9ad48727d943868c5/src/sentry_mode/vision/media_gateway.py)
- **[R10]** [Identità e tipi di sorgente, qualità e clock](https://github.com/giacintop89/sentry-mode/blob/b597535d23d077f91cab84b9ad48727d943868c5/src/sentry_mode/sources/models.py)
- **[R11]** [Trasporto MQTT/media del satellite Linux](https://github.com/giacintop89/sentry-mode/blob/b597535d23d077f91cab84b9ad48727d943868c5/satellite/src/sentry_satellite/mqtt.py)
- **[R12]** [Agente Linux: stato, health e lifecycle](https://github.com/giacintop89/sentry-mode/blob/b597535d23d077f91cab84b9ad48727d943868c5/satellite/src/sentry_satellite/agent.py)
- **[R13]** [Baseline verificata della repository](https://github.com/giacintop89/sentry-mode/commit/b597535d23d077f91cab84b9ad48727d943868c5)

### Documentazione primaria esterna

- **[E1]** [Raspberry Pi — Pico microcontroller boards, varianti e interfacce](https://www.raspberrypi.com/documentation/microcontrollers/pico-series.html)
- **[E2]** [Raspberry Pi — release Pico SDK 2.3.1, candidato da bloccare](https://github.com/raspberrypi/pico-sdk/releases/tag/2.3.1)
- **[E3]** [Raspberry Pi SDK — Networking Libraries, lwIP/Mbed TLS/BTstack](https://www.raspberrypi.com/documentation/pico-sdk/networking.html)
- **[E4]** [Raspberry Pi — esempi SDK, incluso MQTT/TLS](https://github.com/raspberrypi/pico-examples)
- **[E5]** [lwIP upstream — client MQTT, CONNECT e lunghezze Will; verificare poi il submodule SDK](https://github.com/lwip-tcpip/lwip/blob/master/src/apps/mqtt/mqtt.c)
- **[E6]** [Eclipse Mosquitto — protocolli supportati e comportamento del broker](https://mosquitto.org/man/mosquitto-8.html)
- **[E7]** [Arducam — esempi Pico per camera SPI Mini](https://github.com/ArduCAM/PICO_SPI_CAM)
- **[E8]** [TinyUSB — supporto classi USB device, incluso CDC](https://docs.tinyusb.org/en/latest/)
- **[E9]** [Raspberry Pi SDK — API high-level, random e supporto di sistema](https://www.raspberrypi.com/documentation/pico-sdk/high_level.html)

Gli esempi upstream dimostrano la disponibilità di componenti, non qualificano questo firmware. Restano da misurare footprint TLS, coesistenza radio, buffer audio, consumo e stabilità del cablaggio effettivo.
