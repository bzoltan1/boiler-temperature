# boiler-temperature

Reads a hot-water boiler display from a webcam, extracts the two temperatures
with a local vision model, validates them, and publishes them to InfluxDB,
MQTT/Home Assistant, and an optional HTML page.

## Why this rewrite

The previous shell version had three problems that caused bad data:

- Tesseract/VLM output was parsed with `awk` and a permissive
  `sed`/`${var//[^0-9-]/}` cleanup. Decimal points, labels, and stray text could
  become a number.
- A single reading was compared only with the previous value. A reading up to
  30 °C away was accepted, so ordinary OCR errors (for example 25 → 32 and
  76 → 69) went straight into InfluxDB. A wrong reading could also become the
  new baseline and lock out the correct values.
- The optional web upload (`scp` to juhfark.hu) ran before InfluxDB/MQTT and had
  no timeout. One hung SSH connection blocked the systemd oneshot service and
  stopped all monitoring until it was killed.

The OCR itself was also non-deterministic: the `ollama run` CLI used its default
sampling parameters, so the same image could be read as `25/76` one minute and
`32/69` the next. The image in this repository's incident was `25/76`; the old
script wrote `32/69`.

## How it works

1. Capture with `fswebcam` on the Raspberry Pi over SSH.
2. Crop and enhance the display with ImageMagick.
3. Ask Ollama for strict JSON (`{"s1": ..., "s2": ...}`) with
   `temperature=0` and a fixed seed.
4. Validate each channel independently:
   - finite number and within `roof_min..roof_max` / `boiler_min..boiler_max`;
   - accept if it is within `max_delta` of the last accepted value **or** the
     median of recent history;
   - otherwise store it as `pending`. It is accepted only when the next reading
     is within `confirm_delta` of that pending value.
5. Publish to InfluxDB and MQTT **before** the optional web upload.
6. Upload HTML and the latest image at most once per `web.interval` seconds,
   with timeouts. A failed upload never blocks monitoring.

The state is persisted in `state_dir/state.json`, so restarts do not lose the
history or the last accepted values. A lock file prevents overlapping runs.

The first reading after a fresh state is not published: it becomes `pending`
and is published only after a second consistent reading. That is intentional
protection against a wrong first OCR result.

## Requirements

- Python 3.6 or newer (standard library only)
- ImageMagick (`magick` or `convert`)
- Ollama with a vision model, default `qwen2.5vl:7b`
- `mosquitto_pub` for MQTT
- SSH key access to the camera host and, if enabled, the web host
- A Raspberry Pi with `fswebcam` and the camera on `/dev/video0`

## Install

```sh
sudo install -m 0755 boiler_temperature.py /home/balogh/boiler_temperature.py
sudo install -m 0644 boiler-temperature.conf.example /etc/boiler-temperature.conf
sudo install -m 0644 boiler-script.service /etc/systemd/system/boiler-script.service
sudo install -m 0644 boiler-script.timer /etc/systemd/system/boiler-script.timer
sudo systemctl daemon-reload
sudo systemctl enable --now boiler-script.timer
```

Adjust the user and paths in the service and config for your host.

## Configuration

All settings live in `/etc/boiler-temperature.conf` (see
`boiler-temperature.conf.example`).

Validation settings:

- `max_delta`: largest accepted change from the last value/median, in °C.
- `confirm_delta`: two consecutive readings must be this close to confirm a
  large step change.
- `history`: number of accepted values kept for the median.
- `pending_ttl`: how long a pending value may wait for confirmation.
- `*_min` / `*_max`: absolute range per channel.

InfluxDB receives:

- `roof`, `boiler`: last accepted values;
- `roof_valid`, `boiler_valid`: `1` if this run was accepted, `0` if stale;
- `roof_raw`, `boiler_raw`: the OCR values, for debugging.

MQTT publishes the last accepted values to `topic/roof` and `topic/boiler`
(retained by default).

## Test

```sh
python3 -m unittest discover -s tests -v
```

## Manual run

```sh
/usr/bin/python3 /home/balogh/boiler_temperature.py --config /etc/boiler-temperature.conf
```

Use `--dry-run` to capture, OCR and validate without saving state or publishing.

## Troubleshooting

- Logs go to the systemd journal and to `state_dir/boiler.log`.
- `OCR response:` in the log shows the raw JSON from the model.
- `unreadable`, `out of range`, `outlier`, and `needs confirmation` explain why
  a reading was not accepted.
- The HTML page is only updated after at least one accepted value and at most
  once per `web.interval`.
- Check the timer with `systemctl list-timers boiler-script.timer`.

## Legacy

`legacy/` contains the earlier Tesseract, Gemini, and shell experiments. They
are kept for reference and are not used by the service.
