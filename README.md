# Good-Fences
Progetto per il corso di NCIS

Lo script `scripts/start-network.sh` rimuove automaticamente le precedenti
run prima dell'avvio: termina i nodi Mininet hN/leafN/spineN/c0 e rimuove
i bridge leafN/spineN e le relative interfacce. Questi nomi sono riservati
a Good-Fences: una nuova esecuzione sostituisce la simulazione precedente,
anche se ancora attiva. Il controller Ryu resta in esecuzione.

## Visualizzatore GUI

La dashboard Streamlit avvia e arresta **Mininet e Ryu**, mostra la topologia,
invia i casi di `packet_io.py` dagli host e confronta flow e bucket OpenFlow.
Usa la `.venv` Python 3.9 esistente con dipendenze opzionali:

```bash
.venv/bin/python -m pip install -r requirements-visualizer.txt
bash scripts/start-visualizer.sh
```

Aprire <http://127.0.0.1:8501>, premere **Avvia rete** e attendere **5/5 switch
READY** con la configurazione attuale. In questa modalità non avviare i due
script CLI: chiudere prima eventuali reti Mininet con `exit` e controller con
`Ctrl+C`. La GUI segnala i conflitti senza sostituire una rete esterna.
**Arresta rete** chiude prima Mininet e poi Ryu. La chiusura della scheda del
browser lascia attiva la simulazione; arrestarla dalla GUI prima di terminare
Streamlit con `Ctrl+C`.

La [guida del visualizzatore](docs/visualizer.md) descrive viste e verifiche.

## Proxy ARP

Il controller risponde alle richieste ARP degli host assegnati usando IP e MAC
del modello YAML. Il tenant viene identificato da DPID e porta di ingresso:
la risoluzione usa `(tag tenant, IP)`, anche quando due tenant hanno IP uguali.
La risposta untagged torna soltanto alla porta richiedente. Gli ARP non vengono
inoltrati agli spine; il traffico IPv4 segue il forwarding unicast descritto sotto.

La logica del protocollo e' in `src/controller/arp_proxy.py`: la funzione
`build_arp_reply` controlla il tipo di richiesta e la destinazione e restituisce
il frame di risposta oppure `None`. `app.py` gestisce l'evento PacketIn,
i controlli sul datapath e l'invio
PacketOut alla porta richiedente.

Sono scartate le destinazioni sconosciute nel tenant del richiedente.
Le regole OpenFlow scartano frame tagged dalle porte host e ARP dalle porte spare
o dagli uplink. Le entry OpenFlow verificano MAC Ethernet, MAC ARP e IP sorgente contro
il binding della porta. Probe e gratuitous ARP vengono scartati esplicitamente.
Il forwarding IPv4 è statico e isolato per tenant. Non sono implementati
apprendimento dinamico e rate limiting delle richieste verso il controller.
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
Il ping genera prima ARP e deve riuscire tra h2 e h6, entrambi nel tenant B.

### Risultati attesi da h1

Prima di ogni caso: `h1 ip neigh flush dev h1-eth0`. Dopo il ping,
controllare la cache con `h1 ip neigh show`.

| Caso | Comando CLI Mininet | Risultato atteso |
| --- | --- | --- |
| 1: stesso tenant | `h1 ping -c 1 -W 1 h5` | ARP risolto: `.2` → `00:00:00:00:00:05` (`REACHABLE` o `STALE`). Ping riuscito. |
| 2: nome di altro tenant, stesso IP | `h1 ping -c 1 -W 1 h6` | Raggiunge h5: h6 ha lo stesso IP di h5, quindi h1 risolve il MAC di h5 nel tenant A. Questo comando non verifica l'isolamento. |
| 3: IP assente nel tenant A | `h1 ping -c 1 -W 1 h10` | `10.0.0.13` senza MAC: `INCOMPLETE`, poi `FAILED`. Nessuna risposta ARP. |
| 4: subnet diversa | `h1 ping -c 1 -W 1 h7` | `Network is unreachable`: manca una route verso `10.0.1.0/24`, nessuna richiesta ARP. |


## Forwarding unicast isolato con ECMP

IPv4 untagged dalle porte assegnate viene ammesso solo con MAC/IP sorgenti del
modello e riceve la VLAN del tenant in tabella 0. La tabella 1 ammette soltanto
destinazioni con VLAN, MAC e IP coerenti con un host del tenant; la tabella 2
consegna sul leaf locale togliendo la VLAN oppure richiama il gruppo ECMP.
Ogni leaf ha un gruppo OpenFlow SELECT con ID `1`: i due bucket hanno peso `1`
e inoltrano agli uplink del modello, porte **5 verso spine1** e **6 verso spine2**
nella configurazione attuale. Entrambi gli uplink ammettono IPv4 con VLAN nota.
Dalla fabric si può consegnare solo a host locali: non esiste un percorso
uplink → uplink. Entrambi gli spine conservano la VLAN e inoltrano sul leaf di
destinazione, mai sulla porta di ingresso. Spare e ingressi non ammessi ricadono
nel default DROP a priorità 0. Isolamento, binding sorgente e proxy ARP restano
invariati. Non sono introdotti meter, NORMAL o FLOOD.

La funzione di supporto ECMP è direttamente in `src/controller/pipeline.py`.
All'avvio e alla riconnessione il controller rimuove i vecchi flow, ripristina i
DROP e ricrea soltanto il proprio gruppo ECMP. Una prima Barrier conferma questa
preparazione prima di inviare i flow che usano il gruppo; la Barrier finale
conferma l'intera pipeline. Errori OpenFlow o timeout impediscono lo stato READY.

Avvio dalla radice, in due terminali:

```bash
bash scripts/start-controller.sh
bash scripts/start-network.sh
```

Attendere `ALL SWITCHES READY: 5/5`, dopo entrambe le fasi di installazione.
Per ispezionare manualmente le regole:

```bash
sudo ovs-ofctl -O OpenFlow13 dump-flows leaf1 table=0
sudo ovs-ofctl -O OpenFlow13 dump-flows leaf1 table=1
sudo ovs-ofctl -O OpenFlow13 dump-flows leaf1 table=2
sudo ovs-ofctl -O OpenFlow13 dump-groups leaf1
sudo ovs-ofctl -O OpenFlow13 dump-group-stats leaf1
```

La [procedura manuale della pipeline](docs/pipeline.md) riporta matrice delle regole,
trace, lettura dei contatori, comandi dalla CLI Mininet per frame falsificati,
catture ARP e riconnessione. Include i comandi dell'utility
`tests/network/packet_io.py` per aggiungere ARP Probe, Gratuitous ARP, RARP,
IPv4 tagged e untagged ai test manuali degli sprint precedenti, con ascolto sul
destinatario e casi di isolamento. Negli attacchi con IP sovrapposti va indicato
il MAC della vittima: il solo IP risolve un host del tenant della sorgente.
La procedura riporta risultati attesi e, in fondo, le verifiche eseguite.

L'utility usa Scapy, incluso in `requirements.txt`. Per installarlo nella `.venv`:

```bash
.venv/bin/python -m pip install 'scapy>=2.6,<3'
```

Con controller e rete avviati, dopo `ALL SWITCHES READY: 5/5`, dalla CLI Mininet:

```text
h2 .venv/bin/python tests/network/packet_io.py --iface h2-eth0 --case arp-request
h2 .venv/bin/python tests/network/packet_io.py --iface h2-eth0 --case arp-probe
```

Il caso `arp-request` invia una richiesta ARP normale verso h6: è attesa una
risposta del proxy. Per `arp-probe` sono invece attese zero risposte.
Questi due comandi inviano un frame e mostrano richiesta e risposte con MAC, IP,
operazione e VLAN leggibili, senza stringhe esadecimali. La verifica si fa
manualmente confrontando i contatori di `ovs-ofctl dump-flows` prima e dopo.

### Usare `--listen` con due terminali

Il listener ascolta sul destinatario h6 mentre h2 invia un frame. Si usa la
stessa CLI Mininet per entrambi: **non serve un terzo terminale**.
Eseguire un comando alla volta; non digitare il prefisso `mininet>`.

1. **Terminale 1**, dove gira `start-controller.sh`: lasciarlo in esecuzione.
   Attendere `ALL SWITCHES READY: 5/5`; qui non bisogna inserire comandi di test.

2. **Terminale 2**, dove gira `start-network.sh`: al prompt `mininet>` avviare
   il listener su h6:

   ```text
   h6 .venv/bin/python tests/network/packet_io.py --iface h6-eth0 --listen --timeout 60 > /tmp/good-fences-h6-ipv4.log 2>&1 &
   ```

   `h6` esegue il comando dentro quell'host; `--listen` ascolta per 60 secondi.
   Il `&` finale lascia il listener in background e restituisce il prompt
   Mininet, così si può inviare da h2. L'output viene salvato nel file di log.

3. Sempre nel **terminale 2**, controllare che il listener sia pronto:

   ```text
   h6 cat /tmp/good-fences-h6-ipv4.log
   ```

   Deve comparire `Cattura pronta su h6-eth0`. Se il file è ancora vuoto,
   ripetere questo `cat` dopo un secondo. Appena compare la riga, proseguire.

4. Sempre nel **terminale 2**, inviare da h2 verso h6:

   ```text
   h2 .venv/bin/python tests/network/packet_io.py --iface h2-eth0 --case ipv4-untagged --target-ip 10.0.0.2 --target-mac 00:00:00:00:00:06
   ```

5. I frame ricevuti vengono stampati **al termine della cattura**, non subito.
   Per aspettare senza cronometrare, eseguire questi comandi nel **terminale 2**:

   ```text
   h6 sleep 60
   h6 cat /tmp/good-fences-h6-ipv4.log
   ```

   `sleep` tiene occupata la CLI per un minuto. Il log deve poi mostrare
   `Frame IPv4 protocollo 253 catturati: 1`, Ethernet h2 (`...:02`) → h6
   (`...:06`), IP `10.0.0.1` → `10.0.0.2` e `VLAN: assente (untagged)`.
   Per un nuovo test ripartire dal punto 2: il listener precedente è terminato.

Il protocollo sperimentale 253 non ha una risposta applicativa, ma il kernel
del destinatario può generare un ICMP `Protocol Unreachable`. Questo traffico
di ritorno incrementa la regola dello spine verso il mittente: un solo invio
può quindi incrementare due regole distinte, una per direzione.
`--listen` cattura in promiscuità tutti i frame IPv4 con protocollo 253 e non
mostra questi ICMP (protocollo 1). Per provare la consegna leggere il listener,
non l'assenza di reply sul mittente. `--timeout` vale 5 secondi in ascolto e 1 in invio
quando non è specificato. Nella procedura della pipeline sono riportati gli
attacchi cross-tenant e i percorsi leaf/spine attesi.
Per nuovi scenari basta aggiungere un nome a `CASES` e costruire il relativo
pacchetto in `build_packet`.

### Visualizzatore dei contatori leaf/spine

Da un altro terminale Linux, con la rete avviata:

```bash
sudo .venv/bin/python tests/network/flow_visualizer.py
```

Scegli il numero o il nome dello switch. Il visualizzatore mostra un riepilogo
per tabella/tipo di regola, i contatori delle singole regole e i bucket dei gruppi
dello switch scelto. Per ciascun bucket mostra peso, porta di uscita, pacchetti e
byte prima/dopo, delta e percentuale dei pacchetti rispetto al delta del gruppo.
Non richiede nuove dipendenze e legge soltanto lo stato OVS.

- `b`: salva i contatori attuali di flow e bucket come riferimento **prima del test**.
- Esegui il caso con `packet_io.py` nella CLI Mininet, poi `r` per aggiornare:
  `Delta pkt` mostra gli incrementi rispetto al riferimento, anche per i bucket.
- `a`: alterna tutte le regole e quelle con pacchetti.
- Inserisci l'ID di una regola per vedere match, azioni e delta byte leggibili.
- `s`: scegli un altro leaf/spine e ricrea il riferimento; `q`: esci.

Per aprire direttamente leaf1 o ottenere una singola schermata:

```bash
sudo .venv/bin/python tests/network/flow_visualizer.py --switch leaf1
sudo .venv/bin/python tests/network/flow_visualizer.py --switch spine1 --once
```

Puoi anche leggere un dump salvato (o il testo copiato dal terminale), senza sudo:

```bash
.venv/bin/python tests/network/flow_visualizer.py --file /tmp/leaf1-flows.txt --active
```

La modalità `--file` legge solo i flow, senza informazioni sui gruppi.
Assenza di gruppi, errori di lettura e contatori azzerati sono indicati nella
schermata. Con un riferimento invalido o delta totale nullo la percentuale dei
bucket non viene calcolata.

I contatori sono passaggi nelle regole: un pacchetto può essere contato in più
tabelle. `ARP al controller` conta le richieste, non prova che il proxy abbia
risposto; controlla anche l'output di `packet_io.py`. `reset` indica contatori
diminuiti o regole reinstallate; `nuova` indica una regola assente nel riferimento.


### Verifica manuale ECMP

Il caso `ecmp-udp` invia più flussi UDP: cambia la porta sorgente da `40000` in
poi e mantiene la porta destinazione `9000`. I valori predefiniti sono 100
flussi e 10 pacchetti per flusso. Gli indirizzi restano configurabili con le
opzioni già presenti; l'output riporta tuple e pacchetti effettivamente inviati.

1. Nel visualizzatore scegliere `leaf1` e premere `b` per salvare il riferimento.
2. Dalla CLI Mininet inviare il traffico autorizzato h2 → h6:

   ```text
   h2 .venv/bin/python tests/network/packet_io.py --iface h2-eth0 --case ecmp-udp --flows 100 --packets-per-flow 10
   ```

3. Nel visualizzatore premere `r` e leggere i due bucket del gruppo `1`.
   Entrambi devono avere un delta pacchetti positivo. Registrare contatori
   prima/dopo, traffico offerto, percentuali osservate e versione OVS.

Ripetere la stessa misura scegliendo `leaf2` per h6 → h10 e `leaf3` per h10 → h2;
i comandi completi sono nella [procedura ECMP](docs/pipeline.md#verifica-manuale-ecmp).
Un risultato diverso da 50/50 può essere corretto: i pesi sono uguali, ma la
selezione dipende dall'hash e dalla configurazione OVS. Un solo ping non deve
necessariamente attraversare entrambi gli spine.

`--listen` continua a catturare soltanto IPv4 con protocollo 253: non è il
listener per il traffico UDP di `ecmp-udp`. La distribuzione si verifica dai
bucket, la consegna e l'isolamento con le prove IPv4/ARP precedenti. Non viene
aggiunto `test_ecmp.py`; i test automatici esistenti verificano la configurazione
e le regressioni. La tolleranza ai guasti degli uplink richiede un lavoro separato.
