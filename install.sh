#!/bin/sh
# Sets up Task Manager in the folder it was cloned to: a Python environment, and an icon in the app grid.
#
#   ./install.sh               makes venv/ with requirements.txt, then adds "Task Manager" to the app grid
#   ./install.sh --uninstall   takes the icon away again (this folder and its venv/ stay)
set -e
here=$(cd "$(dirname "$0")" && pwd -P)
apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
desktop="$apps/taskmgr.desktop"

if [ "$1" = "--uninstall" ]; then
    rm -f "$desktop"
    echo "Removed $desktop"
    exit 0
fi

case "$here" in
    *[\"\`\$\\%]*)  # the app grid's file format would need these escaped; not worth the risk
        echo "This folder's path has one of \" \` \$ \\ % in it. Move the folder and run ./install.sh again." >&2
        exit 1 ;;
esac

if [ ! -x "$here/venv/bin/python" ]; then
    echo "Making the Python environment in $here/venv"
    python3 -m venv "$here/venv"
    "$here/venv/bin/pip" install --quiet -r "$here/requirements.txt"
fi

# GNOME Terminal opens it at a size that fits the Performance page; any other terminal at its own size.
if command -v gnome-terminal >/dev/null 2>&1; then
    run="gnome-terminal --geometry=130x40 --title=\"Task Manager\" -- \"$here/taskmgr\""
    terminal=false
else
    run="\"$here/taskmgr\""
    terminal=true
fi

mkdir -p "$apps"
cat > "$desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Task Manager
Comment=Processes, each CPU core, memory, disks, network and GPUs, laid out like the Windows Task Manager
Keywords=taskmgr;process;cpu;core;gpu;memory;monitor;工作管理員;
Exec=$run
Icon=$here/icon.svg
Terminal=$terminal
Categories=System;Monitor;
StartupNotify=true
EOF
echo "Added Task Manager to the app grid ($desktop)"
echo "Run it from there, or here with ./taskmgr"
