#!/usr/bin/env python3
# vk_phone_harvester.py — scrape phone numbers from VK groups/publics (wall, comments, info)
# deps: pip install requests phonenumbers
# usage: VK_TOKEN=<access_token> python vk_phone_harvester.py <group> [<group> ...] [--out out.csv]
#        <group> can be screen_name (durov, apiclub) or numeric id (1, -1).
# notes:
#   - VK API v5.199. User token recommended (service tokens cannot read most comments / closed groups).
#   - Respects rate limits with sleep + retry on error codes 6 and 29.
#   - Phone candidates are validated with libphonenumber; stored in E.164.
#   - Region hint defaults to RU; override with --region US (etc.).

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Iterator

import phonenumbers
import requests
from phonenumbers import PhoneNumberMatcher

API = "https://api.vk.com/method"
V = "5.199"

# Loose phone candidate pattern — libphonenumber validates after.
PHONE_RE = re.compile(
    r"(?:(?:\+|00)\d{1,3}[\s\-\.\(\)]*)?"
    r"(?:\(?\d{2,4}\)?[\s\-\.]*)?"
    r"\d{2,4}[\s\-\.]*\d{2,4}[\s\-\.]*\d{0,4}"
)

log = logging.getLogger("vk")


class VKError(Exception):
    pass


@dataclass
class Harvest:
    numbers: dict[str, set[str]] = field(default_factory=dict)  # E.164 -> source URLs

    def add(self, number: str, source: str) -> None:
        self.numbers.setdefault(number, set()).add(source)


class Client:
    def __init__(self, token: str, rps: float = 3.0):
        self.token = token
        self.min_interval = 1.0 / rps
        self._last = 0.0
        self.session = requests.Session()

    def call(self, method: str, **params) -> dict | list:
        params["access_token"] = self.token
        params["v"] = V
        for attempt in range(6):
            dt = time.monotonic() - self._last
            if dt < self.min_interval:
                time.sleep(self.min_interval - dt)
            try:
                r = self.session.get(f"{API}/{method}", params=params, timeout=30)
            except requests.RequestException as e:
                log.warning("%s network error: %s", method, e)
                time.sleep(2 ** attempt)
                continue
            self._last = time.monotonic()
            try:
                data = r.json()
            except json.JSONDecodeError:
                time.sleep(2 ** attempt)
                continue
            if "error" in data:
                code = data["error"].get("error_code")
                msg = data["error"].get("error_msg", "")
                if code in (6, 29):  # rate limit / too many requests
                    time.sleep(1 + attempt)
                    continue
                if code in (7, 15, 18, 203, 212):  # permission / blocked / access denied
                    log.warning("access denied on %s: %s", method, msg)
                    return {"items": [], "count": 0}
                raise VKError(f"{method}: {msg} (code {code})")
            return data["response"]
        raise VKError(f"{method}: retries exhausted")


def resolve_group(c: Client, ident: str) -> tuple[int, str]:
    """Returns (owner_id_negative, screen_name)."""
    key = ident.lstrip("-") if ident.lstrip("-").isdigit() else ident
    r = c.call("groups.getById", group_id=key)
    g = r["groups"][0] if isinstance(r, dict) and "groups" in r else r[0]
    return -int(g["id"]), g.get("screen_name", str(g["id"]))


def iter_wall(c: Client, owner_id: int) -> Iterator[dict]:
    offset = 0
    while True:
        r = c.call("wall.get", owner_id=owner_id, offset=offset, count=100)
        items = r.get("items", []) if isinstance(r, dict) else []
        if not items:
            return
        yield from items
        offset += len(items)
        if offset >= r.get("count", 0):
            return


def iter_comments(c: Client, owner_id: int, post_id: int) -> Iterator[dict]:
    offset = 0
    while True:
        r = c.call(
            "wall.getComments",
            owner_id=owner_id,
            post_id=post_id,
            offset=offset,
            count=100,
            need_likes=0,
            thread_items_count=10,
            preview_length=0,
        )
        items = r.get("items", []) if isinstance(r, dict) else []
        if not items:
            return
        for cm in items:
            yield cm
            for sub in cm.get("thread", {}).get("items", []) or []:
                yield sub
        offset += len(items)
        if offset >= r.get("count", 0):
            return


def iter_group_info(c: Client, owner_id: int) -> Iterator[tuple[str, str]]:
    gid = abs(owner_id)
    r = c.call(
        "groups.getById",
        group_id=gid,
        fields="description,status,contacts,links,addresses,site",
    )
    g = (r["groups"] if isinstance(r, dict) and "groups" in r else r)[0]
    screen = g.get("screen_name", str(gid))
    base = f"https://vk.com/{screen}"
    for key in ("description", "status", "site"):
        if g.get(key):
            yield str(g[key]), base
    for contact in g.get("contacts", []) or []:
        if contact.get("phone"):
            yield str(contact["phone"]), base
        if contact.get("desc"):
            yield str(contact["desc"]), base
    for ln in g.get("links", []) or []:
        chunks = " ".join(
            x for x in (ln.get("name"), ln.get("desc"), ln.get("url")) if x
        )
        if chunks:
            yield chunks, base


def extract_phones(text: str, source: str, h: Harvest, region: str = "RU") -> None:
    if not text:
        return
    text = text.replace(" ", " ").replace("\xa0", " ")

    # 1) libphonenumber matcher — strongest signal.
    try:
        for m in PhoneNumberMatcher(text, region):
            if phonenumbers.is_valid_number(m.number):
                e164 = phonenumbers.format_number(
                    m.number, phonenumbers.PhoneNumberFormat.E164
                )
                h.add(e164, source)
    except Exception:
        pass

    # 2) Regex fallback for compact or oddly-punctuated numbers the matcher skips.
    for raw in PHONE_RE.findall(text):
        digits = re.sub(r"\D", "", raw)
        if not (10 <= len(digits) <= 15):
            continue
        parsed = None
        for candidate in (raw, "+" + digits):
            try:
                parsed = phonenumbers.parse(candidate, region)
                if phonenumbers.is_valid_number(parsed):
                    break
                parsed = None
            except phonenumbers.NumberParseException:
                continue
        if parsed is not None:
            e164 = phonenumbers.format_number(
                parsed, phonenumbers.PhoneNumberFormat.E164
            )
            h.add(e164, source)


def collect_post_text(post: dict) -> str:
    parts: list[str] = [post.get("text", "") or ""]
    for att in post.get("attachments", []) or []:
        t = att.get("type")
        payload = att.get(t, {}) if t else {}
        for key in ("title", "description", "text", "caption", "url", "phone"):
            v = payload.get(key)
            if v:
                parts.append(str(v))
    # Reposts carry copy_history with their own payload.
    for repost in post.get("copy_history", []) or []:
        parts.append(collect_post_text(repost))
    return "\n".join(parts)


def harvest_group(
    c: Client, ident: str, h: Harvest, region: str, with_comments: bool
) -> None:
    owner_id, screen = resolve_group(c, ident)
    log.info("harvesting %s (owner_id=%d)", screen, owner_id)

    for text, src in iter_group_info(c, owner_id):
        extract_phones(text, src, h, region)

    posts_seen = 0
    for post in iter_wall(c, owner_id):
        pid = post["id"]
        post_url = f"https://vk.com/wall{owner_id}_{pid}"
        extract_phones(collect_post_text(post), post_url, h, region)
        if with_comments and post.get("comments", {}).get("count", 0) > 0:
            for cm in iter_comments(c, owner_id, pid):
                cm_url = f"{post_url}?reply={cm.get('id')}"
                extract_phones(cm.get("text", ""), cm_url, h, region)
        posts_seen += 1
        if posts_seen % 100 == 0:
            log.info("  %s: %d posts scanned, %d unique numbers", screen, posts_seen, len(h.numbers))
    log.info("done %s: %d posts, %d unique numbers so far", screen, posts_seen, len(h.numbers))


def write_csv(h: Harvest, path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["phone_e164", "source_count", "sources"])
        for number in sorted(h.numbers):
            srcs = sorted(h.numbers[number])
            w.writerow([number, len(srcs), " | ".join(srcs)])


def main() -> int:
    ap = argparse.ArgumentParser(description="Harvest phone numbers from VK groups.")
    ap.add_argument("groups", nargs="+", help="group screen_names or ids")
    ap.add_argument("--out", default="vk_phones.csv", help="output CSV path")
    ap.add_argument("--region", default="RU", help="default region hint (ISO-2)")
    ap.add_argument("--no-comments", action="store_true", help="skip comments")
    ap.add_argument("--rps", type=float, default=3.0, help="requests per second")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    token = os.environ.get("VK_TOKEN")
    if not token:
        print("set VK_TOKEN env var (user or service access_token)", file=sys.stderr)
        return 2

    c = Client(token, rps=args.rps)
    h = Harvest()
    for ident in args.groups:
        try:
            harvest_group(c, ident, h, args.region, not args.no_comments)
        except VKError as e:
            log.error("group %s failed: %s", ident, e)

    write_csv(h, args.out)
    log.info("wrote %d unique numbers to %s", len(h.numbers), args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
