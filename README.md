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


## Sprint 1.3: forwarding unicast isolato

IPv4 untagged dalle porte assegnate viene ammesso solo con MAC/IP sorgenti del
modello e riceve la VLAN del tenant in tabella 0. La tabella 1 ammette soltanto
destinazioni con VLAN, MAC e IP coerenti con un host del tenant; la tabella 2
consegna sul leaf locale togliendo la VLAN oppure inoltra verso spine1.
Il solo uplink attivo è `hosts_per_leaf + 1` (porta 5 nella configurazione
attuale). Gli altri uplink e spine2 restano in DROP. Dal primo uplink si può
consegnare solo a host locali: non esiste un percorso uplink → uplink.
Spine1 conserva la VLAN e inoltra sul leaf di destinazione, mai sulla porta di
ingresso. Spare e ingressi non ammessi ricadono nel default DROP a priorità 0.
Non sono introdotti ECMP, meter, gruppi, NORMAL o FLOOD.

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
Ogni comando invia un frame e mostra richiesta e risposte con MAC, IP,
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
quando non è specificato. Nella procedura dello sprint sono riportati gli
attacchi cross-tenant e i percorsi leaf/spine attesi.
Per nuovi scenari basta aggiungere un nome a `CASES` e costruire il relativo
pacchetto in `build_packet`.

### Visualizzatore dei contatori leaf/spine

Da un altro terminale Linux, con la rete avviata:

```bash
sudo .venv/bin/python tests/network/flow_visualizer.py
```

Scegli il numero o il nome dello switch. Il visualizzatore mostra un riepilogo
per tabella/tipo di regola e i contatori delle singole regole. Non richiede nuove
dipendenze e legge soltanto lo stato OVS.

- `b`: salva i contatori attuali come riferimento **prima del test**.
- Esegui il caso con `packet_io.py` nella CLI Mininet, poi `r` per aggiornare:
  `Delta pkt` mostra gli incrementi rispetto al riferimento.
- `a`: alterna tutte le regole e quelle con pacchetti.
- Inserisci l'ID di una regola per vedere match, azioni e delta byte leggibili.
- `s`: scegli un altro leaf/spine; `q`: esci.

Per aprire direttamente leaf1 o ottenere una singola schermata:

```bash
sudo .venv/bin/python tests/network/flow_visualizer.py --switch leaf1
sudo .venv/bin/python tests/network/flow_visualizer.py --switch spine1 --once
```

Puoi anche leggere un dump salvato (o il testo copiato dal terminale), senza sudo:

```bash
.venv/bin/python tests/network/flow_visualizer.py --file /tmp/leaf1-flows.txt --active
```

I contatori sono passaggi nelle regole: un pacchetto può essere contato in più
tabelle. `ARP al controller` conta le richieste, non prova che il proxy abbia
risposto; controlla anche l'output di `packet_io.py`. `reset` indica contatori
diminuiti o regole reinstallate; `nuova` indica una regola assente nel riferimento.
