# QoS IPv4 tramite meter OpenFlow 1.3

## Contratto e installazione

`GOOD_FENCES_QOS=on|off` viene letto all'avvio di Ryu; default `on`.
Valori diversi sono errori. La baseline `off` conserva i flow senza istruzione
meter. Non sono presenti endpoint REST.

| Tenant | ID meter | Rate | Banda DROP | Burst |
| --- | ---: | ---: | ---: | ---: |
| A | 101 | 20 Mbps | 20000 kbit/s | 2000 kbit |
| B | 102 | 20 Mbps | 20000 kbit/s | 2000 kbit |
| C | 103 | 20 Mbps | 20000 kbit/s | 2000 kbit |

Su ogni leaf viene creato un meter per ciascun tenant con host locali. L'ID è
il tag VLAN; istanze con lo stesso ID su datapath diversi sono indipendenti.
Il budget è **aggregato per tenant per leaf**, condiviso da tutti gli host e
flussi IPv4 del tenant sul leaf. Non è un limite globale sulla fabric, né una
prenotazione o garanzia di banda. Gli spine non hanno meter.

`rate_mbps` è un intero positivo, esclusi booleani, rappresentabile nel campo
OpenFlow a 32 bit dopo `Mbps × 1000`. Le unità sono decimali. Flag:
`KBPS | BURST | STATS`; una sola banda DROP; burst fisso di 100 ms:
`burst_kbit = rate_kbps × 100 / 1000`.

Il meter compare soltanto nei flow IPv4 host della tabella 0, cookie
`0x47460040`, il cui match verifica porta, assenza di VLAN, MAC e IP sorgenti:

```text
METER tenant_id
APPLY_ACTIONS push_vlan:0x8100, set_field:tenant_vlan
GOTO_TABLE 1
```

Viene eseguito prima dell'aggiunta della VLAN. Il rate dei contatori OVS e il
goodput del payload UDP sono misure distinte: Ethernet/IP/UDP/VLAN introducono
overhead. ARP, frame rifiutati, uplink, tabelle 1/2 e spine conservano il loro
comportamento. Le richieste al proxy ARP non consumano il budget IPv4.

Avvio e riconnessione: rimozione flow, default DROP, richiesta meter-features,
verifica di DROP/KBPS/BURST/STATS e capacità, preparazione gruppo ECMP e meter,
Barrier risorse, flow, Barrier finale, READY. Timeout per fase: 10 s. Errori,
timeout o capacità insufficienti impediscono READY. La modalità off non
richiede capacità di policing; se supportati, elimina i meter degli ID tenant
configurati. Gli ID estranei vengono preservati. Il provisioning dinamico è
escluso: un meter di un tag rimosso dal YAML richiede pulizia esplicita.

## GUI e runner CLI

Avviare `bash scripts/start-visualizer.sh`, scegliere la QoS iniziale e premere
**Avvia rete**. Attendere tutti gli switch READY.

- **Statistiche → Singolo switch**, o lo switch nella topologia, mostra
  capacità, bande, riferimenti flow, ingressi, DROP e delta dei meter.
- Salvare un riferimento prima del traffico, poi aggiornare. Zero attività,
  dati mancanti e reset sono distinti. Riavviare il controller invalida i
  vecchi riferimenti.
- **QoS** avvia test isolato o noisy neighbor, con avanzamento, annullamento,
  tabelle e download. La matrice isolata prova tutti gli host/leaf tenant
  contro un host remoto dello stesso tenant.
- Un solo esperimento può essere attivo. Gli altri invii GUI sono sospesi.
  Annullamento/arresto fermano i worker e ripristinano la QoS iniziale.

Senza GUI, dalla radice:

```bash
sudo .venv/bin/python -B -m src.qos_experiment --scenario isolated
sudo .venv/bin/python -B -m src.qos_experiment --scenario isolated --matrix
sudo .venv/bin/python -B -m src.qos_experiment --scenario noisy-neighbor
```

Il runner crea e chiude il proprio laboratorio. Rifiuta bridge Good-Fences o
controller già attivi: chiudere la precedente simulazione dalla sua GUI/CLI.
Non arresta reti esterne. Richiede Plotly per i grafici, già compreso in
`requirements-visualizer.txt`.

Opzioni: `--duration 30`, `--repetitions 6` (pari, 2–20), `--seed 42`,
`--source h1 --target h5`, `--noise-mbps 90`, `--output DIRECTORY_NUOVA`.
Durate ridotte servono per controllare il funzionamento; l'accettazione usa
30 s di misura, 3 s di assestamento e sei coppie.

Il runner usa `qos_traffic.py`: UDP a tuple esplicite, payload 1200 byte,
sequenze e timestamp CLOCK_MONOTONIC condiviso dai namespace Mininet. Tutti i
worker condividono la stessa finestra; i ricevitori sono pronti prima degli
invii. Drain: 2 s. I pacchetti vengono attribuiti al secondo di invio, anche
quando arrivano durante il drain. Perdita = differenza fra invii osservati e
ricezioni uniche, divisa per gli invii. Il bitrate configurato non viene
usato come conteggio dei pacchetti. Jitter: EWMA della variazione di transito,
coefficiente 1/16. RTT: ping concorrente ogni 100 ms.

### Test isolato

Senza altri carichi: baseline off a 40 Mbps, on a 15 Mbps, on a 40 Mbps.
La baseline deve sostenere almeno 38 Mbps e perdita ≤2%, senza congestione
misurata. Sotto soglia: ingressi positivi e zero DROP. Sopra soglia: DROP,
riferimenti flow coerenti e rate ammesso compatibile con la configurazione.

Il rate ammesso è `(delta ingressi byte − delta DROP byte) × 8 / T`.
`T` è il tempo effettivo fra letture. Intervallo di verifica:

```text
0.95 × rate_Mbps ≤ rate_ammesso ≤
1.05 × rate_Mbps + burst_kbit / (1000 × T_s) + un_frame_bit / (1e6 × T_s)
```

Contatori mancanti/incoerenti rendono la verifica incompleta. Non confrontare
direttamente il payload ricevuto con i 20 Mbps di contabilità del meter.

### Noisy neighbor

Topologia: due uplink da 60 Mbps, host da 100 Mbps, 1 ms per collegamento.
Vittima B: h2→h6 a 15 Mbps. Aggressori A h1→h5 e C h3→h7: ciascuno 90 Mbps
su otto tuple. Senza meter, una calibrazione seleziona quattro tuple per
uplink per ogni aggressore e registra i percorsi di vittima e ping. Non cambia
gruppi, hash, code o capacità. Se non trova tale distribuzione entro 64
candidati per aggressore, si ferma richiedendo di riprogettare lo scenario.

Sei coppie: tre off/on e tre on/off, mescolate con seed 42. Fra modalità si
riavvia soltanto Ryu, mantenendo Mininet attivo, attendendo READY e acquisendo
nuovi riferimenti. Le tuple restano identiche e il percorso della vittima
viene ricontrollato.

Campioni ogni secondo: flow, bucket, meter, TX e `tc -j -s qdisc` sui due
uplink del leaf sorgente e sulle uscite spine verso il leaf destinatario.
Le qdisc gerarchiche rimangono separate: parent e child non si sommano.
La congestione richiede utilizzo ≥90% con DROP, oppure tre letture consecutive
con backlog e utilizzo ≥90%, su un percorso della vittima. Il solo carico
offerto non dimostra congestione.

Senza tale evidenza nella baseline, il confronto è **non dimostrativo**.
Riprogettare carico/tuple e ripetere entrambe le condizioni con la medesima
configurazione. Non selezionare soltanto prove favorevoli. I target della
vittima (≥14 Mbps e perdita ≤2%) sono verificati per prova e non garantiti.

## Procedura manuale

In un terminale avviare Ryu:

```bash
GOOD_FENCES_QOS=off bash scripts/start-controller.sh
```

In un secondo terminale: `bash scripts/start-network.sh`. Attendere
`ALL SWITCHES READY: 5/5`. Per cambiare modalità fermare **solo Ryu** con
Ctrl+C, riavviarlo con `GOOD_FENCES_QOS=on` e attendere READY. Mininet resta
attivo. Fermare prima i generatori della prova precedente.

Da Linux, oppure dalla CLI Mininet anteponendo `sh` e omettendo `sudo`:

```bash
sudo ovs-ofctl -O OpenFlow13 meter-features leaf1
sudo ovs-ofctl -O OpenFlow13 dump-meters leaf1
sudo ovs-ofctl -O OpenFlow13 meter-stats leaf1
sudo ovs-ofctl -O OpenFlow13 dump-flows leaf1 table=0
sudo ovs-ofctl -O OpenFlow13 dump-group-stats leaf1
sudo tc -j -s qdisc show dev leaf1-eth5
sudo tc -j -s qdisc show dev leaf1-eth6
sudo tc -j -s qdisc show dev spine1-eth2
sudo tc -j -s qdisc show dev spine2-eth2
```

In on verificare ID, flag, rate e burst della tabella e `meter:ID` soltanto nei
flow IPv4 host. In off questi riferimenti e i meter configurati devono essere
assenti. Ripetere su leaf2/3; sugli spine non devono esserci meter.

Per il limite isolato, dalla CLI Mininet:

```text
h5 iperf3 -s -1 -p 5201 -J --logfile /tmp/qos-h5-rx.json &
h1 iperf3 -c 10.0.0.2 -u -p 5201 --cport 40000 -b 40M -l 1200 -O 3 -t 30 -J --get-server-output > /tmp/qos-h1-tx.json 2>&1 &
```

Registrare contatori e istanti prima/dopo la finestra utile. Ripetere off a
40 Mbps, on a 15 Mbps e on a 40 Mbps; riavviare il server `-1` ogni volta.
Usare i dati del **ricevitore** per goodput, perdita e jitter; conservare il
JSON del mittente con invii e tuple.

Per il noisy neighbor avviare prima i tre server, poi il traffico:

```text
h6 iperf3 -s -1 -p 5201 -J --logfile /tmp/qos-B-rx.json &
h5 iperf3 -s -1 -p 5300 -J --logfile /tmp/qos-A-rx.json &
h7 iperf3 -s -1 -p 5400 -J --logfile /tmp/qos-C-rx.json &
h2 ping -n -D -i 0.1 -w 40 10.0.0.2 > /tmp/qos-B-rtt.log 2>&1 &
h1 iperf3 -c 10.0.0.2 -u -p 5300 --cport 41000 -P 8 -b 11250000 -l 1200 -O 3 -t 30 -J --get-server-output > /tmp/qos-A-tx.json 2>&1 &
h3 iperf3 -c 10.0.1.2 -u -p 5400 --cport 42000 -P 8 -b 11250000 -l 1200 -O 3 -t 30 -J --get-server-output > /tmp/qos-C-tx.json 2>&1 &
h2 iperf3 -c 10.0.0.2 -u -p 5201 --cport 40000 -b 15M -l 1200 -O 3 -t 30 -J --get-server-output > /tmp/qos-B-tx.json 2>&1 &
```

Con iperf3 `-b` vale **per stream**: otto ×11.25 Mbps =90 Mbps. Salvare le
tuple effettive nel JSON; `-P` non garantisce la stessa selezione del runner
né una ripartizione 50/50. Controllare i bucket e calibrare le tuple.
I comandi manuali partono in istanti diversi: usare solo intervalli comuni
registrando gli avvii; per la sincronizzazione esatta usare il runner CLI.

Campionare ogni secondo code e TX **durante** il carico: il backlog può sparire
alla fine. I contatori di banda DROP provano il policing; i flow contano
anche pacchetti poi scartati dal meter. Ripetere sei coppie, alternare l'ordine,
usare nomi file distinti, registrare configurazione, throughput, perdita,
RTT, jitter e dispersione temporale/fra prove. Un ping o una sola prova non
bastano a dimostrare la mitigazione.

## Artefatti e interpretazione

Ogni run salva `results/qos/<data-id>/`, escluso da Git e scaricabile anche ZIP:

- `config.json`: topologia, tenant, parametri, unità e versioni effettive.
- `tuples.json`: tuple calibrate e percorsi del noisy neighbor.
- `intervals.csv`: misure per stream/secondo con configurazione e validità.
- `summary.csv`, `pairs.csv`: singole prove e giudizi sulle coppie.
- `meters.csv`: ingressi e DROP osservati per tenant e prova.
- `aggregate.csv`: medie e deviazioni standard fra prove valide, con numero
  totale e valido. I dati mancanti restano vuoti.
- `charts.html`: grafici autonomi Plotly con configurazione rilevante.
- Sottocartelle: JSON worker, campioni OpenFlow/TC, ping, log ed evidenze di
  congestione, mantenuti anche su errore o annullamento.

`complete` indica la fine dell'esecuzione. Leggere `criteria_verified`,
`pairs.csv` e gli obiettivi della vittima per conoscere l'esito sperimentale.
Errori, contatori mancanti o campioni oltre 750 ms in ritardo rendono la prova
incompleta. Dati parziali non attestano il successo.

Riferimenti: [OpenFlow 1.3](https://opennetworking.org/wp-content/uploads/2014/10/openflow-spec-v1.3.0.pdf),
[ovs-ofctl](https://www.openvswitch.org/support/dist-docs/ovs-ofctl.8.html),
[iperf3](https://software.es.net/iperf/invoking.html).

Le [verifiche del 19 ottobre 2026](qos-validation.md) riportano misure osservate,
prove eseguite e limiti, con un riepilogo CSV conservato nel repository.
