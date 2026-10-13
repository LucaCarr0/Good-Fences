# Good-Fences
Progetto per il corso di NCIS

Lo script `scripts/start-network.sh` rimuove automaticamente le precedenti
run prima dell'avvio: termina i nodi Mininet hN/leafN/spineN/c0 e rimuove
i bridge leafN/spineN e le relative interfacce. Questi nomi sono riservati
a Good-Fences: una nuova esecuzione sostituisce la simulazione precedente,
anche se ancora attiva. Il controller Ryu resta in esecuzione.

## Proxy ARP

Il controller risponde alle richieste ARP degli host assegnati usando IP e MAC
del modello YAML. Il tenant viene identificato da DPID e porta di ingresso:
la risoluzione usa `(tag tenant, IP)`, anche quando due tenant hanno IP uguali.
La risposta untagged torna soltanto alla porta richiedente. Gli ARP non vengono
inoltrati agli spine; le altre tabelle mantengono default drop.

La logica del protocollo e' in `src/controller/arp_proxy.py`: la funzione
`build_arp_reply` controlla il tipo di richiesta e la destinazione e restituisce
il frame di risposta oppure `None`. `app.py` gestisce l'evento PacketIn,
i controlli sul datapath e l'invio
PacketOut alla porta richiedente.

Sono scartate le destinazioni sconosciute nel tenant del richiedente.
Le regole OpenFlow scartano frame tagged dalle porte host e ARP dalle porte spare
o dagli uplink. Flow e proxy verificano MAC Ethernet, MAC ARP e IP sorgente contro
il binding della porta. Probe e gratuitous ARP vengono scartati esplicitamente.
Non sono ancora implementati forwarding IP, apprendimento dinamico e rate limiting
delle richieste verso il controller. Un ping quindi fallisce anche se ARP riesce.
I binding sono caricati all'avvio: future modifiche runtime ai tenant dovranno
aggiornare sia gli indici del controller sia le regole delle porte.

### Test rapido del proxy ARP

Con controller e rete avviati, attendere `ALL SWITCHES READY`.

1. Dal terminale Linux, vedere le tabelle prima della richiesta:

```bash
sudo ovs-ofctl -O OpenFlow13 dump-flows leaf1
```

2. Dalla CLI Mininet, svuotare la cache e generare la richiesta:

```text
h2 ip neigh flush dev h2-eth0
h2 ping -n -c 1 -W 1 h6
h2 ip neigh show 10.0.0.2
```

3. Dal terminale Linux, vedere le tabelle dopo la richiesta:

```bash
sudo ovs-ofctl -O OpenFlow13 dump-flows leaf1
```

Risultato atteso: aumenta `n_packets` della regola ARP su `leaf1-eth2`
e la cache di h2 mostra `lladdr 00:00:00:00:00:06` (h6).
Il ping genera prima ARP, ma fallisce perché manca il forwarding IP.

### Risultati attesi da h1

Prima di ogni caso: `h1 ip neigh flush dev h1-eth0`. Dopo il ping,
controllare la cache con `h1 ip neigh show`.

| Caso | Comando CLI Mininet | Risultato atteso |
| --- | --- | --- |
| 1: stesso tenant | `h1 ping -c 1 -W 1 h5` | ARP risolto: `.2` → `00:00:00:00:00:05` (`REACHABLE` o `STALE`). Ping fallisce: manca forwarding IP. |
| 2: altro tenant, stesso IP | `h1 ping -c 1 -W 1 h6` | Stesso risultato del caso 1: h6 ha lo stesso IP di h5, quindi h1 risolve il MAC di h5 nel tenant A. |
| 3: IP assente nel tenant A | `h1 ping -c 1 -W 1 h10` | `10.0.0.13` senza MAC: `INCOMPLETE`, poi `FAILED`. Nessuna risposta ARP. |
| 4: subnet diversa | `h1 ping -c 1 -W 1 h7` | `Network is unreachable`: manca una route verso `10.0.1.0/24`, nessuna richiesta ARP. |


## Sprint 1.2: classificazione IPv4

IPv4 untagged dalle porte assegnate viene ammesso solo con MAC/IP sorgenti del
modello, riceve la VLAN del tenant e attraversa le tabelle 0 → 1 → 2, dove termina
in DROP. Dagli uplink sono ammessi soltanto IPv4 con VLAN nota, sempre fino al DROP
finale. Spare e ingressi non ammessi ricadono nel default DROP a priorità 0;
non ci sono DROP generici per porta a priorità 100.

Avvio dalla radice, in due terminali:

```bash
bash scripts/start-controller.sh
bash scripts/start-network.sh
```

Attendere `ALL SWITCHES READY: 5/5`. La Barrier conferma l'intera installazione.
Per ispezionare manualmente le regole:

```bash
sudo ovs-ofctl -O OpenFlow13 dump-flows leaf1 table=0
sudo ovs-ofctl -O OpenFlow13 dump-flows leaf1 table=1
sudo ovs-ofctl -O OpenFlow13 dump-flows leaf1 table=2
```

La [procedura manuale dello sprint](docs/pipeline.md) riporta matrice delle regole,
trace, lettura dei contatori, comandi dalla CLI Mininet per frame falsificati,
catture ARP e riconnessione. Non sono presenti suite o script di test automatizzati.
Il forwarding resta da implementare: un ping fallito da solo non verifica
l'anti-spoofing. Le verifiche documentate indicano risultati attesi.
