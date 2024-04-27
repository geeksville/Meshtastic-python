rm log_*
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
PYTHONPATH=$SCRIPT_DIR/.. python3 -m meshtastic "$@"
