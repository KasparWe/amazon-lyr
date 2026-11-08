#!/usr/bin/env bash
set -euo pipefail

# ─── amazon-lyr installer ────────────────────────────────────────────────
# git clone https://github.com/KasparWe/amazon-lyr && bash amazon-lyr/install.sh
# (hardened fork: installs from KasparWe/amazon-lyr, never from upstream)
#
# Production install path (family convention: reddit-lyr / threads-lyr):
#   1. uv tool install  — isolated CLI on PATH, upgrades via `uv tool upgrade`
#   2. pipx             — fallback when only pipx exists
#   3. pip --user       — last resort
# Zero-install alternative (no persistence):
#   uvx --from git+https://github.com/KasparWe/amazon-lyr amazon-lyr --version
# ──────────────────────────────────────────────────────────────────────────

REPO="https://github.com/KasparWe/amazon-lyr.git"
PKG_SPEC="git+${REPO}"
BIN="amazon-lyr"
MIN_PYTHON_VERSION="3.11"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
info()  { echo -e "${GREEN}▸${NC} $*"; }
warn()  { echo -e "${YELLOW}▸${NC} $*"; }
err()   { echo -e "${RED}▸${NC} $*" >&2; }
step()  { echo -e "${BLUE}▸${NC} $*"; }

check_python() {
    local py=""
    for cmd in python3 python3.13 python3.12 python3.11; do
        if command -v "$cmd" &>/dev/null; then
            if "$cmd" -c "import sys; exit(0 if sys.version_info >= (3, 11) else 1)" 2>/dev/null; then
                py="$cmd"; break
            fi
        fi
    done
    if [[ -z "$py" ]]; then
        err "Python ${MIN_PYTHON_VERSION}+ not found. Install it first: https://www.python.org/downloads/"
        exit 1
    fi
    info "Found Python: $($py --version)"
}

install_uv() {
    step "Installing uv…"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
}

install_with_uv() {
    step "Installing with uv tool (isolated environment)…"
    command -v uv &>/dev/null || install_uv
    command -v uv &>/dev/null || return 1
    uv tool install --force "${PKG_SPEC}"
}

install_with_pipx() {
    command -v pipx &>/dev/null || return 1
    step "Installing with pipx…"
    pipx install --force "${PKG_SPEC}"
}

install_with_pip() {
    step "Falling back to pip --user install…"
    python3 -m pip install --user --upgrade "${PKG_SPEC}" || {
        err "pip install failed."
        exit 1
    }
    warn "Ensure ~/.local/bin is on your PATH to use ${BIN}."
}

verify_install() {
    if command -v "${BIN}" &>/dev/null; then
        echo ""
        info "✔ ${BIN} installed at $(command -v "${BIN}") ($(${BIN} --version))"
        cat <<EOF

  Get started:
    ${BIN}                          # home view + next steps
    ${BIN} login --interactive          # sign in in a Chrome window (one-time)
    ${BIN} serve                    # start the MCP server

  MCP config:
    {"mcpServers": {"amazon": {"command": "${BIN}", "args": ["serve"]}}}

  Upgrade later:  uv tool upgrade amazon-lyr
  Zero-install:   uvx --from ${PKG_SPEC} ${BIN} --help
EOF
        echo ""
    else
        err "'${BIN}' not found on PATH after install. Open a new shell and run: ${BIN} --version"
        exit 1
    fi
}

main() {
    echo ""
    step "Installing ${BIN} from ${REPO}"
    check_python

    if command -v "${BIN}" &>/dev/null; then
        warn "Existing installation found at $(command -v "${BIN}") — upgrading."
    fi

    install_with_uv || install_with_pipx || install_with_pip
    verify_install
}

main "$@"
