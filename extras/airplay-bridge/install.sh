#!/usr/bin/env bash
# airplay-bridge installer and readiness checker.
#
# Usage:
#   install.sh               # install (idempotent; run with sudo access)
#   install.sh --check       # no changes; just report what's missing
#
# Targets Arch/Manjaro. Assumes yay is available for AUR packages.

set -u -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

RED=$'\033[0;31m'; GRN=$'\033[0;32m'; YLW=$'\033[1;33m'; NC=$'\033[0m'
ok()    { printf '[%sOK%s]      %s\n'      "$GRN" "$NC" "$*"; }
miss()  { printf '[%sMISSING%s] %s\n'      "$YLW" "$NC" "$*"; }
info()  { printf '[%sINFO%s]    %s\n'      "$GRN" "$NC" "$*"; }
warn()  { printf '[%sWARN%s]    %s\n'      "$YLW" "$NC" "$*"; }
fatal() { printf '[%sFATAL%s]   %s\n'      "$RED" "$NC" "$*" >&2; exit 1; }

BEGIN_MARK='### airplay-bridge:begin (managed; do not edit between markers)'
END_MARK='### airplay-bridge:end'

MODE="install"
[[ "${1:-}" == "--check" ]] && MODE="check"

OWNTONE_API="http://localhost:3689/api"
CFG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/mpd-owntone-bridge"
CFG_FILE="$CFG_DIR/config.env"
MPD_CONF="${HOME}/.mpd/mpd.conf"
I3_CONF="${HOME}/.i3/config"
SWAY_CONF="${HOME}/.config/sway/config"
SYSTEMD_UNIT="${HOME}/.config/systemd/user/mpd-owntone-metadata.service"
PADDER_UNIT="${HOME}/.config/systemd/user/mpd-owntone-padder.service"
PW_DROPIN="${HOME}/.config/pipewire/pipewire-pulse.conf.d/20-raop-discover.conf"
PW_BRIDGE_PIN="${HOME}/.config/pipewire/pipewire-pulse.conf.d/30-mpd-bridge-pin.conf"
PW_NULLSINK="${HOME}/.config/pipewire/pipewire.conf.d/30-owntone-bridge.conf"
WP_BRIDGE_ROUTE="${HOME}/.config/wireplumber/wireplumber.conf.d/40-owntone-bridge-route.conf"

# -------- helpers --------

has_cmd()  { command -v "$1" >/dev/null 2>&1; }
pkg_installed() { pacman -Q "$1" >/dev/null 2>&1; }
in_group() { id -nG "$USER" | tr ' ' '\n' | grep -qx "$1"; }
file_has()  { [[ -f "$1" ]] && grep -qF "$2" "$1"; }

remove_block() {
  # Remove sentinel-bounded block from $1. No-op if block absent.
  local f="$1"
  [[ -f "$f" ]] || return 0
  sed -i "/$BEGIN_MARK/,/$END_MARK/d" "$f"
}

append_block() {
  # Append begin..content..end to $1.
  local f="$1" content="$2"
  {
    printf '\n%s\n' "$BEGIN_MARK"
    printf '%s\n' "$content"
    printf '%s\n' "$END_MARK"
  } >> "$f"
}

replace_block() {
  remove_block "$1"
  append_block "$@"
}

wait_for_owntone_api() {
  local i
  for i in {1..20}; do
    command curl --silent --max-time 1 "$OWNTONE_API/outputs" >/dev/null && return 0
    sleep 0.5
  done
  return 1
}

# -------- checks --------

check_pkg() {
  # $1 = package name
  if pkg_installed "$1"; then ok "pkg: $1"; else miss "pkg: $1 (yay -S $1)"; fi
}

check_dir() {
  if [[ -d "$1" ]]; then ok "dir: $1"; else miss "dir: $1"; fi
}

check_fifo() {
  if [[ -p "$1" ]]; then ok "fifo: $1"; else miss "fifo: $1"; fi
}

check_owntone_conf() {
  if file_has /etc/owntone.conf '/var/lib/owntone-stream'; then
    ok "/etc/owntone.conf has our library dir"
  else
    miss "/etc/owntone.conf library dir not set to /var/lib/owntone-stream"
  fi
  if grep -Eq '^\s*type\s*=\s*"disabled"' /etc/owntone.conf 2>/dev/null; then
    ok "/etc/owntone.conf local audio is disabled"
  else
    miss "/etc/owntone.conf local audio not disabled"
  fi
  if grep -Eq '^\s*pipe_autostart\s*=\s*true' /etc/owntone.conf 2>/dev/null; then
    ok "/etc/owntone.conf pipe_autostart enabled"
  else
    miss "/etc/owntone.conf pipe_autostart not enabled"
  fi
}

check_service() {
  if [[ -f "$SYSTEMD_UNIT" ]]; then ok "systemd unit: $SYSTEMD_UNIT"; else miss "systemd unit missing"; fi
  if systemctl --user is-enabled --quiet mpd-owntone-metadata 2>/dev/null; then
    ok "mpd-owntone-metadata enabled"
  else
    miss "mpd-owntone-metadata not enabled"
  fi
}

run_checks() {
  echo "== Packages =="
  check_pkg mpd
  check_pkg owntone-server
  check_pkg pipewire-zeroconf
  check_pkg jq
  check_pkg avahi
  check_pkg rofi

  echo; echo "== System state =="
  check_dir /var/lib/owntone-stream
  check_fifo /var/lib/owntone-stream/mpd.pcm
  check_fifo /var/lib/owntone-stream/mpd.pcm.metadata
  if [[ -f /var/log/owntone.log ]]; then ok "/var/log/owntone.log"; else miss "/var/log/owntone.log"; fi
  check_owntone_conf
  if in_group owntone; then ok "user in owntone group (may need re-login after first add)"
  else miss "user not in owntone group"; fi

  echo; echo "== Per-machine config =="
  if [[ -f "$CFG_FILE" ]]; then
    ok "$CFG_FILE"
    # shellcheck source=/dev/null
    (. "$CFG_FILE"; [[ -n "${SPEAKER_DENON:-}" ]] && ok "  SPEAKER_DENON=$SPEAKER_DENON" || miss "  SPEAKER_DENON empty"
                    [[ -n "${PIPEWIRE_LAPTOP_SINK:-}" ]] && ok "  PIPEWIRE_LAPTOP_SINK=$PIPEWIRE_LAPTOP_SINK" || miss "  PIPEWIRE_LAPTOP_SINK empty")
  else
    miss "$CFG_FILE"
  fi

  echo; echo "== User integrations =="
  check_service
  if file_has "$MPD_CONF" "$BEGIN_MARK"; then ok "mpd.conf has bridge block"; else miss "mpd.conf lacks bridge block"; fi
  if [[ -f "$I3_CONF" ]]; then
    if file_has "$I3_CONF" "$BEGIN_MARK"; then ok "i3 config has bridge bindings"; else miss "i3 config lacks bridge bindings"; fi
  fi
  if [[ -f "$SWAY_CONF" ]]; then
    if file_has "$SWAY_CONF" "$BEGIN_MARK"; then ok "sway config has bridge bindings"; else miss "sway config lacks bridge bindings"; fi
  fi
  if [[ ! -f "$I3_CONF" && ! -f "$SWAY_CONF" ]]; then miss "no i3 or sway config found"; fi
  if [[ -f "$PW_DROPIN" ]]; then ok "pipewire raop drop-in"; else miss "pipewire raop drop-in"; fi
  if [[ -f "$PW_BRIDGE_PIN" ]]; then ok "pipewire bridge-pin drop-in"; else miss "pipewire bridge-pin drop-in"; fi
  if [[ -f "$PW_NULLSINK" ]]; then ok "pipewire null-sink drop-in"; else miss "pipewire null-sink drop-in"; fi
  if [[ -f "$WP_BRIDGE_ROUTE" ]]; then ok "wireplumber bridge-route drop-in"; else miss "wireplumber bridge-route drop-in"; fi
  if [[ -f "$PADDER_UNIT" ]]; then ok "padder systemd unit"; else miss "padder systemd unit"; fi

  echo; echo "== Runtime =="
  if systemctl is-active --quiet owntone; then ok "owntone.service running"; else miss "owntone.service not running"; fi
  if systemctl --user is-active --quiet mpd-owntone-metadata; then ok "mpd-owntone-metadata running"; else miss "mpd-owntone-metadata not running"; fi
  if systemctl --user is-active --quiet mpd-owntone-padder; then ok "mpd-owntone-padder running"; else miss "mpd-owntone-padder not running"; fi
  if pactl list short sinks 2>/dev/null | grep -q '^\S*\s*owntone-bridge\s'; then ok "owntone-bridge null sink loaded"; else miss "owntone-bridge null sink not loaded"; fi
  if command curl --silent --max-time 1 "$OWNTONE_API/outputs" >/dev/null; then
    ok "owntone API reachable"
    local n_ap
    n_ap=$(command curl --silent "$OWNTONE_API/outputs" | jq '[.outputs[] | select(.type|startswith("AirPlay"))] | length')
    info "  $n_ap AirPlay receiver(s) discovered"
  else
    miss "owntone API unreachable"
  fi
}

# -------- install actions --------

install_packages() {
  local needed=()
  for p in owntone-server pipewire-zeroconf libpulse jq avahi rofi; do
    pkg_installed "$p" || needed+=("$p")
  done
  if (( ${#needed[@]} == 0 )); then
    info "all packages already installed"
    return
  fi
  has_cmd yay || fatal "yay is required for AUR packages (install yay first)"
  info "installing packages: ${needed[*]}"
  yay -S --needed --noconfirm "${needed[@]}" || fatal "package install failed"
}

bootstrap_owntone_state() {
  info "ensuring /var/lib/owntone-stream and FIFOs"
  sudo install -d -o owntone -g owntone -m 2775 /var/lib/owntone-stream
  [[ -p /var/lib/owntone-stream/mpd.pcm ]] || sudo -u owntone mkfifo -m 666 /var/lib/owntone-stream/mpd.pcm
  [[ -p /var/lib/owntone-stream/mpd.pcm.metadata ]] || sudo -u owntone mkfifo -m 666 /var/lib/owntone-stream/mpd.pcm.metadata

  info "ensuring /var/log/owntone.log"
  sudo install -o owntone -g owntone -m 644 /dev/null /var/log/owntone.log

  info "patching /etc/owntone.conf (library dir, local audio off, mpd listener off, pipe autostart)"
  sudo sed -i -E 's|^\s*directories\s*=\s*\{\s*"[^"]+"\s*\}|\tdirectories = { "/var/lib/owntone-stream" }|' /etc/owntone.conf
  if ! grep -Eq '^\s*type\s*=\s*"disabled"' /etc/owntone.conf; then
    sudo sed -i 's|^#\ttype = "alsa"|\ttype = "disabled"|' /etc/owntone.conf
  fi
  if ! grep -Eq '^\s*port\s*=\s*0\s*$' /etc/owntone.conf; then
    sudo sed -i 's|^#\tport = 6600|\tport = 0|' /etc/owntone.conf
  fi
  # pipe_autostart=true makes OwnTone re-attach to a pipe library item
  # whenever data appears. Without it, an EOF on the pipe (e.g. xmpd's
  # Tidal proxy switching streams between tracks) detaches the consumer
  # and the AirPlay session goes silent until manually re-routed.
  if ! grep -Eq '^\s*pipe_autostart\s*=\s*true' /etc/owntone.conf; then
    sudo sed -i 's|^#\s*pipe_autostart\s*=\s*true|\tpipe_autostart = true|' /etc/owntone.conf
  fi

  if ! in_group owntone; then
    info "adding $USER to owntone group (re-login required for it to take effect in new sessions)"
    sudo usermod -aG owntone "$USER"
  fi

  info "starting owntone"
  sudo systemctl enable --now owntone
}

discover_and_configure() {
  info "waiting for owntone API..."
  wait_for_owntone_api || fatal "owntone API did not come up within 10s"

  info "discovering AirPlay receivers"
  local outputs_json
  outputs_json=$(command curl --silent "$OWNTONE_API/outputs")
  local airplay
  airplay=$(jq -c '.outputs[] | select(.type|startswith("AirPlay")) | {id,name,type}' <<< "$outputs_json")
  if [[ -z "$airplay" ]]; then
    warn "no AirPlay receivers found. Power on your Denon/Home 150 and rerun."
    fatal "cannot continue without at least one AirPlay receiver"
  fi

  echo
  echo "Discovered AirPlay receivers:"
  local i=0
  local -a IDS=() NAMES=()
  while IFS= read -r line; do
    local id name
    id=$(jq -r .id   <<< "$line")
    name=$(jq -r .name <<< "$line")
    IDS+=("$id"); NAMES+=("$name")
    printf "  [%d] %s (%s)\n" "$i" "$name" "$(jq -r .type <<< "$line")"
    i=$((i+1))
  done <<< "$airplay"

  local pick_denon pick_kitchen
  echo
  read -rp "Main AirPlay AVR (Denon) — index: " pick_denon
  read -rp "Kitchen speaker index (blank to skip): " pick_kitchen

  local denon_id="${IDS[$pick_denon]}"
  local kitchen_id=""
  if [[ -n "$pick_kitchen" ]]; then kitchen_id="${IDS[$pick_kitchen]}"; fi

  # Laptop sink autodetect: first analog-stereo sink.
  local laptop_sink
  laptop_sink=$(pactl list sinks short 2>/dev/null | awk '/analog-stereo/ {print $2; exit}')

  mkdir -p "$CFG_DIR"
  sed -e "s|^SPEAKER_DENON=.*|SPEAKER_DENON=\"$denon_id\"|" \
      -e "s|^SPEAKER_KITCHEN=.*|SPEAKER_KITCHEN=\"$kitchen_id\"|" \
      -e "s|^PIPEWIRE_LAPTOP_SINK=.*|PIPEWIRE_LAPTOP_SINK=\"$laptop_sink\"|" \
      "$SCRIPT_DIR/config.env.template" > "$CFG_FILE"
  info "wrote $CFG_FILE"
}

install_systemd_unit() {
  info "installing systemd user unit -> $SYSTEMD_UNIT"
  mkdir -p "$(dirname "$SYSTEMD_UNIT")"
  sed -e "s|@SCRIPT_DIR@|$SCRIPT_DIR|g" "$SCRIPT_DIR/mpd-owntone-metadata.service.template" > "$SYSTEMD_UNIT"
  systemctl --user daemon-reload
  systemctl --user enable --now mpd-owntone-metadata
}

patch_mpd_conf() {
  info "patching $MPD_CONF (adding Owntone Bridge output)"
  [[ -f "$MPD_CONF" ]] || fatal "$MPD_CONF not found; create your MPD config first"
  # MPD writes to a PipeWire null sink (owntone-bridge) rather than directly
  # to the FIFO. The null sink runs on the audio clock and emits silence on
  # its monitor when idle; parec then pumps the monitor into the FIFO at a
  # constant rate. This decouples OwnTone's real-time AirPlay session from
  # MPD's source-side stalls (Tidal proxy EOFs between tracks, network
  # hiccups). Without this layer, AP2 receivers like JBL Boombox 3 tear
  # down the RTSP session on the first underrun.
  local block
  block=$(cat <<'EOF'
audio_output {
	type        "pulse"
	name        "Owntone Bridge"
	sink        "owntone-bridge"
	format      "44100:16:2"
	always_on   "yes"
	# Volume is controlled per-receiver via OwnTone's API; bypass the
	# PulseAudio mixer so MPD passes audio through at unity gain.
	mixer_type  "none"
}
EOF
  )
  replace_block "$MPD_CONF" "$block"
  info "reload MPD to pick up changes: systemctl --user restart mpd"
}

patch_wm_conf() {
  # $1 = config file path, $2 = label (i3/sway), $3 = reload command
  local conf="$1" label="$2" reload="$3"
  [[ -f "$conf" ]] || { info "$label config $conf not found; skipping"; return 0; }
  info "patching $conf ($label: vol-wrap + speaker-rofi keybindings)"
  local block
  block=$(cat <<EOF
bindsym XF86AudioRaiseVolume exec $SCRIPT_DIR/vol-wrap up
bindsym XF86AudioLowerVolume exec $SCRIPT_DIR/vol-wrap down
bindsym XF86AudioMute        exec $SCRIPT_DIR/vol-wrap mute
bindsym Mode_switch+z        exec $SCRIPT_DIR/vol-wrap down
bindsym Mode_switch+c        exec $SCRIPT_DIR/vol-wrap up
bindsym Mode_switch+x        exec $SCRIPT_DIR/vol-wrap mute
bindsym \$mod+Shift+s        exec $SCRIPT_DIR/speaker-rofi
EOF
  )
  replace_block "$conf" "$block"
  info "reload $label: $reload"
}

patch_wm_confs() {
  patch_wm_conf "$I3_CONF"   "i3"   "i3-msg reload"
  patch_wm_conf "$SWAY_CONF" "sway" "swaymsg reload"
  if [[ ! -f "$I3_CONF" && ! -f "$SWAY_CONF" ]]; then
    warn "no i3 or sway config found; volume/speaker keybindings not installed"
  fi
}

install_pipewire_dropin() {
  info "installing pipewire raop discovery drop-in"
  mkdir -p "$(dirname "$PW_DROPIN")"
  cat > "$PW_DROPIN" <<'EOF'
pulse.cmd = [
    { cmd = "load-module" args = "module-raop-discover" }
]
EOF
}

install_pipewire_bridge_pin() {
  info "installing pipewire bridge-pin drop-in -> $PW_BRIDGE_PIN"
  mkdir -p "$(dirname "$PW_BRIDGE_PIN")"
  # Set node.dont-move=true on every MPD pulse stream. This makes
  # WirePlumber's find-defined-target.lua skip its metadata-override
  # branch (see /usr/share/wireplumber/scripts/linking/find-defined-target
  # .lua line 55 "if metadata and not dont_move"), so the bridge stream's
  # target.object="owntone-bridge" stays authoritative and cannot be
  # overridden by pavucontrol moves, wpctl, or pulse module-stream-restore
  # writing -1 / another sink into the default metadata for its node.id.
  #
  # Without this, the original 2026-05-12 fix (wireplumber stream.rules
  # rewriting media.role to Music-Bridge) only isolated WirePlumber's
  # state-stream restore slot; the visible node.media.role stayed "Music"
  # so pipewire-pulse module-stream-restore still grouped both MPD
  # streams under sink-input-by-media-role:music, and any pavucontrol
  # move of one stream dragged the bridge onto the local default sink
  # (HDMI, Bluetooth, ...) - the same track playing twice in parallel.
  #
  # MPD's local "PulseAudio" output also gets node.dont-move=true and is
  # therefore not movable via pavucontrol either - that is fine because
  # this repo's speaker-rofi / speaker scripts switch outputs by changing
  # the default sink (pactl set-default-sink), not by per-stream moves,
  # and linking.follow-default-target keeps the local stream tracking
  # whichever sink the user selected.
  #
  # The padder (parec, application.name=mpd-owntone-padder) gets the same
  # pin: its stream had a stale 'target.node = -1' metadata override on
  # 2026-07-24 which relinked it to the analog sink's monitor - the FIFO
  # was fed from an idle sink, OwnTone starved, and the AP2 receiver tore
  # down the RTSP session. dont-move closes that hole for good.
  #
  # node.dont-fallback + node.linger on top: if owntone-bridge does not
  # exist yet (pipewire still bringing up conf.d objects at login), the
  # stream waits for its defined target instead of falling back to the
  # default sink/source. A fallback link here is silently wrong - the
  # padder would pump the wrong monitor (or even a mic) into the FIFO.
  # Streams without target.object (MPD's local output) are unaffected.
  cat > "$PW_BRIDGE_PIN" <<'EOF'
pulse.rules = [
    {
        matches = [
            {
                application.name = "Music Player Daemon"
            }
        ]
        actions = {
            update-props = {
                node.dont-move     = true
                node.dont-fallback = true
                node.linger        = true
            }
        }
    }
    {
        matches = [
            {
                application.name = "mpd-owntone-padder"
            }
        ]
        actions = {
            update-props = {
                node.dont-move     = true
                node.dont-fallback = true
                node.linger        = true
            }
        }
    }
]
EOF
  systemctl --user restart pipewire-pulse 2>/dev/null || true
  # pulse.rules apply at stream creation; recreate the padder stream so the
  # pin takes effect now, not on the next reboot.
  systemctl --user try-restart mpd-owntone-padder 2>/dev/null || true
}

install_wireplumber_bridge_route() {
  info "installing wireplumber bridge-route drop-in -> $WP_BRIDGE_ROUTE"
  mkdir -p "$(dirname "$WP_BRIDGE_ROUTE")"
  # Isolate the "Owntone Bridge" stream from the local "PulseAudio"
  # output's stream-restore slot. MPD's pulse plugin hardcodes
  # media.role="Music" on every stream it opens, so both of MPD's pulse
  # outputs share the same WirePlumber formKey
  # ("Output/Audio:media.role:Music", from state-stream.lua's formKey).
  # Whichever stream the user moves last via pavucontrol sets the saved
  # target for both, which drags the bridge onto local speakers on the
  # next MPD restart (audible as the same track playing twice).
  #
  # pipewire-pulse's pulse.rules can't fix this because its matches only
  # see client-level properties, and both MPD streams share one client.
  # WirePlumber's stream.rules run per-stream after node creation, so
  # they can match media.name and rewrite media.role only for the bridge
  # stream. The bridge then keys under "Music-Bridge", independent of
  # the local output.
  cat > "$WP_BRIDGE_ROUTE" <<'EOF'
stream.rules = [
    {
        matches = [
            {
                application.name = "Music Player Daemon"
                media.name       = "Owntone Bridge"
            }
        ]
        actions = {
            update-props = {
                media.role = "Music-Bridge"
            }
        }
    }
]
EOF
  systemctl --user restart wireplumber 2>/dev/null || true
}

install_pipewire_nullsink() {
  info "installing pipewire null-sink drop-in -> $PW_NULLSINK"
  mkdir -p "$(dirname "$PW_NULLSINK")"
  cat > "$PW_NULLSINK" <<'EOF'
# Null sink that fronts the OwnTone AirPlay bridge.
#
# Why this exists: OwnTone reads audio from a FIFO at real-time rate. MPD's
# pipe sources (xmpd's Tidal stream proxy) briefly stall during track
# transitions, leaving the FIFO empty; OwnTone's player.c then logs
# "Source is not providing sufficient data" and suspends playback, which
# on AP2 receivers like the JBL Boombox 3 tears down the RTSP session.
# See owntone-server issues #452, #1343.
#
# Solution: put a PipeWire null sink between MPD and the FIFO. The sink
# runs on its own clock (always-process=true) and emits silence on its
# monitor whenever the input is idle. A parec systemd user unit
# (mpd-owntone-padder.service) pumps the monitor into the FIFO at exactly
# 44100*2*2 bytes/sec, so the FIFO is never empty regardless of upstream.

context.objects = [
    {
        factory = adapter
        args = {
            factory.name              = support.null-audio-sink
            node.name                 = "owntone-bridge"
            node.description          = "OwnTone Bridge Null Sink"
            media.class               = Audio/Sink
            audio.format              = S16LE
            audio.rate                = 44100
            audio.channels            = 2
            audio.position            = [ FL FR ]
            # false (the PipeWire default) so the monitor always carries
            # unity-gain PCM no matter where the sink's volume slider sits.
            # With true, the slider becomes a second, invisible attenuator
            # in front of OwnTone: the receiver's own AirPlay volume reads
            # high while the audio arriving at it is quiet AND quantization
            # -degraded. OwnTone's per-output volume (the receiver's own
            # AirPlay volume) is the one and only knob.
            monitor.channel-volumes   = false
            node.always-process       = true
            adapter.auto-port-config  = {
                mode     = dsp
                monitor  = true
                position = preserve
            }
        }
    }
]
EOF
  systemctl --user restart pipewire pipewire-pulse wireplumber 2>/dev/null || true
}

install_padder_unit() {
  info "installing systemd user unit -> $PADDER_UNIT"
  mkdir -p "$(dirname "$PADDER_UNIT")"
  local parec_path
  parec_path="$(command -v parec || echo /usr/sbin/parec)"
  cat > "$PADDER_UNIT" <<EOF
[Unit]
Description=Pump PipeWire null-sink monitor into OwnTone FIFO at real-time rate
After=pipewire.service pipewire-pulse.service wireplumber.service
Wants=pipewire.service pipewire-pulse.service wireplumber.service
StartLimitIntervalSec=60
StartLimitBurst=10

[Service]
Type=simple
# parec opens stdout (the FIFO) which blocks until OwnTone has the read
# end open. systemd restarts on PulseAudio hiccups or owntone restarts.
ExecStart=/bin/sh -c 'exec $parec_path --device=owntone-bridge.monitor --format=s16le --rate=44100 --channels=2 --latency-msec=100 --no-remix --client-name=mpd-owntone-padder > /var/lib/owntone-stream/mpd.pcm'
Restart=always
RestartSec=2

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now mpd-owntone-padder
}

make_executable() {
  chmod +x "$SCRIPT_DIR"/mpd_owntone_metadata.py \
           "$SCRIPT_DIR"/vol-wrap \
           "$SCRIPT_DIR"/speaker \
           "$SCRIPT_DIR"/speaker-rofi
}

# -------- main --------

if [[ "$MODE" == "check" ]]; then
  run_checks
  exit 0
fi

info "airplay-bridge install starting"
info "script dir: $SCRIPT_DIR"

has_cmd sudo || fatal "sudo required"
has_cmd pactl || warn "pactl not found; PipeWire sink autodetection will fail"

make_executable
install_packages
bootstrap_owntone_state
discover_and_configure
install_pipewire_dropin
install_pipewire_bridge_pin
install_wireplumber_bridge_route
install_pipewire_nullsink
install_systemd_unit
install_padder_unit
patch_mpd_conf
patch_wm_confs

echo
info "install complete."
info "you may need to:"
info "  - log out/in if you were just added to the owntone group"
info "  - systemctl --user restart mpd (to pick up the new pulse output)"
info "  - i3-msg reload / swaymsg reload (to activate new keybindings)"
info
info "run '$0 --check' any time to audit state."
