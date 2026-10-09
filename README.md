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
`build_arp_reply` valida la richiesta e restituisce il frame di risposta oppure
`None`. `app.py` gestisce l'evento PacketIn, i controlli sul datapath e l'invio
PacketOut alla porta richiedente.

Sono scartati mittenti con IP/MAC incoerenti, destinazioni sconosciute,
probe e gratuitous ARP, frame tagged e traffico dalle porte spare o dagli uplink.
Non sono ancora implementati forwarding IP, apprendimento dinamico e rate limiting
delle richieste verso il controller. Un ping quindi fallisce anche se ARP riesce.
I binding sono caricati all'avvio: future modifiche runtime ai tenant dovranno
aggiornare sia gli indici del controller sia le regole delle porte.

### Test automatici

Dalla radice del progetto:

```bash
PYTHONPATH=src .venv/bin/python -m unittest discover -s tests -v
```

I test verificano risposte serializzate, isolamento degli IP sovrapposti,
scarto delle richieste non valide e regole OpenFlow senza flooding.

### Verifica in Mininet

Avviare controller e rete con gli script in `scripts/`. Attendere
`ALL SWITCHES READY`, quindi nella CLI Mininet:

```text
h1 ip neigh flush all
h1 ping -c 1 -W 1 10.0.0.2
h1 ip neigh show 10.0.0.2
h2 ip neigh flush all
h2 ping -c 1 -W 1 10.0.0.2
h2 ip neigh show 10.0.0.2
```

Il ping deve fallire, ma la neighbor entry di h1 deve contenere
`00:00:00:00:00:05` (h5), quella di h2 `00:00:00:00:00:06` (h6).
Con una cattura ARP attiva sugli uplink durante la prova, verificare che non
compaiano richieste o risposte. Nella topologia corrente gli uplink dei leaf
sono le porte 5 e 6. Ripetere dopo una riconnessione del leaf al controller
per verificare la reinstallazione delle regole.
