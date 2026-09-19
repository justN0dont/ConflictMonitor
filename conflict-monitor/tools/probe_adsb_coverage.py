"""Probe ADS-B coverage and integrity-field availability at named points.

Answers two questions the interference layer depends on:
  1. Is there any receiver coverage at this point? (Baghdad and Tehran: no. Hormuz: yes.)
  2. Do the returned aircraft carry nic / nac_p, without which degradation cannot be measured?

adsb.lol rejects a generic User-Agent with 403 ("include valid contact info"), so one is set below.
api.airplanes.live returns 403 unconditionally and requires prior arrangement by email.

Usage:  python tools/probe_adsb_coverage.py
"""
import json, urllib.request

UA = "conflict-monitor/1.0 (+https://github.com/troofevades-rgb/conflict-monitor)"
POINTS = [
    ("AO-Baghdad",     33.3, 44.4, 250),
    ("AO-Tehran",      35.7, 51.4, 250),
    ("AO-Hormuz",      26.6, 56.3, 250),
    ("AO-TelAviv",     32.0, 34.8, 250),
    ("CTRL-Baltic",    55.0, 21.0, 250),   # known heavily jammed; expect a high degraded ratio
]
# gpsjam.org thresholds
NIC_MIN, NACP_MIN = 7, 8

def fetch(lat, lon, nm):
    url = f"https://api.adsb.lol/v2/lat/{lat}/lon/{lon}/dist/{nm}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.loads(r.read().decode())

print(f"{'point':<16}{'ac':>6}{'nic':>6}{'nacp':>6}{'degraded':>10}{'ratio':>8}")
for name, lat, lon, nm in POINTS:
    try:
        ac = [a for a in (fetch(lat, lon, nm).get("ac") or []) if a.get("lat") is not None]
    except Exception as e:
        print(f"{name:<16}  FAILED: {type(e).__name__} {e}")
        continue
    nic  = [a for a in ac if a.get("nic") is not None]
    nacp = [a for a in ac if a.get("nac_p") is not None]
    deg  = [a for a in ac if (a.get("nic") is not None and a["nic"] < NIC_MIN)
                          or (a.get("nac_p") is not None and a["nac_p"] < NACP_MIN)]
    ratio = f"{100*len(deg)/len(nic):.1f}%" if nic else "n/a"
    print(f"{name:<16}{len(ac):>6}{len(nic):>6}{len(nacp):>6}{len(deg):>10}{ratio:>8}")
