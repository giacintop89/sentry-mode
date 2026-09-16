# Sentry Mode — Satelliti Raspberry Pi Zero W
## Piano funzionale: telecamere, sensori, microfoni e presenza

**Data:** 16 settembre 2026  
**Stato:** proposta funzionale, non implementata  
**Repository analizzata:** `giacintop89/sentry-mode`  
**Riferimento verificato:** `4febced020fba69df70d8ad08df7ae4d2c3c0917`  
**Hardware destinatario:** Raspberry Pi Zero W originale, non Zero 2 W  
**Hub:** Raspberry Pi 5 con l'installazione esistente di Sentry Mode

Questo documento definisce comportamenti, confini, requisiti e criteri di accettazione. I percorsi nuovi, gli endpoint, gli schemi e i parametri indicati come proposti non sono supportati dalla versione attuale. La verifica riguarda codice e documentazione: non sono stati eseguiti benchmark sui dispositivi né modifiche alla repository.

## 1. Decisione consigliata

Estendere Sentry Mode con un **hub centrale sul Pi 5** e un **agente satellite leggero e distinto sullo Zero W**. Il satellite acquisisce e trasmette; l'hub interpreta, correla, applica le regole ed esegue le azioni.

Un nodo dichiara più capacità, ma viene configurato con un profilo operativo sostenibile. Non si sviluppano quattro applicazioni differenti e non si installa l'applicazione completa del Pi 5 su ogni Zero.

Il primo risultato utile deve essere **telecamera remota + PIR + regola sul Pi 5**, senza perdere la StreamCam locale. L'ordine di sviluppo parte tuttavia da identità, stato dei nodi e ingresso eventi: sono le fondamenta necessarie anche a telecamere e microfoni.

Il perimetro funzionale completo comprende tutti i quattro ruoli. La prima milestone utilizzabile non deve attendere microfoni, BLE o integrazione con il router.

| Ruolo | Valore per Sentry | Priorità proposta | Responsabilità dello Zero |
|---|---|---|---|
| Telecamera satellite | Estendere la copertura visiva e acquisire evidenze | Alta | Acquisizione, codifica compatibile, trasmissione |
| Sensori GPIO/ambientali | Eventi semplici, soglie, attivazione e conferma della visione | Alta; prima integrazione del protocollo | Lettura, antirimbalzo, filtri elementari, stato del sensore |
| Microfono remoto | Ascolto esplicito, registrazioni selettive, eventi acustici semplici | Successiva | Acquisizione PCM, eventuale livello RMS, trasmissione |
| Presenza BLE/Wi-Fi | Contesto aggiuntivo per le regole | Ultima; non requisito per la prima milestone | Osservazioni BLE selezionate; contributo Wi-Fi solo dove realmente utile |

**Principio guida:** un PIR può indicare movimento, la camera può confermare una persona, un beacon può aggiungere contesto. Nessuno dei tre segnali va trasformato in una certezza che il sensore non fornisce.

## 2. Cosa esiste oggi e cosa manca

I riferimenti `[R1]`–`[R8]` rimandano ai file del commit analizzato, elencati alla fine.

| Area verificata | Stato attuale | Conseguenza per il piano |
|---|---|---|
| `config.py` e `hardware/camera.py` | Una camera configurata; `device` accetta intero o stringa, passata a `cv2.VideoCapture` | Un URL può essere provato con un backend compatibile, ma sostituisce la sorgente corrente: non aggiunge una flotta [R1, R2] |
| `web.py`, `NodeControls` | Un `VideoStream`, un monitor audio e un `Sentry` associato al video | Servono identificazione e gestione delle sorgenti, non solo nuovi indirizzi [R3] |
| `sentry/config.py` | Ogni regola richiede una categoria visuale `object` | Sensori, audio e presenza devono avere trigger tipizzati, non finte categorie visuali [R4] |
| `sentry/engine.py` | L'armamento avvia sempre la componente video/detection | L'armamento deve dipendere dalle risorse richieste dalle regole; devono funzionare regole solo sensori [R5] |
| Acquisizioni e azioni | Foto/video/audio non hanno una selezione distribuita della sorgente | Il contesto di origine deve accompagnare regole, azioni ed evidenze [R3, R4, R5] |
| API | Sono documentati controllo, monitor audio e PTT, ma non un ingresso eventi esterni; non esiste un login applicativo | Serve un'interfaccia macchina autenticata; l'header di controllo esistente non è un'identità del satellite [R6] |
| Packaging | Il pacchetto principale dipende da OpenCV e Pydantic 2 | L'agente Zero deve avere una distribuzione e dipendenze separate [R7] |
| Esecuzione | Sono presenti sequenziatore, coda limitata, cancellazione e modalità di test | Riutilizzare queste funzioni, estendendo il contesto delle azioni [R5, R8] |

La detection corrente è basata su **OpenCV DNN e YOLOX Nano ONNX**. Il piano non richiede di passare a Ultralytics o di riscrivere il motore di inferenza [R8].

### 2.1 Due correzioni alla proposta iniziale

**Una stringa accettata dal validatore non garantisce uno stream operativo.** Protocollo, codec e backend devono essere compatibili. Impostare larghezza o FPS sul ricevitore non deve essere considerato un comando affidabile al codificatore remoto; la configurazione di acquisizione appartiene al satellite. Le proprietà OpenCV dipendono dal backend [R2, E4].

**Codec e protocollo non sono equivalenti.** `rpicam-vid` può produrre un flusso codificato e trasmetterlo, ma `--listen` non crea automaticamente un server HTTP MJPEG. Il profilo scelto deve specificare anche incapsulamento e trasporto. La documentazione ufficiale distingue lo stack corrente da quello legacy, ormai non supportato [E3].

## 3. Confini dell'hardware e profili operativi

Lo Zero W originale dispone di CPU single-core a 1 GHz, 512 MB di RAM, Wi-Fi, BLE, una porta USB dati OTG e una connessione CSI con cavo specifico. Il processore appartiene alla famiglia BCM2835/ARM1176; non va confuso con quello dello Zero 2 W [E1, E2].

Adottare **Raspberry Pi OS Lite a 32 bit**, selezionando e bloccando un'immagine effettivamente provata sullo Zero originale. La pagina ufficiale distingue la compatibilità delle immagini a 32 e 64 bit [E5].

Per scelta progettuale, nessun modello neurale, riconoscimento vocale o detector di persone viene eseguito sullo Zero. Sono ammessi filtri elementari, antirimbalzo, isteresi e misura del livello audio. Non è una dichiarazione che ogni forma di inferenza o OpenCV sia impossibile: è un confine di prodotto per evitare una dipendenza da prestazioni e pacchetti non verificati.

| Profilo proposto | Capacità principali | Politica iniziale |
|---|---|---|
| `camera-sensor` | Una camera CSI e sensori a bassa frequenza | Profilo prioritario; microfono e BLE continuo non attivati di default |
| `sensor-presence` | GPIO/ambiente e osservazioni BLE | Profilo per nodi senza video |
| `audio-sensor` | Un microfono USB e sensori leggeri | Sessioni audio selettive; niente video USB concorrente di default |
| `custom` | Combinazione scelta dall'operatore | Richiede validazione del carico e delle periferiche effettive |

Un microfono USB da solo richiede il collegamento OTG, non necessariamente un hub. Camera USB e microfono USB contemporanei richiedono invece di risolvere la condivisione della porta e l'alimentazione. La scelta CSI lascia la porta dati libera [E1].

Alimentazione continua e storage affidabile fanno parte del profilo di installazione. Autonomia a batteria, sleep profondo e alimentazioni marginali non sono obiettivi di questa versione.

## 4. Architettura funzionale

```text
SATELLITI ZERO W                              HUB RASPBERRY PI 5

Camera ── video codificato ────────────────> gestione sorgenti video
                                           │ decodifica / frame recenti
                                           └ detection visuale centralizzata

Microfono ── sessione audio ──────────────> gestione sorgenti audio
                                           │ ascolto / acquisizioni
                                           └ eventi acustici abilitati

GPIO / ambiente / BLE ── MQTT ───────────> identità e validazione
Diagnostica / stato ──── MQTT ───────────> stato aggiornato e registro eventi
                                           │
                                           v
                                      regole e correlazioni
                                           │
                                           v
                                      sequenziatore esistente
                                      foto / video / audio
                                      TTS / suoni / SSH / Telegram

Agente <── comandi limitati e scadenti ──── gestione satelliti e sessioni
```

L'hub ospita il broker MQTT e, se il profilo video scelto lo richiede, un gateway multimediale. MediaMTX sul Pi 5 è un candidato per la distribuzione dei flussi; non viene presupposta la disponibilità di un suo binario adatto allo Zero originale [E6].

L'interfaccia utente resta centralizzata. Nessuna dashboard, motore di regole, credenziale Telegram o chiave per le azioni SSH viene replicata sui satelliti.

### 4.1 Separare le classi di traffico

| Dati | Trasporto funzionale proposto | Politica |
|---|---|---|
| Eventi, letture, stato e comandi | MQTT | Messaggi piccoli e validati; priorità a eventi e salute |
| Video | H.264 con pipeline di rete verificata; presentazione RTSP preferita | Nessun frame o video in MQTT |
| Video di compatibilità | HTTP MJPEG reale | Profilo alternativo dichiarato, non equivalente a un flusso JPEG grezzo |
| Audio | Connessione persistente autenticata e cifrata, con blocchi PCM ordinati | Nessun audio in MQTT |
| Diagnostica | Messaggi health MQTT | Nessun exporter o sistema di metriche aggiuntivo obbligatorio |
| Ingresso sporadico alternativo | HTTP autenticato, opzionale | Stesso schema e stesso ingresso logico degli eventi MQTT |

L'HTTP può avere retry e deduplicazione; non viene escluso per un limite intrinseco di affidabilità. La scelta MQTT evita però di costruire due meccanismi separati per stato, disconnessione e distribuzione dei comandi. La prima implementazione utilizza un solo percorso eventi canonico.

## 5. Modello comune: nodo, sorgente e zona

**Nodo:** dispositivo fisico con identità stabile, credenziali, versione e capacità dichiarate.  
**Sorgente:** singola origine dati del nodo: camera, PIR, microfono, temperatura o osservatore BLE.  
**Zona:** area logica definita sull'hub e assegnata dall'operatore.  
**Osservazione:** misura o risultato visuale/acustico.  
**Evento:** cambiamento o condizione significativa normalizzata.  
**Evidenza:** foto, registrazione o riferimento ai dati usati per una regola.

Esempio di identificazione:

```text
node_id: zero-ingresso
source_id: zero-ingresso.camera-1
source_id: zero-ingresso.pir-1
zone_id: ingresso
```

Gli identificativi sono immutabili e distinti dai nomi modificabili mostrati nell'interfaccia. La zona utilizzata dalle regole deriva dal registro centrale: un payload del satellite non può autoassegnarsi a una zona privilegiata.

Ogni frame, risultato di inferenza, evento, azione e acquisizione deve conservare la sorgente. L'anteprima selezionata dall'operatore non cambia implicitamente la sorgente di una regola.

### 5.1 Ciclo di vita

L'approvazione del dispositivo è distinta dalla sua connettività e dalla salute delle periferiche.

| Dimensione | Stati proposti |
|---|---|
| Autorizzazione | In attesa, approvato, revocato |
| Collegamento | Online, dati non aggiornati, offline, stato sconosciuto |
| Capacità | Pronta, disabilitata, degradata, guasta |
| Compatibilità | Compatibile, aggiornamento richiesto, protocollo non supportato |

Un nodo può essere online con la camera guasta. Questa condizione non deve diventare un generico indicatore verde.

Registrazione manuale e indirizzo dell'hub configurabile sono sufficienti per la prima versione. Eventuale discovery automatico propone dispositivi, ma non li autorizza. Il funzionamento non deve dipendere da mDNS, DNS pubblico o servizi cloud.

## 6. Ruolo 1 — Telecamera satellite

### 6.1 Funzioni richieste

**CAM-01 — Registrazione.** Associare una camera al nodo e alla zona, con nome, formato, orientamento e profilo di acquisizione. Distinguere parametri richiesti e parametri effettivamente rilevati.

**CAM-02 — Uso concorrente.** La camera locale del Pi 5 e quelle remote devono poter coesistere. La visualizzazione di un'altra camera non deve disarmare o riconfigurare quella già usata dalle regole.

**CAM-03 — Anteprima.** Selezionare una sorgente, vedere ultimo aggiornamento, stato, FPS effettivi e ritardo disponibile. Un'ultima immagine congelata deve essere etichettata come non aggiornata, mai mostrata come live.

**CAM-04 — Detection.** Eseguire sul Pi 5 l'inferenza dei frame recenti della sorgente. Una coda per camera non deve crescere per recuperare tutto il passato: si privilegia il frame più recente.

**CAM-05 — Evidenze.** Salvare foto e video della sorgente scelta dalla regola. Il media deve contenere o accompagnare data, sorgente, zona e riferimento dell'evento, senza esporre credenziali di connessione.

**CAM-06 — Guasti.** Applicare timeout, riconnessione con backoff e limiti ai tentativi. Un'apertura o lettura bloccata non deve immobilizzare la dashboard, il disarmo o tutte le altre sorgenti.

### 6.2 Profilo video iniziale

Profilo di partenza proposto: **640 × 480 a 10 FPS**, con detection pianificata separatamente sul Pi 5. Non è un massimo hardware né una garanzia di stabilità radio.

Preferire H.264 usando il percorso di codifica hardware disponibile e verificato sulla combinazione camera/OS. Il satellite non deve decodificare e ricodificare il proprio video per applicare overlay o detection. Overlay, conversioni per il browser e registrazioni appartengono al Pi 5.

La pipeline deve essere validata end-to-end. Sono ammissibili un publisher leggero verso il gateway o l'acquisizione del flusso codificato da parte dell'hub. Se si utilizza un tratto TCP grezzo, questo deve essere dichiarato come tale e incapsulato nell'adapter: non diventa un URL RTSP o HTTP semplicemente cambiandone il prefisso.

MJPEG è un'alternativa per prototipazione o compatibilità. Per stimarne il traffico occorre misurare la dimensione media dei JPEG: ad esempio, **30 kB × 10 FPS × 8 = 2,4 Mbit/s**, prima dell'overhead. È un esempio aritmetico, non una misura dello Zero.

### 6.3 Detection condivisa, non moltiplicata senza controllo

L'hub mantiene un budget globale di inferenza e assegna opportunità alle sorgenti attive. L'aggiunta di una camera non deve moltiplicare automaticamente i modelli residenti o accodare richieste illimitate.

Ogni camera ha uno slot per il frame recente, un contatore di sequenza e una misura della freschezza. Il pianificatore deve evitare che una camera ad alta frequenza impedisca l'analisi delle altre. L'interfaccia distingue frequenza video, frequenza di inferenza effettiva e tempo di conferma della regola.

Tre rilevazioni consecutive a 2 inferenze/s richiedono già circa un secondo fra la prima e la terza osservazione, oltre ad acquisizione, rete e calcolo. La configurazione deve rendere visibile questo compromesso, non promettere reazioni istantanee.

### 6.4 Integrazione con PIR

Modalità proposte: visuale continua a frequenza limitata; priorità visuale aumentata dopo un PIR; correlazione PIR seguito da persona nella stessa zona.

L'attivazione completamente a freddo della camera può perdere l'inizio dell'evento. Per una zona importante si preferisce una pipeline già attiva a carico ridotto. Il pre-roll è un'estensione possibile solo quando esisteva già una cattura con buffer; non si può ricostruire ciò che la camera non ha acquisito.

## 7. Ruolo 2 — Sensori GPIO e ambientali

### 7.1 Famiglie funzionali

| Famiglia | Esempi di ingressi | Dato da esporre |
|---|---|---|
| Digitale | PIR, contatto porta, pulsante | Stato booleano, transizione e qualità |
| Ambientale | BME280, DS18B20 | Valore, unità, istante di lettura, validità |
| Luminosità | Sensore digitale o LDR con conversione adatta | Misura relativa o calibrata, esplicitamente distinta |

I GPIO devono essere interfacciati a livelli compatibili: la disponibilità di una linea di alimentazione a 5 V non rende un ingresso GPIO tollerante a 5 V. Una lettura analogica quantitativa richiede un convertitore appropriato, per esempio un ADC esterno: non si collega una LDR aspettandosi un valore analogico dal solo GPIO [E9, E10]. I dettagli elettrici vanno verificati sul modulo effettivamente usato.

### 7.2 Requisiti

**SNS-01 — Configurazione.** Ogni sensore ha tipo, pin/bus/indirizzo, unità, periodo di campionamento, polarità e stato abilitato. La configurazione deve impedire conflitti di pin o periferica.

**SNS-02 — Elaborazione locale minima.** Antirimbalzo e segnalazione dei fronti sul satellite; isteresi e permanenza delle condizioni nel modello di regola sull'hub. Eventuali filtri locali sono documentati e non duplicati in modo ambiguo.

**SNS-03 — Valore assente.** Un errore di lettura, un CRC errato o un sensore scollegato producono stato non valido, mai uno zero inventato o un falso “nessun movimento”.

**SNS-04 — Avvio.** Gestire il periodo di assestamento previsto dal sensore, acquisire uno stato iniziale e distinguerlo da una transizione operativa. Il riavvio non deve simulare l'apertura di una porta.

**SNS-05 — Frequenza e rumore.** Pubblicare misure con frequenza limitata o variazione significativa; mantenere eventi digitali pronti senza saturare la rete con valori ripetuti.

**SNS-06 — Autonomia funzionale.** Una regola solo PIR o temperatura deve potersi armare anche se camera e detection sono disabilitate. La mancanza del Pi 5 impedisce le azioni centralizzate: l'agente non inventa un motore di allarme autonomo.

Esempi proposti: PIR → registrazione sulla camera assegnata; contatto aperto per una durata minima → notifica; temperatura oltre soglia con isteresi → evento ambientale; luminosità bassa → contesto per una regola, non cambio automatico incontrollato dell'esposizione.

## 8. Ruolo 3 — Microfono remoto

### 8.1 Obiettivo della prima versione

Ascoltare esplicitamente una sorgente remota, acquisire clip selettive e rendere disponibile un eventuale evento di livello audio. Trascrizione continua, riconoscimento vocale e classificazione di rumori specifici non fanno parte di questa prima capacità.

Formato iniziale proposto: **PCM signed 16-bit little-endian, mono, 16 kHz**. Il traffico utile è **16.000 × 16 = 256.000 bit/s**, ossia 32 kB/s; un blocco di 100 ms contiene 3.200 byte, prima delle intestazioni.

### 8.2 Requisiti

**AUD-01 — Sorgente.** Introdurre una `AudioSource` locale o remota, con campionamento, canali, qualità e origine espliciti.

**AUD-02 — Sessione.** Ogni trasmissione ha identificativo, autorizzazione, scadenza, sequenza dei blocchi e contatore dei campioni. Blocchi fuori sessione, duplicati o troppo vecchi non devono essere riprodotti.

**AUD-03 — Trasporto.** Utilizzare un canale persistente autenticato e cifrato. Un WebSocket binario su TLS è il candidato della versione iniziale; costituisce un nuovo adapter e la relativa dipendenza va provata su ARMv6. L'applicazione esistente non viene descritta come se offrisse già questo endpoint.

**AUD-04 — Buffer limitato.** Assorbire un breve jitter senza accumulare secondi di audio. Segnalare i buchi, chiudere sessioni inattive e ripartire dal presente dopo una riconnessione.

**AUD-05 — Comandi espliciti.** Ascolto, registrazione e analisi audio sono interruttori distinti. L'interfaccia mostra la sorgente attiva e permette mute/disabilitazione. Una scadenza di sessione impedisce di lasciare un microfono aperto dopo la perdita dell'hub.

**AUD-06 — Privacy.** Nessuna conservazione o trascrizione di default. Salvare solo clip richieste da un'azione abilitata o dall'operatore; prevedere una politica di cancellazione e un indicatore di acquisizione.

**AUD-07 — Eventi elementari.** Una soglia RMS può produrre un evento di attività acustica con durata minima e isteresi. Non va etichettato come voce, vetro rotto o altro suono riconosciuto senza un classificatore specifico. Una misura in dBFS non è una misura calibrata di pressione sonora.

**AUD-08 — Ritorno acustico.** Evitare che TTS e suoni del Pi 5 generino cicli di allarme nel microfono remoto: associare le sorgenti alla zona e prevedere finestre di inibizione o marcatura delle emissioni note.

### 8.3 Compatibilità con le funzioni esistenti

Il monitor audio corrente invia il microfono locale al browser; il PTT riceve la voce dal telefono e la riproduce sullo speaker. Non sono ingressi equivalenti per un microfono satellite [R6, R8].

La nuova gestione riusa ove possibile lettura, riproduzione e acquisizione, ma non dirotta il PTT per creare un ingresso permanente. Lo speaker Bluetooth del Pi 5 resta l'uscita predefinita.

Un video remoto non deve incorporare automaticamente il microfono locale del Pi 5. La regola sceglie un `audio_source_id`, oppure registra senza audio e lo indica chiaramente.

## 9. Ruolo 4 — Presenza BLE e Wi-Fi

### 9.1 BLE come sorgente primaria di questo ruolo

Usare osservazioni di beacon o dispositivi esplicitamente configurati, con filtri e accesso BlueZ limitato alle funzioni necessarie [E11]. L'identità applicativa deve essere associata al segnale realmente disponibile: un indirizzo radio non va presupposto immutabile per ogni dispositivo.

**PRS-01 — Elenco consentito.** Non creare un inventario indiscriminato dei dispositivi dei passanti. L'operatore seleziona quali identificativi sono pertinenti.

**PRS-02 — Evidenza.** Raccogliere ultimo avvistamento, numero di osservazioni e RSSI filtrato, senza trasformare l'RSSI in metri o in una localizzazione precisa non calibrata.

**PRS-03 — Stato.** Esporre `present`, `absent` e `unknown`. La transizione ad assente richiede un osservatore funzionante e una finestra senza avvistamenti; un nodo scollegato produce invece stato sconosciuto.

**PRS-04 — Stabilizzazione.** Richiedere osservazioni ripetute per l'ingresso e una scadenza più lunga per l'uscita. Esempio da tarare: tre avvistamenti entro dieci secondi per presente; 120 secondi senza avvistamenti, con scansione valida, per assente.

**PRS-05 — Limite semantico.** La presenza di un dispositivo non autentica una persona e non prova che questa sia fisicamente nella zona. Non disarmare automaticamente Sentry in base al solo beacon.

### 9.2 Wi-Fi: non duplicare il lavoro su ogni Zero

La scansione Wi-Fi ordinaria dei punti di accesso non equivale all'elenco delle persone o dei telefoni collegati. Inoltre i sistemi mobili possono usare indirizzi Wi-Fi privati [E12, E13].

Per questo, il contributo Wi-Fi consigliato è un **adapter sull'hub che legge dati autorizzati dell'access point/router della propria rete**, quando esiste un'interfaccia appropriata. Non è obbligatorio trasformare ogni Zero in uno scanner Wi-Fi.

L'adapter distingue associazione attiva, lease DHCP e semplice mancata risposta: non sono osservazioni equivalenti. La lettura di un lease storico non deve produrre “presente adesso”. In assenza di dati aggiornati e affidabili, il risultato è sconosciuto.

L'integrazione Wi-Fi dipende dall'interfaccia del router e non blocca il completamento del profilo BLE. Nessuna cattura indiscriminata del traffico o scansione di reti altrui rientra nel perimetro.

## 10. Contratto degli eventi e affidabilità

### 10.1 Schema logico proposto

```json
{
  "schema_version": 1,
  "event_id": "8b7b7d17-6cbd-4d2e-b1f6-28b116a4b6d3",
  "node_id": "zero-ingresso",
  "source_id": "zero-ingresso.pir-1",
  "boot_id": "ade69d53-4c6d-4496-8d9a-fb71d1bc5f69",
  "sequence": 42,
  "kind": "sensor.motion",
  "occurred_at": "2026-09-16T10:20:30Z",
  "clock_status": "synced",
  "value": true,
  "unit": null,
  "quality": "valid",
  "replayed": false
}
```

Questo è un esempio di protocollo nuovo, non un payload accettato oggi da Sentry Mode. Lo schema effettivo deve vincolare tipo e unità al `kind`; non deve accettare indifferentemente qualsiasi oggetto nel campo `value`.

L'hub aggiunge identità autenticata, zona registrata, istante di ricezione, qualità temporale, stato di elaborazione e riferimenti alle azioni. `event_id` rimane invariato in ogni ritrasmissione. Il contatore `sequence` è progressivo all'interno di `boot_id`.

Il timestamp UTC del satellite e il tempo monotono dell'hub hanno scopi diversi. Non confrontare direttamente tempi monotoni di macchine differenti. Quando il clock non è affidabile, non attribuire una precisione temporale inventata alle correlazioni.

### 10.2 Regole di ingresso

**EVT-01 — Autenticazione.** L'identità derivata dalle credenziali deve corrispondere al nodo del messaggio e alla sorgente registrata.

**EVT-02 — Validazione.** Controllare versione, dimensioni, tipi, unità, intervalli, frequenza e sorgente. Un messaggio non può contenere comandi shell, azioni da eseguire o endpoint arbitrari da contattare.

**EVT-03 — Deduplicazione.** Persistenza sull'hub della deduplicazione per un intervallo definito; una ritrasmissione non crea una nuova transizione di regola. Il QoS MQTT non sostituisce questa logica [E8].

**EVT-04 — Freschezza.** Eventi scaduti o provenienti dal recupero di una disconnessione possono essere registrati come storici, ma non azionano allarmi in tempo reale. L'istante di arrivo recente non rende recente l'evento.

**EVT-05 — Riconnessione.** Stabilire una nuova epoca di connessione, separare la coda storica dalle nuove letture e sincronizzare uno stato iniziale. Il contenuto retained o la prima fotografia dello stato non simulano un fronte appena avvenuto.

**EVT-06 — Timestamp incerto.** Impedire correlazioni strette o azioni basate sull'età quando non è possibile determinarne sufficientemente la freschezza. Il sistema può continuare a mostrare il dato, qualificandolo.

**EVT-07 — Carico.** Coda limitata con contatori degli scarti. Accorpare misure ripetitive; evitare che telemetria ambientale o un nodo rumoroso impediscano di ricevere eventi dagli altri.

**EVT-08 — Effetti esterni.** Non promettere “exactly once” per notifiche o comandi fisici: una richiesta può essere stata eseguita prima della perdita della risposta. Registrare l'esito incerto e non ritentare automaticamente azioni non idempotenti.

### 10.3 Topic MQTT proposti

```text
sentry/v1/nodes/{node_id}/events
sentry/v1/nodes/{node_id}/state
sentry/v1/nodes/{node_id}/health
sentry/v1/nodes/{node_id}/commands
sentry/v1/nodes/{node_id}/acks
```

| Canale | Politica iniziale |
|---|---|
| Eventi | QoS 1, non retained, deduplicazione applicativa |
| Stato sintetico | Retained, sempre con freschezza verificabile; non usato da solo come impulso |
| Health | Periodico, perdita di singoli campioni tollerata |
| Comandi | Non retained, identità hub, scadenza ed epoca del nodo |
| Riscontri | Correlati al comando; distinguere ricevuto, applicato, fallito |

Last Will e heartbeat contribuiscono allo stato del collegamento. Un retained “online” non è una prova sufficiente che il nodo stia funzionando adesso. Autenticazione, ACL e persistenza del broker devono essere configurate esplicitamente [E7, E8].

La persistenza del broker non salva eventi che il satellite non è mai riuscito a trasmettere. La prima versione mantiene sullo Zero una coda RAM limitata; il riavvio o la perdita di alimentazione del nodo possono perderla. Un journal locale persistente è un'estensione deliberata, con limiti di scrittura e recupero, non una garanzia implicita di MQTT.

## 11. Regole e correlazioni

### 11.1 Tipi di trigger

Proporre una discriminante `type` con famiglie distinte: `vision`, `sensor_event`, `threshold`, `audio_event`, `presence_state` e `health_event`. Ogni famiglia ha campi, validazioni e semantica appropriati.

Le correlazioni della prima versione devono essere limitate a condizioni esplicite e finestre temporali finite, non a un linguaggio di programmazione generale. La condizione “movimento e poi persona entro cinque secondi nella stessa zona” è sufficiente come caso iniziale.

Non introdurre una categoria visuale fittizia “GPIO”. Non richiedere `min_confidence` a un contatto digitale né trattare un valore mancante come un `false`.

### 11.2 Esempio di regola proposta

```yaml
schema_version: 2
name: ingresso-confermato
trigger:
  type: sequence
  within_seconds: 5
  steps:
    - type: sensor_event
      source_id: zero-ingresso.pir-1
      kind: sensor.motion
      value: true
    - type: vision
      source_id: zero-ingresso.camera-1
      object: person
      min_confidence: 0.70
      consecutive_detections: 2
cooldown_seconds: 60
actions:
  - type: photo
    source_id: zero-ingresso.camera-1
    count: 1
  - type: telegram
    text: "Movimento confermato da una persona all'ingresso."
```

**Esempio concettuale, non configurazione eseguibile nella versione attuale.** La forma `sequence` rappresenta l'estensione di correlazione limitata; le regole semplici devono essere disponibili prima. L'azione Telegram dell'esempio invia testo: allegare automaticamente la foto sarebbe una funzione ulteriore, non presupposta.

### 11.3 Isolamento e azioni

Lo stato visuale deve essere distinto per regola e sorgente. Due rilevazioni sulla camera A e una sulla B non soddisfano “tre consecutive” su una camera. La rivalutazione dello stesso frame non conta come nuova osservazione.

Un'azione foto/video/audio dichiara la sorgente o eredita un contesto non ambiguo dalla regola. Per trigger non multimediali, il riferimento deve essere esplicito. Se la sorgente è indisponibile, la politica è dichiarata: errore, salto del passo o interruzione della sequenza. Non ripiegare silenziosamente su un'altra stanza.

Le correlazioni vengono invalidate quando una sorgente perde freschezza. La perdita di video non conta come assenza osservata di una persona e non riabilita automaticamente il latch. Gli eventi di salute, come “nodo offline”, sono invece prodotti dall'hub: le relative regole restano valutabili proprio durante il guasto e non richiedono che il nodo guasto invii un messaggio.

## 12. Armamento, guasti e modalità di test

### 12.1 Armamento dipendente dalle risorse

All'armamento, calcolare le risorse richieste dalle sole regole abilitate. Una regola ambientale non deve aprire la camera; una visuale deve verificare la propria camera e il percorso di detection.

Preview, registrazioni e regole sono utilizzatori distinti della stessa sorgente. Chiudere la preview non ferma una cattura ancora necessaria a una regola; il disarmo elimina le azioni automatiche pendenti senza cambiare implicitamente il significato dei controlli manuali esistenti.

### 12.2 Politica di guasto

La modalità legacy conserva il comportamento attuale di fault globale. La nuova modalità distribuita può attivare esplicitamente l'isolamento per sorgente: il guasto di una camera invalida le regole dipendenti e rende il sistema **armato ma degradato**, mentre le regole indipendenti continuano.

Questa scelta deve essere visibile nell'interfaccia e nella configurazione. Nessuna migrazione silenziosa da arresto globale a tolleranza dei guasti. Se tutte le risorse necessarie sono indisponibili, la dashboard non mostra un normale stato “protetto”.

Il riavvio dell'hub resta disarmato, come nella gestione attuale. Il satellite può riavviare il proprio servizio, ma non arma Sentry né recupera azioni pregresse. Sessioni audio/video comandate dall'hub hanno una scadenza rinnovabile: se il controllo scompare, terminano secondo la politica dichiarata.

### 12.3 Test: preservare una distinzione esistente

Nel codice attuale, il test mode trattiene le azioni automatiche, mentre il comando manuale di test della regola ne esegue davvero i passi [R5].

La nuova interfaccia deve distinguere chiaramente:

- **Simula evento:** mostra regole candidate e azioni previste, senza effetti esterni o registrazioni.
- **Esegui azioni manualmente:** mantiene il comportamento reale esistente, con destinazioni visibili e controllo esplicito dell'operatore.

Un messaggio proveniente da un satellite non può attivare il secondo percorso né aggirare disarmo o test mode.

## 13. Interfaccia utente e API proposte

### 13.1 Pagina Satelliti

Mostrare nodi, capacità, zone, ultima comunicazione, versione e problemi. Dal dettaglio: modificare nome e assegnazione, approvare/revocare, testare una capacità, aprire l'anteprima, ascoltare esplicitamente, vedere letture ed eventi.

La pagina hardware esistente continua a gestire le periferiche locali. Non trasformare l'elenco locale `/dev/video*` in un elenco indistinto di URL remoti.

### 13.2 Editor regole e registro eventi

L'editor propone sorgenti compatibili con il trigger, segnala riferimenti mancanti e rende visibili le dipendenze. Il registro mostra origine, qualità, freschezza, risultato della regola e azioni completate/fallite/saltate.

Mantenere il riepilogo eventi corrente per compatibilità. Aggiungere sull'hub un journal persistente e limitato, per esempio SQLite locale, senza obbligare lo Zero a mantenere un database o introdurre un servizio dati esterno.

### 13.3 Contratti API, non endpoint già disponibili

| Endpoint proposto | Funzione | Autorizzazione |
|---|---|---|
| `GET /api/satellites` | Elenco e salute | Operatore |
| `GET /api/satellites/{id}` | Dettaglio e capacità | Operatore |
| `POST /api/satellites/{id}/approve` | Approvazione | Amministrazione |
| `POST /api/satellites/{id}/revoke` | Revoca | Amministrazione |
| `GET /api/sources` | Sorgenti selezionabili | Operatore |
| `POST /api/events/simulate` | Simulazione senza effetti | Operatore |
| `POST /api/satellites/v1/events` | Ingresso HTTP opzionale | Identità macchina per nodo |
| Canale `/api/satellites/v1/audio` | Sessione audio binaria autenticata | Identità macchina + sessione autorizzata |

Il percorso HTTP opzionale condivide normalizzazione, deduplicazione e semantica MQTT. Non consente di eseguire direttamente una regola.

## 14. Sicurezza e riservatezza

**SEC-01 — Identità individuale.** Credenziali differenti per nodo, revoca selettiva e segreti esterni al repository. Una microSD clonata non deve produrre due dispositivi con la stessa identità operativa.

**SEC-02 — Privilegi MQTT.** Il nodo pubblica soltanto i propri eventi/stati/riscontri e legge solo i propri comandi. Non scrive eventi di altri nodi né comandi all'hub. Il broker non accetta accesso anonimo [E7].

**SEC-03 — Cifratura verificata.** Usare TLS sui canali macchina con verifica del server, e proteggere il tratto video attraverso un trasporto o tunnel compatibile e provato. Credenziali RTSP non equivalgono da sole a cifratura. Un profilo senza cifratura, limitato al laboratorio isolato, deve essere esplicitamente marcato e non distribuito come impostazione sicura.

**SEC-04 — Separazione dei controlli.** L'header browser `X-Sentry-Mode-Control` non viene riutilizzato come autenticazione dei satelliti. Proteggere separatamente la dashboard attuale mediante rete amministrativa, VPN o autenticazione a monte; la protezione dell'ingresso eventi non risolve da sola l'assenza di login dell'interfaccia esistente [R6].

**SEC-05 — Nessuna esecuzione arbitraria.** Comandi ammessi: stato, gestione di sessioni, applicazione di configurazioni validate. Niente shell remota generica, percorsi di file arbitrari, installazione di pacchetti o modifica delle regole tramite evento.

**SEC-06 — URL controllati.** Destinazioni media registrate dall'amministratore, protocolli consentiti, controllo di redirect e risoluzione. Un satellite non può usare il Pi 5 per contattare arbitrariamente servizi interni tramite URL nel payload.

**SEC-07 — Minimo privilegio locale.** Servizi non eseguiti interamente come root; accesso limitato a GPIO, bus, audio e Bluetooth realmente necessari. Limiti a log, processi figli, memoria e file temporanei.

**SEC-08 — Dati.** Selezione delle sorgenti registrate, scadenza delle evidenze e pseudonimi per dispositivi di presenza. Niente raccolta audio/video continua implicita, credenziali in URL mostrati o identificativi radio grezzi nei log generali.

## 15. Parametri iniziali e osservabilità

Tutti i numeri di questa sezione sono **impostazioni iniziali o obiettivi di collaudo**, non risultati misurati e non limiti assoluti dell'hardware.

| Parametro | Proposta iniziale | Verifica richiesta |
|---|---|---|
| Video satellite | 640 × 480, 10 FPS | FPS e bitrate effettivi, stabilità, qualità |
| Detection | Richiesta iniziale 1–2 FPS per camera attiva, entro un budget globale | Tempo inferenza, equità e conferma delle regole |
| Audio | PCM16 mono, 16 kHz | Perdita blocchi, jitter, carico e qualità |
| Heartbeat | Ogni 15 s | Rilevamento guasti e traffico |
| Stato non aggiornato / offline | Dopo 45 / 60 s senza prove di vita | Distinguere guasto nodo da guasto broker |
| Coda eventi satellite | Massimo 256 eventi o 1 MiB; scadenza 5 min | Nessuna crescita illimitata; storico non aziona |
| Dimensione evento | Massimo 8 KiB | Validazione e risposta al sovraccarico |
| Journal hub | 7 giorni o 100 MiB, configurabile | Rotazione e spazio disponibile |
| Lease sessione media | 30 s, rinnovabile | Chiusura dopo perdita del controllo |
| Valutazione eventi sull'hub | Obiettivo p95 ≤ 250 ms da ricezione a decisione | Misura locale; esclude acquisizione e rete |
| Recupero sorgente | Obiettivo ≤ 30 s dopo ripristino effettivo di rete e servizio | Test guasti ripetuti, non garanzia in ogni rete |

La freschezza visuale deve essere misurata quando possibile dalla cattura. Un timestamp assegnato solo all'arrivo sul Pi 5 misura l'età dall'ingresso, non tutta la latenza end-to-end: la UI deve dichiarare la metrica disponibile.

Registrare almeno stato periferiche, CPU/RAM, spazio libero, errori, riavvii dei processi, segnale Wi-Fi disponibile, bitrate/FPS reali, frame scartati, età dei frame, durata inferenze, profondità code e scarti per scadenza.

Per il primo collaudo esteso proporre un insieme di quattro Zero registrati, con **camera locale + due stream remoti attivi + un microfono remoto**, distribuiti tra i nodi. È un perimetro di prova, non una dichiarazione che quattro sia il massimo o che quel carico sia già sostenibile. La concorrenza autorizzata sarà fissata dai test effettivi.

Per ridurre variabili nella prima validazione, collegare preferibilmente l'hub via Ethernet e tenere disabilitata la scansione BLE nei profili media finché non è stata collaudata la combinazione.

## 16. Organizzazione del software e compatibilità

### 16.1 Agente separato nella stessa repository

Proposta organizzativa:

```text
sentry-mode/
  src/sentry_mode/                 # applicazione hub esistente
  satellite/
    pyproject.toml                 # distribuzione indipendente
    src/sentry_satellite/          # agente leggero
    config/                        # esempi privi di segreti
    systemd/                       # unità per il satellite
  docs/
    sentry-mode-zero-w-functional-plan.md
```

Questi percorsi sono proposti. L'agente non deve dipendere transitivamente dal pacchetto principale né importare OpenCV, modelli neurali o componenti di sintesi vocale. Utilizzare librerie leggere e pacchetti di sistema effettivamente disponibili; ogni nuova dipendenza binaria richiede prova sullo Zero originale.

I servizi devono avviarsi senza desktop, riconnettersi senza interventi manuali, interrompersi correttamente e non creare file illimitati. Installazione e aggiornamento del satellite non eseguono automaticamente lo script di bootstrap del Pi 5.

### 16.2 Aree dell'hub da estendere

| Area attuale | Estensione funzionale |
|---|---|
| `config.py` | Registro sorgenti, opzioni distribuite e migrazione dello schema |
| `hardware/camera.py` e gestione video | Adapter rete, cancellazione e timeout; gestione di più sorgenti |
| `web.py` | `NodeControls` multi-sorgente, API e pagina satelliti |
| `sentry/config.py` | Trigger tipizzati, riferimenti sorgente, correlazioni limitate |
| `sentry/engine.py` | Armamento per risorsa, stato per sorgente, contesto dei job, guasti configurabili |
| Acquisizioni video e audio | Destinazioni esplicite e metadati di origine |
| Nuovi moduli hub | Registro nodi, ingresso eventi, stato, deduplicazione e journal |
| Test e documentazione | Compatibilità, guasti distribuiti e prove hardware |

Non occorre cambiare framework web o motore di detection solo per introdurre i satelliti. Le responsabilità nuove vanno isolate invece di riversare tutto nel controller esistente.

### 16.3 Migrazione senza perdita di funzioni

La vecchia configurazione `camera` diventa una sorgente stabile, per esempio `legacy-primary`, conservandone esattamente il `device`. Non assumere che sia una camera fisicamente locale: il campo potrebbe già contenere un URL.

Le regole precedenti diventano regole visuali su quella sorgente, preservando categorie, ROI, conferme, cooldown, assenza, azioni e sequenze. Conservare le API precedenti come alias della sorgente primaria finché sono necessarie alla compatibilità.

Versionare lo schema, creare un backup prima della conversione e salvare atomicamente. Una configurazione nuova non deve essere riscritta silenziosamente in un formato non leggibile dal precedente rilascio senza avvertimento e percorso di ripristino.

TTS, soundboard, suoni, SSH, Telegram, PTT, monitor locale, catture, configurazione hardware e semantica dei test devono continuare a funzionare con zero satelliti configurati.

## 17. Sequenza di realizzazione

| Fase | Risultato osservabile | Criterio per procedere |
|---|---|---|
| 0 — Compatibilità reale | Un vero Zero W avvia l'agente minimo e trasmette dati; pipeline camera verificata separatamente | OS, periferiche, pacchetti, codec e trasporto documentati; nessuna promessa basata sullo Zero 2 |
| 1 — Fondamenta e sensori | Registro, identità, health, MQTT, PIR e regole solo sensori | Deduplicazione, disarmo, test e stato offline verificati; nessuna dipendenza obbligatoria dalla camera |
| 2 — Telecamere distribuite | StreamCam locale e camera remota coesistono, con foto della sorgente corretta | Isolamento, timeout, scheduler e freschezza collaudati; poi seconda camera remota |
| 3 — Microfono remoto | Ascolto selettivo e clip da una sorgente esplicita | Sessioni, mute, scadenze e assenza di ritorni acustici incontrollati |
| 4 — Presenza | Beacon configurati producono stati stabili; Wi-Fi integrato solo con un adapter reale | Offline distinto da assente; nessun disarmo basato sul solo beacon |
| 5 — Consolidamento | Aggiornamento, ripristino, sicurezza e carico distribuito | Regressione completa, prove guasto e funzionamento prolungato |

Il prototipo URL della fase 0 può essere fatto in una configurazione di laboratorio. Non va presentato come completamento della fase 2 o applicato alla produzione sostituendo la StreamCam senza volerlo.

## 18. Criteri di accettazione

| ID | Prova | Risultato richiesto |
|---|---|---|
| ACC-01 | Installazione su Zero W originale | Avvio senza dipendenze AI dell'hub e senza binari incompatibili |
| ACC-02 | Avvio con vecchia configurazione e nessun satellite | Funzioni locali e controlli esistenti invariati |
| ACC-03 | Armamento di regola PIR con camera disabilitata | Armamento riuscito; nessuna apertura camera |
| ACC-04 | Invio ripetuto dello stesso evento | Una sola transizione logica e nessuna seconda azione dovuta alla ritrasmissione |
| ACC-05 | Ripristino MQTT con retained e coda storica | Nessun allarme causato da vecchi eventi |
| ACC-06 | Nodo perde rete | Stato diventa sconosciuto/non aggiornato/offline, mai falsa assenza |
| ACC-07 | Camera remota blocca la lettura | Dashboard e disarmo restano operativi; recupero limitato e osservabile |
| ACC-08 | Camera A e B alternano detection | I conteggi consecutivi non si sommano tra sorgenti |
| ACC-09 | PIR A richiede foto mentre la preview mostra B | L'evidenza proviene da A |
| ACC-10 | Sorgente foto o audio richiesta non disponibile | Errore o salto dichiarato; nessuna sostituzione silenziosa |
| ACC-11 | Video remoto senza microfono associato | Nessuna acquisizione involontaria del microfono locale |
| ACC-12 | Simulazione evento | Nessun TTS, SSH, Telegram, registrazione o altro effetto reale |
| ACC-13 | Test manuale azioni | Comportamento reale esistente conservato e chiaramente indicato |
| ACC-14 | Disarmo durante attese e sequenze concorrenti | Cancellazione coerente, nessuna coda automatica residua |
| ACC-15 | Riavvio dell'hub | Sentry disarmato; nessun replay delle azioni |
| ACC-16 | Caduta dell'hub durante ascolto | Sessione microfono termina alla scadenza prevista |
| ACC-17 | Ritardo o duplicazione blocchi audio | Buffer limitato, ordine verificato, nessuna riproduzione tardiva incontrollata |
| ACC-18 | Beacon non più visto, scanner funzionante | Assenza solo dopo la finestra configurata |
| ACC-19 | Scanner BLE guasto o offline | Presenza sconosciuta, non assenza confermata |
| ACC-20 | Credenziali di un nodo usate per un altro | Evento e comandi rifiutati |
| ACC-21 | Revoca del nodo | Perdita dell'autorizzazione a eventi, comandi e sessioni media |
| ACC-22 | Payload e URL malformati o troppo grandi | Rifiuto, log sanitizzato, nessuna esecuzione o richiesta arbitraria |
| ACC-23 | Saturazione da una sorgente | Memoria e code limitate; altre sorgenti non completamente escluse |
| ACC-24 | Guasto con modalità legacy e distribuita | Comportamento conforme alla politica selezionata e visibile |
| ACC-25 | Migrazione e ripristino configurazione | Backup leggibile e nessuna perdita di regole o riferimenti |
| ACC-26 | Funzionamento prolungato e guasti ripetuti | Prova proposta di 72 ore, senza crescita continua della memoria o file illimitati; metriche raccolte |

Le prove ACC-01, ACC-07, ACC-16, ACC-17 e ACC-26 richiedono hardware/rete reali. I test sintetici non sostituiscono la validazione di carico e compatibilità ARMv6.

## 19. Fuori perimetro e rischi residui

Non rientrano nella prima versione: inferenza neurale sullo Zero, trascrizione continua, speaker satelliti, replica del sequenziatore sui nodi, videosorveglianza continua con archivio locale su ogni SD, identificazione certa delle persone tramite BLE, localizzazione radio precisa, sincronizzazione audiovisiva professionale tra stanze, ripuntamento geometrico automatico di altre camere e aggiornamenti remoti via comandi shell generici.

La dipendenza dal Pi 5 e dalla rete rimane esplicita. In caso di indisponibilità dell'hub, non viene garantita l'esecuzione di allarmi. Questo progetto non va presentato come un sistema di sicurezza certificato o come una funzione di protezione delle persone.

La principale incertezza tecnica è la combinazione reale di OS, camera, driver, encoder, periferiche USB e rete sullo Zero originale. La principale modifica applicativa non è il trasporto MQTT: è il passaggio da una sola catena camera–regola a **sorgenti esplicite e regole indipendenti dalla presenza della camera**.

## 20. Riferimenti

### Repository — snapshot verificato

- [R1 — Configurazione](https://github.com/giacintop89/sentry-mode/blob/4febced020fba69df70d8ad08df7ae4d2c3c0917/src/sentry_mode/config.py)
- [R2 — Adapter camera](https://github.com/giacintop89/sentry-mode/blob/4febced020fba69df70d8ad08df7ae4d2c3c0917/src/sentry_mode/hardware/camera.py)
- [R3 — Controller web](https://github.com/giacintop89/sentry-mode/blob/4febced020fba69df70d8ad08df7ae4d2c3c0917/src/sentry_mode/web.py)
- [R4 — Schema regole e azioni](https://github.com/giacintop89/sentry-mode/blob/4febced020fba69df70d8ad08df7ae4d2c3c0917/src/sentry_mode/sentry/config.py)
- [R5 — Motore Sentry](https://github.com/giacintop89/sentry-mode/blob/4febced020fba69df70d8ad08df7ae4d2c3c0917/src/sentry_mode/sentry/engine.py)
- [R6 — API HTTP](https://github.com/giacintop89/sentry-mode/blob/4febced020fba69df70d8ad08df7ae4d2c3c0917/docs/http-api.md)
- [R7 — Packaging](https://github.com/giacintop89/sentry-mode/blob/4febced020fba69df70d8ad08df7ae4d2c3c0917/pyproject.toml)
- [R8 — Architettura](https://github.com/giacintop89/sentry-mode/blob/4febced020fba69df70d8ad08df7ae4d2c3c0917/docs/architecture.md)

### Documentazione primaria esterna — consultata il 16 settembre 2026

- [E1 — Raspberry Pi Zero W: caratteristiche e connettori](https://www.raspberrypi.com/products/raspberry-pi-zero-w/)
- [E2 — Processori Raspberry Pi](https://www.raspberrypi.com/documentation/computers/processors.html)
- [E3 — Stack camera, rpicam e streaming](https://www.raspberrypi.com/documentation/computers/camera_software.html)
- [E4 — OpenCV: proprietà e backend VideoCapture](https://docs.opencv.org/4.x/d4/d15/group__videoio__flags__base.html)
- [E5 — Immagini e compatibilità Raspberry Pi OS](https://www.raspberrypi.com/software/operating-systems/)
- [E6 — MediaMTX: integrazione Raspberry Pi Camera](https://mediamtx.org/docs/publish/raspberry-pi-cameras)
- [E7 — Eclipse Mosquitto: configurazione, autenticazione, ACL e persistenza](https://mosquitto.org/man/mosquitto-conf-5.html)
- [E8 — OASIS: specifica MQTT 3.1.1](https://docs.oasis-open.org/mqtt/mqtt/v3.1.1/os/mqtt-v3.1.1-os.html)
- [E9 — Raspberry Pi: GPIO e interfacce hardware](https://www.raspberrypi.com/documentation/computers/raspberry-pi.html)
- [E10 — GPIO Zero: ADC e dispositivi SPI](https://gpiozero.readthedocs.io/en/stable/api_spi.html)
- [E11 — BlueZ: Adapter API e filtri di discovery](https://bluez.readthedocs.io/en/latest/adapter-api/)
- [E12 — NetworkManager: nmcli e scansione degli access point](https://networkmanager.dev/docs/api/latest/nmcli.html)
- [E13 — Apple: indirizzi Wi-Fi privati](https://support.apple.com/en-us/102509)

Le scelte architetturali, le priorità e i valori proposti sono raccomandazioni progettuali di questo documento, non dichiarazioni di compatibilità universale ricavate dalle fonti.
