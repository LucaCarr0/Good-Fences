# Immagine con Mininet, Open vSwitch, Ryu e GUI. Ryu 4.34 richiede Python 3.9.
FROM python:3.9-slim-bookworm

# mnexec e gli strumenti di rete usati da Mininet; ps e sudo servono agli script.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      mininet openvswitch-switch iproute2 iputils-ping net-tools kmod procps sudo \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dipendenze prima del codice, per riusare la cache di build.
COPY requirements.txt build-constraints.txt ./
RUN python -m venv .venv \
 && .venv/bin/python -m pip install --no-cache-dir --upgrade pip \
 && .venv/bin/python -m pip install --no-cache-dir \
      --build-constraint build-constraints.txt -r requirements.txt

COPY . .

# Nel container la GUI ascolta su tutte le interfacce; compose la pubblica solo in locale.
ENV GOOD_FENCES_GUI_ADDRESS=0.0.0.0
EXPOSE 8501

ENTRYPOINT ["bash", "scripts/docker-entrypoint.sh"]
CMD ["bash", "scripts/start-visualizer.sh"]
