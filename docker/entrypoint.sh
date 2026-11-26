#!/bin/sh
# Prepares the /data volume, then starts what the image was asked to run (see DOCKER.md).
set -eu

DATA=/data
SRC=/import   # the installation folder, mounted read-only (optional after the first start)

mkdir -p "$DATA/webui" "$DATA/downloads" "$DATA/temp" "$DATA/home" "$DATA/config" "$DATA/esn" "$DATA/logs"

# seed_file <file in the project> <name in /data>: copied once, never overwritten.
seed_file() {
    if [ ! -e "$DATA/$2" ] && [ -f "$SRC/$1" ]; then
        echo "First start: copying $1"
        cp "$SRC/$1" "$DATA/$2"
    fi
}

# seed_dir <folder in the project> <name in /data>: copied while /data's is still empty.
seed_dir() {
    mkdir -p "$DATA/$2"
    if [ -d "$SRC/$1" ] && [ -z "$(ls -A "$DATA/$2")" ]; then
        echo "First start: copying $1"
        cp -R "$SRC/$1/." "$DATA/$2/"
    fi
}

seed_file vinetrimmer/vinetrimmer.yml vinetrimmer.yml
seed_file vinetrimmer/key_store.db key_store.db
seed_file webui/webui_settings.json webui/webui_settings.json
seed_file webui/pins.json webui/pins.json
seed_dir vinetrimmer/services services
seed_dir vinetrimmer/config/services config/services
for name in devices cookies certs cache; do
    seed_dir "vinetrimmer/$name" "$name"
done

# The services are code: while the project folder is mounted, take its current version on
# every start, so updating the folder updates the container. Scripts uploaded on the web
# UI's Services tab (listed in .webui-uploads) and other files are left alone.
if [ -d "$SRC/vinetrimmer/services" ]; then
    uploads="$DATA/services/.webui-uploads"
    (cd "$SRC/vinetrimmer/services" &&
        find . -name '*.py' ! -path '*/__pycache__/*' | while IFS= read -r file; do
            if [ -f "$uploads" ] && grep -qxF "${file#./}" "$uploads"; then continue; fi
            cp --parents "$file" "$DATA/services/"
        done)
fi

# Netflix ESN files live next to the program.
for file in "$SRC/ESNID" "$SRC"/.esn-*; do
    if [ -f "$file" ] && [ ! -e "$DATA/esn/$(basename "$file")" ]; then
        cp "$file" "$DATA/esn/"
    fi
done
for file in "$DATA/esn"/* "$DATA/esn"/.esn-*; do
    if [ -f "$file" ]; then
        ln -sf "$file" "/app/$(basename "$file")" 2>/dev/null || true
    fi
done

if [ ! -s "$DATA/vinetrimmer.yml" ]; then
    printf "directories:\n  downloads: ''\n  temp: ''\n" > "$DATA/vinetrimmer.yml"
fi

if [ ! -f "$DATA/services/__init__.py" ]; then
    echo "ERROR: no services found. Mount your installation folder at /import for the first" >&2
    echo "start, or copy vinetrimmer/services into the data folder's services/ (DOCKER.md)." >&2
    exit 1
fi

# An update installed on the web UI's Update tab is kept in /data: put it back, since a
# new container starts from the image's files.
(cd /app && python -m webui.docker_update) || echo "WARNING: the kept update couldn't be restored." >&2

exec "$@"
