"""Dashboard locale: avvio tramite scripts/start-visualizer.sh."""
from pathlib import Path
import sys
import subprocess
import time

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import yaml

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.visualizer_support import (
    ManagedLab, available_switches, flow_rows, group_rows, load_model,
    read_switch, summarize_flows, topology_data,
)
from src.network_models import ConfigValidationError

st.set_page_config(page_title='Good-Fences Visualizer', page_icon='🕸️', layout='wide')


@st.cache_resource
def managed_lab():
    return ManagedLab()


def refresh_switch(switch, save_reference=False):
    try:
        snapshot = read_switch(switch)
    except (RuntimeError, ValueError, OSError) as error:
        st.error(f'{switch}: {error}')
        return
    st.session_state.snapshots[switch] = snapshot
    if save_reference or switch not in st.session_state.baselines:
        st.session_state.baselines[switch] = snapshot


def render_switch(switch, prefix):
    st.subheader(f'OpenFlow · {switch}')
    refresh, baseline = st.columns(2)
    if refresh.button('Aggiorna statistiche', key=f'{prefix}_refresh'):
        refresh_switch(switch)
    if baseline.button('Salva riferimento prima del test', key=f'{prefix}_baseline'):
        refresh_switch(switch, save_reference=True)
    if switch not in st.session_state.snapshots:
        refresh_switch(switch)
    snapshot = st.session_state.snapshots.get(switch)
    if snapshot is None:
        st.info('Avviare la simulazione per leggere i contatori reali.')
        return
    reference = st.session_state.baselines.get(switch)
    st.caption(f'Ultima lettura: {snapshot.captured_at} · Riferimento: '
               f'{reference.captured_at if reference else "assente"}')
    st.caption('I contatori misurano passaggi nelle regole: uno stesso pacchetto può '
               'essere contato in più tabelle. ARP al controller conta richieste, non risposte.')
    rows = flow_rows(snapshot, reference)
    tables = st.multiselect('Tabelle', sorted({row['Tabella'] for row in rows}),
                            key=f'{prefix}_tables')
    categories = st.multiselect('Tipi di regola', sorted({row['Regola'] for row in rows}),
                                key=f'{prefix}_categories')
    query = st.text_input('Filtra match o azioni (porta, IP, MAC, VLAN)', key=f'{prefix}_query')
    active = st.checkbox('Solo regole con pacchetti', key=f'{prefix}_active')
    filtered = [row for row in rows
                if (not tables or row['Tabella'] in tables)
                and (not categories or row['Regola'] in categories)
                and (not active or row['Pacchetti'])
                and (not query or query.casefold() in (row['Match'] + ' ' + row['Azioni']).casefold())]
    if filtered:
        summary = pd.DataFrame(summarize_flows(filtered))
        summary['Tabella / regola'] = summary['Tabella'].astype(str) + ' · ' + summary['Regola']
        st.bar_chart(summary.set_index('Tabella / regola')[['Pacchetti']])
        st.dataframe(pd.DataFrame(filtered).drop(columns=['OVS originale']), hide_index=True,
                     use_container_width=True)
        detail = st.selectbox('Dettaglio regola', filtered, key=f'{prefix}_detail',
                              format_func=lambda row: f'{row["ID"]} · tabella {row["Tabella"]} · {row["Regola"]}')
        st.code(detail['OVS originale'], language='text')
    else:
        st.info('Nessuna regola corrisponde ai filtri.')

    st.subheader('Gruppi e bucket ECMP')
    if snapshot.groups is None:
        st.info('Informazioni sui gruppi non disponibili per questa lettura.')
    elif snapshot.groups.error:
        st.warning(snapshot.groups.error)
    elif not snapshot.groups.groups:
        st.info('Nessun gruppo installato su questo switch.')
    else:
        buckets, notices = group_rows(snapshot, reference)
        for notice in notices:
            st.info(notice)
        st.dataframe(pd.DataFrame(buckets), hide_index=True, use_container_width=True)
        measured = [row for row in buckets if row['Quota pkt (%)'] is not None]
        if measured:
            chart = pd.DataFrame(measured)
            chart['Gruppo / bucket'] = (chart['Gruppo'].astype(str) + ' / '
                                         + chart['Bucket'].astype(str))
            st.bar_chart(chart.set_index('Gruppo / bucket')[['Delta pkt']])
        st.caption('Quota = delta pacchetti del bucket / somma dei delta del gruppo. '
                   'I pesi uguali non garantiscono una distribuzione esatta 50/50. '
                   'Contatori indisponibili e reset non vengono rappresentati come zeri.')


def topology_figure(model, nodes, links, bridges):
    positions = {}
    spacing = model.hosts_per_leaf + 2
    center = (model.leaves - 1) * spacing / 2
    for index in range(1, model.spines + 1):
        positions[f'spine{index}'] = (center + (index - (model.spines + 1) / 2) * spacing, 2)
    for index in range(1, model.leaves + 1):
        positions[f'leaf{index}'] = ((index - 1) * spacing, 1)
    for node in nodes:
        if node['kind'] in ('Host', 'Spare'):
            positions[node['name']] = (positions[node['leaf']][0]
                                        + node['port'] - (model.hosts_per_leaf + 1) / 2, 0)
    x, y = [], []
    for link in links:
        source, target = positions[link['source']], positions[link['target']]
        x.extend([source[0], target[0], None])
        y.extend([source[1], target[1], None])
    figure = go.Figure(go.Scatter(x=x, y=y, mode='lines', line={'color': '#94a3b8', 'width': 1.5},
                                hoverinfo='skip', showlegend=False))
    palette = ['#2563eb', '#e87924', '#db2777', '#16a34a', '#a855f7', '#0891b2']
    tenant_colors = {tenant.tag: palette[index % len(palette)]
                     for index, tenant in enumerate(model.tenants)}
    groups = {}
    for node in nodes:
        label = (f'Tenant {node["tenant"]} · VLAN {node["vlan"]}'
                 if node['kind'] == 'Host' else node['kind'])
        groups.setdefault(label, []).append(node)
    for label, members in groups.items():
        kind = members[0]['kind']
        color = tenant_colors.get(members[0].get('vlan'),
                                  {'Spine': '#7c3aed', 'Leaf': '#0d9488', 'Spare': '#64748b'}.get(kind))
        hover = []
        for node in members:
            if kind in ('Spine', 'Leaf'):
                state = ('rilevato' if node['name'] in bridges else 'non rilevato') if bridges is not None else 'non disponibile'
                hover.append(f'{node["name"]}<br>DPID {node["dpid"]}<br>OVS: {state}')
            else:
                hover.append(f'{node["name"]}<br>{label}<br>IP {node["ip"] or "non assegnato"}'
                             f'<br>MAC {node["mac"]}<br>{node["leaf"]}, porta {node["port"]}')
        figure.add_trace(go.Scatter(
            x=[positions[node['name']][0] for node in members],
            y=[positions[node['name']][1] for node in members],
            mode='markers+text', text=[node['name'] for node in members],
            textposition='bottom center', customdata=[node['name'] for node in members],
            hovertext=hover, hoverinfo='text', name=label,
            marker={'size': 24 if kind in ('Spine', 'Leaf') else 18, 'color': color,
                    'symbol': 'square' if kind in ('Spine', 'Leaf') else 'circle'},
            selected={'marker': {'opacity': 1}}, unselected={'marker': {'opacity': 0.65}}))
    figure.update_layout(height=450, margin={'l': 20, 'r': 20, 't': 35, 'b': 30},
                         xaxis={'visible': False}, yaxis={'visible': False, 'range': [-0.4, 2.4]},
                         clickmode='event+select', legend={'orientation': 'h', 'y': 1.15},
                         plot_bgcolor='rgba(0,0,0,0)', paper_bgcolor='rgba(0,0,0,0)')
    return figure


def select_graph_node():
    points = st.session_state.topology_graph['selection']['points']
    if points and points[-1].get('customdata'):
        st.session_state.inspected_node = points[-1]['customdata']


def render_topology(model):
    st.title('Topologia della rete')
    st.caption('Collegamenti e tenant dai file YAML. La presenza di un bridge OVS '
               'non dimostra che i collegamenti siano operativi o che la pipeline sia pronta.')
    nodes, links = topology_data(model)
    try:
        bridges = available_switches()
    except RuntimeError as error:
        bridges = None
        st.warning(f'Stato OVS non disponibile: {error}')
    st.plotly_chart(topology_figure(model, nodes, links, bridges), use_container_width=True,
                    key='topology_graph', on_select=select_graph_node, selection_mode='points')
    names = [node['name'] for node in nodes]
    if st.session_state.get('inspected_node') not in names:
        st.session_state.inspected_node = names[0]
    selected = st.selectbox('Nodo da ispezionare (oppure selezionalo nel grafo)', names,
                            key='inspected_node')
    node = next(node for node in nodes if node['name'] == selected)
    if node['kind'] in ('Leaf', 'Spine'):
        if bridges is not None and selected in bridges:
            render_switch(selected, 'topology')
        else:
            st.info('Switch configurato; bridge OVS non rilevato o non accessibile.')
    else:
        st.dataframe(pd.DataFrame([node]), hide_index=True, use_container_width=True)
    with st.expander('Porte e collegamenti configurati'):
        st.dataframe(pd.DataFrame(links), hide_index=True, use_container_width=True)


def render_tests(model, lab):
    from tests.network.packet_io import CASES

    st.title('Esecuzione test')
    st.write('Invio di pacchetti dagli host della simulazione gestita dalla GUI.')
    hosts = model.get_all_hosts_dict()
    names = sorted(hosts, key=lambda name: hosts[name].host_id)
    source = st.selectbox('Host sorgente', names, index=names.index('h2') if 'h2' in names else 0)
    targets = [name for name in names if name != source]
    if not targets:
        st.info('Servono almeno due host per configurare una destinazione.')
        return
    source_host = hosts[source]
    source_tenant = model.get_tenant_by_host_port(source_host.leaf, source_host.port)
    preferred = [name for name in targets if hosts[name].ip and source_tenant
                 and any(host.name == name for host in source_tenant.hosts)]
    target = st.selectbox('Host destinazione', targets,
                          index=targets.index(preferred[0]) if preferred else 0, key=f'target_{source}')
    target_host = hosts[target]
    case = st.selectbox('Caso di test', CASES)
    st.caption('IP e MAC vengono proposti dal modello e restano modificabili per le prove di spoofing '
               'e isolamento. Con IP sovrapposti, il MAC identifica la vittima dei test IPv4.')
    with st.form(f'packet_test_{source}_{target}_{case}'):
        left, right = st.columns(2)
        mac = left.text_input('MAC sorgente', value=source_host.mac)
        ip = left.text_input('IP sorgente', value=source_host.ip or '0.0.0.0')
        target_ip = right.text_input('IP destinazione', value=target_host.ip or '0.0.0.0')
        target_mac = right.text_input('MAC destinazione', value=target_host.mac)
        options = {'mac': mac, 'ip': ip, 'target_ip': target_ip, 'target_mac': target_mac}
        if case == 'ipv4-tagged':
            options['vlan'] = st.number_input('VLAN', min_value=1, max_value=4094,
                                              value=source_tenant.tag if source_tenant else 102)
        if case == 'ecmp-udp':
            options['flows'] = st.number_input('Flussi UDP', min_value=1, max_value=25536, value=100)
            options['packets_per_flow'] = st.number_input('Pacchetti per flusso', min_value=1,
                                                         max_value=100000, value=10)
            st.caption('Massimo 100000 pacchetti totali per invio dalla GUI.')
        elif case in ('arp-request', 'arp-probe', 'gratuitous-arp', 'rarp'):
            options['timeout'] = st.number_input('Attesa risposte (secondi)', min_value=0.1,
                                                 max_value=10.0, value=1.0)
        submitted = st.form_submit_button('Avvia test', disabled=not lab.status()['ready'])
    if not lab.status()['ready']:
        st.info('Avviare la simulazione e attendere tutti gli switch READY. Poi aggiornare la pagina.')
    if submitted:
        try:
            with st.spinner(f'Esecuzione nel namespace di {source}…'):
                st.session_state.last_test = lab.run_test(source, case, **options)
        except (RuntimeError, ValueError, OSError) as error:
            st.session_state.pop('last_test', None)
            st.error(str(error))
    result = st.session_state.get('last_test')
    if result:
        st.subheader(f'Ultimo invio · {result["case"]} · {result["iface"]}')
        st.metric('Pacchetti inviati', result['sent_packets'])
        if result['case'] in ('arp-request', 'arp-probe', 'gratuitous-arp', 'rarp'):
            st.metric('Risposte ARP/RARP osservate', len(result['replies']))
        st.info('Invio completato. Per verificare consegna e isolamento usare una cattura sul '
                'destinatario; per ECMP confrontare i delta dei bucket.')
        if 'frame' in result:
            st.code(result['frame']['description'], language='text')
        for packet in result['replies']:
            st.code(packet['description'], language='text')
        with st.expander('Risultato strutturato'):
            st.json(result)


def render_statistics(model):
    st.title('Statistiche OpenFlow')
    mode = st.radio('Vista', ['Rete', 'Singolo switch'], horizontal=True)
    configured = [f'leaf{i}' for i in range(1, model.leaves + 1)] + [
        f'spine{i}' for i in range(1, model.spines + 1)]
    if mode == 'Singolo switch':
        selected = st.selectbox('Switch', configured)
        render_switch(selected, 'statistics')
        return
    st.caption('Somme dei contatori di regola per switch. Non sono volume end-to-end '
               'né pacchetti unici: i passaggi nelle tabelle si sommano.')
    if st.button('Leggi tutti gli switch'):
        for switch in configured:
            refresh_switch(switch)
    rows = []
    for switch in configured:
        snapshot = st.session_state.snapshots.get(switch)
        if snapshot:
            rows.append({'Switch': switch, 'Regole': len(snapshot.flows),
                         'Passaggi nelle regole': sum(flow.packets for flow in snapshot.flows),
                         'Byte nelle regole': sum(flow.byte_count for flow in snapshot.flows),
                         'Ultima lettura': snapshot.captured_at})
    if rows:
        data = pd.DataFrame(rows)
        st.bar_chart(data.set_index('Switch')[['Passaggi nelle regole']])
        st.dataframe(data, hide_index=True, use_container_width=True)
    else:
        st.info('Premere «Leggi tutti gli switch» per raccogliere i contatori.')


@st.fragment(run_every=2)
def render_lab_status(lab):
    status = lab.status()
    current = (status['running'], status['ready'])
    previous = st.session_state.get('lab_state', current)
    st.session_state.lab_state = current
    if previous != current:
        st.rerun()
    if status['ready']:
        st.success(f'Simulazione pronta · {status["ready_count"]}/{status["expected_count"]} switch READY')
    elif status['running']:
        st.info(f'Ryu: {"attivo" if status["controller_alive"] else "fermo"} · '
                f'Mininet: {"attivo" if status["network_alive"] else "fermo"} · '
                f'{status["ready_count"]}/{status["expected_count"]} switch READY')
        if status['started_at'] and time.monotonic() - status['started_at'] > 30:
            st.warning('La rete non è pronta: consultare i log e arrestare la simulazione prima di riprovare.')
    else:
        st.info('Simulazione ferma')
    if status['error']:
        st.warning(status['error'])
    for name in ('network', 'controller'):
        if status[f'{name}_exit'] is not None:
            st.error(f'{name} terminato con codice {status[f"{name}_exit"]}. Consultare il log.')


def main():
    lab = managed_lab()
    if st.session_state.get('generation') != lab.generation:
        st.session_state.snapshots = {}
        st.session_state.baselines = {}
        st.session_state.pop('last_test', None)
        st.session_state.generation = lab.generation
    st.session_state.setdefault('snapshots', {})
    st.session_state.setdefault('baselines', {})
    st.sidebar.title('Good-Fences')
    page = st.sidebar.radio('Navigazione', ['Topologia', 'Test', 'Statistiche'])
    st.sidebar.caption('Una simulazione condivisa tra le schede del browser. '
                       'La chiusura della scheda lascia il laboratorio attivo.')
    start, stop = st.sidebar.columns(2)
    if start.button('Avvia rete', disabled=lab.status()['running']):
        try:
            with st.spinner('Avvio di Ryu e Mininet…'):
                lab.start()
            st.rerun()
        except (RuntimeError, ValueError, OSError, ConfigValidationError,
                yaml.YAMLError, subprocess.SubprocessError) as error:
            st.error(str(error))
    if stop.button('Arresta rete', disabled=not lab.status()['running']):
        try:
            with st.spinner('Arresto di Mininet e Ryu…'):
                lab.stop()
            st.session_state.snapshots = {}
            st.session_state.baselines = {}
            st.session_state.pop('last_test', None)
            st.rerun()
        except (RuntimeError, OSError, subprocess.SubprocessError) as error:
            st.error(str(error))
    with st.sidebar:
        render_lab_status(lab)
        st.button('Aggiorna pagina')
        with st.expander('Log Mininet'):
            st.code(lab.logs('network'), language='text')
        with st.expander('Log controller'):
            st.code(lab.logs('controller'), language='text')
    try:
        model = load_model()
    except (ConfigValidationError, yaml.YAMLError, TypeError, KeyError, ValueError, OSError) as error:
        st.error(f'Configurazione non valida: {error}')
        return
    if page == 'Topologia':
        render_topology(model)
    elif page == 'Test':
        render_tests(model, lab)
    else:
        render_statistics(model)


if __name__ == '__main__':
    main()
