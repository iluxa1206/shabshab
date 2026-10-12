#!/usr/bin/env python3
"""Обновляет outbound'ы xray-прокси (tgproxy) из подписки VLESS-Reality.

Запускается на проде по cron. URL подписки — в /root/xray/sub.url (chmod 600),
в репозиторий и в лог он не попадает. Конфиг меняется ТОЛЬКО если набор
серверов изменился, прошёл `xray run -test`, и подписка отдала >= 1 ссылки.
Старый config.json сохраняется как config.json.bak-<дата>.
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.parse as up
import urllib.request

XRAY_DIR = os.environ.get("XRAY_DIR", "/root/xray")
CONFIG = os.path.join(XRAY_DIR, "config.json")
SUB_FILE = os.path.join(XRAY_DIR, "sub.url")
CONTAINER = os.environ.get("XRAY_CONTAINER", "floaters-prod-tgproxy-1")
IMAGE = os.environ.get("XRAY_IMAGE", "ghcr.io/xtls/xray-core:latest")
PROBE = {"subjectSelector": ["vless-"], "probeUrl": "https://www.gstatic.com/generate_204",
         "probeInterval": "60s", "enableConcurrency": True}


def log(msg: str) -> None:
    print(time.strftime("%F %T"), msg, flush=True)


def fetch_links(url: str) -> list:
    req = urllib.request.Request(url, headers={"User-Agent": "v2rayN/6.0"})
    raw = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore").strip()
    if "vless://" not in raw:
        raw = base64.b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8", "ignore")
    return [l.strip() for l in raw.splitlines() if l.strip().startswith("vless://")]


def outbound(tag: str, link: str) -> dict:
    p = up.urlparse(link)
    q = dict(up.parse_qsl(p.query))
    if q.get("security") != "reality" or not (p.hostname and p.port and p.username and q.get("pbk")):
        raise ValueError("не VLESS-Reality или неполная ссылка")
    return {
        "tag": tag, "protocol": "vless",
        "settings": {"vnext": [{"address": p.hostname, "port": p.port, "users": [
            {"id": p.username, "encryption": q.get("encryption", "none"), "flow": q.get("flow", "")}]}]},
        "streamSettings": {"network": q.get("type", "tcp"), "security": "reality",
                           "realitySettings": {"serverName": q.get("sni", ""),
                                               "fingerprint": q.get("fp", "chrome"),
                                               "publicKey": q["pbk"], "shortId": q.get("sid", ""),
                                               "spiderX": q.get("spx", "")}}}


def build(old: dict, links: list) -> dict:
    outs, seen = [], set()
    for l in links:
        try:
            o = outbound("vless-%d" % (len(outs) + 1), l)
        except ValueError as e:
            log("пропуск ссылки: %s" % e)
            continue
        key = json.dumps(o["settings"], sort_keys=True) + json.dumps(o["streamSettings"], sort_keys=True)
        if key not in seen:
            seen.add(key)
            outs.append(o)
    if not outs:
        raise SystemExit("подписка не дала ни одного рабочего сервера — конфиг не трогаю")
    return {"log": old["log"], "inbounds": old["inbounds"], "outbounds": outs,
            "observatory": PROBE,
            "routing": {"domainStrategy": "AsIs",
                        "balancers": [{"tag": "bal", "selector": ["vless-"],
                                       "strategy": {"type": "leastPing"}}],
                        "rules": [{"type": "field", "inboundTag": ["socks-in"], "balancerTag": "bal"}]}}


def validate(path: str) -> None:
    d, name = os.path.split(path)
    r = subprocess.run(["docker", "run", "--rm", "--user", "root", "-v", "%s:/etc/x:ro" % d, IMAGE,
                        "run", "-test", "-format", "json", "-c", "/etc/x/" + name],
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise SystemExit("xray -test: %s" % (r.stdout + r.stderr).strip()[-300:])


def main() -> None:
    url = open(SUB_FILE).read().strip()
    old = json.load(open(CONFIG))
    links = fetch_links(url)
    new = build(old, links)
    sig = lambda c: sorted(json.dumps({k: v for k, v in o.items() if k != "tag"}, sort_keys=True)
                           for o in c.get("outbounds", []))
    if sig(old) == sig(new):
        log("без изменений (%d серверов)" % len(new["outbounds"]))
        return
    fd, tmp = tempfile.mkstemp(prefix="config.", suffix=".json", dir=XRAY_DIR)
    with os.fdopen(fd, "w") as f:
        json.dump(new, f, indent=1)
    os.chmod(tmp, 0o600)
    try:
        validate(tmp)
    except BaseException:
        os.unlink(tmp)
        raise
    bak = "%s.bak-%s" % (CONFIG, time.strftime("%Y%m%d-%H%M"))
    os.replace(CONFIG, bak)
    os.replace(tmp, CONFIG)
    try:
        subprocess.run(["docker", "restart", CONTAINER], check=True, capture_output=True, timeout=60)
    except BaseException:
        os.replace(bak, CONFIG)
        raise
    log("конфиг обновлён: %d серверов, бэкап %s, tgproxy перезапущен"
        % (len(new["outbounds"]), os.path.basename(bak)))
    for old_bak in sorted(f for f in os.listdir(XRAY_DIR) if ".bak-" in f)[:-5]:
        os.unlink(os.path.join(XRAY_DIR, old_bak))


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        log("ОШИБКА: %s" % e)
        sys.exit(1)
    except Exception as e:
        log("ОШИБКА: %s: %s" % (type(e).__name__, e))
        sys.exit(1)
