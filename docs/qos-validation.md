# Verifica QoS del 19 ottobre 2026

Le misure seguenti sono osservazioni di questa esecuzione, non garanzie per
configurazioni o sistemi diversi. OVS 3.3.9, iperf3 3.16, Python 3.9; topologia
con due uplink da 60 Mbps, host da 100 Mbps e ritardo 1 ms per collegamento.
Meter: 20 Mbps per tenant per leaf, burst 100 ms, banda DROP, IPv4 host soltanto.

Le prove live hanno usato una fabric temporanea `qtleaf*`/`qtspine*` e Ryu sulla
porta 16653, preservando la rete già aperta. DPID, tenant, pipeline e parametri
TC erano quelli del progetto. Il motore delle misure è lo stesso della GUI e
del runner CLI; un harness temporaneo ha soltanto separato nomi e porta.

## Test isolato e regressioni

Run `20261019-085818-883a48c7`: h1→h5, UDP 1200 byte, 3 s di assestamento e
30 s di misura per prova.

| Modalità | Offerti | Goodput osservato | Perdita |
| --- | ---: | ---: | ---: |
| Senza meter | 40 Mbps | 40,000 Mbps | 0% |
| Con meter, sotto soglia | 15 Mbps | 15,000 Mbps | 0% |
| Con meter, sovraccarico | 40 Mbps | 19,316 Mbps | 51,709% |

Il rate ammesso ricavato dai contatori del meter è **19,972 Mbps**, con
**64.566 DROP** nel periodo misurato. La differenza rispetto al goodput UDP
è coerente con l'overhead; il controllo include durata effettiva, burst e
tolleranza del 5%. La baseline non mostrava congestione. Il limite isolato
è stato verificato; la matrice completa di sovraccarico dei nove meter non è
stata eseguita.

Sono passati **26 casi live** nelle modalità on/off: sei coppie di host dello
stesso tenant, consegna IPv4, attacchi incrociati A/B con MAC della vittima,
sorgente IP falsa, MAC falso, VLAN host e porta spare. I ping includono la
risoluzione ARP tramite il proxy.

Un controllo finale breve, run `20261019-091407-1e7fa403`, ha verificato anche
salvataggio dei log del controller e lettura dei contatori porte nella versione
finale del runner. Le prove brevi non sostituiscono le misure da 30 s sopra.
L'inventario live finale ha confermato **nove meter**, tre per leaf, con
rate/burst e riferimenti flow attesi; gli spine non avevano meter.

## Noisy neighbor ripetuto

Run `20261019-090424-7fdd9429`: sei coppie, tre on/off e tre off/on, seed 42.
Vittima h2→h6 a 15 Mbps; aggressori h1→h5 e h3→h7, ciascuno a 90 Mbps su otto
tuple calibrate, quattro per uplink. Ogni prova usa 3 s di assestamento e
30 s di misura, con gli stessi tuple, code e link nelle due condizioni.

| Misura della vittima, media di 6 prove | Senza meter | Con meter |
| --- | ---: | ---: |
| Goodput ricevuto | 8,339 Mbps | 15,000 Mbps |
| Perdita UDP | 44,408% | 0% |
| RTT medio | 172,605 ms | 8,101 ms |
| SD temporale throughput, media delle SD per prova | 0,17538 Mbps | 0,00487 Mbps |
| SD fra i goodput medi delle prove | 0,03776 Mbps | 0,00017 Mbps |
| Jitter UDP medio | 0,08034 ms | 0,04128 ms |

Tutte le **6 baseline** mostravano congestione sul percorso della vittima:
utilizzo degli uplink prossimo al 100%, backlog e DROP TC. Per esempio, nella
prima baseline `leaf1-eth5` mostrava 4.908 DROP di qdisc nell'intervallo e circa
1,24 MB di backlog. I contatori gerarchici non sono stati sommati. Nessuna
prova con meter mostrava congestione secondo lo stesso criterio.

Gli obiettivi ≥14 Mbps e perdita ≤2% sono stati raggiunti in **6/6 prove con
meter** e in 0/6 baseline. I CSV mantengono anche RTT, jitter e variabilità per
intervallo e gli esiti delle singole coppie. Le prove fallite durante lo
sviluppo del parser/calibrazione restano nelle rispettive directory locali
e non sono incluse in queste medie.

## Riproduzione e artefatti

La [guida QoS](qos.md) descrive GUI, runner CLI e procedura manuale iperf3/OVS.
Sono passati **72 test automatici**: 15 controller, 43 utility di rete,
14 visualizzatore, inclusi i 53 test preesistenti.

Un riepilogo CSV e la configurazione della prova noisy neighbor sono conservati
in [results/qos-2026-10-19](results/qos-2026-10-19/summary.csv).
I dati completi, i campioni, i log e `charts.html` sono nelle directory
`results/qos/<run-id>/` della workspace, escluse da Git e scaricabili dalla GUI.
