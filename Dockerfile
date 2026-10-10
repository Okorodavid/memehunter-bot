# Runs the bot anywhere that takes a container: Railway, Fly, Render, a VPS,
# Oracle Cloud's always-free ARM box, or Docker on your own machine.
FROM python:3.12-slim

# Unbuffered so logs appear in the host's log viewer immediately rather than
# sitting in a buffer until the process exits.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependencies first, so a code change does not re-install everything.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The database lives on a mounted volume, NOT in the image. Without this the
# container's filesystem is wiped on every redeploy, taking your tracked
# wallets, position baselines and the whole leaderboard history with it.
ENV DB_PATH=/data/memehunter.db
VOLUME ["/data"]

# Fail the build rather than the deploy if something is broken. These checks
# are offline and need no keys.
RUN python selftest.py

CMD ["python", "main.py"]
