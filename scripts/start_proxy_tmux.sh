#!/bin/zsh
set -eu

readonly SESSION_NAME="${LLM_PROXY_TMUX_SESSION:-LLM}"
readonly SCRIPT_DIR="${0:A:h}"
readonly REPO_DIR="${SCRIPT_DIR:h}"
readonly PYTHON_BIN="${LLM_PROXY_PYTHON:-${REPO_DIR}/.venv/bin/python}"
readonly ENTRYPOINT="${REPO_DIR}/start_proxy.py"
readonly CONSOLE_LOG="${LLM_PROXY_CONSOLE_LOG:-${REPO_DIR}/logs/proxy-console.log}"

if [[ -n "${TMUX_BIN:-}" ]]; then
  tmux_bin="${TMUX_BIN}"
elif [[ -x /opt/homebrew/bin/tmux ]]; then
  tmux_bin="/opt/homebrew/bin/tmux"
elif [[ -x /usr/local/bin/tmux ]]; then
  tmux_bin="/usr/local/bin/tmux"
else
  tmux_bin="$(command -v tmux || true)"
fi

if [[ -z "${tmux_bin}" || ! -x "${tmux_bin}" ]]; then
  print -u2 "tmux is not installed; set TMUX_BIN or install tmux first"
  exit 1
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
  print -u2 "Python virtual environment not found: ${PYTHON_BIN}"
  exit 1
fi

if [[ ! -f "${ENTRYPOINT}" ]]; then
  print -u2 "Proxy entrypoint not found: ${ENTRYPOINT}"
  exit 1
fi

if "${tmux_bin}" has-session -t "=${SESSION_NAME}" 2>/dev/null; then
  exit 0
fi

/bin/mkdir -p "${CONSOLE_LOG:h}"
proxy_command="set -o pipefail; ${(q)PYTHON_BIN} -u ${(q)ENTRYPOINT} --ui 2>&1 | /usr/bin/tee -a ${(q)CONSOLE_LOG}"

exec "${tmux_bin}" new-session -d -s "${SESSION_NAME}" -c "${REPO_DIR}" "${proxy_command}"
