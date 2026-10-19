"""UDP a tuple fisse, finestre comuni CLOCK_MONOTONIC e contatori osservati.

Ogni namespace Mininet condivide l'orologio del kernel. I ricevitori attribuiscono
i pacchetti al secondo di INVIO codificato nel payload, includendo quelli giunti
durante il drain. Nessuna stima di perdita basata sul solo bitrate configurato.
"""
import argparse
import json
import math
from pathlib import Path
import socket
import struct
import time

HEADER = struct.Struct('!4sQd')
MAGIC = b'GFQ1'


def send(args):
    intervals = [{'second': i, 'packets': 0, 'bytes': 0} for i in range(args.duration)]
    begin = args.start_at + args.warmup
    end = begin + args.duration
    spacing = args.payload * 8 / args.bitrate
    sequence = 0
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as channel:
        channel.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
        channel.bind((args.bind, args.source_port))
        channel.connect((args.peer, args.port))
        while True:
            deadline = args.start_at + sequence * spacing
            now = time.monotonic()
            if now >= end or deadline >= end:
                break
            if deadline > now:
                time.sleep(deadline - now)
            sent_at = time.monotonic()
            if sent_at >= end:
                break
            payload = HEADER.pack(MAGIC, sequence, sent_at) + bytes(args.payload - HEADER.size)
            channel.send(payload)
            index = math.floor(sent_at - begin)
            if 0 <= index < args.duration:
                intervals[index]['packets'] += 1
                intervals[index]['bytes'] += len(payload)
            sequence += 1
    return {'intervals': intervals, 'start_at': args.start_at, 'finished_at': time.monotonic()}


def receive(args):
    intervals = [{'second': i, 'packets': 0, 'bytes': 0, 'jitter_ms': None,
                  'duplicates': 0, 'out_of_order': 0} for i in range(args.duration)]
    begin = args.start_at + args.warmup
    end = begin + args.duration + args.drain
    seen, previous_transit, jitter, highest = set(), None, 0., -1
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as channel:
        channel.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
        channel.bind((args.bind, args.port))
        channel.settimeout(0.1)
        Path(args.ready_file).write_text('ready')
        while time.monotonic() < end:
            try:
                payload, peer = channel.recvfrom(65535)
            except socket.timeout:
                continue
            arrival = time.monotonic()
            if peer != (args.peer, args.source_port) or len(payload) != args.payload:
                continue
            magic, sequence, sent_at = HEADER.unpack_from(payload)
            if magic != MAGIC or not math.isfinite(sent_at):
                continue
            index = math.floor(sent_at - begin)
            if not 0 <= index < args.duration:
                continue
            row = intervals[index]
            if sequence in seen:
                row['duplicates'] += 1
                continue
            seen.add(sequence)
            row['out_of_order'] += int(sequence < highest)
            highest = max(highest, sequence)
            transit = arrival - sent_at
            if previous_transit is not None:
                jitter += (abs(transit - previous_transit) - jitter) / 16
            previous_transit = transit
            row['packets'] += 1
            row['bytes'] += len(payload)
            row['jitter_ms'] = jitter * 1000
    return {'intervals': intervals, 'start_at': args.start_at, 'drain_seconds': args.drain}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['send', 'receive'])
    parser.add_argument('--bind', required=True)
    parser.add_argument('--peer', required=True)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--source-port', type=int, required=True)
    parser.add_argument('--bitrate', type=int, default=15000000)
    parser.add_argument('--payload', type=int, default=1200)
    parser.add_argument('--start-at', type=float, required=True)
    parser.add_argument('--warmup', type=int, default=3)
    parser.add_argument('--duration', type=int, default=30)
    parser.add_argument('--drain', type=float, default=2)
    parser.add_argument('--ready-file')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    if (not HEADER.size <= args.payload <= 1400 or args.duration < 1 or args.warmup < 0
            or args.bitrate < 1 or not 1 <= args.port <= 65535
            or not 1 <= args.source_port <= 65535 or not math.isfinite(args.start_at)
            or not math.isfinite(args.drain) or args.drain < 0
            or (args.mode == 'receive' and not args.ready_file)):
        parser.error('Parametri del traffico non validi.')
    result = send(args) if args.mode == 'send' else receive(args)
    Path(args.output).write_text(json.dumps(result))


if __name__ == '__main__':
    main()
