# Sentry Mode — Satelliti Raspberry Pi Zero W
## Piano di implementazione: hub, agente, quattro ruoli e migrazione

**Data:** 16 settembre 2026  
**Stato:** piano implementativo; nessuna modifica applicata alla repository  
**Repository:** `giacintop89/sentry-mode`  
**Baseline verificata su `main`:** `4febced020fba69df70d8ad08df7ae4d2c3c0917`  
**Documento di ingresso:** [Piano funzionale dei satelliti](sentry-mode-zero-w-functional-plan.md)  
**Destinazione:** Raspberry Pi 5 come hub; Raspberry Pi Zero W originale, ARMv6, come satellite. Non Zero 2 W.

Il documento traduce il piano funzionale in decisioni tecniche, modifiche ai file, contratti, attività ordinabili, test e criteri di rilascio. I nuovi moduli, comandi, endpoint e schemi qui descritti **sono da implementare**, non sono già disponibili. Il codice e i riferimenti tecnici sono stati consultati; non sono stati eseguiti test della repository o prove sui Raspberry.

### Consultazione del piano

| Per trovare | Sezione |
|---|---|
| Cambiamenti nel codice e architettura | [Baseline](#2-baseline-del-codice-e-punti-di-intervento), [decisioni](#3-decisioni-architetturali), [file](#4-struttura-proposta-della-repository) |
| Ordine delle attività e criteri per procedere | [PR-00–PR-13](#7-piano-di-lavoro-ordinato-per-incrementi), [dipendenze](#8-dipendenze-parallelizzazione-e-confine-del-primo-rilascio) |
| Messaggi, identità e affidabilità | [Modelli](#5-contratti-interni-e-dati-persistenti), [MQTT](#6-protocollo-mqtt-identità-e-freschezza) |
| Compatibilità con l'installazione attuale | [Migrazione](#9-migrazione-v1--v2-e-compatibilità), [API](#10-api-nuove-interfaccia-e-autorizzazione) |
| Installazione e parametri operativi | [Configurazione](#11-configurazione-e-packaging), [limiti](#12-concorrenza-limiti-e-osservabilità), [rollout](#14-installazione-gestione-e-rollout) |
| Verifiche prima del rilascio | [Test e 26 ACC](#13-strategia-di-test-e-tracciabilità), [rischi](#15-rischi-e-criteri-di-arresto), [checklist](#16-checklist-di-completamento) |

## 1. Obiettivo e primo risultato utile

Realizzare un unico sistema Sentry capace di usare la camera esistente e satelliti con camera, sensori, microfono e presenza. Lo Zero acquisisce e trasmette; il Pi 5 conserva detection, regole, azioni e interfaccia.

**Primo traguardo di prodotto:** StreamCam esistente + uno Zero con camera CSI e PIR; una regola riceve movimento, cerca una persona sulla camera associata e salva la foto corretta. Cambiare anteprima non cambia la camera usata dalla regola.

**Primo incremento eseguibile:** agente con PIR → MQTT autenticato → regola sensore sul Pi 5, anche con camera disabilitata. Audio e presenza non bloccano questo incremento.

Si mantiene la stessa repository, con due distribuzioni Python indipendenti. Non servono un nuovo frontend, un cambio di detector, Kubernetes, un database esterno o una copia dell'applicazione completa su ogni Zero.

### 1.1 Invarianti non negoziabili

- L'installazione con zero satelliti continua a funzionare; l'estensione disabilitata non richiede broker, gateway media o librerie satellite.
- Nessun modello neurale, OpenCV, NumPy, Pydantic o componente di sintesi vocale è richiesto dall'agente satellite.
- Ogni dato e ogni azione multimediale conserva origine, sessione e freschezza. Non si sostituisce una sorgente indisponibile con quella attualmente in preview.
- Disarmo, test mode, test manuale reale, sequenze di azioni, speaker, PTT e funzioni locali conservano la loro semantica.
- Messaggi storici, retained, duplicati o precedenti all'armamento non generano nuovi allarmi.
- Un guasto produce `unknown`/`unavailable`, non un valore falso, una temperatura zero o una presunta assenza.
- Tutte le code, le sessioni, i processi e gli archivi hanno limiti. Nessuna azione esterna viene promessa come «exactly once».

## 2. Baseline del codice e punti di intervento

Il commit di `main` verificato coincide con quello del piano funzionale. Questi sono i vincoli effettivi, non astrazioni da aggiungere indiscriminatamente.

| File esistente | Osservazione verificata | Modifica necessaria |
|---|---|---|
| `src/sentry_mode/config.py` | `Settings.camera` è singolare; `device` accetta anche stringhe [R1] | Conservare il campo legacy e introdurre configurazione distribuita opzionale |
| `hardware/camera.py` | `VideoCapture(device)`, apertura/lettura sincrone, proprietà richieste al backend [R2] | Non affidare la cancellazione delle letture di rete a un flag Python |
| `vision/stream.py` | Ogni `VideoStream` costruisce un `DetectionWorker`; acquisizione e preview hanno domanda distinta [R3] | Separare capture, domanda e inferenza; un detector condiviso, non uno per satellite |
| `vision/detection.py` | Slot recente singolo; callback `on_result(detections, captured)` senza sorgente; rete DNN mutabile [R4] | Conservare detector e preprocessing, aggiungere metadati e scheduler seriale |
| `sentry/config.py` | Regole visuali con `object`; azioni media senza origine distribuita [R5] | Schema V2 con trigger tipizzati e sorgenti esplicite |
| `sentry/engine.py` | `arm()` chiama sempre `video.set_sentry`; `_healthy()` dipende dal video; `last_sample` è globale [R6] | Pianificare risorse per regola e separare stato per sorgente |
| `sentry/engine.py` | `Job` contiene regola, passi, creazione e attesa, ma non contesto di origine [R6] | Aggiungere contesto immutabile ai job, senza riscrivere il sequenziatore |
| `web.py` | `NodeControls` possiede un video, un monitor, un lock hardware e uno audio; usa `ThreadingHTTPServer` [R7] | Delegare a servizi dedicati; non trasformare `web.py` in un gestore di flotta monolitico |
| `vision/recording.py` | Acquisizioni in cartella limitata; registrazione video da frame e audio da comando microfono [R8] | Adapter sorgente, metadati laterali, nessuna sostituzione audio implicita |
| `pyproject.toml` | Il pacchetto hub include OpenCV e Pydantic [R9] | Packaging satellite separato, extra hub opzionali |
| `docs/http-api.md` | Header di controllo browser, nessun login applicativo; monitor locale e PTT distinti [R10] | Canali macchina separati e protezione amministrativa esplicita |

I test già presenti includono `test_sentry.py`, `test_web.py`, `test_stream.py`, `test_detection.py`, `test_recording.py`, `test_camera.py`, `test_audio.py`, `test_talk.py`, `test_speech.py`, `test_voice_effects.py`, `test_telegram.py`, `test_config.py`, `test_app.py`, `test_cli.py` e `test_status.py` in `tests/unit/` [R11]. Vanno estesi, non sostituiti con test soltanto del nuovo protocollo.

## 3. Decisioni architetturali

### ADR-01 — Due distribuzioni, una repository

`src/sentry_mode` rimane l'hub. `satellite/src/sentry_satellite` diventa un agente installabile senza il progetto principale. Gli schemi JSON e le fixture di contratto sono file condivisi nella repository; **non serve una terza libreria Python condivisa** nella prima versione.

L'hub può validare con Pydantic. Il satellite usa dataclass, validazione esplicita e `json`; una suite comune verifica che i due lati accettino e rifiutino gli stessi messaggi. Per la configurazione satellite usare TOML con `tomllib` e Python 3.11 o successivo, subordinatamente alla verifica dell'immagine OS scelta. Non introdurre una dipendenza YAML soltanto per uniformare l'aspetto dei file.

### ADR-02 — Conservare il modello di esecuzione dell'hub

Non migrare l'intera applicazione ad ASGI/FastAPI o ad `asyncio`. Usare servizi con API interne brevi, thread dedicati e code limitate. L'ingresso MQTT non esegue regole nel callback di rete. Il database ha un writer dedicato. L'inferenza ha un solo proprietario.

Per WebSocket audio utilizzare una libreria mantenuta con interfaccia sincrona, in un listener macchina distinto gestito dallo stesso runtime. La disponibilità delle API, TLS e dei limiti di buffer va verificata nella versione bloccata [E3]. Un eventuale event loop richiesto da BlueZ resta confinato al suo adapter satellite.

### ADR-03 — Un solo ingresso canonico degli eventi

MQTT 5 su TLS è il percorso iniziale. L'hub usa Mosquitto e un client Paho; il satellite usa Paho. La versione del protocollo wire Sentry (`schema_version=1`) è indipendente dalla versione MQTT.

Non sviluppare MQTT e HTTP in parallelo. L'eventuale `POST /api/satellites/v1/events` sarà un adapter successivo dello stesso `EventIngress`, non un motore alternativo. Tutti i media restano fuori da MQTT.

### ADR-04 — Identità individuali e canali separati

Profilo raccomandato: certificato client per nodo per MQTT e audio macchina, con verifica del certificato server. La CA non viene consegnata ai satelliti e la sua chiave privata non è leggibile dal servizio web.

Le API browser rimangono dietro rete amministrativa/VPN o autenticazione a monte. Un certificato satellite **non autorizza** API di regole, SSH, TTS, approvazione o test manuale.

### ADR-05 — Video codificato sullo Zero, decodifica sul Pi 5

Pipeline da qualificare come profilo di riferimento:

```text
CSI → rpicam-vid, H.264 hardware → FFmpeg in stream-copy/remux
    → RTSP con RTP interleaved TCP, dentro TLS o tunnel verificato
    → MediaMTX sul Pi 5 → decoder isolato → slot frame recente
    → scheduler condiviso → OpenCV DNN esistente
```

Non eseguire `libx264` o altri encoder software per ricodificare il video sullo Zero. Il processo FFmpeg satellite serve soltanto a incapsulare il flusso già codificato. CSI è il profilo principale; USB UVC e HTTP MJPEG sono profili alternativi da certificare separatamente.

**Decisione vincolante del gate hardware:** provare la pubblicazione RTSPS della build scelta con verifica del server. Se non disponibile o non affidabile, usare RTSP interleaved TCP attraverso un tunnel TLS verificato, per esempio stunnel. Non sostituire con RTSP in chiaro in produzione. Il tunnel deve coprire anche RTP/RTCP: per questo il profilo vieta il trasporto UDP esterno al tunnel. L'incapsulamento e la disponibilità dei backend dipendono dai binari realmente installati [E4, E5, E6, E7].

MediaMTX risiede **solo sull'hub**. Nessun requisito di compilare MediaMTX o un server video pesante su ARMv6. Non implementare due pipeline complete prima del collaudo del profilo principale.

### ADR-06 — Un journal SQLite locale, non un nuovo servizio dati

Introdurre SQLite sull'hub per registro nodi/sorgenti, deduplicazione e journal limitato. Conservare le regole nel loro file versionato e atomico. Il satellite ha una coda RAM, non un database obbligatorio.

Usare un writer e lettori separati, transazioni brevi, migrazioni numerate e un limite a database, WAL e spazio libero. Non risolvere la concorrenza condividendo senza disciplina una connessione tra tutti i thread [E8].

### ADR-07 — Migrazione esplicita, nessun doppio motore permanente

Una configurazione V1 viene normalizzata in memoria; la scrittura V2 è un'operazione esplicita, disarmata e preceduta da backup. Le regole legacy passano nello stesso motore tramite un adapter visuale. Evitare due implementazioni divergenti di cooldown, test mode e cancellazione.

## 4. Struttura proposta della repository

I file sotto riportati sono nuovi, salvo quelli esistenti indicati nei paragrafi di intervento. Creare ciascun modulo nella fase in cui serve, non tutte le directory vuote nel primo commit.

```text
contracts/satellite/v1/
  event.schema.json
  state.schema.json
  health.schema.json
  command.schema.json
  ack.schema.json
  audio-session.schema.json
  fixtures/{valid,invalid}/

src/sentry_mode/
  sources/
    models.py                 # SourceId, FramePacket, AudioPacket, quality
    registry.py               # nodi/sorgenti/zone e autorizzazioni
    manager.py                # ownership, domanda e ciclo di vita
    legacy.py                 # adapter della sorgente primaria
  satellites/
    config.py
    service.py                # composizione del sottosistema opzionale
    identity.py
    mqtt.py                   # trasporto, non regole
    ingress.py                # schema, identità, epoca, dedup, freschezza
    sessions.py               # grant e lease macchina
    health.py
    store.py                  # writer SQLite e query limitate
    migrations/
    api.py                    # dispatch di API browser nuove
  vision/
    network.py                # decoder remoto in processo cancellabile
    scheduler.py              # un modello, slot per sorgente
    media_gateway.py          # autorizzazione e lease dei path pubblicati
  audio/
    sources.py
    satellite_server.py       # WSS macchina, non il PTT
  presence/
    service.py
    wifi.py                   # interfaccia e adapter facoltativo
  sentry/
    migration.py
    triggers.py
    correlation.py
    resources.py
    context.py
  satellites.html
  satellites.js

satellite/
  pyproject.toml
  src/sentry_satellite/
    cli.py
    config.py
    agent.py
    identity.py
    protocol.py
    mqtt.py
    spool.py
    commands.py
    health.py
    subprocesses.py
    sensors/{gpio,onewire,bme280,adc}.py
    camera/{csi,uvc,publisher}.py
    audio/{capture,stream,activity}.py
    presence/bluez.py
  config/{camera-sensor,sensor-presence,audio-sensor}.example.toml
  systemd/sentry-satellite.service
  scripts/{bootstrap,install,doctor,update,rollback}.sh
  tests/{unit,hardware}/

scripts/
  satellite_admin.py          # provisioning/credenziali; invocazione amministrativa
  migrate_satellites.py
  satellite_simulator.py
  benchmark_satellites.py
  install_satellite_services.sh

deploy/satellites/
  mosquitto.conf.example
  acl.example
  mediamtx.yml.example
  media-tunnel.conf.example
  firewall-policy.md

tests/unit/                   # suite esistente + nuove unità
tests/integration/satellites/  # nuovo
tests/fixtures/satellites/     # contratti, config V1/V2, dati sintetici
docs/adr/                     # decisioni e profilo video effettivamente qualificato
```

Le ultime tre directory sono `tests/integration/satellites/`, `tests/fixtures/satellites/` e `docs/adr/`; non sono sottodirectory del pacchetto satellite. Gli script amministrativi non devono diventare comandi shell invocabili da un evento MQTT.

## 5. Contratti interni e dati persistenti

### 5.1 Modelli essenziali

| Modello | Campi essenziali e vincoli |
|---|---|
| `NodeRecord` | `node_id`, nome, stato approvazione, identità/seriali ammessi, protocollo, versione agente, profilo |
| `SourceRecord` | `source_id`, `node_id`, tipo, nome, zona, abilitazione, configurazione validata, revisione |
| `FramePacket` | `source_id`, `stream_epoch`, `frame_sequence`, dimensioni effettive, tempo ingresso monotono hub, timestamp cattura opzionale e qualità, buffer immutabile |
| `DetectionResult` | Identità completa del frame, detection, avvio/fine inferenza, revisione algoritmo, freschezza |
| `NormalizedEvent` | Identità autenticata risolta dall'hub, evento wire, zona da registro, tempi hub, eleggibilità e ragione di esclusione |
| `ActionContext` | `trigger_id`, `rule_id`, revisione regola, epoca armamento, eventi/frame di origine, sorgenti risolte, zona, scadenza |
| `MediaLease` | Nodo, sorgente, sessione, capacità, epoca connessione, scadenza, finalità, identificativo autorizzazione |

`source_id` e `node_id` non dipendono dall'indirizzo IP o dal nome visualizzato. Le zone arrivano dal registro centrale. Modificare il nome non azzera implicitamente uno storico o modifica i riferimenti nelle regole.

Definire `boot_id` come UUID nuovo a ogni avvio dell'agente, incluso il riavvio del solo servizio: evita che un contatore azzerato collida con eventi precedenti. L'eventuale ID di avvio del kernel è una metrica distinta. La sequenza evento cresce all'interno di `boot_id`; la sequenza frame appartiene a `stream_epoch`.

### 5.2 Interfacce da stabilizzare prima dell'integrazione

```text
Contratti indicativi, non codice eseguibile:
SourceManager.acquire(source_id, purpose, owner_id) -> SourceLease
SourceManager.latest_video(source_id, max_age_ms) -> FramePacket | None
SourceManager.release(lease_id) -> None
EventIngress.accept(transport_context, payload) -> IngestReceipt
InferenceScheduler.submit(frame_packet) -> None
TriggerEngine.observe(normalized_event_or_detection) -> list[TriggerDecision]
ActionExecutor.enqueue(action_context, ordered_groups) -> EnqueueResult
SessionManager.grant(node_id, source_id, purpose) -> MediaLease
```

I metodi `acquire` potenzialmente lenti non devono tenere il lock globale del motore. Prevedere apertura con stato `starting`, timeout e verifica della generazione prima di adottare la risorsa. Nessun callback di un driver può richiamare direttamente il sequenziatore.

### 5.3 Schema SQLite minimo

| Tabella | Scopo |
|---|---|
| `schema_migrations` | Versione del database, checksum della migrazione |
| `nodes` / `sources` / `zones` | Registro centrale, stato autorizzativo e revisioni |
| `event_receipts` | Deduplicazione: chiave unica nodo/evento e nodo/boot/sequenza |
| `event_journal` | Payload normalizzato limitato, tempi, eleggibilità, esito |
| `trigger_runs` / `action_runs` | Collegamento evento-regola-azione, esito anche incerto |
| `credential_revocations` | Identità/seriali revocati e data; nessuna chiave privata |

Non scrivere ogni frame, heartbeat o campione PCM nel journal. Le misure ripetitive possono essere aggregate; transizioni, errori e scarti rilevanti sono persistenti. La retention della deduplicazione non deve essere più breve della massima finestra di replay ammessa. La cancellazione degli eventi visibili non deve cancellare per errore le protezioni anti-duplicato ancora necessarie.

I metadati delle evidenze vanno in sidecar JSON privati, conservati e cancellati insieme ai media, per non rendere il vecchio archivio dipendente dal nuovo database. La listing aggiunge metadati opzionali; i file precedenti restano leggibili senza inventare provenienze.

## 6. Protocollo MQTT, identità e freschezza

### 6.1 Dipendenze e impostazioni

Selezionare e bloccare una versione Paho della famiglia 2.x verificata sulla versione Python dell'immagine. Dichiarare esplicitamente la callback API, MQTT 5, TLS e limiti delle code. Paho documenta callback separati e `manual_ack`, che permette all'hub di confermare dopo l'elaborazione persistente invece che al ritorno del callback [E1].

| Topic relativo a `sentry/v1/nodes/{node_id}/` | QoS | Retained | Uso |
|---|---:|---|---|
| `events` | 1 | No | Eventi operativi o storici marcati |
| `state` | 1 | Sì | Snapshot e disponibilità, mai impulso |
| `health` | 0 | No | Metriche e prova di vita periodica |
| `commands` | 1 | No | Grant, stop, rinnovi e configurazione consentita |
| `acks` | 1 | No | Riscontri con ID comando |

Nel profilo MQTT 5 mantenere il flag retained nelle sottoscrizioni dove serve distinguerlo (`retain-as-published`) e testare anche una pubblicazione retained mentre l'hub è già collegato. Non affidare questo controllo soltanto al caso di consegna iniziale dopo subscribe.

Impostare scadenze MQTT quando disponibili, **oltre** alla freschezza applicativa. Coda client, messaggi in-flight, coda broker e dimensione pacchetto devono essere limitati separatamente.

### 6.2 Identità: non fidarsi del payload

Mosquitto verifica certificato e ACL. Nel profilo mTLS, mappare l'identità certificata a uno username/nodo ristretto; usare ACL generate per topic propri. Il subscriber MQTT ordinario riceve topic e payload, non una prova automatica del nome del publisher: l'hub risolve quindi l'identità dal topic **protetto da ACL**, confronta `node_id` nel payload e verifica che la sorgente sia registrata per quel nodo [E1, E2].

Un nodo scrive solo `events/state/health/acks` propri e legge solo `commands` propri. Solo l'identità hub può pubblicare comandi. Nessun wildcard di scrittura generico ai satelliti. Il certificato deve essere associato al nodo atteso nel provisioning; non basta che sia firmato da una qualunque CA del sistema.

Provisioning iniziale tramite CLI amministrativa: chiave generata sul nodo, CSR, firma controllata e registrazione in attesa. L'immagine clonabile contiene soltanto configurazione di esempio e CA pubblica. Approvazione abilita capacità nel registro; revoca disabilita immediatamente l'ingresso sull'hub e invalida sessioni, quindi aggiorna ACL/CRL o il meccanismo equivalente della versione installata. Il reload della configurazione non va assunto sufficiente a terminare connessioni già attive: questa condizione richiede un test dedicato.

### 6.3 Connessione ed epoche

Per ogni connessione creare `connection_id`; l'hub emette un `hub_epoch` e un grant fresco, legati a nodo, boot e connessione. Prima del grant: consentire soltanto handshake, stato e health, senza trigger automatici.

Dopo la riconnessione: nuova epoca, snapshot iniziale, svuotamento controllato del buffer storico e separazione dalle nuove transizioni. Paho può mantenere messaggi outgoing anche attraverso una riconnessione: limitare la coda della libreria e scartare i messaggi della vecchia epoca sull'hub, indipendentemente da quanto dichiara `replayed` [E1].

I comandi contengono `command_id`, epoca, destinatario, capacità, durata e scadenza. Il satellite deduplica i comandi e restituisce `received`, `applied` o `failed`; una ritrasmissione non riapre indefinitamente una sessione. I rinnovi hanno numero progressivo e autorizzano una durata calcolata col clock monotono locale.

### 6.4 Envelope operativo

Mantenere i campi del piano funzionale e aggiungere i metadati di consegna necessari. Separare il contenuto immutabile dell'evento dal wrapper di consegna:

```json
{
  "schema_version": 1,
  "event": {
    "event_id": "7e524a64-021c-4a4c-b6f5-9f206830e2a1",
    "node_id": "zero-ingresso",
    "source_id": "zero-ingresso.pir-1",
    "boot_id": "c4e6f35b-8627-4b53-88bf-a36bc7371b33",
    "sequence": 42,
    "kind": "sensor.motion",
    "occurred_at": "2026-09-16T10:20:30Z",
    "clock_status": "synced",
    "value": true,
    "unit": null,
    "quality": "valid"
  },
  "delivery": {
    "connection_id": "connection-uuid",
    "hub_epoch": "hub-epoch-uuid",
    "grant_id": "grant-uuid",
    "queued_ms": 0,
    "replayed": false,
    "initial_state": false
  }
}
```

È uno schema nuovo da implementare e bloccare con fixture; non una configurazione già utilizzabile. Un relay o un retry cambia solo il wrapper, non identità, sequenza, timestamp o valore dell'evento. `value` è tipizzato per `kind`; rifiutare NaN, infiniti, coercizioni ambiguamente accettate, chiavi sconosciute e ID non registrati.

### 6.5 Algoritmo di ingresso

1. Verificare dimensione prima del parsing, topic, approvazione nodo e rate limit per nodo. Massimo proposto: 8 KiB per evento, non per blocco audio.
2. Validare schema, sorgente, tipo e unità; confrontare identità e contenuto. Sanitizzare gli errori senza loggare segreti o audio.
3. Classificare come live, iniziale, duplicato, storico, fuori ordine, scaduto o temporalmente incerto. Richiedere grant/epoca correnti per il live.
4. In una transazione inserire ricevuta e journal; vincoli unici impediscono la doppia ammissione concorrente. Una collisione di ID con payload diverso è un errore, non un normale retry.
5. Confermare la ricezione MQTT QoS 1 solo dopo il commit, oppure dopo aver registrato/contabilizzato un rifiuto. Evitare retry infiniti dei messaggi deliberatamente invalidi.
6. Inoltrare alla coda del motore soltanto eventi eleggibili, senza bloccare rete o writer. La saturazione produce un esito esplicito, non una coda infinita.
7. Prima di creare le azioni ricontrollare epoca di armamento, revisione regola, dipendenze e freschezza. Registrare la decisione.

Se l'hub termina dopo il commit ma prima dell'azione, non recuperare automaticamente l'azione al riavvio. Registrare l'interruzione e ripartire disarmati. Se termina durante un effetto esterno, l'esito può essere `unknown`; non ritentarlo ciecamente.

### 6.6 Tempo non affidabile e dati fuori ordine

I tempi monotoni di due Raspberry non sono confrontabili direttamente. Il clock del satellite non diventa affidabile soltanto perché il JSON contiene `clock_status=synced`.

Prima versione: per gli eventi automatici remoti richiedere sincronizzazione OS verificata dal doctor, stato corrente di clock valido, grant fresco e finestra UTC ammessa; registrare anche tempo di coda locale e ricezione hub. Salti dell'orologio, timestamp futuri oltre tolleranza o clock non valido mettono la sorgente in `time_uncertain`: letture visibili, nessuna correlazione stretta o azione fondata su una freschezza non dimostrabile. Il ripristino richiede un nuovo baseline, non il replay dei campioni precedenti.

Un timestamp presente su un frame solo al decoder è dichiarato `ingress_only`. Non presentarlo come latenza dalla cattura. Per correlazioni video strette qualificare il ritardo end-to-end della pipeline o fornire un'associazione attendibile tra timestamp media e cattura; altrimenti la modalità stretta non si abilita. La UI può offrire esplicitamente una correlazione basata sui tempi di osservazione dell'hub, con questa limitazione visibile.

Per una sorgente applicare una high-water mark della sequenza nella stessa epoca. Dati fuori ordine vanno nello storico, non riportano indietro lo stato corrente. Un retained online o un vecchio Last Will non prevale su una connessione più recente.

## 7. Piano di lavoro ordinato per incrementi

Ogni incremento corrisponde a una PR o a un piccolo gruppo di commit revisionabili. Le dipendenze indicano quando una funzione può essere integrata e abilitata, non impediscono di scrivere unit test prima di avere l'hardware.

### PR-00 — Baseline, regressione e qualificazione ARMv6

**Dipendenze:** nessuna.  
**File:** test esistenti; nuovi `docs/adr/zero-w-runtime.md`, `docs/adr/satellite-video-profile.md`, fixture V1 e script doctor.

Attività:

- Salvare fixture prive di segreti della configurazione YAML, del file regole e delle risposte API legacy. Includere anche `camera.device` contenente un URL, non solo `0`.
- Eseguire la suite esistente e registrare il risultato. Ogni errore già presente va distinto dalle regressioni introdotte; non nasconderlo con nuovi skip.
- Sullo Zero originale verificare modello, `uname -m`, bitness Python, versione OS/kernel, RAM disponibile, CSI, audio, GPIO, TLS e connessione MQTT. Il semplice successo su `aarch64` non vale come ACC-01.
- Provare camera CSI 640×480/10 FPS, encoder hardware, remux e trasporto protetto. Misurare CPU/RSS, FPS, bitrate, stalli e ripartenza; nessun comando legacy `raspivid` presupposto funzionante nello stack corrente [E4].
- Verificare le dipendenze con un ambiente satellite pulito: nessuna build nascosta di NumPy, Rust/Pydantic o pacchetti AI. Se un pacchetto richiede una compilazione non prevista, fermare quel profilo e sceglierne uno supportato.
- Annotare versioni esatte, checksum dell'immagine e dei pacchetti distribuiti, camera, cavo, alimentazione e modalità di rete nel report. Non bloccare versioni «latest».

**Gate G0:** agente minimo e profilo media provati sullo Zero originale. La parte software può procedere con simulatore; la capacità hardware resta `unqualified` fino alla prova. Non pubblicare una procedura di installazione come verificata prima di G0.

### PR-01 — Contratti, registry e adapter legacy

**Dipendenze:** baseline di PR-00.  
**File:** `sources/models.py`, `sources/registry.py`, `sources/legacy.py`, `satellites/config.py`, `config.py`, `contracts/satellite/v1/`, test di contratto.

Attività:

- Definire ID immutabili, tipi di sorgente, qualità e tempi; introdurre le fixture JSON valide e invalide.
- Aggiungere `satellites.enabled=false` e dipendenze opzionali dell'hub. Import differiti: avviare l'app senza extra deve continuare a funzionare.
- Registrare in memoria la camera preesistente come `legacy-primary`, il microfono come `legacy-microphone` e lo speaker come uscita locale. Il `device` della camera viene conservato esattamente.
- Introdurre `SourceRef`/`SourceRecord`; per ora l'adapter primario delega all'oggetto `VideoStream` già esistente.
- Stabilire una sola fonte autorevole per ogni dato: driver locali nel YAML esistente, sorgenti remote nel registry, regole nel file Sentry. Gli esempi YAML di provisioning non diventano un secondo registry concorrente.

**Test:** validazione tipizzata, proprietà degli ID, nessun import satellite/AI reciproco, avvio senza broker, equivalenza del device legacy e delle API ancora delegate.

**Gate:** nessun cambiamento osservabile delle funzioni esistenti; identità di origine disponibile internamente.

### PR-02 — Scheletro dell'agente e simulatore

**Dipendenze:** PR-01.  
**File:** `satellite/pyproject.toml`, `cli.py`, `config.py`, `agent.py`, `health.py`, `spool.py`, `subprocesses.py`, `scripts/satellite_simulator.py`.

Attività:

- Creare i comandi satellite proposti `run`, `validate`, `doctor` e `identity`; i nomi definitivi vanno congelati nei test CLI.
- Implementare TOML, profili, avvio/stop, gestione SIGTERM, `boot_id`, coda RAM limitata e driver fittizi.
- Separare il thread rete dalla lettura sensori. Operazioni bloccanti e processi figli devono essere sorvegliati senza rallentare health e stop.
- Generare identità soltanto nel provisioning; mai rigenerarla a ogni avvio. Generare invece sempre una nuova epoca runtime.
- Costruire un simulatore con scenari riproducibili: PIR, duplicati, clock errato, backlog, reboot, nodo rumoroso e guasto sensore. Un evento simulato non contiene istruzioni shell o regole.
- Distribuire fixture insieme ai test senza richiedere l'installazione del progetto hub. Usare test unitari indipendenti dal discovery root di pytest.

**Test:** installazione isolata del wheel, avvio senza periferiche, stop pulito, massimo eventi/byte rispettato, stallo di un driver senza arresto del protocollo, assenza di segreti nei log.

**Gate:** l'agente simulato è una periferica del protocollo, non un secondo Sentry.

### PR-03 — Broker, identità e gestione dei nodi

**Dipendenze:** PR-01 e PR-02.  
**File:** `satellites/identity.py`, `mqtt.py`, `sessions.py`, `service.py`, `scripts/satellite_admin.py`, esempi deploy.

Attività:

- Fornire configurazione Mosquitto senza anonimi, con TLS, ACL, persistenza e quote. Non sovrascrivere un broker già presente senza un comando esplicito e backup.
- Implementare provisioning in attesa, approvazione, rinnovo e revoca. La modifica di file di sistema resta a un'operazione amministrativa, non al servizio web non privilegiato.
- Collegare certificato/username al nodo e topic al `SourceRegistry`. Testare username, client ID e identificativo payload discordanti.
- Implementare handshake, epoca hub, grant, heartbeat e Last Will con `connection_id`.
- Rendere visibili separatamente stato del broker, connessione nodo e qualità delle singole periferiche. Se il broker cade, indicare `transport_unavailable`; non generare un allarme offline identico per ogni nodo.
- Generare certificati server con SAN compatibile con l'indirizzo realmente usato, anche un IP. Non richiedere DNS pubblico e non usare `verify=False`.

**Test:** nodo A non pubblica per B, certificato scaduto/non registrato rifiutato, revoca su connessione attiva, retained obsoleto, autenticazione macchina non valida sulle API amministrative.

**Gate:** nessun ingresso anonimo, isolamento tra nodi, controllo del ciclo di vita senza apertura media automatica.

### PR-04 — Journal, deduplicazione e ingresso affidabile

**Dipendenze:** PR-03.  
**File:** `satellites/store.py`, `migrations/`, `ingress.py`, `health.py`, integrazione Paho.

Attività:

- Implementare schema SQLite e migrazioni, writer singolo, ACK dopo commit e gestione esplicita del sovraccarico.
- Applicare algoritmo della sezione 6: validazione, epoche, dedup, baseline, ordinamento e classificazione temporale.
- Coda prioritaria per eventi/stati e percorso coalescente per telemetria; rate limit per nodo e limite globale. Un nodo non deve occupare tutto il budget.
- Persistenza delle ragioni di esclusione: `duplicate`, `retained`, `replayed`, `expired`, `unapproved`, `time_uncertain`, `rate_limited`, `out_of_order`.
- Gestire disco pieno/corrotto: rifiuto o sospensione degli eventi automatici remoti, health visibile, nessuna perdita dichiarata come accettazione. Conservare le funzioni locali indipendenti quando la politica lo consente.
- Separare cancellazione del journal, dedup e stato delle sorgenti. Aggiungere snapshot/backup sicuro, non una copia incauta del solo file SQLite mentre il WAL è attivo.

**Test:** replay prima/dopo riavvio, commit fallito, stesso ID con payload diverso, retry QoS 1, messaggi fuori ordine, retained inviato a subscriber già attivo, saturazione e ripartenza broker.

**Gate:** gli eventi normalizzati sono osservabili e persistenti, ma ancora non hanno accesso diretto alle azioni.

### PR-05 — Regole V2, risorse e contesto delle azioni

**Dipendenze:** PR-01 e PR-04.  
**File:** `sentry/config.py`, `migration.py`, `triggers.py`, `resources.py`, `context.py`, `engine.py`; test Sentry e migrazione.

Attività:

- Introdurre trigger `vision`, `sensor_event`, `threshold`, `audio_event`, `presence_state`, `health_event`; implementare subito i primi tre e validare senza abilitare le capacità non ancora disponibili.
- Spostare categoria, confidence, ROI, conteggio e assenza nei campi del trigger visuale. Cooldown e sequenza di azioni restano a livello regola.
- Aggiungere `rule_id` stabile e `ActionContext` immutabile. Ogni job eredita contesto e token di cancellazione dell'armamento; non rilegge la selezione corrente della dashboard.
- Sostituire il controllo video incondizionato di `arm()` e `_healthy()` con `ResourcePlanner`: trigger PIR richiede sensore, trigger visuale richiede camera+detector, azione foto richiede una sorgente camera risolvibile.
- Separare dipendenze di monitoraggio da dipendenze delle azioni: la camera di una foto può essere acquisita quando serve, ma deve essere configurata e autorizzata prima di armare. Mostrare il possibile ritardo di avvio.
- Mantenere il sequenziatore esistente, `with_previous`, wait, TTL, lock speaker e registrazioni concorrenti. Nel percorso nuovo prenotare la capacità di coda per una sequenza intera o rifiutarla interamente; documentare questo miglioramento rispetto al possibile accodamento parziale a saturazione.
- Aggiungere generazione/epoca di armamento e scartare callback completati dopo disarmo o dopo il successivo armamento.
- Implementare `simulate` con stato di regola isolato e executor nullo. Il test manuale reale resta separato e disponibile solo all'operatore disarmato.

**Test:** PIR senza camera; soglia con isteresi e durata; nessuna transizione da snapshot iniziale; invalidità non equiparata a zero; semantica delle azioni e dei test invariata; saturazione della sequenza atomica; cancellazione di risultati in ritardo.

**Gate:** una regola solo sensore funziona senza caricare il modello. Migrazione V1→V2 reversibile mediante backup e senza perdita dei parametri.

### PR-06 — Driver sensori e prima UI satelliti

**Dipendenze:** PR-02–PR-05; verifica GPIO di G0 per rilascio hardware.  
**File:** driver sotto `satellite/.../sensors/`, `satellites/api.py`, `satellites.html`, `satellites.js`, `web.py`, CSS esistente.

Ordine dei driver: ingresso digitale/PIR, DS18B20, BME280, infine ADC/luminosità. In ogni caso la UI distingue il driver supportato da una semplice capacità teorica.

Attività:

- Adapter GPIO su API della versione di sistema scelta, con numerazione BCM esplicita, polarità, debounce e rilascio delle linee. Non spargere chiamate hardware direttamente nell'agente.
- DS18B20 tramite interfaccia 1-wire del sistema, CRC/lettura valida, timeout e valore assente. Sensori I²C tramite adapter con bus/indirizzo, controlli di conflitto e unità esplicite.
- Per LDR quantitativa richiedere un ADC configurato; non inventare una lettura analogica dal GPIO. Driver e circuito effettivo vanno qualificati.
- Separare assestamento iniziale dal primo fronte reale. Non fissare per tutti i PIR un warm-up presunto universale: il profilo contiene il valore verificato sul modulo.
- Configurazione remota limitata ai driver già installati: validare, applicare per revisione, effettuare rollback alla precedente se il driver non riparte. Niente modifica remota di rete, certificati o pacchetti attraverso questo comando.
- Pagina satelliti con stato, versione, profilo, zona, periferiche, misure, ultimo aggiornamento ed errori. Aggiungere selezione sensore nell'editor regole, senza attendere video e audio.

**Test:** bounce, stato iniziale alto, CRC errato, sensore staccato, conflitti di linee, bus bloccato, aggiornamento configurazione fallito, comando duplicato, navigazione da mobile.

**Gate G1:** primo incremento utile: PIR reale → evento → regola → notifica testuale o azione locale già presente; camera completamente disabilitabile.

### PR-07 — Multi-sorgente e inferenza condivisa

**Dipendenze:** PR-05; input sintetici sufficienti durante lo sviluppo.  
**File:** `sources/manager.py`, `vision/stream.py`, `vision/detection.py`, `vision/scheduler.py`, `sentry/engine.py`, controller.

Attività:

- Trasformare il possesso delle sorgenti in lease/reference counting: preview, monitoraggio e registrazione hanno owner distinti; il contatore non può andare negativo e un owner rilascia solo la propria domanda.
- Mantenere il lock della camera primaria, ma introdurre lock di lifecycle per sorgente. La camera B non attende il lock hardware della camera A. Non tenere lock durante rete, inferenza o `join()`.
- Introdurre un `FramePacket` per ogni frame catturato, non soltanto per ogni JPEG di preview. `sequence` della preview e sequenza capture non sono la stessa cosa.
- Un solo scheduler prende l'ultimo frame non ancora elaborato di ogni sorgente secondo round-robin pesato, frequenza richiesta e budget globale. Il boost PIR ha durata finita e non può azzerare il servizio delle altre camere.
- Conservare `ObjectDetector.detect()` e il suo preprocessing; la rete OpenCV è posseduta da un unico worker. Per il percorso distribuito isolare il worker nativo in un subprocess con un solo modello, una richiesta in-flight e un watchdog; niente modello caricato nel parent e poi duplicato per camera. IPC iniziale semplice e limitato; shared memory solo dopo misure.
- Calcolare confidence di prefiltraggio sufficiente per tutte le regole attive; applicare i filtri specifici per regola dopo inferenza. Non mutare la configurazione della rete da thread concorrenti.
- Risultati e overlay sono indicizzati per sorgente, epoca stream e frame; stale o generazione precedente vengono scartati. Una box della camera A non deve finire sull'anteprima B.
- Sostituire `last_sample` globale con stato per sorgente/regola. Un frame riusato non aumenta `consecutive_detections`; uno slot saltato non dimostra assenza.

**Test:** una sola istanza modello, equità con frame producer veloci/lenti, zero inferenza richiesta da regole solo sensori, nessuna crescita delle code, stale/epoche, preview nascosta, registrazione che mantiene la sorgente, processo inference bloccato.

**Gate:** camera primaria e due sorgenti sintetiche coesistono, senza modificare la semantica dei controlli legacy. Prima della promozione finale lo stesso percorso normalizzato deve servire anche la modalità legacy, evitando due motori permanenti.

### PR-08 — Camera CSI, gateway e decoder di rete

**Dipendenze:** G0 video, PR-03 e PR-07.  
**File:** `satellite/.../camera/`, `vision/network.py`, `vision/media_gateway.py`, `satellites/sessions.py`, deploy media e doctor.

Attività:

- Implementare profilo CSI: validazione camera, risoluzione/FPS, avvio encoder e remux come argv separati con `shell=False`. I comandi finali dipendono dalla build qualificata; non accettare argomenti FFmpeg arbitrari nel TOML remoto.
- Produrre SPS/PPS e keyframe con una frequenza adeguata alla riconnessione, misurando il compromesso bitrate/ritardo. I valori si congelano nel profilo dopo G0.
- Pubblicare su un path hub assegnato, per esempio `satellites/{node_id}/{source_id}`; il nodo non sceglie URL o path di altre sorgenti. Autorizzare publication e lettura separatamente.
- Integrare un controllo di autorizzazione del gateway contro i lease correnti, usando l'interfaccia della versione MediaMTX fissata. Le credenziali/grant media sono a breve durata, redatti nei log e non restituiti al browser.
- Un lease scaduto o una revoca deve chiudere **anche una publication già aperta**, non solo impedirne la prossima. Il controllore hub termina la sessione gateway tramite API amministrativa confinata a loopback e l'agente arresta la propria pipeline alla scadenza.
- Decoder remoto in subprocess sorvegliato: apertura/lettura timeout, socket e pipe chiusi su stop, escalation terminate/kill/reap. Non dichiarare risolto uno stallo semplicemente facendo `thread.join(timeout=...)`.
- Leggere continuamente il flusso live e sostituire lo slot; non attendere il periodo di inferenza prima di drenare il decoder. La gestione attuale delle pause capture non va copiata senza adattamento ai flussi di rete.
- Non eliminare a caso byte di H.264 compresso quando il buffer cresce: invaliderebbe le dipendenze tra frame. Interrompere e ristabilire il flusso dal keyframe successivo; lo scarto latest-only si applica ai frame decodificati.
- Pubblicare dimensioni effettive e qualità temporale. Modificare profilo remoto con comando/ack e nuova epoca stream, non con `CAP_PROP_*` sul ricevitore.

**Test:** source offline all'avvio, connessione senza dati, flusso troncato, encoder morto, gateway riavviato, sessione revoked in corso, perdita rete, niente buffer crescente, processo figlio sempre terminato, accesso negato a un path altrui.

**Gate:** StreamCam locale + una CSI remota in contemporanea; poi seconda remota. Un guasto remoto non blocca dashboard o disarmo.

### PR-09 — Evidenze instradate, preview e API multi-camera

**Dipendenze:** PR-07 e PR-08.  
**File:** `vision/recording.py`, funzioni media di `sentry/engine.py`, `web.py`, `web.html`, `dashboard.js`, editor regole e nuovi test.

Attività:

- Risolvere `source_id` al trigger e trasferirlo nel contesto. L'azione può usare `trigger_source` solo quando il tipo è compatibile; una regola PIR che chiede una foto deve nominare la camera.
- Fotografare un frame recente della sorgente, oppure registrare errore/skip. Conservare sequenza, timestamp e qualità; distinguere «foto al trigger» da «foto acquisita dopo».
- Registrazioni V1 compatibili: mantenere il formato e il percorso attuali. Le sorgenti remote entrano tramite adapter frame, con budget di registrazioni/encoder sul Pi 5. Lo stream-copy per registrazioni senza overlay è un'ottimizzazione successiva, non condizione per G2.
- Associazione audio esplicita: nuove azioni video hanno `audio_source_id` o nessun audio. In migrazione, `audio=true` legacy mantiene intenzionalmente il microfono configurato prima. Non estendere questo comportamento automaticamente alle nuove camere remote.
- Salvataggio media `.part`, rename e sidecar; pulizia coerente su errore, crash, cancellazione e retention. Vecchi nomi/file continuano a essere serviti e scaricabili.
- Aggiungere endpoint per source ID; mantenere quelli legacy come alias della sorgente primaria, non della camera selezionata dal browser.
- UI con selettore, stato `starting/live/stale/offline`, età disponibile e origine. Sessione preview per viewer/browser: una scheda che chiude B non interrompe A o altri utilizzatori di B.

**Test:** preview B mentre PIR A salva foto A; sorgente sparita prima dell'azione; audio non associato; file legacy e range HTTP; camera primaria configurata con URL; identità nei sidecar; due browser indipendenti.

**Gate G2:** primo satellite camera+PIR utile insieme alla camera esistente, con evidenze sempre della sorgente richiesta.

### PR-10 — Correlazioni limitate e politica di guasto distribuita

**Dipendenze:** PR-05 e PR-09.  
**File:** `sentry/correlation.py`, `triggers.py`, `resources.py`, `engine.py`, editor.

Attività:

- Implementare inizialmente una sequenza di due passi: evento sensore e conferma visuale, sorgenti esplicite, stessa zona quando richiesto, finestra finita. Niente DSL generale, codice Python o espressioni da eseguire.
- Un solo candidato pendente per regola e coppia sorgenti; decidere e documentare che un secondo PIR non prolunga indefinitamente il primo candidato. La conferma viene consumata una sola volta.
- Contare solo risultati visuali nuovi e ammissibili nella finestra; requisito temporale stretto disponibile solo con qualità adeguata. Stato invalido, perdita sorgente o cambio configurazione cancellano il candidato.
- Separare `fault_policy=global` e `fault_policy=isolated`. Il default delle configurazioni migrate resta globale. L'isolamento è attivato esplicitamente.
- Guasto camera: sospendere le regole dipendenti. Guasto detector condiviso: sospendere tutte le regole visuali, ma non quelle sensore indipendenti. Se non resta alcuna regola operativa, lo stato è degradato/non operativo, non normale protezione.
- Mantenere stato latched distinto da unknown. La perdita del sensore non deve contare come il periodo di assenza necessario al riarmo.
- Al recupero riacquisire baseline e campioni validi; non «recuperare» la conferma di una sequenza scaduta. Il riavvio dell'hub resta disarmato.

**Test:** finestra scaduta, PIR ripetuti, dati fuori ordine, camera in zona diversa, qualità temporale insufficiente, isolamento selettivo e modalità globale, assenza reale contro dati mancanti.

**Gate:** regola «PIR → persona → foto → testo Telegram» corretta, senza promettere allegato della foto alla notifica se non implementato separatamente.

### PR-11 — Microfono remoto, sessioni e attività acustica

**Dipendenze:** PR-03, PR-05, PR-07 e PR-09; G0 audio sullo Zero.  
**File:** `satellite/.../audio/`, `audio/sources.py`, `audio/satellite_server.py`, `sessions.py`, registrazioni e `listen.js`.

Attività:

- Acquisizione ALSA sullo Zero con periferica esplicita: PCM signed 16 bit little-endian, mono, 16 kHz; processo senza shell, lettura continua e arresto controllato. Non rendere PipeWire o PulseAudio obbligatori sul satellite.
- WebSocket binario su TLS, autenticazione nodo e sessione concessa dal Pi 5. Listener distinto dall'HTTP browser e dal PTT; una nuova connessione non riprende la precedente.
- Handshake JSON limitato con formato, sessione, epoca e finalità; successivamente blocchi da 100 ms. Payload PCM nominale 3.200 byte, cioè 32.000 byte/s. Sono dimensioni del formato, non misure di rete.
- Header binario versionato per blocco: sequenza, indice primo campione, tempo monotono di acquisizione e numero campioni. Il sample index consente di rilevare buchi; il monotono satellite non è confrontato direttamente con quello hub.
- Una sola cattura per microfono, con fan-out verso listener/recorder autorizzati. Coda di circa 300–500 ms come valore iniziale da tarare; limiti applicativi e della libreria, compressione WebSocket disabilitata per il PCM.
- Sender bloccato o backlog eccessivo: chiudere e ristabilire una nuova sessione, senza inviare secondi di passato. Duplicati/out-of-order rifiutati; buchi segnalati e, se necessario, breve silenzio esplicito per continuità, mai dati inventati come campioni reali.
- Lease 30 s, rinnovo ogni 10 s e timeout di inattività iniziale 3 s; verificare questi valori sul target. Mute/disabilitazione invalida immediatamente grant, acquisizione e inoltro; nessuna riapertura automatica su retry.
- Separare ascolto, registrazione e analisi livello. RMS/dBFS richiede un consenso di acquisizione autonomo, non si abilita quando nessuno ascolta solo per comodità.
- Implementare evento `audio.activity` con isteresi e permanenza; non classificarlo come parola, persona o suono specifico. Associare emissioni TTS/suoni alle zone per sopprimere o marcare l'eco secondo una policy visibile.
- Registrazione audio remota tramite adapter PCM, non costruendo un comando `Microphone` locale. Video+audio scelti esplicitamente hanno metadati del disallineamento; non promettere sincronizzazione professionale.

**Test:** revoca attiva, heartbeat hub perso, lease scaduto, microfono rimosso, blocchi corrotti, byte dispari, sequence gap, formati inattesi, memoria limitata, stop browser, eco, regressione PTT e speaker Bluetooth.

**Gate G3:** ascolto esplicito e clip dalla sorgente richiesta, nessuna registrazione continua implicita e nessun microfono aperto oltre la lease dopo perdita dell'hub.

### PR-12 — Presenza BLE e adapter Wi-Fi facoltativo

**Dipendenze:** PR-03–PR-06; prove scanner reali.  
**File:** `satellite/.../presence/bluez.py`, `presence/service.py`, `presence/wifi.py`, trigger e UI.

Attività BLE:

- Usare le API D-Bus BlueZ con binding leggero verificato; evitare parsing dell'output interattivo di `bluetoothctl`. Applicare filtri e allowlist del dispositivo/beacon configurato [E9].
- Normalizzare identificativi radio in ID applicativi pseudonimi sul satellite. Non inoltrare inventari di sconosciuti o MAC grezzi nei log generali.
- Stabilire salute/coverage dello scanner, osservazioni realmente nuove, RSSI filtrato e finestra temporale. Rileggere ripetutamente un oggetto D-Bus cached non equivale a nuovi avvistamenti.
- Aggregare le osservazioni ripetitive senza gonfiare il numero di hit. Default da tarare: tre osservazioni in dieci secondi per ingresso, 120 secondi di scansione valida senza osservazioni per assenza.
- Offline, scanning stopped, adapter reset o connessione hub persa producono `unknown`. Con più scanner, un positivo fresco può indicare presenza; dichiarare assenza globale richiede copertura valida da tutti gli osservatori previsti, altrimenti unknown.
- Vietare l'uso della sola presenza come autenticazione/disarmo automatico. Non convertire RSSI in metri senza un progetto di calibrazione distinto.

Attività Wi-Fi:

Definire `PresenceProvider` sull'hub e fixture per associazione attiva, lease DHCP e dato stale. Implementare un adapter reale soltanto per un'interfaccia router autorizzata e verificata: identità di servizio read-only, schema bloccato, timeout e rate limit. Un lease storico non significa client attualmente connesso. La semplice scansione degli access point dallo Zero non realizza questa funzione.

**Test:** beacon scomparso con scanner valido, scanner guasto, indirizzo cambiato, dato cached, falsi ingressi ravvicinati, più osservatori, reset BlueZ, traffico media concorrente solo nei profili qualificati.

**Gate G4:** ruolo presenza completato tramite BLE; Wi-Fi rimane esplicitamente facoltativo e non viene dichiarato supportato senza un adapter reale collaudato.

### PR-13 — Installazione, hardening, aggiornamenti e collaudo finale

**Dipendenze:** tutti gli incrementi richiesti per il profilo rilasciato.  
**File:** script install/update/rollback, unità systemd, documentazione, test integrazione e hardware, benchmark.

Attività:

- Installazione indipendente hub/satellite, idempotente, senza modificare servizi esistenti non pertinenti. Utenti, directory e permessi minimi per ciascun profilo.
- Vincolare capability e versione wire; un protocollo non supportato mostra errore, non un nodo verde che trasmette dati ignorati.
- Pacchetti/release immutabili, manifest con checksum, configurazione separata, backup consistente, rollback applicativo e dei dati compatibili.
- Verificare revoca dei quattro canali: eventi, comandi, publication video e audio attivo. Controllare che un listener amministrativo non sia accidentalmente esposto alla rete IoT.
- Aggiornare API, regole, sicurezza, setup Raspberry, schema e guide operative. Rimuovere rami temporanei o adapter duplicati introdotti durante la migrazione.
- Eseguire tutti gli ACC e il test proposto di 72 ore, misurando processi, file descriptor, memoria, storage e code. Ridurre il profilo autorizzato se il carico target non regge; non nascondere il risultato alzando buffer o disabilitando i test.

**Gate G5:** checklist di rilascio completa, prove riproducibili e percorso di recupero verificato.

## 8. Dipendenze, parallelizzazione e confine del primo rilascio

```text
PR-00 baseline / prove target
  └─ PR-01 contratti e legacy
       ├─ PR-02 agente / simulatore
       └─ PR-03 identità e broker  ← PR-02
            └─ PR-04 ingresso e journal
                 └─ PR-05 trigger V2 / risorse
                      ├─ PR-06 sensori / UI minima ── G1
                      └─ PR-07 sorgenti / scheduler
                           └─ PR-08 video reale
                                └─ PR-09 evidenze / UI ── G2
                                     ├─ PR-10 correlazioni
                                     ├─ PR-11 audio ── G3
                                     └─ PR-12 presenza ── G4
                                          └─ PR-13 consolidamento ── G5
```

Il primo rilascio sperimentale utilizzabile è G1; il primo rilascio corrispondente al caso d'uso camera+PIR è G2 con PR-10 per la conferma visuale correlata. Audio e BLE possono essere sviluppati in parallelo **dopo** aver stabilizzato protocollo, sessioni e modello delle sorgenti. Non parallelizzare modifiche concorrenti a `engine.py`, schema regole e migrazione prima dei rispettivi contratti.

Le verifiche di isolamento, TLS, lease e anti-replay appartengono ai primi incrementi: PR-13 le consolida ma non le introduce per la prima volta.

### 8.1 Funzioni da rimandare senza lasciare ambiguità

| Funzione | Decisione |
|---|---|
| Ingresso eventi HTTP aggiuntivo | Dopo MQTT; medesimo `EventIngress` |
| Archivio persistente sullo Zero | Non richiesto; coda RAM e perdita al reboot dichiarate |
| Pre-roll video | Solo dopo un buffer già attivo, limitato e collaudato |
| Stream-copy registrazioni sull'hub | Ottimizzazione successiva, con semantica overlay esplicita |
| Allegati media automatici su Telegram | Funzione distinta; il piano riusa l'azione testuale esistente |
| Trascrizione o classificazione audio | Non prevista in questa versione |
| Speaker/attuatori sui satelliti | Non previsti; niente esecuzione di azioni arbitrarie sui nodi |
| Multi-camera con un modello per camera | Non adottato |
| Wi-Fi presence generica per qualsiasi router | Non dichiarata; adapter specifico facoltativo |
| Tutti i quattro ruoli simultanei sul medesimo Zero | Solo profilo custom dopo qualifica |
| Nuovo frontend o cambio framework hub | Non necessario |

## 9. Migrazione V1 → V2 e compatibilità

### 9.1 Struttura versionata

`schema_version` indica il formato persistito; `revision` continua a gestire concorrenza e aggiornamenti ottimistici. La revisione non sostituisce la versione di schema. Il numero di schema appartiene al documento di regole, non a ogni singola regola.

Formato V2 proposto, mostrato in YAML solo per leggibilità; la persistenza Sentry può rimanere JSON:

```yaml
schema_version: 2
revision: 8
config:
  test_mode: true
  action_ttl_seconds: 15
  rules:
    - id: entrance-confirmed
      name: Ingresso confermato
      enabled: true
      trigger:
        type: sequence
        within_seconds: 5
        time_basis: hub_observation
        steps:
          - type: sensor_event
            source_id: zero-ingresso.pir-1
            kind: sensor.motion
            edge: rising
          - type: vision
            source_id: zero-ingresso.camera-1
            object: person
            min_confidence: 0.70
            min_count: 1
            consecutive_detections: 2
            rearm_after_absence_seconds: 10
      cooldown_seconds: 60
      actions:
        - type: photo
          source_id: zero-ingresso.camera-1
          count: 1
        - type: telegram
          text: "Movimento confermato all'ingresso."
  ssh_commands: {}
  telegram:
    bot_token: ""
    chat_id: ""
```

L'esempio è **da implementare**; è volutamente in test mode e senza credenziali. `time_basis=hub_observation` dichiara la semantica dei tempi osservati sull'hub, non una garanzia di cattura entro cinque secondi. Non abilitarla silenziosamente in sostituzione di una correlazione stretta richiesta dall'operatore.

### 9.2 Tabella di conversione

| Campo V1 | Destinazione V2 |
|---|---|
| `camera.device/width/height/fps` | Adapter `legacy-primary`, configurazione originaria preservata |
| Regola `name` | Nome invariato; nuovo `id` stabile generato una sola volta |
| `object`, confidence, count, consecutive, ROI, assenza | `trigger.type=vision` su `legacy-primary` |
| Cooldown e ordine azioni | Identici |
| `with_previous` e wait | Identici |
| Photo/Video senza sorgente | `source_id=legacy-primary` |
| AudioAction senza sorgente | `audio_source_id=legacy-microphone` |
| Video `audio=true` | Associazione esplicita al microfono precedente |
| Video `audio=false` | Nessuna sorgente audio |
| Telegram, SSH, suoni e voci | Riferimenti e segreti preservati privatamente |
| Fault globale | `fault_policy=global` |
| Armed | Non persistito; avvio sempre disarmato |

Prima di convertire verificare anche il YAML caricato, gli override ambiente e il file locale di regole: una migrazione non deve ignorare la configurazione effettiva a favore di un esempio presente nel repository.

### 9.3 Procedura della migrazione

Implementare `scripts/migrate_satellites.py` con operazioni esplicite di dry-run, applicazione e ripristino. I flag precisi vanno documentati quando lo script è creato.

Procedura: leggere e validare V1 → produrre report redatto → disarmare e fermare i writer → creare backup privato con manifest/checksum → convertire e validare in staging → salvare atomicamente → aggiornare la revisione → riavviare disarmato → smoke test. Prima del cutover il manifest registra la versione di tutte le componenti e lo stato delle migrazioni DB.

La conversione è idempotente: eseguire nuovamente lo script su V2 non duplica sorgenti, cambia ID o incrementa revisioni senza necessità. Il dry-run non scrive configurazioni e non apre hardware.

Rollback: fermare il servizio, conservare separatamente lo stato V2 corrente, selezionare release precedente e backup corrispondente, ripristinare file coerenti e verificare. Le regole nuove create dopo la migrazione non vengono magicamente convertite in V1; il report deve segnalarne l'esclusione dal rollback, senza cancellarle dall'export di recupero.

### 9.4 Contratti API legacy

Le API media legacy continuano a indirizzare `legacy-primary` e il microfono legacy. Non diventano alias della selezione dell'ultimo browser.

Per le regole introdurre `/api/sentry/v2/config` e `/api/sentry/v2/rules/test`. L'editor aggiornato usa V2. Le API V1 restano utilizzabili quando l'intero set di regole è rappresentabile senza perdita; in presenza di trigger V2 non rappresentabili devono restituire un errore esplicito `schema_upgrade_required`, non una proiezione incompleta salvabile che eliminerebbe le nuove regole. Un POST V1 non può sovrascrivere un set V2 incompatibile.

Con zero satelliti e sole regole legacy i contratti esistenti devono rimanere equivalenti. Questa limitazione dei client vecchi, quando vengono create funzioni che non conoscono, deve essere documentata prima del passaggio.


Se `satellites.enabled=false` ma esistono regole V2 che richiedono sorgenti remote, mostrare una configurazione non operativa e rifiutare l’armamento di quelle regole. Non ignorarle silenziosamente e non far apparire il sistema completamente operativo.

## 10. API nuove, interfaccia e autorizzazione

Tutti gli endpoint seguenti sono proposti. Gli handler devono delegare ai servizi e usare limiti di payload, timeout e risposte strutturate. Non esporre oggetti Pydantic con segreti tramite un `model_dump` non filtrato.

| API | Scopo e vincolo |
|---|---|
| `GET /api/satellites` | Elenco paginato o limitato, stato e capacità |
| `GET /api/satellites/{id}` | Dettaglio, problemi e versioni; niente chiavi private |
| `POST /api/satellites/{id}/approve` | Abilitazione operatore autorizzato; non firma CA dal web |
| `POST /api/satellites/{id}/revoke` | Deny immediato nel registry e invalidazione sessioni |
| `GET /api/sources` | Sorgenti tipizzate, zona, stato e revisione |
| `POST /api/sources/{id}/configure` | Configurazione driver valida, revisione, ack applicazione |
| `GET /api/sources/{id}/status` | Stato periferica e qualità temporale |
| `POST /api/sources/{id}/video/start` | Acquisizione di una lease preview del browser |
| `GET /api/sources/{id}/video` | MJPEG hub della sorgente con lease preview; nessun URL del nodo esposto |
| `POST /api/sources/{id}/video/stop` | Rilascia solo la lease del chiamante |
| `POST /api/sources/{id}/audio/start` | Ascolto esplicito; crea domanda e grant se autorizzati |
| `GET /api/sources/{id}/audio` | PCM dal fan-out hub, non connessione browser diretta al satellite |
| `POST /api/sources/{id}/audio/stop` | Ferma l'ascolto richiesto, non registrazioni indipendenti |
| `GET /api/events` | Journal limitato con cursore e filtri autorizzati |
| `POST /api/events/simulate` | Stato isolato e nessun effetto reale |
| `GET/POST /api/sentry/v2/config` | Regole versionate e optimistic concurrency |
| `POST /api/sentry/v2/rules/test` | Esecuzione manuale reale, esplicita e solo disarmato |

Il listener WSS macchina `/api/satellites/v1/audio` non è un upgrade gestito alla buona da `BaseHTTPRequestHandler`: è un servizio separato con libreria WebSocket, mTLS e controllo sessione. L'eventuale proxy deve preservare questa separazione e non rendere fidato un header di identità inviabile dal client.

Le lease preview servono a gestire ownership e domanda, non sostituiscono l'autenticazione operatore. Se l'app continua a non avere login, la protezione amministrativa resta quella dichiarata a monte; il deployment deve impedirne l'accesso ai satelliti. Conservare le protezioni same-origin/CSRF esistenti per i POST browser.

### 10.1 Modifiche UI senza redesign generale

Pagina Satelliti: tabella o card adattabili al mobile, problemi distinti per nodo e sorgente, approvazione/revoca, zona, profilo, versione e tasti di test espliciti. Nessun live audio/video su semplice apertura pagina.

Dashboard: selettore della sorgente, preview aggiornata, badge stale/offline, FPS richiesti ed effettivi, inferenza effettiva e qualità del tempo. Il cambio sorgente riguarda solo la preview. L'ultima immagine non aggiornata non è etichettata «live».

Editor: prima tipo trigger, poi sorgenti compatibili, poi condizioni e azioni. Visualizzare le dipendenze delle azioni multimediali e richiedere una camera esplicita dopo un PIR. «Simula evento» e «Esegui azioni ora» sono controlli differenti.

Registro: evento → criterio soddisfatto/non soddisfatto → sorgenti → azioni → evidenze o errori. Un'azione Telegram rimane indicata come testo se non invia media.

### 10.2 Casi da non dimenticare nel ResourcePlanner

Un trigger `health_event` relativo a un nodo offline dipende dal servizio health dell'hub, non dal fatto che quel nodo sia online: diversamente non potrebbe mai notificare il guasto. Le transizioni di salute generate dall'hub hanno un'origine interna affidabile distinta dagli eventi che un nodo invia su sé stesso.

Per azioni già in corso, la perdita di una sorgente non deve cambiare la policy delle altre azioni del gruppo. Registrare il fallimento specifico e continuare o cancellare secondo la policy esplicita; disarmo globale continua a prevalere su tutto il lavoro automatico.

## 11. Configurazione e packaging

### 11.1 Estensione hub proposta

```yaml
satellites:
  enabled: true
  store_path: .local/satellites.sqlite3
  fault_policy: isolated       # scelta esplicita; migrazione legacy = global
  mqtt:
    host: 127.0.0.1
    port: 8883
    protocol: 5
    topic_prefix: sentry/v1
    tls_ca_file: /etc/sentry-mode/satellites/ca.crt
    tls_cert_file: /etc/sentry-mode/satellites/hub.crt
    tls_key_file: /etc/sentry-mode/satellites/hub.key
  health:
    heartbeat_seconds: 15
    stale_after_seconds: 45
    offline_after_seconds: 60
  limits:
    event_max_bytes: 8192
    journal_max_bytes: 104857600
    journal_retention_days: 7
  media:
    lease_seconds: 30
    renew_every_seconds: 10
    video_gateway: mediamtx
```

Esempio non ancora supportato dal validatore attuale. Anche con `host=127.0.0.1` il certificato server deve essere verificabile rispetto al nome/IP usato o a un server name esplicitamente configurato e validato: non disabilitare la verifica per far funzionare l'esempio. L'indirizzo del broker visto dai satelliti è quello del Pi 5 sulla rete dedicata, non il loopback.

### 11.2 Agente satellite proposto

```toml
[node]
id = "zero-ingresso"
profile = "camera-sensor"

[hub]
mqtt_host = "192.168.1.10" # esempio da sostituire con l'hub reale
mqtt_port = 8883
protocol = 5

[tls]
ca_file = "/etc/sentry-satellite/ca.crt"
cert_file = "/etc/sentry-satellite/node.crt"
key_file = "/etc/sentry-satellite/node.key"

[limits]
event_max_count = 256
event_max_bytes = 1048576
event_max_age_seconds = 300

[[sources]]
id = "zero-ingresso.pir-1"
kind = "gpio"
line_numbering = "bcm"
line = 17
active_high = true
debounce_ms = 100
# Warm-up da configurare dopo verifica del sensore effettivo.

[[sources]]
id = "zero-ingresso.camera-1"
kind = "csi"
profile = "qualified-csi-h264"
width = 640
height = 480
fps = 10
```

È un esempio di contratto da implementare, non istruzioni di cablaggio. Pin e debounce vanno verificati sul montaggio reale. Una capacità nel TOML non autorizza il nodo a pubblicare: è necessaria l'approvazione dell'hub. L'URL media viene assegnato in base al registro e al profilo amministrativo, non da un evento.

### 11.3 Matrice delle dipendenze

| Componente | Dipendenze/profilo |
|---|---|
| Hub esistente | Conservare dipendenze attuali e modello YOLOX |
| Hub extra satelliti | Paho; SQLite standard library; dipendenza WebSocket solo quando serve audio |
| Hub video | MediaMTX e decoder compatibile con Pi 5, versioni bloccate |
| Satellite base | Python, standard library e Paho; nessuna dipendenza dal pacchetto hub |
| Satellite sensori | API GPIO di sistema qualificata; binding I²C/ADC solo nei profili che lo richiedono |
| Satellite video | rpicam-apps del sistema, FFmpeg stream-copy; tunnel solo se necessario al profilo protetto |
| Satellite audio | ALSA e client WebSocket verificato; nessun motore STT |
| Satellite BLE | BlueZ e binding D-Bus leggero qualificato; niente scanner Wi-Fi promiscuo |

Le versioni esatte si congelano nel report di G0 e nei file di lock. I pacchetti binari di sistema hanno una distinta separata dalle dipendenze pip. Non attribuire il successo di una wheel x86_64 alla compatibilità ARMv6.

Per un pacchetto di sistema visibile fuori dalla virtualenv scegliere esplicitamente un ambiente controllato con accesso ai site-packages oppure un'installazione compatibile nella venv; documentare la scelta. Non aggiungere percorsi arbitrari a `sys.path` finché «parte».

## 12. Concorrenza, limiti e osservabilità

### 12.1 Proprietari delle risorse

| Risorsa | Proprietario |
|---|---|
| Socket MQTT | Thread rete Paho; callback brevi |
| Scritture SQLite | Writer dedicato, transazioni e coda limitate |
| Stato delle regole | Valutatore serializzato con ingressi normalizzati |
| Camera locale | Un proprietario capture, condiviso tramite lease |
| Decoder remoto | Processo per sorgente con supervisione e timeout |
| Rete DNN | Un worker/processo condiviso per hub |
| Speaker locale | Lock audio già esistente |
| Microfono remoto | Un reader satellite e fan-out hub per sorgente |
| Publisher video | Pipeline satellite unica per sorgente, governata dalla lease |

Ordine di shutdown: invalidare nuovi grant e azioni → disarmare → annullare job e sessioni → rilasciare la domanda automatica → chiudere socket/pipe → terminare e reap dei worker → flush limitato del journal → chiudere MQTT. Il servizio non deve aspettare indefinitamente una periferica.

Un heartbeat di rete può continuare mentre un worker è morto: health deve verificare avanzamento reale delle capacità, non soltanto che il processo principale esista. Lo stesso vale per lo watchdog systemd.

### 12.2 Valori iniziali, da misurare

| Parametro | Valore/obiettivo iniziale | Politica in caso di superamento |
|---|---|---|
| Video satellite | 640×480, 10 FPS | Ridurre profilo dopo misura; non accumulare frame |
| Inferenza richiesta | 1–2 FPS per camera, entro budget globale | Esporre frequenza effettiva e condizioni non soddisfatte |
| Modelli residenti | Uno nel percorso distribuito | Test automatico contro duplicazione accidentale |
| Buffer video applicativo | Un frame recente per sorgente, un task inferenza in-flight | Sostituzione dello slot e contatore scarti |
| Outbox satellite | 256 eventi, 1 MiB, massimo 5 minuti | Rimozione scaduti/storico e conteggio; perdita al reboot dichiarata |
| Messaggio evento | 8 KiB | Rifiuto prima del parsing |
| Heartbeat/stale/offline | 15/45/60 s | Separare guasto broker da nodo e periferica |
| Lease media | 30 s, rinnovo ogni 10 s | Stop publisher/audio e invalidazione lato hub |
| Buffer audio | 300–500 ms da tarare | Scarto controllato o nuova sessione, mai backlog illimitato |
| Journal | 7 giorni o 100 MiB | Retention e manutenzione anche WAL; dedup separata |
| Valutazione evento | p95 ≤250 ms da ricezione hub a decisione | Metriche e limitazione carico; esclude rete e azione |
| Recupero sorgente | Obiettivo ≤30 s dopo effettivo ripristino | Backoff con jitter, errore visibile se non raggiunto |
| Test prolungato | 72 ore come prova di rilascio | Report, analisi crescita memoria/storage e leak |

Il numero di nodi registrati non equivale al numero di stream analizzabili. Il profilo di prova proposto è quattro Zero registrati, camera locale + due camere remote attive e un microfono remoto, distribuiti su nodi diversi. È un **carico da provare**, non una capacità già dimostrata.

Non allargare automaticamente le finestre di una regola per nascondere una frequenza di inferenza insufficiente. Mostrare `insufficient_sampling`/degraded, mantenendo distinti FPS richiesti, FPS erogati e tempo necessario alla conferma.

### 12.3 Metriche richieste

Contatori di ingresso, rifiuti, duplicati, replay, coda, drop e latenze del journal; salute broker/nodi/sorgenti; reconnect e restart; CPU, RSS, file descriptor e spazio libero; FPS cattura/decodifica/inferenza; bitrate; età frame e sua qualità; lease attive; blocchi audio e campioni mancanti; tempi di stop; regole operative/degradate e azioni con esito incerto.

Utilizzare timestamp monotoni per le durate misurate sullo stesso host. Separare metriche operative da dati sensibili: niente immagini, audio, password, URL con credenziali o identificativi radio grezzi negli export diagnostici.

## 13. Strategia di test e tracciabilità

### 13.1 Livelli di prova

**Unitari:** parser, migrazione, regole, qualità, dedup, scheduler, contesto e cancellazione con clock finto. Nessun `sleep` lungo per dimostrare una finestra di 120 secondi.

**Contratto:** stesse fixture JSON su hub e agente; accettazione identica di casi positivi/negativi e compatibilità wire. Parsing indipendente non deve significare divergenza silenziosa.

**Integrazione:** broker Mosquitto reale in ambiente isolato, CA temporanea, database temporaneo, simulatori e gateway media quando necessario. I mock di `on_message` non dimostrano TLS, ACL, retained o persistenza broker.

**Hardware:** Zero W originale, camera e cavo reali, audio USB, sensori e BLE. QEMU o un Pi 5 sono utili per altro, non certificano camera, radio o carico ARMv6.

**UI/regressione:** tutte le funzioni esistenti con zero satelliti e con sorgenti multiple; due browser e schermo mobile. Test automatizzati del DOM/API dove possibile e smoke test espliciti per audio browser.

Aggiungere marker `integration` senza includere accidentalmente queste prove nella suite unit ordinaria; mantenere il marker `hardware` già previsto. Un comando di collaudo non deve avviare notifiche o SSH reali per default.

### 13.2 Matrice ACC del piano funzionale

I nomi dei test nella colonna centrale sono proposti; possono essere organizzati in meno file, ma gli scenari devono restare distinti e riconoscibili.

| ID | Scenario/test da implementare | Incremento / prova |
|---|---|---|
| ACC-01 | `test_zero_w_install_without_hub_dependencies` | PR-00/02, hardware |
| ACC-02 | `test_legacy_config_and_api_without_satellites` | PR-01/05/09, regressione completa |
| ACC-03 | `test_sensor_rule_arms_without_camera_or_model` | PR-05/06 |
| ACC-04 | `test_duplicate_event_has_one_trigger_decision` | PR-04/05, anche concorrenza |
| ACC-05 | `test_reconnect_retained_and_history_never_trigger` | PR-03/04, broker reale |
| ACC-06 | `test_connectivity_loss_never_means_absence` | PR-03/06/12 |
| ACC-07 | `test_stalled_remote_decoder_is_killed_without_ui_block` | PR-08, integrazione e hardware |
| ACC-08 | `test_vision_hits_are_per_source_and_unique_frame` | PR-07 |
| ACC-09 | `test_photo_uses_action_context_not_current_preview` | PR-09 |
| ACC-10 | `test_missing_media_source_has_no_silent_fallback` | PR-09/11 |
| ACC-11 | `test_remote_video_never_opens_unassigned_local_mic` | PR-09/11 |
| ACC-12 | `test_simulation_has_no_side_effects_or_live_state_mutation` | PR-05/10 |
| ACC-13 | `test_manual_rule_test_still_executes_real_actions` | PR-05/09, con executor controllato |
| ACC-14 | `test_disarm_cancels_waits_parallel_groups_and_late_results` | PR-05/07/11 |
| ACC-15 | `test_hub_restart_is_disarmed_and_does_not_replay_actions` | PR-04/05/13 |
| ACC-16 | `test_audio_lease_expires_after_hub_loss` | PR-11, hardware/rete |
| ACC-17 | `test_audio_duplicates_gaps_stalls_and_bounded_buffer` | PR-11, integrazione e hardware |
| ACC-18 | `test_presence_absent_requires_healthy_scan_window` | PR-12 |
| ACC-19 | `test_scanner_failure_sets_unknown` | PR-12, anche BlueZ reale |
| ACC-20 | `test_node_cannot_impersonate_other_node` | PR-03/04, TLS/ACL reali |
| ACC-21 | `test_revoke_closes_existing_media_and_rejects_messages` | PR-03/08/11/13 |
| ACC-22 | `test_payload_limits_url_validation_and_log_redaction` | PR-04/08/10 |
| ACC-23 | `test_noisy_node_cannot_starve_other_sources` | PR-04/07/13 |
| ACC-24 | `test_global_and_isolated_fault_policies` | PR-05/10 |
| ACC-25 | `test_migrate_restore_preserves_rules_sources_and_secrets` | PR-05/13 |
| ACC-26 | `test_qualified_profile_soak_and_repeated_faults` | PR-13, prova 72 ore |

### 13.3 Scenari aggiuntivi obbligatori

Testare crash tra commit e ACK, crash tra decisione ed effetto esterno, database pieno, clock avanti/indietro, grant vecchio inviato durante una connessione nuova, clone con identità duplicata, retained malevolo su `events`, revoca mentre la publication è già autenticata, certificato scaduto durante il funzionamento, decoder che non emette frame pur mantenendo il socket aperto, native inference bloccata, errore del gateway, doppia chiusura della stessa lease, config aggiornata in due browser e tentativo V1 di sovrascrivere regole V2.

Il clone di una chiave valida non è distinguibile crittograficamente dal suo originale senza ulteriori misure hardware: il progetto previene la clonazione nelle immagini, limita l'identità operativa concorrente e segnala churn/incoerenze, ma non promette un'identificazione fisica impossibile con la stessa credenziale copiata.

### 13.4 Regressione delle funzioni attuali

Verificare preview e snapshot primari; rilevamento e ROI; condizioni di armamento; stop preview mentre Sentry rimane attivo; test mode e test manuale; wait e gruppi paralleli; foto multiple; registrazioni video/audio; archivio, range e cancellazione; TTS, Kokoro/eSpeak e fallback; soundboard e file audio; effetti vocali; comandi SSH salvati; Telegram testuale; PTT telefono; ascolto microfono locale; configurazione hardware; CLI; runtime idle e shutdown; redazione dei segreti.

I test non devono fare chiamate reali a Telegram o eseguire SSH reali in CI. Il collaudo manuale reale va autorizzato separatamente e con destinazioni sicure.

## 14. Installazione, gestione e rollout

### 14.1 Servizi sul Pi 5

Sentry Mode rimane il processo principale. Mosquitto e MediaMTX sono servizi separati quando richiesti; il listener WSS è attivato dal sottosistema satellite. Le dipendenze di sistema non devono rendere l'app locale impossibile da avviare quando i satelliti sono disabilitati.

Le porte di controllo MediaMTX e broker amministrativo sono confinati a loopback/rete amministrativa. Il firewall autorizza solo i percorsi necessari dai satelliti: MQTT TLS, audio macchina e publication video protetta. Nessun port forwarding pubblico implicito.

Il provisioning distingue file pubblici (CA) da certificati/chiavi privati. Se l'utente installa via IP, il certificato contiene un SAN appropriato. Rinnovi sono pianificati prima della scadenza e non richiedono di disattivare la verifica TLS.

### 14.2 Servizio satellite

Directory proposte: `/etc/sentry-satellite/` per configurazione/segreti, `/var/lib/sentry-satellite/` per stato minimo autorizzato, `/run/sentry-satellite/` per file effimeri. Permessi privati sulle chiavi e sulle configurazioni sensibili.

Unità systemd con utente non root, `Restart=on-failure`, limite di riavvii, `KillMode=control-group`, timeout di stop e accessi ai soli device richiesti. Impostare opzioni di sandbox compatibili con camera, GPIO, ALSA e D-Bus: `PrivateDevices=true` indiscriminato può impedire l'accesso alle periferiche che il servizio deve usare, quindi verificare la policy anziché copiare un template generico.

Installare solo il profilo selezionato. Nessun desktop, browser kiosk, modello AI o copia dei segreti Telegram/SSH dell'hub. Configurare log rotation e limiti di journald; niente file PCM o H.264 permanenti salvo un comando di diagnostica esplicito.

L'agente effettua reconnect con backoff anche quando `network-online.target` è già stato raggiunto: quel target non garantisce che il broker o il Wi-Fi resteranno disponibili.

### 14.3 Aggiornamenti e rollback

Adottare directory release immutabili e un puntatore alla release attiva, mantenendo configurazione e dati fuori dal codice. Su ARMv6 usare wheel/artifact prodotti e verificati per il profilo; evitare una compilazione imprevista durante un aggiornamento remoto.

Aggiornare prima l'hub a una versione compatibile con il wire dei satelliti correnti. Aggiornare un satellite campione e verificarlo prima di tutti gli altri. Un protocollo incompatibile viene rifiutato esplicitamente. Definire nel manifest la matrice di compatibilità; non promettere «N−1» finché non esistono due versioni effettivamente testate.

Prima dell'update: validare, disarmare le regole interessate, chiudere sessioni, salvare stato/config coerenti. Dopo: avvio, doctor e smoke test; in errore ripristinare la release precedente e la relativa configurazione compatibile. Non distribuire aggiornamenti come comandi shell ricevuti dal broker.

### 14.4 Workflow Git consigliato

Sviluppare su branch e worktree separati dal checkout che eroga il servizio. Una PR per incremento; niente `git reset --hard`, force push o modifiche dirette al main operativo come parte automatica di questo piano.

Per ogni PR allegare modifiche, test eseguiti, test non eseguibili senza hardware e criteri del gate. Integrare solo dopo verifica; il deploy usa un commit/release esplicito, non un `git pull` non tracciato mentre il servizio scrive configurazioni.

## 15. Rischi e criteri di arresto

| Rischio | Risposta implementativa | Condizione che blocca il rilascio |
|---|---|---|
| Pacchetto o encoder incompatibile ARMv6 | G0 e distinto profilo OS/hardware | Compatibilità dimostrata solo su Pi 5/Zero 2 |
| Pi 5 saturo con più stream | Scheduler unico, quote, misure decode e recording | Latenza in crescita o regole che non ricevono campioni sufficienti |
| Lettura nativa bloccata | Processo cancellabile e watchdog | Dashboard/disarmo attendono la rete indefinitamente |
| Evento vecchio trattato come live | Epoche, grant, dedup, baseline e controllo temporale | Allarme dopo recupero di un backlog |
| Fonte foto/audio sbagliata | `ActionContext` e riferimenti espliciti | Fallback silenzioso alla sorgente primaria/preview |
| Sicurezza applicata solo a MQTT | Protezione anche dashboard/media/controllo gateway | Satellite autorizzato a invocare azioni amministrative |
| Revoca solo sulle nuove connessioni | Invalidazione e chiusura sessioni in corso | Audio/video continua dopo revoca oltre il limite dichiarato |
| Migrazione distruttiva | Backup, dry-run, V2 e blocco dei write V1 incompatibili | Perdita di regole, segreti o riferimenti |
| Scansione BLE compromette il video | Profili separati e prove di coesistenza | Profilo custom proposto come supportato senza misure |
| SD/log/journal illimitati | Quote, retention e manutenzione | Crescita continua non spiegata in soak test |

Il sistema resta dipendente da Pi 5, alimentazione e rete. Non è un sistema di sicurezza certificato e non garantisce azioni durante un guasto dell'hub. Il piano non introduce una logica autonoma di allarme sullo Zero per nascondere questa dipendenza.

## 16. Checklist di completamento

- [ ] Commit baseline e risultato regressione documentati.
- [ ] Due distribuzioni indipendenti, installazione satellite senza dipendenze AI.
- [ ] Contratti wire, fixture e compatibilità versioni verificati.
- [ ] Credenziali per nodo, approvazione e revoca su sessioni attive.
- [ ] Event ingress con dedup persistente, epoche e no replay automatico.
- [ ] Regole sensore senza camera/modello.
- [ ] Camera primaria e remote concorrenti con un solo detector.
- [ ] Timeout reali, cancellazione e reap dei processi bloccati.
- [ ] Foto/video/audio della sorgente esplicita, senza fallback nascosti.
- [ ] Correlazione PIR/persona con semantica temporale dichiarata.
- [ ] Audio remoto con grant, mute, lease e buffer limitati.
- [ ] BLE con present/absent/unknown e allowlist; Wi-Fi solo se adapter qualificato.
- [ ] UI multi-sorgente e API legacy compatibili nei casi rappresentabili.
- [ ] Migrazione e rollback realmente provati.
- [ ] Tutti i 26 ACC coperti e regressione delle funzioni precedenti.
- [ ] Soak test e fault injection sul profilo hardware autorizzato.
- [ ] Documentazione e manifest release senza segreti, metriche reali allegate.

**Ordine operativo finale:** fondamenta e sensori → multi-camera ed evidenze → correlazione PIR/persona → audio → presenza → consolidamento. Il lavoro centrale è rendere sorgenti, tempi e dipendenze espliciti; MQTT è il trasporto, non la soluzione completa.

## 17. Riferimenti verificati

### Repository — commit `4febced020fba69df70d8ad08df7ae4d2c3c0917`

- [R1 — Configurazione hub](https://github.com/giacintop89/sentry-mode/blob/4febced020fba69df70d8ad08df7ae4d2c3c0917/src/sentry_mode/config.py)
- [R2 — Adapter camera](https://github.com/giacintop89/sentry-mode/blob/4febced020fba69df70d8ad08df7ae4d2c3c0917/src/sentry_mode/hardware/camera.py)
- [R3 — VideoStream](https://github.com/giacintop89/sentry-mode/blob/4febced020fba69df70d8ad08df7ae4d2c3c0917/src/sentry_mode/vision/stream.py)
- [R4 — Detector e worker](https://github.com/giacintop89/sentry-mode/blob/4febced020fba69df70d8ad08df7ae4d2c3c0917/src/sentry_mode/vision/detection.py)
- [R5 — Configurazione regole](https://github.com/giacintop89/sentry-mode/blob/4febced020fba69df70d8ad08df7ae4d2c3c0917/src/sentry_mode/sentry/config.py)
- [R6 — Motore Sentry e azioni](https://github.com/giacintop89/sentry-mode/blob/4febced020fba69df70d8ad08df7ae4d2c3c0917/src/sentry_mode/sentry/engine.py)
- [R7 — NodeControls e HTTP](https://github.com/giacintop89/sentry-mode/blob/4febced020fba69df70d8ad08df7ae4d2c3c0917/src/sentry_mode/web.py)
- [R8 — Acquisizioni](https://github.com/giacintop89/sentry-mode/blob/4febced020fba69df70d8ad08df7ae4d2c3c0917/src/sentry_mode/vision/recording.py)
- [R9 — Packaging](https://github.com/giacintop89/sentry-mode/blob/4febced020fba69df70d8ad08df7ae4d2c3c0917/pyproject.toml)
- [R10 — API attuali](https://github.com/giacintop89/sentry-mode/blob/4febced020fba69df70d8ad08df7ae4d2c3c0917/docs/http-api.md)
- [R11 — Test unitari esistenti](https://github.com/giacintop89/sentry-mode/tree/4febced020fba69df70d8ad08df7ae4d2c3c0917/tests/unit)

### Documentazione primaria esterna — consultata il 16 settembre 2026

- [E1 — Paho: client, callback, code e acknowledgement](https://eclipse.dev/paho/files/paho.mqtt.python/html/client.html)
- [E2 — Mosquitto: TLS, ACL, listener e persistenza](https://mosquitto.org/man/mosquitto-conf-5.html)
- [E3 — WebSocket client sincrono: TLS e limiti](https://websockets.readthedocs.io/en/stable/reference/sync/client.html)
- [E4 — Raspberry Pi: stack camera e streaming](https://www.raspberrypi.com/documentation/computers/camera_software.html)
- [E5 — FFmpeg: protocolli, RTSP e timeout](https://ffmpeg.org/ffmpeg-protocols.html)
- [E6 — MediaMTX: pubblicazione dei flussi](https://mediamtx.org/docs/features/publish)
- [E7 — stunnel: trasporto TLS](https://www.stunnel.org/howto.html)
- [E8 — Python SQLite: connessioni, transazioni e backup](https://docs.python.org/3/library/sqlite3.html)
- [E9 — BlueZ: discovery e filtri dell'adapter](https://bluez.readthedocs.io/en/latest/adapter-api/)

Le fonti documentano le API e i vincoli generali. Architettura, fasi, schemi, quote e criteri di rilascio sono proposte di questo piano. Le fonti non dimostrano la compatibilità di una combinazione hardware non ancora collaudata né le prestazioni del sistema risultante.
