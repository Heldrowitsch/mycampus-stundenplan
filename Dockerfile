# Bauanleitung für den Internet-Server (Render).
# Ein "Docker-Image" ist ein kleiner, fertig eingerichteter Linux-Rechner,
# in dem nur unser Konnektor läuft.

FROM python:3.13-slim

# curl wird zum Herunterladen des Kalenders gebraucht (siehe stundenplan.py)
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Aus Sicherheitsgründen nicht als Administrator (root) laufen, sondern als normaler Benutzer
RUN useradd -m -u 1000 user
USER user
ENV PATH="/home/user/.local/bin:$PATH"
WORKDIR /home/user/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

COPY --chown=user server.py stundenplan.py ./

# Zwischenspeicher nach /tmp (wird bei jedem Neustart geleert)
ENV DATEN_ORDNER=/tmp
# Render gibt den Port selbst über PORT vor; 7860 gilt nur, wenn nichts vorgegeben ist
ENV PORT=7860

CMD ["python", "server.py"]
