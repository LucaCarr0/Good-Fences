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
Le regole OpenFlow scartano frame tagged e traffico dalle porte spare o dagli uplink.
La versione corrente non verifica la coerenza di IP/MAC del mittente e non
scarta esplicitamente probe e gratuitous ARP.
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
h2 ip neigh show h6
```

3. Dal terminale Linux, vedere le tabelle dopo la richiesta:

```bash
sudo ovs-ofctl -O OpenFlow13 dump-flows leaf1
```

Risultato atteso: aumenta `n_packets` della regola ARP su `leaf1-eth2`
e la cache di h2 mostra `lladdr 00:00:00:00:00:06` (h6).
Il ping genera prima ARP, ma fallisce perché manca il forwarding IP.
