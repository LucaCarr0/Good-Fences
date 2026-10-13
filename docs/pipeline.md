# Sprint 1.2: classificazione e anti-spoofing

## Politica e percorso

Il modello fornisce `(DPID, porta) -> (tag, host)` e `(tag, IP) -> host`.
VLAN mancanti/non valide, IPv4 mancanti/non validi e porte assegnate più volte
impediscono l'installazione. Gli IP possono essere uguali tra tenant diversi.
I binding sono statici e caricati all'avvio.

ARP resta untagged in tabella 0: la porta assegnata, il MAC Ethernet, il MAC ARP
e l'IP ARP sorgente devono corrispondere al binding. Il proxy ripete i controlli,
verifica il formato Ethernet/IPv4 ARP e risponde solo a destinazioni del tenant.
Probe (sorgente 0.0.0.0), gratuitous ARP (sorgente uguale alla destinazione),
reply spontanee e richieste malformate non producono risposte. La reply del proxy
è untagged e viene inviata soltanto alla porta richiedente, senza flooding.

IPv4 dalle porte host richiede porta, MAC e IP sorgente configurati e assenza di
VLAN. La tabella 0 applica il tag del modello e passa alla 1, poi alla 2: qui
il pacchetto termina in DROP. Il controllo riguarda la sorgente, non ancora
l'appartenenza della destinazione al tenant. Dagli uplink sono ammessi solo
IPv4 con VLAN nota: non viene applicato il binding delle porte host alla fabric.
IPv6 e gli altri protocolli non hanno regole permissive.

| Switch/tabella | Priorità | Match | Istruzioni |
| --- | ---: | --- | --- |
| Leaf/0 | 400 | Porta host, VLAN presente, anche VID 0 | DROP |
| Leaf/0 | 400 | Porta host, EtherType residuo 0x8100/0x88a8/0x9100 | DROP |
| Leaf/0 | 350 | Porta assegnata, ARP untagged, SPA zero oppure TPA uguale all'IP configurato | DROP |
| Leaf/0 | 300 | ARP request untagged, porta e sorgenti Ethernet/ARP del binding | Controller, frame completo |
| Leaf/0 | 200 | IPv4 untagged, porta/MAC/IP sorgenti del binding | APPLY push VLAN 0x8100, set VLAN tenant; goto 1 |
| Leaf/0 | 200 | Uplink, IPv4, VLAN nota | goto 1 |
| Leaf/1 | 300 | ARP | DROP |
| Leaf/1 | 200 | IPv4, VLAN nota | goto 2 |
| Leaf/0,1,2 | 0 | Table-miss | DROP |
| Spine/0 | 0 | Table-miss | DROP |

Non ci sono DROP per porta a priorità 100: spare, ingressi sconosciuti e sorgenti
non ammesse ricadono nel table-miss. I tag annidati dalle porte host vengono
bloccati dal tag esterno oppure dall'EtherType residuo.

Cookie per leggere i contatori: `0x47460010` default DROP, `0x47460020` VLAN DROP,
`0x47460021` ARP DROP, `0x47460030` ARP controller, `0x47460040` IPv4 host,
`0x47460041` IPv4 uplink, `0x47460042` passaggio in tabella 1.

`install_base_pipeline` pulisce e installa soltanto i default DROP;
`install_sprint12_pipeline` aggiunge la politica corrente. L'app chiama il secondo.
La Barrier segue tutta l'installazione; timeout/errori impediscono ready.
Non sono introdotti forwarding, ECMP, meter, REST o provisioning dinamico.

## Avvio e ispezione manuale

Dalla radice del repository, in due terminali:

```bash
bash scripts/start-controller.sh
bash scripts/start-network.sh
```

Il primo script conserva `PYTHONPATH=src` tramite il percorso assoluto al modulo.
Attendere `ALL SWITCHES READY: 5/5`. Gli script conservano la pulizia precedente
prevista dal progetto. Tutti i comandi seguenti sono verifiche manuali: non c'è
una suite automatizzata. I risultati indicati sono attesi, non già misurati.

Dal terminale Linux (oppure anteporre `sh` dalla CLI Mininet):

```bash
sudo ovs-ofctl -O OpenFlow13 dump-flows leaf1 table=0
sudo ovs-ofctl -O OpenFlow13 dump-flows leaf1 table=1
sudo ovs-ofctl -O OpenFlow13 dump-flows leaf1 table=2
sudo ovs-ofctl -O OpenFlow13 dump-flows spine1
```

Ripetere per leaf2/leaf3 e spine2. Non devono esserci output dati, NORMAL, FLOOD,
group o meter. L'unico output nei flow è quello ARP verso il controller.

## Classificazione e ingressi: trace manuali

Dal terminale Linux:

```bash
sudo ovs-appctl ofproto/trace leaf1 'in_port=2,dl_src=00:00:00:00:00:02,dl_dst=00:00:00:00:00:06,dl_type=0x0800,nw_src=10.0.0.1,nw_dst=10.0.0.2'
```

Atteso: flow host a priorità 200, push VLAN 102, tabella 1, tabella 2, DROP finale.
Il trace simula il percorso e non incrementa i contatori dei pacchetti reali.
Ripetere cambiando un solo campo per volta:

| Variazione del comando | Risultato atteso |
| --- | --- |
| `dl_src=00:00:00:00:00:01` | Table-miss di tabella 0 |
| `nw_src=10.0.0.99` | Table-miss di tabella 0 |
| `in_port=4` | Spare: table-miss di tabella 0 |
| `in_port=LOCAL` | Ingresso non ammesso: table-miss di tabella 0 |
| Aggiungere `dl_vlan=101`, poi 102, poi 0 | DROP tagged a priorità 400 |
| `in_port=5`, aggiungere `dl_vlan=102` | Ammissione uplink, tabella 1, DROP in 2 |
| `in_port=5`, senza VLAN | Table-miss di tabella 0 |
| `in_port=5`, aggiungere `dl_vlan=999` | Table-miss di tabella 0 |

Per ARP dagli uplink:

```bash
sudo ovs-appctl ofproto/trace leaf1 'in_port=5,dl_vlan=102,dl_type=0x0806,arp_op=1,arp_spa=10.0.0.1,arp_tpa=10.0.0.2,arp_sha=00:00:00:00:00:02,arp_tha=00:00:00:00:00:00'
```

Atteso: DROP in tabella 0; nessun output controller.

## Pacchetti reali e contatori dalla CLI Mininet

Per IPv4 legittimo, leggere prima e dopo i contatori delle tre tabelle di leaf1:

```text
h2 ip neigh flush dev h2-eth0
h2 ping -n -c 3 -W 1 10.0.0.2
h2 ip neigh show 10.0.0.2
```

ARP risolve il MAC `00:00:00:00:00:06`; i dati incrementano il flow host di porta 2,
il flow VLAN 102 in tabella 1 e il table-miss in tabella 2. Il ping fallisce.

Per inviare un singolo frame esatto senza installare altre librerie, incollare
questo comando nella CLI Mininet. Usa solo Python standard e il socket del namespace h2:

```text
h2 python3 -c "import socket; s=socket.socket(socket.AF_PACKET,socket.SOCK_RAW); s.bind(('h2-eth0',0)); s.send(bytes.fromhex('0000000000060000000000020800450000140000000040fd65eb0a0000010a000002'))"
```

È IPv4 untagged con sorgente valida e protocollo sperimentale 253, senza payload.
Per gli attacchi sostituire soltanto la stringa esadecimale nel comando:

| Caso | Frame esadecimale |
| --- | --- |
| MAC falso | `0000000000060000000000010800450000140000000040fd65eb0a0000010a000002` |
| IP falso (.99), checksum corretto | `0000000000060000000000020800450000140000000040fd65890a0000630a000002` |
| VLAN 101 | `000000000006000000000002810000650800450000140000000040fd65eb0a0000010a000002` |
| VLAN 102 | `000000000006000000000002810000660800450000140000000040fd65eb0a0000010a000002` |
| VID 0 | `000000000006000000000002810000000800450000140000000040fd65eb0a0000010a000002` |
| Doppio tag 101/102 | `00000000000600000000000281000065810000660800450000140000000040fd65eb0a0000010a000002` |

Per i TPID alternativi, sostituire il primo `8100` dei frame tagged con `88a8`
o `9100`. MAC/IP falsi incrementano il table-miss in tabella 0; i tagged un DROP
con cookie `0x47460020`. Il flow host legittimo non deve incrementare.
Per spare, eseguire il comando da h4 cambiando anche il bind in `h4-eth0`:
il frame untagged termina nel table-miss. Ripetere da h8 e h12 sui rispettivi leaf.

## Regressioni ARP e catture manuali

Dalla CLI Mininet:

```text
h1 ip neigh flush dev h1-eth0
h1 ping -n -c 1 -W 1 10.0.0.2
h1 ip neigh show 10.0.0.2
h2 ip neigh flush dev h2-eth0
h2 ping -n -c 1 -W 1 10.0.0.2
h2 ip neigh show 10.0.0.2
h1 ping -n -c 1 -W 1 10.0.0.13
h1 ip neigh show 10.0.0.13
```

Atteso: h1 risolve h5 (`...:05`), h2 risolve h6 (`...:06`), h1 non risolve `.13`.
La stessa destinazione IP resta separata per tenant. Nessun ping riesce.

Avviare una cattura temporanea dalla CLI, poi inviare i frame entro 20 secondi:

```text
h2 timeout 20 tcpdump -l -e -n -i h2-eth0 arp > /tmp/good-fences-h2-arp.log 2>&1 &
```

In terminali Linux separati, catturare entrambi gli uplink e una porta host diversa:

```bash
sudo timeout 20 tcpdump -e -n -i leaf1-eth5
sudo timeout 20 tcpdump -e -n -i leaf1-eth6
sudo timeout 20 tcpdump -e -n -i leaf1-eth1 arp
```

Sui link possono esserci protocolli locali: cercare i frame con le identità inviate.
Non devono comparire ARP inoltrati o dati dell'esperimento. La cattura h2 deve
mostrare la sola reply untagged del proxy per richieste valide.

Inviare da CLI una richiesta ARP valida con il medesimo comando socket precedente,
sostituendo la stringa con:

```text
ffffffffffff000000000002080600010800060400010000000000020a0000010000000000000a000002
```

Per i casi negativi modificare un solo campo, come indicato qui. I campi del frame
sono `dst Ethernet | src Ethernet | 0806 | 0001 0800 06 04 | opcode | SHA | SPA | THA | TPA`.

| Caso | Modifica alla richiesta valida | Atteso |
| --- | --- | --- |
| Ethernet sorgente falso | Secondo MAC `000000000002` -> `000000000001` | Table-miss 0, nessuna reply |
| SHA falso | MAC dopo opcode -> `000000000001` | Table-miss 0, nessuna reply |
| SPA falso | `0a000001` -> `0a000063` | Table-miss 0, nessuna reply |
| Probe | SPA -> `00000000` | DROP priorità 350 |
| Gratuitous ARP | TPA finale -> `0a000001` | DROP priorità 350 |
| Reply spontanea | Opcode `0001` -> `0002` | Table-miss 0 |
| Formato invalido | Lunghezze `06 04` -> `05 04` | Nessuna reply dal proxy |
| Tagged | EtherType `0806` -> `810000660806` | DROP priorità 400 |

Per gratuitous reply cambiare insieme opcode e TPA: resta DROP a priorità 350.
Leggere la cattura con `h2 cat /tmp/good-fences-h2-arp.log`. Per i casi negativi
non deve esserci una reply del proxy; un incremento del flow controller per un
ARP malformato non implica una risposta: il proxy valida anche il formato.

## Riconnessione

Fermare con Ctrl-C soltanto il controller e riavviare `bash scripts/start-controller.sh`.
Lasciare la rete attiva. Attendere nuovamente `ALL SWITCHES READY: 5/5`, controllare
l'assenza di errori OpenFlow, rileggere i flow e ripetere ARP/classificazione/tagged.
I contatori ripartono dopo la reinstallazione e le vecchie eccezioni ARP non devono
restare. Registrare separatamente ciò che è stato osservato: il solo ping fallito
non dimostra isolamento perché anche il traffico legittimo termina in DROP.
