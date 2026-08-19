#!/bin/sh
set -eu

REPO_ARCHIVE_URL="https://github.com/chrometechhype/codexproxy/archive/refs/heads/main.zip"
PYTHON_VERSION="3.14.0"
MIN_UV_VERSION="0.11.16"
CODEX_INSTALL_URL="https://chatgpt.com/codex/install.sh"
RTK_VERSION="0.44.2"
RTK_RELEASE_BASE_URL="https://github.com/rtk-ai/rtk/releases/download/v$RTK_VERSION"
UV_INSTALL_URL="https://astral.sh/uv/install.sh"
CODEX_PROXY_MACOS_BUNDLE_ID="io.github.alishahryar1.codexproxy"
CODEX_PROXY_MACOS_OWNER_FILE=".codexproxy-owner"
CODEX_PROXY_COMMANDS="cdx-desktop cdx-server cdx-codex"

dry_run=0
voice_nim=0
voice_local=0
voice_all=0
install_codex=1
enable_rtk=0
torch_backend=""
temporary_file=""
temporary_binary=""
tool_bin=""
rtk_path=""

show_usage() {
    cat <<'USAGE'
Usage: install.sh [options]

Installs or updates CodexProxy and lets you choose which coding agents to install or verify.

Options:
  --voice-nim              Install NVIDIA NIM voice transcription support.
  --voice-local            Install local Whisper voice transcription support.
  --voice-all              Install all voice transcription backends.
  --torch-backend VALUE    Use a uv PyTorch backend, such as cu130. Requires local voice.
  --rtk                    Install and configure RTK for the selected coding agents.
  --dry-run                Print commands without running them.
  --help                   Show this help text.
USAGE
}

fail() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

installer_is_interactive() {
    [ -t 1 ] && ( : </dev/tty ) 2>/dev/null
}

prompt_yes_no() {
    question=$1
    default_answer=${2:-yes}
    case "$default_answer" in
        yes) prompt='[Y/n]' ;;
        no) prompt='[y/N]' ;;
        *) fail "Unsupported prompt default: $default_answer" ;;
    esac

    while :; do
        printf '%s %s ' "$question" "$prompt" >&4
        if ! IFS= read -r answer <&3; then
            fail "Could not read the installer selection."
        fi
        case "$answer" in
            '')
                if [ "$default_answer" = "yes" ]; then
                    return 0
                fi
                return 1
                ;;
            [Yy]|[Yy][Ee][Ss]) return 0 ;;
            [Nn]|[Nn][Oo]) return 1 ;;
            *) printf 'Please answer Y or N.\n' >&4 ;;
        esac
    done
}

choose_coding_agents() {
    selection_input=$1
    selection_output=$2
    exec 3<"$selection_input"
    exec 4>"$selection_output"

    while :; do
        if prompt_yes_no "Install or verify Codex for cdx-codex?"; then
            install_codex=1
        else
            install_codex=0
        fi

        if [ "$install_codex" -eq 1 ]; then
            break
        fi
        printf 'Select at least one coding agent.\n\n' >&4
    done

    if [ "$enable_rtk" -eq 0 ] &&
        prompt_yes_no "Enable RTK token optimization globally for the selected coding agents?" no; then
        enable_rtk=1
    fi

    exec 3<&-
    exec 4>&-
}

step() {
    printf '\n==> %s\n' "$1"
}

quote_arg() {
    case "$1" in
        *[!A-Za-z0-9_./:@%+=,-]*|"")
            escaped=$(printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g')
            printf '"%s"' "$escaped"
            ;;
        *)
            printf '%s' "$1"
            ;;
    esac
}

print_command() {
    printf '+'
    for arg in "$@"; do
        printf ' '
        quote_arg "$arg"
    done
    printf '\n'
}

run() {
    print_command "$@"
    if [ "$dry_run" -eq 1 ]; then
        return 0
    fi

    if "$@"; then
        return 0
    else
        status=$?
    fi

    fail "Command failed with exit code $status: $1"
}

cleanup() {
    if [ -n "$temporary_file" ] && [ -e "$temporary_file" ]; then
        rm -f "$temporary_file"
    fi
    if [ -n "$temporary_binary" ] && [ -e "$temporary_binary" ]; then
        rm -f "$temporary_binary"
    fi
}

trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' HUP TERM

add_path_entry() {
    [ -n "$1" ] || return 0
    case ":$PATH:" in
        *":$1:"*) ;;
        *) PATH="$1:$PATH" ;;
    esac
}

add_known_bin_directories() {
    if [ -n "${XDG_BIN_HOME:-}" ]; then
        add_path_entry "$XDG_BIN_HOME"
    fi

    if [ -n "${HOME:-}" ]; then
        add_path_entry "$HOME/.local/bin"
        add_path_entry "$HOME/.cargo/bin"
    fi

    export PATH
    hash -r 2>/dev/null || true
}

add_npm_bin_directories() {
    [ "$dry_run" -eq 0 ] || return 0
    add_known_bin_directories
    if command -v npm >/dev/null 2>&1; then
        npm_prefix=$(npm prefix -g 2>/dev/null || npm config get prefix 2>/dev/null || true)
        if [ -n "$npm_prefix" ]; then
            add_path_entry "$npm_prefix/bin"
            export PATH
            hash -r 2>/dev/null || true
        fi
    fi
}

fcc_process_ids() {
    command_name=$1

    if command -v pgrep >/dev/null 2>&1; then
        {
            pgrep -x "$command_name" 2>/dev/null || true
            pgrep -f "(^|/)${command_name}([[:space:]]|$)" 2>/dev/null || true
        } | sort -nu
        return 0
    fi

    ps -A -o pid= -o args= 2>/dev/null |
        awk -v command_name="$command_name" '
            BEGIN {
                pattern = "(^|/)" command_name "([[:space:]]|$)"
            }
            {
                process_id = $1
                sub(/^[[:space:]]*[0-9]+[[:space:]]+/, "")
                if ($0 ~ pattern) {
                    print process_id
                }
            }
        ' || true
}

assert_no_fcc_processes_running() {
    running=""
    for command_name in $CODEX_PROXY_COMMANDS; do
        process_ids=$(fcc_process_ids "$command_name")
        [ -n "$process_ids" ] || continue

        for process_id in $process_ids; do
            process="$command_name (PID $process_id)"
            if [ -n "$running" ]; then
                running="$running, $process"
            else
                running=$process
            fi
        done
    done

    if [ -n "$running" ]; then
        fail "CodexProxy is still running ($running). Stop those processes, then rerun the installer."
    fi
}

require_command() {
    if [ "$dry_run" -eq 0 ] && ! command -v "$1" >/dev/null 2>&1; then
        fail "$1 is required. Install it first, then rerun this installer."
    fi
}

download_and_run() {
    url=$1
    interpreter=$2
    label=$3
    non_interactive=${4:-0}

    if [ "$dry_run" -eq 1 ]; then
        print_command curl -fsSL "$url" -o "<temporary-script>"
        if [ "$non_interactive" -eq 1 ]; then
            printf '+ CODEX_NON_INTERACTIVE=1 '
            quote_arg "$interpreter"
            printf ' <temporary-script>\n'
        else
            print_command "$interpreter" "<temporary-script>"
        fi
        return 0
    fi

    temporary_file=$(mktemp "${TMPDIR:-/tmp}/cdx-install.XXXXXX") || fail "Unable to create a temporary file for $label."
    print_command curl -fsSL "$url" -o "$temporary_file"
    if curl -fsSL "$url" -o "$temporary_file"; then
        :
    else
        status=$?
        fail "Could not download the $label installer (curl exit code $status)."
    fi

    if [ ! -s "$temporary_file" ]; then
        fail "The downloaded $label installer was empty."
    fi

    if [ "$non_interactive" -eq 1 ]; then
        printf '+ CODEX_NON_INTERACTIVE=1 '
        quote_arg "$interpreter"
        printf ' '
        quote_arg "$temporary_file"
        printf '\n'
        if CODEX_NON_INTERACTIVE=1 "$interpreter" "$temporary_file"; then
            :
        else
            status=$?
            fail "$label installation failed with exit code $status."
        fi
    else
        print_command "$interpreter" "$temporary_file"
        if "$interpreter" "$temporary_file"; then
            :
        else
            status=$?
            fail "$label installation failed with exit code $status."
        fi
    fi

    rm -f "$temporary_file"
    temporary_file=""
}

verify_command() {
    command_name=$1
    display_name=$2

    if [ "$dry_run" -eq 1 ]; then
        print_command "$command_name" --version
        return 0
    fi

    command_path=$(command -v "$command_name" 2>/dev/null) || fail "$display_name was installed, but '$command_name' is not available on PATH."
    run "$command_path" --version
}

verify_rtk_command() {
    if [ "$dry_run" -eq 1 ]; then
        print_command env RTK_TELEMETRY_DISABLED=1 rtk --version
        print_command env RTK_TELEMETRY_DISABLED=1 rtk gain
        return 0
    fi

    rtk_path=$(command -v rtk 2>/dev/null) || fail "RTK was installed, but 'rtk' is not available on PATH."
    print_command env RTK_TELEMETRY_DISABLED=1 "$rtk_path" --version
    if ! RTK_TELEMETRY_DISABLED=1 "$rtk_path" --version; then
        fail "The 'rtk' command at $rtk_path is not a compatible Rust Token Killer installation. Remove the conflicting command from PATH, then rerun the installer."
    fi

    print_command env RTK_TELEMETRY_DISABLED=1 "$rtk_path" gain
    if ! RTK_TELEMETRY_DISABLED=1 "$rtk_path" gain; then
        fail "The 'rtk' command at $rtk_path is not a compatible Rust Token Killer installation. Remove the conflicting command from PATH, then rerun the installer."
    fi
}

select_rtk_release() {
    rtk_platform=$(uname -s)
    rtk_architecture=$(uname -m)
    case "$rtk_platform:$rtk_architecture" in
        Linux:x86_64|Linux:amd64)
            rtk_asset_name="rtk-x86_64-unknown-linux-musl.tar.gz"
            rtk_asset_sha256="d94cc2a3e57fa534892b5235a726e7eeb7523f205a5f8f48f853bfcae7be7e33"
            ;;
        Linux:aarch64|Linux:arm64)
            rtk_asset_name="rtk-aarch64-unknown-linux-gnu.tar.gz"
            rtk_asset_sha256="5cd3f7fa2697faf9e5b77a10ce4e699006e02d4752d792f06550697eb4b8e8a9"
            ;;
        Darwin:x86_64|Darwin:amd64)
            rtk_asset_name="rtk-x86_64-apple-darwin.tar.gz"
            rtk_asset_sha256="636f808db86b2cefab7db7dd9393da8b6e4721bb2ffaa0644e3ffa52d3420d81"
            ;;
        Darwin:aarch64|Darwin:arm64)
            rtk_asset_name="rtk-aarch64-apple-darwin.tar.gz"
            rtk_asset_sha256="b7c2218eca538b54e63fa594a8ce58bd3716851b01b3b0dc026515323baf6393"
            ;;
        *)
            fail "RTK $RTK_VERSION does not provide a release for $rtk_platform $rtk_architecture."
            ;;
    esac
}

install_rtk() {
    select_rtk_release
    rtk_archive_url="$RTK_RELEASE_BASE_URL/$rtk_asset_name"
    if [ "$dry_run" -eq 1 ]; then
        print_command curl -fsSL "$rtk_archive_url" -o "<temporary-archive>"
        printf '+ verify pinned SHA-256 for %s\n' "$rtk_asset_name"
        printf '+ extract rtk to %s\n' "${HOME:-~}/.local/bin/rtk"
        return 0
    fi

    [ -n "${HOME:-}" ] || fail "HOME is required to install RTK."
    temporary_file=$(mktemp "${TMPDIR:-/tmp}/cdx-rtk.XXXXXX") || fail "Unable to create a temporary RTK archive."
    print_command curl -fsSL "$rtk_archive_url" -o "$temporary_file"
    if curl -fsSL "$rtk_archive_url" -o "$temporary_file"; then
        :
    else
        status=$?
        fail "Could not download RTK $RTK_VERSION (curl exit code $status)."
    fi
    [ -s "$temporary_file" ] || fail "The downloaded RTK archive was empty."

    if command -v sha256sum >/dev/null 2>&1; then
        print_command sha256sum "$temporary_file"
        rtk_actual_sha256=$(sha256sum "$temporary_file") || fail "Could not hash the downloaded RTK archive."
    elif command -v shasum >/dev/null 2>&1; then
        print_command shasum -a 256 "$temporary_file"
        rtk_actual_sha256=$(shasum -a 256 "$temporary_file") || fail "Could not hash the downloaded RTK archive."
    else
        fail "RTK installation requires sha256sum or shasum for checksum verification."
    fi
    rtk_actual_sha256=${rtk_actual_sha256%% *}
    [ "$rtk_actual_sha256" = "$rtk_asset_sha256" ] || fail "RTK checksum verification failed for $rtk_asset_name."

    if rtk_archive_entries=$(tar -tzf "$temporary_file"); then
        :
    else
        fail "The verified RTK archive could not be inspected."
    fi
    [ "$rtk_archive_entries" = "rtk" ] || fail "The verified RTK archive did not contain exactly one root rtk executable."

    rtk_install_directory="$HOME/.local/bin"
    run mkdir -p "$rtk_install_directory"
    temporary_binary=$(mktemp "$rtk_install_directory/.rtk.XXXXXX") || fail "Unable to create a temporary RTK executable."
    print_command tar -xOzf "$temporary_file" rtk
    if tar -xOzf "$temporary_file" rtk >"$temporary_binary"; then
        :
    else
        fail "The verified RTK archive could not be extracted."
    fi
    [ -s "$temporary_binary" ] || fail "The verified RTK executable was empty."
    run chmod +x "$temporary_binary"
    run mv "$temporary_binary" "$rtk_install_directory/rtk"
    temporary_binary=""
    rm -f "$temporary_file"
    temporary_file=""
}

ensure_rtk() {
    if command -v rtk >/dev/null 2>&1; then
        printf 'RTK already found on PATH; verifying it without updating it.\n'
    else
        install_rtk
        add_known_bin_directories
    fi

    verify_rtk_command
}

run_rtk_init() {
    print_command env RTK_TELEMETRY_DISABLED=1 rtk "$@"
    if [ "$dry_run" -eq 1 ]; then
        return 0
    fi

    if RTK_TELEMETRY_DISABLED=1 "$rtk_path" "$@"; then
        return 0
    else
        status=$?
    fi

    fail "RTK configuration failed with exit code $status. Correct the reported RTK error, then rerun the installer."
}

configure_rtk_for_selected_agents() {
    [ "$enable_rtk" -eq 1 ] || return 0

    step "Installing and configuring RTK token optimization"
    ensure_rtk

    if [ "$install_codex" -eq 1 ]; then
        run_rtk_init init --global --codex
    fi
}

ensure_codex() {
    if command -v codex >/dev/null 2>&1; then
        printf 'Codex already found on PATH; verifying it.\n'
    else
        download_and_run "$CODEX_INSTALL_URL" sh "Codex" 1
        add_known_bin_directories
    fi

    verify_command codex "Codex"
}

ensure_selected_coding_agents() {
    if [ "$install_codex" -eq 1 ]; then
        step "Ensuring Codex is installed"
        ensure_codex
    fi

    if [ "$install_codex" -eq 0 ]; then
        fail "No selected coding agent was installed. Re-run the installer and choose at least one."
    fi
}

current_uv_version() {
    if output=$(uv --version); then
        :
    else
        return 1
    fi

    case "$output" in
        uv\ *) version=${output#uv } ;;
        *) version=$output ;;
    esac
    version=${version%% *}

    case "$version" in
        [0-9]*.[0-9]*.[0-9]*) printf '%s\n' "$version" ;;
        *) return 1 ;;
    esac
}

stable_version_is_supported() {
    case "$1" in
        *-*) return 1 ;;
    esac

    current=${1%%+*}
    minimum=${2%%+*}

    old_ifs=$IFS
    IFS=.
    set -- $current
    current_major=${1:-0}
    current_minor=${2:-0}
    current_patch=${3:-0}
    set -- $minimum
    minimum_major=${1:-0}
    minimum_minor=${2:-0}
    minimum_patch=${3:-0}
    IFS=$old_ifs

    case "$current_major$current_minor$current_patch$minimum_major$minimum_minor$minimum_patch" in
        *[!0-9]*) return 1 ;;
    esac

    [ "$current_major" -gt "$minimum_major" ] && return 0
    [ "$current_major" -lt "$minimum_major" ] && return 1
    [ "$current_minor" -gt "$minimum_minor" ] && return 0
    [ "$current_minor" -lt "$minimum_minor" ] && return 1
    [ "$current_patch" -ge "$minimum_patch" ]
}

verify_uv() {
    if [ "$dry_run" -eq 1 ]; then
        print_command uv --version
        return 0
    fi

    command -v uv >/dev/null 2>&1 || fail "uv was installed, but it is not available on PATH."
    version=$(current_uv_version) || fail "uv is present, but 'uv --version' did not return a valid version."
    if ! stable_version_is_supported "$version" "$MIN_UV_VERSION"; then
        fail "Stable uv $MIN_UV_VERSION or newer is required; found uv $version after installation."
    fi

    printf 'Verified uv %s.\n' "$version"
}

ensure_uv() {
    if [ "$dry_run" -eq 1 ]; then
        if command -v uv >/dev/null 2>&1; then
            print_command uv --version
            printf 'A compatible existing uv will be left unchanged; an obsolete one will be replaced by the standalone installer.\n'
        else
            printf 'uv is not installed; the current standalone uv would be installed.\n'
            download_and_run "$UV_INSTALL_URL" sh "uv"
            verify_uv
        fi
        return 0
    fi

    if command -v uv >/dev/null 2>&1; then
        version=$(current_uv_version) || fail "uv is present, but 'uv --version' did not return a valid version."
        if stable_version_is_supported "$version" "$MIN_UV_VERSION"; then
            printf 'uv %s already satisfies >=%s; leaving it unchanged.\n' "$version" "$MIN_UV_VERSION"
            return 0
        fi
        printf 'uv %s does not satisfy stable >=%s; installing the current standalone uv.\n' "$version" "$MIN_UV_VERSION"
    else
        printf 'uv is not installed; installing the current standalone uv.\n'
    fi

    download_and_run "$UV_INSTALL_URL" sh "uv"
    add_known_bin_directories
    verify_uv
}

parse_args() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --voice-nim)
                voice_nim=1
                ;;
            --voice-local)
                voice_local=1
                ;;
            --voice-all)
                voice_all=1
                ;;
            --torch-backend)
                shift
                [ "$#" -gt 0 ] || fail "--torch-backend requires a value."
                torch_backend=$1
                [ -n "$torch_backend" ] || fail "--torch-backend requires a non-empty value."
                ;;
            --torch-backend=*)
                torch_backend=${1#*=}
                [ -n "$torch_backend" ] || fail "--torch-backend requires a non-empty value."
                ;;
            --rtk)
                enable_rtk=1
                ;;
            --dry-run)
                dry_run=1
                ;;
            --help|-h)
                show_usage
                exit 0
                ;;
            *)
                show_usage >&2
                fail "unknown option: $1"
                ;;
        esac
        shift
    done
}

validate_args() {
    include_local=$voice_local
    if [ "$voice_all" -eq 1 ]; then
        include_local=1
    fi

    if [ -n "$torch_backend" ] && [ "$include_local" -ne 1 ]; then
        fail "--torch-backend requires --voice-local or --voice-all."
    fi
}

package_spec() {
    include_nim=$voice_nim
    include_local=$voice_local

    if [ "$voice_all" -eq 1 ]; then
        include_nim=1
        include_local=1
    fi

    if [ "$include_nim" -eq 1 ] && [ "$include_local" -eq 1 ]; then
        printf 'codexproxy[voice,voice_local] @ %s' "$REPO_ARCHIVE_URL"
    elif [ "$include_nim" -eq 1 ]; then
        printf 'codexproxy[voice] @ %s' "$REPO_ARCHIVE_URL"
    elif [ "$include_local" -eq 1 ]; then
        printf 'codexproxy[voice_local] @ %s' "$REPO_ARCHIVE_URL"
    else
        printf 'codexproxy @ %s' "$REPO_ARCHIVE_URL"
    fi
}

install_codexproxy() {
    assert_no_fcc_processes_running
    spec=$(package_spec)

    if [ -n "$torch_backend" ]; then
        run uv tool install --force --refresh-package codexproxy --python "$PYTHON_VERSION" --torch-backend "$torch_backend" "$spec"
    else
        run uv tool install --force --refresh-package codexproxy --python "$PYTHON_VERSION" "$spec"
    fi
}

configure_and_verify_codexproxy() {
    run uv tool update-shell

    if [ "$dry_run" -eq 1 ]; then
        print_command uv tool dir --bin
        printf '+ verify cdx-desktop, cdx-server, cdx-codex, and cdx-init in the uv tool bin directory\n'
        print_command cdx-server --version
        return 0
    fi

    print_command uv tool dir --bin
    if tool_bin=$(uv tool dir --bin); then
        :
    else
        status=$?
        fail "Could not determine the uv tool bin directory (exit code $status)."
    fi
    [ -n "$tool_bin" ] || fail "uv returned an empty tool bin directory."

    add_path_entry "$tool_bin"
    export PATH
    hash -r 2>/dev/null || true

    for command_name in cdx-desktop cdx-server cdx-codex; do
        [ -x "$tool_bin/$command_name" ] || fail "CodexProxy installation did not create $tool_bin/$command_name."
    done

    run "$tool_bin/cdx-server" --version
}

shell_quote() {
    escaped=$(printf '%s' "$1" | sed "s/'/'\\\\''/g")
    printf "'%s'" "$escaped"
}

macos_app_is_fcc_owned() {
    app_dir=$1
    owner_file="$app_dir/Contents/$CODEX_PROXY_MACOS_OWNER_FILE"
    [ -d "$app_dir" ] &&
        [ ! -L "$app_dir" ] &&
        [ -f "$owner_file" ] &&
        [ "$(cat "$owner_file")" = "$CODEX_PROXY_MACOS_BUNDLE_ID" ]
}

install_macos_desktop_app() {
    [ "$(uname -s)" = "Darwin" ] || return 0

    app_dir="$HOME/Applications/CodexProxy.app"
    contents_dir="$app_dir/Contents"
    owner_file="$contents_dir/$CODEX_PROXY_MACOS_OWNER_FILE"
    executable_dir="$contents_dir/MacOS"
    executable_path="$executable_dir/cdx-desktop"
    resources_dir="$contents_dir/Resources"
    icon_path="$resources_dir/AppIcon.icns"
    desktop_dir="$HOME/Desktop"
    desktop_link="$desktop_dir/CodexProxy.app"

    if [ -e "$app_dir" ] || [ -L "$app_dir" ]; then
        macos_app_is_fcc_owned "$app_dir" ||
            fail "An app not managed by CodexProxy already exists at $app_dir. Move it, then rerun the installer."
    fi

    if [ "$dry_run" -eq 1 ]; then
        print_command mkdir -p "$executable_dir" "$resources_dir" "$desktop_dir"
        print_command cdx-desktop --export-icon "$icon_path"
        printf '+ write %s, %s, and %s\n' "$owner_file" "$contents_dir/Info.plist" "$executable_path"
        print_command ln -s "$app_dir" "$desktop_link"
        return 0
    fi

    mkdir -p "$executable_dir" "$resources_dir" "$desktop_dir"
    run "$tool_bin/cdx-desktop" --export-icon "$icon_path"
    [ -f "$icon_path" ] || fail "CodexProxy did not export its macOS app icon to $icon_path."
    printf '%s\n' "$CODEX_PROXY_MACOS_BUNDLE_ID" > "$owner_file"
    cat > "$contents_dir/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDisplayName</key>
    <string>CodexProxy</string>
    <key>CFBundleExecutable</key>
    <string>cdx-desktop</string>
    <key>CFBundleIdentifier</key>
    <string>io.github.alishahryar1.codexproxy</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundleName</key>
    <string>CodexProxy</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>LSMultipleInstancesProhibited</key>
    <true/>
    <key>LSUIElement</key>
    <true/>
</dict>
</plist>
PLIST
    desktop_command=$(shell_quote "$tool_bin/cdx-desktop")
    {
        printf '%s\n' '#!/bin/sh'
        printf 'exec %s\n' "$desktop_command"
    } > "$executable_path"
    chmod +x "$executable_path"

    if [ -L "$desktop_link" ]; then
        if [ "$(readlink "$desktop_link")" = "$app_dir" ]; then
            rm -f "$desktop_link"
        else
            printf 'A non-CDX link already exists at %s; leaving it unchanged.\n' "$desktop_link"
            return 0
        fi
    elif [ -e "$desktop_link" ]; then
        printf 'A non-CDX item already exists at %s; leaving it unchanged.\n' "$desktop_link"
        return 0
    fi
    ln -s "$app_dir" "$desktop_link"
}

parse_args "$@"
validate_args
add_known_bin_directories

step "Checking for running CodexProxy processes"
assert_no_fcc_processes_running

if installer_is_interactive; then
    step "Choosing coding agents"
    choose_coding_agents /dev/tty /dev/tty
fi

step "Checking installation prerequisites"
require_command curl
require_command sh
require_command mktemp
if [ "$enable_rtk" -eq 1 ] && ! command -v rtk >/dev/null 2>&1; then
    require_command tar
    if [ "$dry_run" -eq 0 ] &&
        ! command -v sha256sum >/dev/null 2>&1 &&
        ! command -v shasum >/dev/null 2>&1; then
        fail "RTK installation requires sha256sum or shasum for checksum verification."
    fi
fi

ensure_selected_coding_agents
configure_rtk_for_selected_agents

step "Ensuring uv $MIN_UV_VERSION or newer is installed"
ensure_uv

step "Installing or updating CodexProxy"
install_codexproxy

step "Configuring PATH and verifying CodexProxy"
configure_and_verify_codexproxy

if [ "$(uname -s)" = "Darwin" ]; then
    step "Installing the CodexProxy desktop launcher"
    install_macos_desktop_app
fi

if [ "$dry_run" -eq 1 ]; then
    printf '\nDry run complete. No changes were made.\n'
else
    if [ "$(uname -s)" = "Darwin" ]; then
        printf '\nCodexProxy is installed and verified. Open CodexProxy from Applications or the desktop to run it in the background.\n'
        printf 'For terminal use, start the proxy with: cdx-server\n'
    else
        printf '\nCodexProxy is installed and verified. Start the proxy with: cdx-server\n'
    fi
    if [ "$install_codex" -eq 1 ]; then
        printf 'Run Codex with: cdx-codex\n'
    fi
fi
