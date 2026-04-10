from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import requests

API_BASE = "https://api.mantrachain.io"
HTML_PATH = Path(__file__).resolve().parent / "tokenomics_daily.html"
BLOCKS_PER_YEAR = 9_562_910
SECONDS_PER_YEAR = 365 * 24 * 60 * 60
SECONDS_PER_BLOCK = SECONDS_PER_YEAR / BLOCKS_PER_YEAR
BUCKET_KEYS = ("mi", "le", "ou", "gd", "tb", "tr", "ps", "se", "ec")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def parse_block_header(payload: dict) -> tuple[int, datetime]:
    candidates = [payload, payload.get("block", {}), payload.get("sdk_block", {})]
    for candidate in candidates:
        header = candidate.get("header") if isinstance(candidate, dict) else None
        if isinstance(header, dict) and "height" in header and "time" in header:
            return int(header["height"]), parse_time(header["time"])
    raise ValueError("Unable to parse block header from response")


class MantraLCD:
    def __init__(self) -> None:
        self.session = requests.Session()
        self._block_cache: dict[int, datetime] = {}

    def _get_json(self, path: str, *, headers: dict[str, str] | None = None) -> dict:
        response = self.session.get(f"{API_BASE}{path}", headers=headers, timeout=30)
        response.raise_for_status()
        return response.json()

    def latest_block(self) -> tuple[int, datetime]:
        payload = self._get_json("/cosmos/base/tendermint/v1beta1/blocks/latest")
        height, block_time = parse_block_header(payload)
        self._block_cache[height] = block_time
        return height, block_time

    def block_time(self, height: int) -> datetime:
        if height in self._block_cache:
            return self._block_cache[height]
        payload = self._get_json(f"/cosmos/base/tendermint/v1beta1/blocks/{height}")
        parsed_height, block_time = parse_block_header(payload)
        if parsed_height != height:
            raise ValueError(f"Expected height {height}, got {parsed_height}")
        self._block_cache[height] = block_time
        return block_time

    def annual_provisions(self, height: int) -> float:
        payload = self._get_json(
            "/cosmos/mint/v1beta1/annual_provisions",
            headers={"x-cosmos-block-height": str(height)},
        )
        value = payload.get("annual_provisions")
        if value is None:
            raise ValueError("annual_provisions missing from response")
        return float(value)


def find_first_block_at_or_after(
    lcd: MantraLCD,
    target_time: datetime,
    latest_height: int,
    latest_time: datetime,
) -> int:
    if target_time > latest_time:
        raise ValueError("Target time is in the future")

    est_delta = int((latest_time - target_time).total_seconds() / SECONDS_PER_BLOCK)
    guess = max(1, min(latest_height, latest_height - est_delta))
    guess_time = lcd.block_time(guess)

    day_span = max(1, int(24 * 60 * 60 / SECONDS_PER_BLOCK))
    step = day_span

    if guess_time >= target_time:
        high = guess
        low = max(1, high - step)
        while low > 1 and lcd.block_time(low) >= target_time:
            high = low
            step *= 2
            low = max(1, high - step)
        if low == 1 and lcd.block_time(low) >= target_time:
            return 1
    else:
        low = guess
        high = min(latest_height, low + step)
        while high < latest_height and lcd.block_time(high) < target_time:
            low = high
            step *= 2
            high = min(latest_height, low + step)
        if lcd.block_time(high) < target_time:
            raise ValueError("Could not bracket target time")

    while low + 1 < high:
        mid = (low + high) // 2
        if lcd.block_time(mid) >= target_time:
            high = mid
        else:
            low = mid

    first = high
    first_time = lcd.block_time(first)
    if first_time < target_time:
        raise ValueError("Boundary search failed for first block")
    if first > 1 and lcd.block_time(first - 1) >= target_time:
        raise ValueError("Found block is not the first block after target")
    return first


def sum_buckets(row: dict) -> float:
    return sum(float(row[key]) for key in BUCKET_KEYS)


def norm_number(value: float, decimals: int = 2) -> int | float:
    rounded = round(value, decimals)
    return int(rounded) if float(rounded).is_integer() else rounded


def update_raw(raw: list[dict], target_date: str, minted_mantra: float) -> bool:
    target_idx = next((i for i, row in enumerate(raw) if row.get("d") == target_date), None)
    if target_idx is None:
        raise ValueError(f"Date {target_date} not found in RAW")
    if target_idx == 0:
        raise ValueError("Target date has no previous day for cumulative calculation")

    target_row = raw[target_idx]
    if target_row.get("fc") is False:
        return False

    prev_ci = float(raw[target_idx - 1]["ci"])
    target_row["di"] = norm_number(minted_mantra)
    target_row["ci"] = norm_number(prev_ci + float(target_row["di"]))
    target_row["fc"] = False

    for i in range(target_idx, len(raw)):
        row = raw[i]
        if i > target_idx:
            if not row.get("fc", False):
                break
            row["ci"] = norm_number(float(raw[i - 1]["ci"]) + float(row["di"]))

        ts_offset = float(row["ts"]) - float(row["cs"]) - float(row["lk"])
        row["gc"] = norm_number(sum_buckets(row), decimals=0)
        row["cs"] = norm_number(float(row["gc"]) + float(row["ci"]))
        row["ts"] = norm_number(float(row["cs"]) + float(row["lk"]) + ts_offset)

    return True


def update_html_raw(target_date: str, minted_mantra: float) -> bool:
    html = HTML_PATH.read_text(encoding="utf-8")
    pattern = re.compile(r"const RAW = (\[.*?\]);", re.DOTALL)
    match = pattern.search(html)
    if not match:
        raise ValueError("RAW constant not found in tokenomics_daily.html")

    raw = json.loads(match.group(1))
    changed = update_raw(raw, target_date, minted_mantra)
    if not changed:
        return False

    new_raw = json.dumps(raw, separators=(",", ":"))
    updated_html = f"{html[:match.start(1)]}{new_raw}{html[match.end(1):]}"
    HTML_PATH.write_text(updated_html, encoding="utf-8")
    return True


def main() -> None:
    """Update yesterday's forecast row with actual inflation data from MANTRA LCD."""
    target_day = date.today() - timedelta(days=1)
    target_start = datetime.combine(target_day, time.min, tzinfo=timezone.utc)
    next_start = target_start + timedelta(days=1)

    lcd = MantraLCD()
    latest_height, latest_time = lcd.latest_block()

    first_block = find_first_block_at_or_after(lcd, target_start, latest_height, latest_time)
    first_next_block = find_first_block_at_or_after(lcd, next_start, latest_height, latest_time)
    last_block = first_next_block - 1

    if last_block < first_block:
        raise ValueError("Invalid block range for target day")

    first_time = lcd.block_time(first_block)
    last_time = lcd.block_time(last_block)
    if first_time < target_start:
        raise ValueError("first_block validation failed")
    if last_time >= next_start:
        raise ValueError("last_block validation failed")

    prov_start = lcd.annual_provisions(first_block)
    prov_end = lcd.annual_provisions(last_block)

    blocks_in_day = last_block - first_block + 1
    avg_provisions = (prov_start + prov_end) / 2
    minted_amantra_base_units = avg_provisions * blocks_in_day / BLOCKS_PER_YEAR
    minted_mantra = round(minted_amantra_base_units / 1e18, 2)

    changed = update_html_raw(target_day.isoformat(), minted_mantra)
    if changed:
        print(
            f"Updated {target_day.isoformat()} with di={minted_mantra}, "
            f"first_block={first_block}, last_block={last_block}"
        )
    else:
        print(f"No update needed for {target_day.isoformat()} (already actual)")


if __name__ == "__main__":
    main()
