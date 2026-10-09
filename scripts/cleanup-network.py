"""Rimuove vecchi nodi e link Good-Fences, preservando il controller Ryu."""
import json
import os
import re
import signal
import subprocess


SWITCH_NAME = re.compile(r'(?:leaf|spine)[1-9][0-9]*')
NODE_NAME = re.compile(r'(?:h|leaf|spine)[1-9][0-9]*|c0')
INTERFACE_NAME = re.compile(r'(?:h|leaf|spine)[1-9][0-9]*-eth[0-9]+')


def run(*args):
    return subprocess.check_output(args, text=True)


def cleanup():
    # Mininet lancia ogni nodo in una shell con un proprio process group.
    # I nomi hN/leafN/spineN sono riservati a questa simulazione.
    processes = run('ps', '-eo', 'pid=,args=')
    for line in processes.splitlines():
        pid_text, _, command = line.strip().partition(' ')
        args = command.split()
        if (len(args) != 5 or args[:4] !=
                ['bash', '--norc', '--noediting', '-is']):
            continue
        if not args[4].startswith('mininet:'):
            continue
        if not NODE_NAME.fullmatch(args[4][len('mininet:'):]):
            continue
        pid = int(pid_text)
        try:
            if os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # Il nodo potrebbe essere gia' terminato.

    bridges = run('ovs-vsctl', '--timeout=5', 'list-br').splitlines()
    for bridge in bridges:
        if SWITCH_NAME.fullmatch(bridge):
            run('ovs-vsctl', '--timeout=5', '--if-exists', 'del-br', bridge)

    # Rileggere i link dopo ogni rimozione: eliminare una veth elimina il peer.
    while True:
        links = json.loads(run('ip', '-j', 'link', 'show'))
        stale = next((link['ifname'] for link in links
                      if INTERFACE_NAME.fullmatch(link['ifname'])), None)
        if stale is None:
            break
        run('ip', 'link', 'delete', 'dev', stale)


if __name__ == '__main__':
    cleanup()
