# Third-party notices

The 15 Superpowers skills in this bundle retain the files and executable bits from
[obra/superpowers](https://github.com/obra/superpowers), release v6.4.2 at revision
`8ca22dba9a94f28898bbce59f2537ff4d87c747d`, under the MIT license copied exactly to
`licenses/superpowers.LICENSE`.

Khenrix Utilities adds one privacy overlay to
`brainstorming/scripts/start-server.sh`: it exports
`SUPERPOWERS_DISABLE_TELEMETRY=1` before starting the optional visual server. This
prevents its remote logo request and telemetry path even for GUI-launched CLIs.

The selected upstream paths, immutable revision, tree hash, bundle members, and allowed
local provenance files are recorded in `upstreams.toml`. The copies are distributed by
Khenrix Utilities and are not represented as an official Superpowers release.
