#!/usr/bin/env python3
"""Refresh data/contributions.json: last year's contribution calendar + public repo metadata.

With GITHUB_TOKEN (or GH_TOKEN) set, uses the GraphQL API; this is what the
scheduled Action does. Without a token it falls back to the public contribution
calendar page and the unauthenticated REST API, so you can refresh locally.

    GITHUB_LOGIN=sl1ghtlyyy python scripts/fetch_data.py
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

LOGIN = os.environ.get("GITHUB_LOGIN", "sl1ghtlyyy")
OUT = Path(__file__).resolve().parent.parent / "data" / "contributions.json"
UA = {"User-Agent": "profile-cards-refresh"}

LEVELS = {"NONE": 0, "FIRST_QUARTILE": 1, "SECOND_QUARTILE": 2, "THIRD_QUARTILE": 3, "FOURTH_QUARTILE": 4}

QUERY = """
query($login: String!) {
  user(login: $login) {
    contributionsCollection {
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date contributionCount contributionLevel } }
      }
    }
    repositories(first: 100, ownerAffiliations: OWNER, privacy: PUBLIC,
                 orderBy: {field: PUSHED_AT, direction: DESC}) {
      nodes {
        name description url stargazerCount forkCount pushedAt
        primaryLanguage { name color }
      }
    }
  }
}
"""


def _get(url: str, headers: dict | None = None, data: bytes | None = None) -> bytes:
    req = urllib.request.Request(url, data=data, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def via_graphql(token: str) -> dict:
    body = json.dumps({"query": QUERY, "variables": {"login": LOGIN}}).encode()
    res = json.loads(_get("https://api.github.com/graphql",
                          {"Authorization": f"bearer {token}", "Content-Type": "application/json"}, body))
    if res.get("errors"):
        sys.exit(f"GraphQL error: {res['errors']}")
    user = res["data"]["user"]
    cal = user["contributionsCollection"]["contributionCalendar"]
    days = [{"date": d["date"], "count": d["contributionCount"], "level": LEVELS[d["contributionLevel"]]}
            for w in cal["weeks"] for d in w["contributionDays"]]
    repos = [{
        "name": n["name"], "url": n["url"], "description": n["description"] or "",
        "stars": n["stargazerCount"], "forks": n["forkCount"], "pushed_at": n["pushedAt"],
        "language": (n["primaryLanguage"] or {}).get("name"),
        "language_color": (n["primaryLanguage"] or {}).get("color"),
    } for n in user["repositories"]["nodes"]]
    return {"total": cal["totalContributions"], "days": days, "repos": repos}


def via_public_pages() -> dict:
    page = _get(f"https://github.com/users/{LOGIN}/contributions").decode()
    cells = {}
    for td in re.findall(r"<td[^>]*data-date=[^>]*>", page):
        cid = re.search(r'id="([^"]+)"', td).group(1)
        cells[cid] = {"date": re.search(r'data-date="([^"]+)"', td).group(1),
                      "level": int(re.search(r'data-level="(\d)"', td).group(1)), "count": 0}
    for cid, text in re.findall(r'<tool-tip[^>]*for="([^"]+)"[^>]*>([^<]*)</tool-tip>', page):
        m = re.match(r"\s*([\d,]+) contributions? on", html.unescape(text))
        if cid in cells and m:
            cells[cid]["count"] = int(m.group(1).replace(",", ""))
    days = sorted(cells.values(), key=lambda d: d["date"])
    if not days:
        sys.exit("Could not parse the public contribution calendar; set GITHUB_TOKEN to use GraphQL.")
    raw = json.loads(_get(f"https://api.github.com/users/{LOGIN}/repos?per_page=100&sort=pushed&type=owner"))
    repos = [{
        "name": r["name"], "url": r["html_url"], "description": r["description"] or "",
        "stars": r["stargazers_count"], "forks": r["forks_count"], "pushed_at": r["pushed_at"],
        "language": r["language"], "language_color": None,  # REST has no colour; renderer falls back
    } for r in raw if not r["fork"]]
    return {"total": sum(d["count"] for d in days), "days": days, "repos": repos}


def main() -> None:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    data = via_graphql(token) if token else via_public_pages()
    data = {"login": LOGIN, "source": "graphql" if token else "public-pages",
            "generated_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(), **data}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, indent=1) + "\n")
    print(f"wrote {OUT.relative_to(Path.cwd()) if OUT.is_relative_to(Path.cwd()) else OUT}: "
          f"{len(data['days'])} days, {data['total']} contributions, {len(data['repos'])} repos ({data['source']})")


if __name__ == "__main__":
    main()
