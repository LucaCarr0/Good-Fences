# Visualizzatore Good-Fences

## Avvio e arresto

Prerequisiti: Linux, Mininet, Open vSwitch in esecuzione e la `.venv` del progetto.
Le dipendenze GUI sono opzionali e compatibili con Python 3.9:

```bash
.venv/bin/python -m pip install -r requirements-visualizer.txt
bash scripts/start-visualizer.sh
```

Lo script richiede sudo e apre il server soltanto su <http://127.0.0.1:8501>.
Premere **Avvia rete** nella barra laterale: la GUI avvia Ryu sulla porta 6653 e
Mininet senza CLI. Attendere **Simulazione pronta**, con tutti gli switch READY.
Non eseguire separatamente `start-controller.sh` o `start-network.sh`.

Se una simulazione è già attiva dal terminale, uscire da Mininet con `exit` e
fermare Ryu con `Ctrl+C`. La GUI rifiuta l'avvio se trova bridge leaf/spine o la
porta 6653 occupata. Per i residui di una simulazione già chiusa resta disponibile
`sudo .venv/bin/python scripts/cleanup-network.py`: rimuove tutti i nodi e bridge
con nomi riservati Good-Fences, quindi non usarlo su una rete da preservare.

**Arresta rete** arresta prima Mininet e poi Ryu. La simulazione è condivisa tra
le schede dello stesso server; chiudere una scheda non arresta la rete. Arrestare
la rete dalla GUI prima di terminare il server Streamlit con `Ctrl+C`.

## Topologia

Il grafo mostra spine, leaf, host e spare con colori per tenant e indicazione
della VLAN. Nodi e porte provengono da `NetworkModel`, come in `SpineLeafTopo`.
Selezionare un nodo nel grafo o nel menu per i dettagli: gli switch presenti in
OVS aprono l'ispezione delle statistiche. La tabella espandibile mostra le porte
di entrambe le estremità dei collegamenti.

I collegamenti sono quelli configurati nei file YAML; la presenza dei bridge
OVS è rilevata separatamente e non prova che i link o la pipeline siano operativi.
Arrestare la simulazione prima di modificare i file YAML e riavviarla dopo.

## Test

Scegliere sorgente, destinazione e uno dei sette casi esistenti. IP e MAC sono
proposti dal modello e restano modificabili per spoofing e isolamento. Con IP
sovrapposti, specificare il MAC della vittima dei test IPv4. Sono selezionabili
anche gli spare. L'interfaccia è derivata dall'host (`hN-eth0`).

La GUI esegue `packet_io.py --json` nel namespace della sorgente con
`mnexec -a PID`, usando il PID pubblicato da Mininet. Non interpreta il testo
della CLI. I test sono abilitati solo dopo le Barrier OpenFlow di tutti gli switch.
Sono ammessi al massimo 100000 pacchetti per invio ECMP dalla GUI; la CLI
mantiene i propri limiti precedenti.

Il risultato riporta gli invii completati e le risposte ARP/RARP osservate.
**Un invio completato non prova la consegna o l'isolamento**: per IPv4 serve una
cattura sul destinatario. La GUI non orchestra il listener. Per ECMP usare i
delta dei bucket. In caso di timeout può essere stato inviato traffico parziale.

## Statistiche e riferimenti

La pagina **QoS** aggiunge test isolato, confronto noisy neighbor ripetuto,
annullamento e download CSV/grafici/dati grezzi. La vista singolo switch mostra
anche capacità, configurazione e delta dei meter. La [guida QoS](qos.md)
descrive budget per tenant per leaf, burst, procedura manuale e validità delle
misure. La modalità QoS iniziale si sceglie prima di avviare la rete.

La vista rete legge gli switch su richiesta e riporta gli istanti delle letture;
non è uno snapshot atomico della fabric. Le somme sono passaggi nelle regole,
anche ripetuti tra tabelle/switch, non pacchetti unici o throughput end-to-end.
La vista singolo switch filtra per tabella, categoria, match, azioni e presenza
di pacchetti. I grafici rispettano i filtri.

1. Scegliere lo switch e premere **Salva riferimento prima del test**.
2. Eseguire il caso nella pagina **Test**.
3. Tornare allo switch e premere **Aggiorna statistiche**.
4. Leggere delta dei flow e dei bucket.

La prima lettura crea un riferimento iniziale. I riferimenti sono separati per
switch e conservati passando tra viste nella stessa sessione browser. Ogni nuovo
avvio invalida i dati della simulazione precedente.

`nuova` e `reset` distinguono regole assenti nel riferimento e contatori
diminuiti/reinstallati. I delta non validi rimangono vuoti, non zero. Le quote dei
bucket usano la somma dei delta del gruppo; non vengono calcolate con contatori
mancanti, gruppo modificato o incremento nullo. Pesi uguali non garantiscono
50/50. Gli errori dei gruppi non nascondono i flow disponibili.

## Architettura e verifiche

`visualizer.py` contiene l'interfaccia Streamlit e `src/visualizer_support.py`
gestisce dati e processi, riusando le API strutturate di `flow_visualizer.py`.
`packet_io.execute_case()` restituisce dati serializzabili senza stampare; la CLI
conserva la presentazione leggibile e aggiunge `--json`. La cattura segnala la
disponibilità del listener tramite callback facoltativo.

Mininet usa `datacenter.py --headless`, con SIGTERM e `net.stop()` in `finally`.
Gli host e lo stato Ryu sono pubblicati in file JSON atomici in una directory
temporanea privata. Il controller pubblica READY dopo le Barrier già esistenti
e aggiorna lo stato su errori, timeout e disconnessioni. Senza la variabile
`GOOD_FENCES_STATUS_FILE` il funzionamento CLI resta invariato.

Test automatici senza avviare la rete:

```bash
PYTHONPATH=src .venv/bin/python -B -m unittest discover -s tests/controller -v
.venv/bin/python -B -m unittest discover -s tests/network -v
PYTHONPATH=src:. .venv/bin/python -B -m unittest discover -s tests/visualizer -v
```

I test GUI verificano navigazione, valori proposti, porte, PID, stato READY,
arresto e contatori con processi/socket simulati. La prova completa con
Mininet/OVS va effettuata dall'interfaccia.
