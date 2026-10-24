#!/usr/bin/env python3
import argparse
import base64
import configparser
import fcntl
import json
import logging
import math
import os
import re
import shlex
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from urllib import error, request

DEFAULT_CONFIG = "/etc/boiler-temperature.conf"
CHANNELS = ("roof", "boiler")
PROMPT = (
    "Read the two temperatures displayed after S1: and S2:. "
    'Return ONLY JSON: {"s1": <number or null>, "s2": <number or null>}. '
    "Numbers may be negative and may contain a decimal point. "
    "Do not return units, labels, markdown or any other text."
)

DEFAULTS = {
    "camera": {
        "host": "192.168.1.8",
        "user": "balogh",
        "device": "/dev/video0",
        "width": "640",
        "height": "480",
        "contrast": "70%",
        "brightness": "25%",
        "delay": "2",
        "frames": "10",
        "skip": "30",
        "timeout": "60",
    },
    "ocr": {
        "model": "qwen2.5vl:7b",
        "url": "http://127.0.0.1:11434",
        "crop": "185x105+310+250",
        "timeout": "180",
        "keep_alive": "10m",
    },
    "validation": {
        "roof_min": "-30",
        "roof_max": "100",
        "boiler_min": "0",
        "boiler_max": "120",
        "max_delta": "5",
        "confirm_delta": "1",
        "history": "8",
        "pending_ttl": "1800",
    },
    "influx": {
        "enabled": "1",
        "url": "http://localhost:8086/write?db=boiler",
        "measurement": "temperatures",
        "timeout": "10",
    },
    "mqtt": {
        "enabled": "1",
        "host": "192.168.1.123",
        "topic": "home/boiler",
        "retain": "1",
        "timeout": "10",
    },
    "web": {
        "enabled": "1",
        "host": "juhfark.hu",
        "user": "balogh",
        "dir": "/var/www/html/share",
        "html": "bojler.html",
        "image": "boiler_screen.jpg",
        "interval": "300",
        "timeout": "25",
    },
    "paths": {
        "state_dir": "/home/balogh/.local/state/boiler-temperature",
        "log_file": "",
    },
}


def as_bool(value):
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def to_number(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        value = float(value)
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        match = re.search(r"-?\d+(?:\.\d+)?", value)
        return float(match.group(0)) if match else None
    return None


def load_config(path):
    config = configparser.ConfigParser(interpolation=None)
    config.read_dict(DEFAULTS)
    if path and os.path.exists(path):
        config.read(path)
    return config


def setup_logging(config):
    state_dir = config.get("paths", "state_dir")
    os.makedirs(state_dir, mode=0o700, exist_ok=True)
    log_path = config.get("paths", "log_file") or os.path.join(state_dir, "boiler.log")
    logger = logging.getLogger("boiler")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger, state_dir, log_path
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    logger.addHandler(stream)
    rotating = RotatingFileHandler(log_path, maxBytes=1024 * 1024, backupCount=3)
    rotating.setFormatter(formatter)
    logger.addHandler(rotating)
    return logger, state_dir, log_path


def load_state(path, logger):
    empty = {"version": 1, "channels": {}, "last_web_upload": None}
    if not os.path.exists(path):
        return empty
    try:
        with open(path, encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError) as exc:
        logger.warning("cannot read state %s: %s", path, exc)
        return empty
    state.setdefault("version", 1)
    state.setdefault("channels", {})
    state.setdefault("last_web_upload", None)
    for channel in CHANNELS:
        state["channels"].setdefault(channel, {"history": [], "pending": None})
    return state


def save_state(path, state):
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)
    os.replace(temp, path)


def run_cmd(cmd, timeout, logger, label):
    logger.info("%s: %s", label, cmd if isinstance(cmd, str) else " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("%s timed out after %s seconds" % (label, timeout))
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError("%s failed with %s: %s" % (label, proc.returncode, detail))
    return proc.stdout


def capture_image(config, workdir, logger):
    user = config.get("camera", "user")
    host = config.get("camera", "host")
    remote = "%s@%s" % (user, host)
    command = (
        "fswebcam --set contrast=%s -D %s --set brightness=%s "
        "-F %s -r %sx%s -S %s -d %s boiler_screen.jpg"
        % (
            shlex.quote(config.get("camera", "contrast")),
            shlex.quote(config.get("camera", "delay")),
            shlex.quote(config.get("camera", "brightness")),
            shlex.quote(config.get("camera", "frames")),
            shlex.quote(config.get("camera", "width")),
            shlex.quote(config.get("camera", "height")),
            shlex.quote(config.get("camera", "skip")),
            shlex.quote(config.get("camera", "device")),
        )
    )
    ssh_options = [
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=10",
        "-o", "ServerAliveCountMax=2",
    ]
    timeout = int(config.get("camera", "timeout"))
    run_cmd(["ssh"] + ssh_options + [remote, command], timeout, logger, "camera capture")
    local_image = os.path.join(workdir, "boiler_screen.jpg")
    run_cmd(
        ["scp"] + ssh_options + [remote + ":boiler_screen.jpg", local_image],
        timeout,
        logger,
        "camera fetch",
    )
    if os.path.getsize(local_image) == 0:
        raise RuntimeError("camera returned an empty image")
    return local_image


def crop_image(config, source, workdir, logger):
    magick = shutil.which("magick") or shutil.which("convert")
    if not magick:
        raise RuntimeError("ImageMagick is not installed")
    target = os.path.join(workdir, "crop.png")
    run_cmd(
        [
            magick,
            source,
            "-crop",
            config.get("ocr", "crop"),
            "+repage",
            "-colorspace",
            "Gray",
            "-normalize",
            "-sharpen",
            "0x1.0",
            target,
        ],
        30,
        logger,
        "crop",
    )
    return target


def ocr_image(config, image, logger):
    with open(image, "rb") as handle:
        encoded = base64.b64encode(handle.read()).decode("ascii")
    payload = {
        "model": config.get("ocr", "model"),
        "prompt": PROMPT,
        "images": [encoded],
        "stream": False,
        "format": "json",
        "keep_alive": config.get("ocr", "keep_alive"),
        "options": {"temperature": 0, "seed": 42},
    }
    url = config.get("ocr", "url").rstrip("/") + "/api/generate"
    req = request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with request.urlopen(req, timeout=int(config.get("ocr", "timeout"))) as response:
            body = json.load(response)
    except (error.URLError, ValueError) as exc:
        raise RuntimeError("Ollama request failed: %s" % exc)
    raw = (body.get("response") or "").strip()
    logger.info("OCR response: %s", raw)
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise RuntimeError("OCR response is not valid JSON: %r" % raw)
    if not isinstance(parsed, dict):
        raise RuntimeError("OCR response is not a JSON object: %r" % raw)
    s1 = to_number(parsed.get("s1", parsed.get("S1")))
    s2 = to_number(parsed.get("s2", parsed.get("S2")))
    return {"s1": s1, "s2": s2}


def evaluate_channel(name, channel, value, config, now, logger):
    if value is None or not math.isfinite(value):
        return False, "unreadable"
    minimum = float(config.get("validation", name + "_min"))
    maximum = float(config.get("validation", name + "_max"))
    if value < minimum or value > maximum:
        return False, "out of range (%.1f not in [%.1f, %.1f])" % (value, minimum, maximum)
    history = channel.get("history") or []
    pending = channel.get("pending")
    confirm = float(config.get("validation", "confirm_delta"))
    delta = float(config.get("validation", "max_delta"))
    ttl = float(config.get("validation", "pending_ttl"))
    size = int(config.get("validation", "history"))
    if not history:
        if pending and now - pending["t"] <= ttl and abs(value - pending["v"]) <= confirm:
            return True, "initial value confirmed"
        channel["pending"] = {"t": now, "v": value}
        return False, "initial value needs confirmation"
    recent = [entry["v"] for entry in history[-size:]]
    last = recent[-1]
    median = statistics.median(recent)
    if abs(value - last) <= delta or abs(value - median) <= delta:
        return True, "within %.1f of last %.1f / median %.1f" % (delta, last, median)
    if pending and now - pending["t"] <= ttl and abs(value - pending["v"]) <= confirm:
        return True, "step change confirmed against last %.1f / median %.1f" % (last, median)
    channel["pending"] = {"t": now, "v": value}
    return False, "outlier vs last %.1f / median %.1f (difference %+.1f)" % (last, median, value - last)


def validate_readings(raw, state, config, now, logger):
    results = {}
    size = int(config.get("validation", "history"))
    for name in CHANNELS:
        key = "s1" if name == "roof" else "s2"
        value = raw.get(key)
        channel = state["channels"].setdefault(name, {"history": [], "pending": None})
        channel.setdefault("history", [])
        channel.setdefault("pending", None)
        accepted, reason = evaluate_channel(name, channel, value, config, now, logger)
        if accepted:
            channel["history"].append({"t": now, "v": value})
            channel["history"] = channel["history"][-size:]
            channel["pending"] = None
        last = channel["history"][-1]["v"] if channel["history"] else None
        results[name] = {
            "accepted": accepted,
            "reason": reason,
            "raw": value,
            "last": last,
        }
        logger.info(
            "%s: raw=%s accepted=%s reason=%s last=%s",
            name,
            "%.1f" % value if value is not None else "none",
            accepted,
            reason,
            "%.1f" % last if last is not None else "none",
        )
    return results


def publish_influx(config, results, logger):
    if not as_bool(config.get("influx", "enabled")):
        return
    fields = []
    for name in CHANNELS:
        result = results[name]
        if result["last"] is not None:
            fields.append("%s=%.1f" % (name, result["last"]))
            fields.append("%s_valid=%di" % (name, 1 if result["accepted"] else 0))
        if result["raw"] is not None and math.isfinite(result["raw"]):
            fields.append("%s_raw=%.1f" % (name, result["raw"]))
    if not fields:
        logger.info("influx: no accepted readings yet")
        return
    line = "%s %s" % (config.get("influx", "measurement"), ",".join(fields))
    req = request.Request(
        config.get("influx", "url"),
        data=line.encode("utf-8"),
        headers={"Content-Type": "text/plain"},
    )
    try:
        with request.urlopen(req, timeout=int(config.get("influx", "timeout"))):
            pass
        logger.info("influx: %s", line)
    except error.URLError as exc:
        logger.warning("influx write failed: %s", exc)


def publish_mqtt(config, results, logger):
    if not as_bool(config.get("mqtt", "enabled")):
        return
    publisher = shutil.which("mosquitto_pub") or "/usr/bin/mosquitto_pub"
    host = config.get("mqtt", "host")
    topic = config.get("mqtt", "topic")
    retain = as_bool(config.get("mqtt", "retain"))
    timeout = int(config.get("mqtt", "timeout"))
    for name in CHANNELS:
        value = results[name]["last"]
        if value is None:
            continue
        command = [publisher, "-h", host, "-t", topic + "/" + name, "-m", "%.1f" % value]
        if retain:
            command.append("-r")
        try:
            subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=True,
                timeout=timeout,
                check=True,
            )
            logger.info("mqtt: %s/%s=%s", topic, name, "%.1f" % value)
        except (subprocess.SubprocessError, OSError) as exc:
            logger.warning("mqtt publish failed for %s: %s", name, exc)


def render_html(config, results, logger):
    def display(name):
        last = results[name]["last"]
        if last is None:
            return "N/A"
        if results[name]["accepted"]:
            return "%.1f&deg;C" % last
        return "%.1f&deg;C (stale)" % last

    image = config.get("web", "image")
    html_name = config.get("web", "html")
    updated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    html = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Temperature Readings</title>
<style>
body{display:flex;justify-content:center;align-items:center;height:100vh;margin:0;
background:#f0f0f0;flex-direction:column;font-family:sans-serif}
.temperature{font-size:5em;font-weight:bold;color:#4CAF50;margin:20px;text-align:center}
.updated{color:#666;margin-top:-10px}
.image-container{display:flex;justify-content:center;flex-wrap:wrap}
.image-container img{max-width:100%%;max-height:100%%;height:auto;margin:10px;object-fit:contain}
@media(max-width:600px){.temperature{font-size:2.5em;margin:10px}
.image-container{flex-direction:column;align-items:center}}
</style>
</head>
<body>
<div class="temperature">Roof Temperature: %s</div>
<div class="temperature">Boiler Temperature: %s</div>
<div class="updated">Updated: %s</div>
<div class="image-container"><img src="%s" alt="Boiler display"></div>
</body>
</html>
""" % (display("roof"), display("boiler"), updated, image)
    return html, html_name


def upload_web(config, state, html, image, logger, now):
    if not as_bool(config.get("web", "enabled")):
        return
    interval = float(config.get("web", "interval"))
    previous = state.get("last_web_upload") or 0
    if now - previous < interval:
        return
    user = config.get("web", "user")
    host = config.get("web", "host")
    directory = config.get("web", "dir").rstrip("/")
    remote = "%s@%s:%s/" % (user, host, directory)
    options = [
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=5",
        "-o", "ServerAliveInterval=10",
        "-o", "ServerAliveCountMax=2",
    ]
    try:
        run_cmd(
            ["scp"] + options + [html, image, remote],
            int(config.get("web", "timeout")),
            logger,
            "web upload",
        )
        state["last_web_upload"] = now
        logger.info("web: uploaded %s and image", config.get("web", "html"))
    except RuntimeError as exc:
        logger.warning("web upload failed: %s", exc)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=os.environ.get("BOILER_CONFIG", DEFAULT_CONFIG))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    config = load_config(args.config)
    logger, state_dir, log_path = setup_logging(config)
    state_path = os.path.join(state_dir, "state.json")
    lock_path = os.path.join(state_dir, "lock")
    lock_handle = open(lock_path, "a+")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.info("another run is active, skipping")
        return 0
    state = load_state(state_path, logger)
    now = time.time()
    workdir = tempfile.mkdtemp(prefix="boiler-")
    try:
        image = capture_image(config, workdir, logger)
        cropped = crop_image(config, image, workdir, logger)
        raw = ocr_image(config, cropped, logger)
        results = validate_readings(raw, state, config, now, logger)
        if args.dry_run:
            logger.info("dry run, not saving state or publishing")
            return 0
        save_state(state_path, state)
        publish_influx(config, results, logger)
        publish_mqtt(config, results, logger)
        if any(results[name]["last"] is not None for name in CHANNELS):
            html, html_name = render_html(config, results, logger)
            html_path = os.path.join(workdir, html_name)
            with open(html_path, "w", encoding="utf-8") as handle:
                handle.write(html)
            upload_web(config, state, html_path, image, logger, now)
            save_state(state_path, state)
        return 0
    except Exception as exc:
        logger.error("run failed: %s", exc)
        return 1
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        try:
            fcntl.flock(lock_handle, fcntl.LOCK_UN)
        finally:
            lock_handle.close()


if __name__ == "__main__":
    sys.exit(main())
