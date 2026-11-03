# Good-Fences

**Isolamento multi-tenant in un data center spine-leaf con SDN**
(Mininet + Open vSwitch + controller Ryu, OpenFlow 1.3).

Progetto per il corso di NCIS. Il nome viene dal proverbio *"good fences make
good neighbours"*: i tenant condividono la stessa infrastruttura fisica, ma
ognuno resta dentro il proprio recinto.

## Autori

| Nome e cognome | Matricola | GitHub |
| --- | --- | --- |
| Marco Canonicoo | DE9000168 | [MarcoCanonico](https://github.com/MarcoCanonico) |
| Luca Carraturo | DE9000145 | [LucaCarr0](https://github.com/LucaCarr0) |
| Marco Fabi | DE9000173 | [kerubysio](https://github.com/kerubysio) |

## Indice

- [Tematica](#tematica)
- [Cosa risolve](#cosa-risolve)
- [Topologia di riferimento](#topologia-di-riferimento)
- [Come funziona](#come-funziona)
- [Struttura del repository](#struttura-del-repository)
- [Installazione e avvio](#installazione-e-avvio)
- [Verifica rapida](#verifica-rapida)
- [Usare il visualizer](#usare-il-visualizer)
- [Modificare topologia e tenant](#modificare-topologia-e-tenant)
- [Test](#test)
- [Documentazione aggiuntiva](#documentazione-aggiuntiva)

## Tematica

In un data center multi-tenant più clienti (*tenant*) condividono gli stessi
switch e gli stessi collegamenti. Ogni tenant sceglie liberamente il proprio
piano di indirizzamento, quindi due tenant possono usare **la stessa subnet e
perfino gli stessi indirizzi IP**. La rete deve comunque garantire che il
traffico di un tenant non raggiunga mai gli host di un altro, anche quando un
host prova a falsificare MAC, IP o tag VLAN.

Good-Fences simula una fabric **spine-leaf** con Mininet e affida tutte le
decisioni di inoltro a un controller SDN Ryu, che installa una pipeline
OpenFlow 1.3 statica e isolata per tenant, ricavata da due file YAML.

## Cosa risolve

- **Isolamento tra tenant con IP sovrapposti**: il leaf identifica il tenant
  dalla porta di ingresso e applica il suo tag VLAN; le regole di inoltro
  confrontano sempre VLAN, MAC e IP di destinazione. I tenant A e B usano
  entrambi `10.0.0.0/24` senza interferire.
- **Anti-spoofing**: dalle porte host passa solo traffico untagged con porta,
  MAC e IP sorgente coerenti con il binding configurato. Frame già taggati,
  tag annidati, ARP probe e gratuitous ARP vengono scartati.
- **Proxy ARP**: il controller risponde alle richieste ARP al posto degli host,
  solo per destinazioni dello stesso tenant. Nessun broadcast ARP attraversa la
  fabric.
- **Default deny**: switch in `failMode=secure` e regola table-miss DROP su ogni
  tabella; porte libere (*spare*) e ingressi sconosciuti non inoltrano nulla.
- **Bilanciamento ECMP**: ogni leaf distribuisce il traffico remoto su tutti gli
  spine con un gruppo OpenFlow SELECT.
- **Osservabilità**: una GUI web mostra topologia, permette di lanciare i casi
  di test dagli host e confronta i contatori OpenFlow prima e dopo ogni prova.

Limiti noti: il forwarding è statico (nessun apprendimento dinamico), i tenant
si caricano solo all'avvio, il campo `rate_mbps` dei tenant non è ancora
applicato (nessun meter/QoS) e non c'è failover in caso di guasto di un uplink.
IPv6 è disabilitato sugli host.

## Topologia di riferimento

Configurazione di default (`config/topology.yaml` e `config/tenants.yaml`):
2 spine, 3 leaf, 4 porte host per leaf, collegamento completo leaf ↔ spine.

```text
              ┌──────────┐                     ┌──────────┐
              │  spine1  │                     │  spine2  │
              │ DPID 101 │                     │ DPID 102 │
              └──────────┘                     └──────────┘
   porta N dello spine ──► leafN        porta 5 del leaf ──► spine1
                                        porta 6 del leaf ──► spine2

       ┌─────────┐            ┌─────────┐            ┌─────────┐
       │  leaf1  │            │  leaf2  │            │  leaf3  │
       │ DPID 1  │            │ DPID 2  │            │ DPID 3  │
       └─────────┘            └─────────┘            └─────────┘
       porte 1..4             porte 1..4             porte 1..4
     h1  h2  h3  h4         h5  h6  h7  h8        h9  h10 h11 h12
```

| Leaf | Porta 1 · tenant A (VLAN 101) | Porta 2 · tenant B (VLAN 102) | Porta 3 · tenant C (VLAN 103) | Porta 4 |
| --- | --- | --- | --- | --- |
| leaf1 | h1 · 10.0.0.1 | h2 · 10.0.0.1 | h3 · 10.0.1.1 | h4 · spare |
| leaf2 | h5 · 10.0.0.2 | h6 · 10.0.0.2 | h7 · 10.0.1.2 | h8 · spare |
| leaf3 | h9 · 10.0.0.3 | h10 · 10.0.0.13 | h11 · 10.0.1.3 | h12 · spare |

Il MAC di ogni host è `00:00:00:00:00:NN`, con `NN` uguale al numero
dell'host in esadecimale (h10 → `00:00:00:00:00:0a`). Uplink a 60 Mbit/s,
link host a 100 Mbit/s, ritardo 1 ms per link.

## Come funziona

**Leaf, 3 tabelle.**

1. *Tabella 0, classificazione*: scarta frame taggati dalle porte host; manda
   al controller le richieste ARP valide; accetta IPv4 untagged solo con
   porta/MAC/IP del binding e applica il tag VLAN del tenant. Dagli uplink accetta
   IPv4 con una VLAN nota.
2. *Tabella 1, destinazione*: ammette solo destinazioni con VLAN, MAC e IP
   coerenti con un host di quel tenant.
3. *Tabella 2, inoltro*: se l'host è locale toglie la VLAN e consegna; altrimenti
   invia al gruppo ECMP (gruppo `1`, tipo SELECT, un bucket per spine).

**Spine, 1 tabella.** Inoltra verso il leaf di destinazione mantenendo la VLAN,
solo per IPv4 con VLAN/MAC/IP noti e mai verso la porta di ingresso.

**Proxy ARP.** La logica è in `src/controller/arp_proxy.py`: dato il tenant
della porta richiedente, risolve `(tag, IP)` e costruisce la risposta, che
torna solo alla porta di ingresso. Gli ARP non vengono mai inoltrati agli spine.

**Installazione sicura.** Alla connessione di ogni switch il controller cancella
i vecchi flow, ripristina i DROP, crea il gruppo ECMP e attende una Barrier
prima di installare le regole permissive. Una seconda Barrier conferma la
pipeline: solo allora lo switch è **READY**. Errori OpenFlow o timeout
impediscono lo stato READY.

Matrice completa delle regole, priorità e cookie:
[docs/pipeline.md](docs/pipeline.md).

## Struttura del repository

```text
config/
  topology.yaml          # numero di spine/leaf/porte, banda e ritardo dei link
  tenants.yaml           # tenant, tag VLAN, host assegnati con IP
src/
  network_models.py      # carica e valida gli YAML (modello condiviso)
  datacenter.py          # costruisce la rete Mininet (SpineLeafTopo)
  controller/app.py      # app Ryu: connessione switch, Barrier, PacketIn
  controller/pipeline.py # regole OpenFlow e gruppo ECMP
  controller/arp_proxy.py
  visualizer_support.py  # processi e dati per la GUI
  runtime_status.py      # stato condiviso con la GUI (file JSON)
visualizer.py            # GUI Streamlit
requirements.txt         # dipendenze Python
build-constraints.txt    # vincoli per compilare Ryu senza Docker
Dockerfile               # immagine con Python 3.9, Ryu, Mininet, OVS e GUI
docker-compose.yml       # container privilegiato, GUI su 127.0.0.1:8501
scripts/
  start-controller.sh    # avvia Ryu sulla porta 6653
  start-network.sh       # pulisce le run precedenti e avvia Mininet con CLI
  start-visualizer.sh    # avvia la GUI su http://127.0.0.1:8501
  cleanup-network.py     # rimuove nodi, bridge e interfacce residue
  docker-entrypoint.sh   # nel container: avvia OVS e pulisce le run precedenti
tests/network/
  packet_io.py           # genera e cattura pacchetti di test (Scapy)
  flow_visualizer.py     # contatori OpenFlow da terminale
  test_*.py              # test automatici delle utility
docs/                    # documentazione di dettaglio
```

## Installazione e avvio

### Con Docker (consigliato)

È necessario un host Linux con Docker Engine e Docker Compose v2 installati e funzionanti.

```bash
git clone https://github.com/LucaCarr0/Good-Fences.git
```

```bash
cd Good-Fences
```

```bash
docker compose up -d --build
```

Aprire <http://127.0.0.1:8501>, premere **Avvia rete** e attendere
**Simulazione pronta · 5/5 switch READY**. Per fermare tutto: **Arresta rete**,
poi `docker compose down`.

Per usare la CLI Mininet al posto della GUI, in due terminali:

```bash
docker compose exec good-fences bash scripts/start-controller.sh
```

```bash
docker compose exec good-fences bash scripts/start-network.sh
```

Attendere `ALL SWITCHES READY: 5/5` nel primo terminale; chiudere con `exit` e
poi `Ctrl+C`. Gli altri comandi della documentazione che iniziano con `sudo` si
lanciano con `docker compose exec good-fences` al posto di `sudo`.

### Installazione manuale (senza Docker)

Tutti i comandi devono essere eseguiti dalla directory principale del progetto.

Clonare il repository:

```bash
git clone https://github.com/LucaCarr0/Good-Fences.git
cd Good-Fences
```

Creare un ambiente virtuale con Python 3.9:

```bash
python3.9 -m venv .venv
```

Aggiornare `pip`:

```bash
.venv/bin/python -m pip install --upgrade pip
```

Installare le versioni compatibili degli strumenti di compilazione necessari per Ryu 4.34:

```bash
.venv/bin/python -m pip install "setuptools==67.6.1" wheel "pbr==5.11.1"
```

Installare le dipendenze del progetto:

```bash
.venv/bin/python -m pip install --no-build-isolation -r requirements.txt
```

Verificare che non siano presenti conflitti tra le dipendenze:

```bash
.venv/bin/python -m pip check
```

**Nota sulla compatibilità:** Ryu 4.34 utilizza strumenti di compilazione datati. L'installazione di una versione compatibile di `setuptools` e di `pbr`, insieme all'opzione `--no-build-isolation`, permette di evitare le incompatibilità riscontrate durante la generazione dei metadati del pacchetto.

L'installazione delle dipendenze Python non sostituisce quella degli strumenti di sistema necessari per Mininet e Open vSwitch.

GUI, poi come sopra da <http://127.0.0.1:8501>:

```bash
bash scripts/start-visualizer.sh
```

Oppure CLI Mininet, in due terminali:

```bash
bash scripts/start-controller.sh
```

```bash
bash scripts/start-network.sh
```

## Verifica rapida

Dalla CLI Mininet (oppure da **Apri shell** nella GUI), con tutti gli switch
READY:

| Comando | Risultato atteso | Cosa dimostra |
| --- | --- | --- |
| `h1 ping -c 2 h5` | 0% packet loss | tenant A, leaf diversi (via spine) |
| `h2 ping -c 2 h6` | 0% packet loss | tenant B con gli stessi IP del tenant A |
| `h3 ping -c 2 h11` | 0% packet loss | tenant C |
| `h1 ping -c 2 10.0.0.13` | 100% packet loss | `10.0.0.13` esiste solo nel tenant B (h10): isolamento |
| `h1 ping -c 2 h7` | `Network is unreachable` | subnet diversa, nessuna route |

Attenzione: `h1 ping h6` riesce, ma raggiunge **h5**. Mininet traduce `h6` nel
suo IP `10.0.0.2`, che nel tenant A appartiene a h5. Per provare l'isolamento
tra tenant con IP sovrapposti serve indicare il MAC della vittima con
`packet_io.py` (vedi [test manuali](docs/test-manuali.md)).

Per vedere le regole installate su uno switch, da un altro terminale:

```bash
sudo ovs-ofctl -O OpenFlow13 dump-flows leaf1
```

```bash
sudo ovs-ofctl -O OpenFlow13 dump-group-stats leaf1
```

## Usare il visualizer

Il visualizer è una dashboard Streamlit che gestisce l'intera simulazione.
Avviarlo come descritto in [Installazione e avvio](#installazione-e-avvio).

### Barra laterale

- **Avvia rete** / **Arresta rete**: avviano e fermano Ryu e Mininet. L'arresto
  chiude prima Mininet e poi Ryu.
- **Apri shell** (solo senza Docker): apre un xterm con la CLI `mininet>` collegata alla rete attiva
  (per esempio `h2 ping h6`). `exit` chiude solo la shell, non la rete.
- **Riquadro di stato**: numero di switch READY ed eventuali errori.
- **Log Mininet** / **Log controller**: ultimi messaggi dei due processi, utili
  se la rete non diventa pronta.
- **Navigazione**: le tre pagine descritte sotto.

### Pagina Topologia

Grafo di spine, leaf e host colorati per tenant, con VLAN e host spare.
Cliccare un nodo (o sceglierlo dal menu) per vederne i dettagli: per un host IP,
MAC, leaf, porta e tenant; per uno switch attivo le regole OpenFlow e i contatori.
L'espansione **Porte e collegamenti configurati** elenca le porte a entrambe le
estremità di ogni link.

### Pagina Test

1. Scegliere **Host sorgente**, **Host destinazione** e il **Caso di test**.
2. IP e MAC sono precompilati dal modello e si possono modificare per simulare
   spoofing o attacchi tra tenant.
3. Premere **Avvia test**: il caso viene eseguito nel namespace dell'host
   sorgente con `packet_io.py`.

Casi disponibili:

| Caso | Cosa invia | Risultato atteso (stesso tenant) |
| --- | --- | --- |
| `arp-request` | richiesta ARP normale | 1 risposta dal proxy |
| `arp-probe` | ARP con IP sorgente 0.0.0.0 | nessuna risposta |
| `gratuitous-arp` | ARP con sorgente = destinazione | nessuna risposta |
| `rarp` | Reverse ARP | nessuna risposta |
| `ipv4-tagged` | IPv4 con VLAN impostata dall'host | scartato dal leaf |
| `ipv4-untagged` | IPv4 normale (protocollo 253) | consegnato |
| `ecmp-udp` | molti flussi UDP con porte sorgente diverse | distribuiti sui bucket ECMP |

Un invio completato non dimostra la consegna: per IPv4 serve una cattura sul
destinatario, per ECMP i contatori dei bucket.

### Pagina Statistiche

- Vista **Rete**: **Leggi tutti gli switch** riassume regole e contatori di ogni
  switch.
- Vista **Singolo switch**: tabella delle regole con filtri per tabella, tipo di
  regola e testo, grafico dei contatori e bucket del gruppo ECMP.

Flusso tipico per misurare l'effetto di un test:

1. **Statistiche** → **Singolo switch** → scegliere lo switch (es. `leaf1`) →
   **Salva riferimento prima del test**.
2. **Test** → eseguire il caso (es. `h2` → `h6`, `ecmp-udp`).
3. Tornare su **Statistiche** → **Aggiorna statistiche** e leggere i delta.
   Nella tabella delle regole la colonna **Delta pkt** va attivata da
   **Colonne aggiuntive**; la tabella dei bucket ECMP la mostra sempre.

Con `ecmp-udp` entrambi i bucket del gruppo `1` devono aumentare. La divisione
non è necessariamente 50/50: dipende dall'hash di OVS.

Dettagli su ogni vista e sul significato di `valido`/`nuova`/`reset`:
[docs/visualizer.md](docs/visualizer.md).

### Visualizzatore da terminale

Alternativa testuale alla pagina Statistiche, utile con l'avvio da CLI:

```bash
sudo .venv/bin/python tests/network/flow_visualizer.py --switch leaf1
```

Tasti: `b` salva il riferimento, `r` aggiorna e mostra i delta, `a` mostra
tutte le regole o solo quelle con pacchetti, `s` cambia switch, `q` esce.
Le istruzioni complete sono nei [test manuali](docs/test-manuali.md#visualizzatore-dei-contatori-leafspine).

## Modificare topologia e tenant

Rete Mininet e controller leggono **entrambi** i file in `config/` all'avvio.
Procedura:

1. Arrestare la simulazione (**Arresta rete** oppure `exit` + `Ctrl+C`).
2. Modificare `config/topology.yaml` e/o `config/tenants.yaml`.
3. Validare la configurazione (vedi sotto).
4. Riavviare. Il numero di switch attesi diventa `leaves + spines`
   (per esempio `7/7` con 3 spine e 4 leaf).

Con Docker la procedura è identica: i file si modificano sull'host e il
container li legge al successivo avvio della rete, senza ricostruire l'immagine.

### `config/topology.yaml`

| Campo | Significato | Default |
| --- | --- | --- |
| `spines` | numero di switch spine | 2 |
| `leaves` | numero di switch leaf | 3 |
| `hosts_per_leaf` | porte host per leaf (ognuna ha un host) | 4 |
| `uplink_bw_mbps` | banda dei link leaf ↔ spine | 60 |
| `host_bw_mbps` | banda dei link host ↔ leaf | 100 |
| `link_delay_ms` | ritardo di ogni link | 1 |

### `config/tenants.yaml`

```yaml
tenants:
  - name: A              # nome del tenant
    tag: 101             # VLAN del tenant: 1..4094, unica
    rate_mbps: 20        # riservato per la QoS, non ancora applicato
    subnet: 10.0.0.0/24  # documentativo: gli host ricevono sempre una /24
    hosts:
      - {name: h1, leaf: leaf1, port: 1, ip: 10.0.0.1}
```

### Regole di numerazione

La topologia è generata in modo deterministico, quindi nomi e porte vanno scritti
rispettando queste regole:

- **Nome host**: `hN` con `N = (numero_leaf - 1) × hosts_per_leaf + porta`.
  Esempio con 4 porte: leaf2 porta 3 → `h7`.
- **MAC**: `00:00:00:00:00:NN`, con `NN` uguale a `N` in esadecimale.
- **Porte del leaf**: da `1` a `hosts_per_leaf` per gli host, poi un uplink per
  ogni spine (`hosts_per_leaf + 1` verso spine1, e così via).
- **Porte dello spine**: la porta `i` va verso `leaf<i>`.
- **DPID**: `leafN` → `N`, `spineN` → `100 + N`.
- Ogni porta host non assegnata a un tenant diventa automaticamente uno spare
  (host senza IP, tutto il suo traffico viene scartato). La lista `spare_hosts`
  in fondo a `tenants.yaml` è solo un promemoria: il programma non la legge.

### Vincoli verificati all'avvio

Il modello rifiuta la configurazione, con un messaggio esplicito, se:

- un tag VLAN è fuori da `1..4094` o è usato da due tenant;
- un IP manca, non è valido o è ripetuto nello stesso tenant
  (tra tenant diversi gli IP possono coincidere);
- un host compare in più tenant o due host usano la stessa porta;
- il leaf non esiste o la porta è fuori da `1..hosts_per_leaf`;
- il nome dell'host non rispetta la formula di numerazione.

Altri limiti da rispettare: al massimo 255 host in totale (il MAC usa un solo
byte), al massimo 100 leaf (DPID degli spine da 101), host di uno stesso tenant
nella stessa /24 (non c'è routing tra subnet).

**Attenzione**: cambiare `hosts_per_leaf` rinumera tutti gli host, quindi
`tenants.yaml` va riscritto. Gli esempi di questo README e dei documenti in
`docs/` fanno riferimento alla configurazione di default.

### Validare senza avviare la rete

```bash
.venv/bin/python -c "import sys; sys.path.insert(0, 'src'); from network_models import NetworkModel; m = NetworkModel(); print('Configurazione valida:', m.spines, 'spine,', m.leaves, 'leaf,', len(m.get_all_hosts_dict()), 'host')"
```

Anche la GUI valida i file: se sono errati mostra **Configurazione non valida**
con il motivo.

### Esempio: 3 spine, 4 leaf e un nuovo tenant D

In `config/topology.yaml`:

```yaml
spines: 3
leaves: 4
hosts_per_leaf: 4
```

In `config/tenants.yaml`, aggiungere `h15` al tenant C e creare il tenant D
con alcuni spare:

```yaml
  - name: C
    tag: 103
    rate_mbps: 20
    subnet: 10.0.1.0/24
    hosts:
      - {name: h3, leaf: leaf1, port: 3, ip: 10.0.1.1}
      - {name: h7, leaf: leaf2, port: 3, ip: 10.0.1.2}
      - {name: h11, leaf: leaf3, port: 3, ip: 10.0.1.3}
      - {name: h15, leaf: leaf4, port: 3, ip: 10.0.1.4}  # nuovo

  - name: D
    tag: 104
    rate_mbps: 20
    subnet: 10.0.2.0/24
    hosts:
      - {name: h4,  leaf: leaf1, port: 4, ip: 10.0.2.1}
      - {name: h8,  leaf: leaf2, port: 4, ip: 10.0.2.2}
      - {name: h16, leaf: leaf4, port: 4, ip: 10.0.2.3}
```

Al riavvio il controller riporta `ALL SWITCHES READY: 7/7`, ogni leaf ha tre
uplink (porte 5, 6 e 7) e tre bucket ECMP, `h4 ping h16` funziona e
`h4 ping h1` fallisce (`Network is unreachable`). Gli host rimasti liberi (h12,
h13, h14) sono spare.

## Test

**Automatici**, senza avviare la rete: generazione e cattura dei pacchetti,
tuple UDP per ECMP, parsing e confronto dei contatori OpenFlow.

```bash
.venv/bin/python -B -m unittest discover -s tests/network -v
```

**Manuali**, con la rete avviata:

- [docs/test-manuali.md](docs/test-manuali.md): proxy ARP, risultati attesi da
  h1, uso di `packet_io.py` e del listener `--listen`, visualizzatore dei
  contatori, misura ECMP.
- [docs/pipeline.md](docs/pipeline.md): trace delle regole, frame falsificati,
  attacchi cross-tenant, catture ARP, riconnessione degli switch.

Esempio dalla CLI Mininet (richiesta ARP da h2 verso h6, attesa una risposta
del proxy):

```text
h2 .venv/bin/python tests/network/packet_io.py --iface h2-eth0 --case arp-request
```

## Documentazione aggiuntiva

- [docs/pipeline.md](docs/pipeline.md): politica di inoltro, matrice delle regole
  OpenFlow, cookie e procedure di verifica della pipeline.
- [docs/visualizer.md](docs/visualizer.md): guida dettagliata della GUI e della
  sua architettura.
- [docs/test-manuali.md](docs/test-manuali.md): procedure di test passo-passo
  dalla CLI Mininet.
