# Ease Life Camera for Home Assistant

Unofficial Home Assistant custom integration for **Ease Life / Blurams /
Closeli** cloud cameras. It logs in to your Ease Life account, exposes each
camera with its cloud snapshot, and bridges the vendor live stream into
Home Assistant so you get smooth HLS live video, PTZ controls, optional audio
and a Frigate-ready feed.

> Unofficial project, built by reverse-engineering the vendor web player and
> cloud API. It can break if the vendor changes their cloud. Requires an
> Ease Life account and an online camera; live video flows through the
> vendor cloud relay (there is no local RTSP on these cameras).

## Features

- **Snapshot + live video** — cloud thumbnail stills plus smooth HLS live
  stream (720p H.264) with seamless upstream handover and timestamp repair.
- **PTZ** — `ease_life.ptz` service: step `left` / `right` / `up` / `down` /
  `home` (pan/tilt where the model supports it; the entity reports
  `ptz_supported`).
- **Speak** — `ease_life.speak` service renders text with an HA text-to-speech
  engine and plays it on the camera speaker, with or without a live video
  window open.
- **Listen (opt-in)** — `enable_audio` option adds AAC audio to the live
  stream (the camera ships G.711, which HA cannot mux, so the integration
  transcodes).
- **Frigate feed** — the local proxy URL is plain ffmpeg-readable FLV, usable
  as a Frigate `ffmpeg` input (24/7).

## Requirements

- Home Assistant 2026.x (Core with the `stream` integration, i.e. any
  standard install).
- An Ease Life account (email + password) with at least one online camera.
- For TTS announcements: any configured HA text-to-speech engine.

## Installation

### Manual (works everywhere)
1. Copy the `ease_life` folder into your Home Assistant config:
   `<config>/custom_components/ease_life/`
2. Restart Home Assistant.
3. Settings → Devices & Services → Add Integration → **Ease Life Camera**.
4. Enter your Ease Life **email** and **password**.

### HACS (custom repository)
1. HACS → Integrations → ⋯ → Custom repositories → add this repository URL
   with category *Integration*.
2. Install **Ease Life Camera**, restart Home Assistant, then add the
   integration as above.

## Configuration options

Settings → Devices & Services → Ease Life Camera → Configure:

| Option           | Default       | What it does                                                        |
|------------------|---------------|---------------------------------------------------------------------|
| `scan_interval`  | `60`          | Seconds between cloud device-list polls (snapshots/thumbnails).     |
| `enable_audio`   | `false`       | Add AAC audio to live video (transcoded from the camera's G.711).   |
| `proxy_host`     | `127.0.0.1`   | Bind address of the internal FLV bridge. Set `0.0.0.0` for Frigate. |
| `proxy_token`    | _(empty)_     | Shared secret for the bridge URL. **Set one when binding to LAN.**  |

Changing options reloads the entry automatically.

## Services

### `ease_life.ptz` — move the camera
```yaml
action: ease_life.ptz
data:
  entity_id: camera.office
  direction: left     # left | right | up | down | home
  steps: 2            # optional, default 1
```

### `ease_life.speak` — talk through the camera speaker
```yaml
action: ease_life.speak
data:
  entity_id: camera.office
  message: "Someone is at the door"
  engine: tts.my_engine   # optional, defaults to the HA default TTS engine
```
> Speak works with the video window open or closed: it rides the live session
> while someone is watching, and when nobody is it starts a temporary hidden
> live session, rides it, and tears it down — so speak no longer requires a
> video stream to be running.

## Frigate

1. Set integration options: **proxy host** `0.0.0.0` and a **proxy token**.
2. Add to `frigate.yml` (replace host, device id and token):
```yaml
cameras:
  office:
    enabled: true
    ffmpeg:
      inputs:
        - path: http://YOUR_HA_IP:8765/live/YOUR_DEVICE_ID.flv?token=YOUR_TOKEN
          input_args: -avoid_negative_ts make_zero -fflags +genpts+discardcorrupt
          roles: [detect, record]
    detect:
      width: 1280
      height: 720
      fps: 5
```
Notes: the device id is the camera entity's unique id; Frigate holds the
stream open 24/7 so the camera cloud-streams continuously; with
`enable_audio` recordings include AAC sound. Alternatively point Frigate's
bundled go2rtc at the same URL and consume it as RTSP.

## How it works (short version)

- Auth and device list use the vendor v4 Soul API (`soul.ehomeease.com`).
- Live video rides the vendor `h5player` WebSocket (FLV over WS); a tiny
  local bridge (`127.0.0.1:8765` by default) re-serves it as HTTP-FLV for
  HA's `stream` component. Sessions are stitched across the vendor's
  ~60–100 s cap with a pre-connected standby (seamless handover), timestamps
  are re-paced so no frames are dropped, and a watchdog reconnects quiet
  sessions.
- PTZ uses the vendor device-control WebSocket (`request 1793 / sub 5`).
- Talk-back uses G.711A audio frames over the live session when one is open,
  or over a dedicated session started on demand.

## Known issues

- The vendor cloud occasionally stalls or caps sessions; the integration
  reconnects automatically (watch for `ease_life: upstream silent` warnings).
- After Home Assistant restarts, hard-refresh the browser tab once so the
  frontend picks up fresh stream URLs.

## Privacy

Your Ease Life credentials are stored in Home Assistant's encrypted config
entries and used only against the vendor cloud (login, device list,
thumbnails, live relay). No analytics, no third parties.

## Version history

- 0.3.7 — Speak no longer needs a video stream running: it transparently
  starts a hidden live-upstream session when nobody is watching, rides it for
  the utterance, then tears it down (the server kills a second concurrent
  session, so speak MUST ride a live one).
- 0.3.6 — Speak standalone reliability: tear down a dead upstream before the
  fallback session, retry that session once if the server has not released
  the previous live slot yet; ride failures no longer block speak.
- 0.3.5 — Speak without a live viewer: drop stale watchers, fall back to a
  dedicated session when nobody is watching.
- 0.3.4 — Speak survives upstream rotation: ride the freshly rotated client,
  retry frames across the ~60–100 s server session cap / standby handover.
- 0.3.3 — LAN bind + token auth for the bridge (Frigate feed); talk re-enabled
  with the exact SDK wire format (urlencoded header, `timeSpan:300`,
  correct STOP frame).
- 0.3.2 — Seamless session handover + silence watchdog (smooth playback).
- 0.3.0 — PTZ service, speak service (see history above), AAC listen option.
- 0.2.2 — Timestamp repair: no more dropped keyframes / frozen playback.
- 0.2.x — Initial snapshot + live video integration.
