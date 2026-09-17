#!/usr/bin/env python3
"""
casio_sale_watcher.py — Poll casiostore.bhawar.com's Shopify storefront for a
live "surprise sale" (discount flip or restock) and push an instant
notification via ntfy.io.

WHY THIS ENDPOINT
    Every Shopify storefront (unless explicitly disabled by the merchant)
    exposes a public, unauthenticated JSON catalog at /products.json. This
    gives structured price / compare_at_price / stock data per variant —
    far more reliable than scraping rendered HTML, and it's what the site's
    own frontend calls under the hood.

DETECTION LOGIC (either one trips an alert)
    1. A variant flips available=false -> available=true
       (every SKU on the site is currently "Sold out").
    2. A variant's compare_at_price > price by more than --min-discount-pct
       (every SKU currently shows "0% Off", i.e. compare_at_price == price).

SETUP (2 minutes)
    1. pip install requests
    2. Install the ntfy app (iOS/Android) — https://ntfy.sh/app — or just
       open https://ntfy.sh/<topic> in a mobile browser tab and leave it.
       Pick a private, hard-to-guess topic name (e.g. "casio-sale-x7f2q").
    3. Run:
           python casio_sale_watcher.py --ntfy-topic casio-sale-x7f2q
    4. Leave it running. See "DEPLOYMENT OPTIONS" at the bottom of this
       file for how to keep it running when your machine is off/asleep.

NOTE ON POLLING INTERVAL
    30s is a reasonable default — frequent enough to catch a flash sale
    within half a minute, not so frequent it looks like abuse. Don't drop
    much below ~15s on a single IP.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests

BASE_URL = "https://casiostore.bhawar.com"
PRODUCTS_ENDPOINT = f"{BASE_URL}/products.json"
STATE_FILE = Path("casio_sale_state.json")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("casio_watcher")


@dataclass
class VariantSnapshot:
    product_handle: str
    product_title: str
    variant_id: int
    price: float
    compare_at_price: Optional[float]
    available: bool


@dataclass
class WatcherConfig:
    ntfy_topic: str
    interval_seconds: int = 30
    min_discount_pct: float = 5.0
    ntfy_server: str = "https://ntfy.sh"


def fetch_all_products(session: requests.Session) -> list[dict]:
    """Pull the full catalog via Shopify's public products.json, paginated."""
    products: list[dict] = []
    page = 1
    while True:
        resp = session.get(
            PRODUCTS_ENDPOINT,
            params={"limit": 250, "page": page},
            headers={"User-Agent": USER_AGENT},
            timeout=15,
        )
        resp.raise_for_status()
        batch = resp.json().get("products", [])
        if not batch:
            break
        products.extend(batch)
        page += 1
        if page > 20:  # safety cap against pagination bugs
            break
    return products


def snapshot_variants(products: list[dict]) -> dict[int, VariantSnapshot]:
    snap: dict[int, VariantSnapshot] = {}
    for p in products:
        for v in p.get("variants", []):
            compare = v.get("compare_at_price")
            snap[v["id"]] = VariantSnapshot(
                product_handle=p["handle"],
                product_title=p["title"],
                variant_id=v["id"],
                price=float(v["price"]),
                compare_at_price=float(compare) if compare else None,
                available=bool(v.get("available", False)),
            )
    return snap


def discount_pct(v: VariantSnapshot) -> float:
    if not v.compare_at_price or v.compare_at_price <= v.price:
        return 0.0
    return round((1 - v.price / v.compare_at_price) * 100, 1)


def detect_changes(
    prev: dict[int, VariantSnapshot],
    curr: dict[int, VariantSnapshot],
    cfg: WatcherConfig,
) -> list[str]:
    """Return one human-readable alert line per variant that just flipped."""
    alerts: list[str] = []
    for vid, cur_v in curr.items():
        prev_v = prev.get(vid)
        pct = discount_pct(cur_v)
        url = f"{BASE_URL}/products/{cur_v.product_handle}"

        went_in_stock = bool(prev_v) and not prev_v.available and cur_v.available
        newly_discounted = pct >= cfg.min_discount_pct and (
            prev_v is None or discount_pct(prev_v) < cfg.min_discount_pct
        )

        if went_in_stock or newly_discounted:
            reason = "back in stock" if went_in_stock else f"{pct}% off"
            alerts.append(f"{cur_v.product_title} — {reason} — {url}")
    return alerts


def load_state() -> dict[int, VariantSnapshot]:
    if not STATE_FILE.exists():
        return {}
    raw = json.loads(STATE_FILE.read_text())
    return {int(k): VariantSnapshot(**v) for k, v in raw.items()}


def save_state(snap: dict[int, VariantSnapshot]) -> None:
    raw = {str(vid): vars(v) for vid, v in snap.items()}
    STATE_FILE.write_text(json.dumps(raw))


def notify(cfg: WatcherConfig, message: str) -> None:
    try:
        requests.post(
            f"{cfg.ntfy_server}/{cfg.ntfy_topic}",
            data=message.encode("utf-8"),
            headers={
                "Title": "Casio surprise sale is LIVE",
                "Priority": "urgent",
                "Tags": "rotating_light",
            },
            timeout=10,
        )
        log.info("Notification sent: %s", message)
    except requests.RequestException as e:
        log.error("Failed to send notification: %s", e)


def poll_once(session: requests.Session, prev: dict[int, VariantSnapshot], cfg: WatcherConfig) -> dict[int, VariantSnapshot]:
    """One fetch/compare/notify/save cycle. Shared by --once and the loop below."""
    products = fetch_all_products(session)
    curr = snapshot_variants(products)
    if prev:
        for alert in detect_changes(prev, curr, cfg):
            notify(cfg, alert)
    else:
        log.info("No prior state found — baselining current catalog, no alert this pass.")
    save_state(curr)
    return curr


def run_once(cfg: WatcherConfig) -> None:
    """Single-shot mode for external schedulers (e.g. GitHub Actions cron)."""
    session = requests.Session()
    poll_once(session, load_state(), cfg)


def run(cfg: WatcherConfig) -> None:
    """Continuous mode for an always-on host (VPS / Pi / Termux)."""
    session = requests.Session()
    prev = load_state()
    log.info(
        "Watching %s every %ss (ntfy topic: %s)",
        BASE_URL, cfg.interval_seconds, cfg.ntfy_topic,
    )
    backoff = cfg.interval_seconds
    while True:
        try:
            prev = poll_once(session, prev, cfg)
            backoff = cfg.interval_seconds
        except requests.RequestException as e:
            log.warning("Fetch failed (%s) — backing off to %ss", e, backoff)
            backoff = min(backoff * 2, 600)
        time.sleep(backoff + random.uniform(0, 3))  # jitter, avoid lockstep polling


def parse_args() -> tuple[WatcherConfig, bool]:
    ap = argparse.ArgumentParser(description="Watch casiostore.bhawar.com for a surprise sale.")
    ap.add_argument("--ntfy-topic", required=True, help="Your private ntfy.sh topic name")
    ap.add_argument("--interval", type=int, default=30, help="Poll interval in seconds (continuous mode only)")
    ap.add_argument("--min-discount-pct", type=float, default=5.0)
    ap.add_argument("--once", action="store_true", help="Run a single check and exit (for cron/GitHub Actions)")
    args = ap.parse_args()
    cfg = WatcherConfig(
        ntfy_topic=args.ntfy_topic,
        interval_seconds=args.interval,
        min_discount_pct=args.min_discount_pct,
    )
    return cfg, args.once


if __name__ == "__main__":
    try:
        cfg, once = parse_args()
        run_once(cfg) if once else run(cfg)
    except KeyboardInterrupt:
        sys.exit(0)

# ---------------------------------------------------------------------------
# DEPLOYMENT OPTIONS (ranked — a script only alerts while it's running)
#
# 1. Always-on box (Raspberry Pi / free-tier VPS, e.g. Oracle Cloud Free):
#      nohup python3 casio_sale_watcher.py --ntfy-topic X &
#    True near-real-time (30s), zero cost, doesn't depend on your laptop
#    being open. Best option if "immediately" matters.
#
# 2. Your own machine, kept open:
#      Run in a terminal / tmux session, or as a systemd/launchd service.
#    Free, but only alerts while the machine is awake.
#
# 3. GitHub Actions scheduled workflow (cron: every 5 min, its minimum
#    granularity) running this script in a single-shot mode against the
#    persisted state committed back to the repo. Zero infra, runs even
#    with your laptop off, but "immediate" becomes "within 5 minutes."
#
# 4. No-code website-change monitors (Visualping, Distill.io free tiers):
#    fastest to set up, but they diff rendered HTML, not the JSON price/
#    stock fields — much more prone to missing a fast discount flip or
#    false-triggering on unrelated banner rotation.
# ---------------------------------------------------------------------------
