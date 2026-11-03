import csv
import datetime
import hashlib
import io
import logging
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

LOCATION_REGEX = re.compile(r"^Sector\s+([0-9]+[a-zA-Z]?)\s*,\s*(.+)$", re.IGNORECASE)

CITY_CANONICAL_MAP = {
    "noida": "Noida",
    "gurugram": "Gurugram",
    "gurgaon": "Gurugram",
    "greater noida west": "Greater Noida West",
}

VALID_AVAILABILITIES = {"available", "reserved", "sold"}

REQUIRED_HEADERS = {
    "property_id",
    "project_name",
    "location",
    "configuration_bhk",
    "availability",
    "listing_updated_at",
}


class ParserError(Exception):
    """Base parser error."""


class MissingHeadersError(ParserError):
    """Raised when required CSV headers are missing."""


@dataclass
class ParsedProperty:
    property_id: str
    project_name: str
    location_raw: str
    city: str
    sector: str
    configuration_bhk: int
    price_lakh: Decimal | None
    price_per_sqft: Decimal | None
    availability: str
    floor: int | None
    area_sqft: int | None
    listing_updated_at: datetime.date
    row_hash: str


@dataclass
class ParseError:
    row_number: int
    property_id: str | None
    reason: str
    raw_row: dict[str, Any]


@dataclass
class ParseResult:
    properties: list[ParsedProperty]
    errors: list[ParseError]
    total_rows_read: int
    file_sha256: str


def parse_location(raw_location: str) -> tuple[str, str] | None:
    """Parse 'Sector <digits+optional letter>, <city>' into (sector, city)."""
    match = LOCATION_REGEX.match(raw_location.strip())
    if not match:
        return None

    sector = match.group(1).upper()
    city_part = match.group(2).strip()
    city_key = city_part.lower()

    if city_key in CITY_CANONICAL_MAP:
        city = CITY_CANONICAL_MAP[city_key]
    else:
        city = city_part.title()
        logger.warning(
            "Unknown city name '%s' in location '%s'; defaulting to title case '%s'",
            city_part,
            raw_location,
            city,
        )

    return sector, city


def compute_row_hash(
    property_id: str,
    project_name: str,
    location_raw: str,
    city: str,
    sector: str,
    configuration_bhk: int,
    price_lakh: Decimal | None,
    availability: str,
    floor: int | None,
    area_sqft: int | None,
    listing_updated_at: datetime.date,
) -> str:
    tokens = [
        property_id,
        project_name,
        location_raw,
        city,
        sector,
        str(configuration_bhk),
        f"{price_lakh:.2f}" if price_lakh is not None else "",
        availability,
        str(floor) if floor is not None else "",
        str(area_sqft) if area_sqft is not None else "",
        listing_updated_at.isoformat(),
    ]
    canonical_str = "|".join(tokens)
    return hashlib.sha256(canonical_str.encode("utf-8")).hexdigest()


def parse_csv_content(content_bytes: bytes) -> ParseResult:
    """Parse raw CSV bytes into validated properties and errors."""
    file_sha256 = hashlib.sha256(content_bytes).hexdigest()
    text = content_bytes.decode("utf-8-sig")

    stream = io.StringIO(text, newline="")
    reader = csv.reader(stream)

    try:
        raw_headers = next(reader)
    except StopIteration:
        raise MissingHeadersError("Empty CSV file without headers") from None

    # Strip and lowercase headers for case-insensitive lookup
    normalized_headers = [h.strip().lower() for h in raw_headers]
    header_indices: dict[str, int] = {
        name: idx for idx, name in enumerate(normalized_headers) if name
    }

    missing_headers = REQUIRED_HEADERS - set(header_indices.keys())
    if missing_headers:
        raise MissingHeadersError(
            f"Missing required headers: {', '.join(sorted(missing_headers))}"
        )

    properties: list[ParsedProperty] = []
    errors: list[ParseError] = []
    seen_property_ids: set[str] = set()
    total_rows_read = 0

    # reader line count starts after header (line 2 in file)
    file_line = 1
    for raw_row in reader:
        file_line += 1
        # Check if fully blank
        if not any(cell.strip() for cell in raw_row):
            continue

        total_rows_read += 1

        # Build raw dict for error reporting / processing
        row_dict: dict[str, str] = {}
        for col_name, idx in header_indices.items():
            row_dict[col_name] = raw_row[idx].strip() if idx < len(raw_row) else ""

        prop_id = row_dict.get("property_id", "").strip()
        if not prop_id:
            errors.append(
                ParseError(
                    row_number=file_line,
                    property_id=None,
                    reason="Missing property_id",
                    raw_row=row_dict,
                )
            )
            continue

        # Check duplicate property_id inside file
        if prop_id in seen_property_ids:
            errors.append(
                ParseError(
                    row_number=file_line,
                    property_id=prop_id,
                    reason=f"Duplicate property_id inside file: {prop_id}",
                    raw_row=row_dict,
                )
            )
            continue

        project_name = row_dict.get("project_name", "").strip()
        if not project_name:
            errors.append(
                ParseError(
                    row_number=file_line,
                    property_id=prop_id,
                    reason="Missing project_name",
                    raw_row=row_dict,
                )
            )
            continue

        location_raw = row_dict.get("location", "").strip()
        parsed_loc = parse_location(location_raw)
        if not parsed_loc:
            errors.append(
                ParseError(
                    row_number=file_line,
                    property_id=prop_id,
                    reason=f"Invalid location format: '{location_raw}'",
                    raw_row=row_dict,
                )
            )
            continue
        sector, city = parsed_loc

        # configuration_bhk (int 1 to 10)
        bhk_raw = row_dict.get("configuration_bhk", "").strip()
        try:
            bhk = int(bhk_raw)
            if not 1 <= bhk <= 10:
                raise ValueError("BHK out of range")
        except ValueError:
            errors.append(
                ParseError(
                    row_number=file_line,
                    property_id=prop_id,
                    reason=(
                        "configuration_bhk must be an integer between 1 and 10, "
                        f"got '{bhk_raw}'"
                    ),
                    raw_row=row_dict,
                )
            )
            continue

        # availability (lowercased, must be available, reserved, or sold)
        avail_raw = row_dict.get("availability", "").strip().lower()
        if avail_raw not in VALID_AVAILABILITIES:
            errors.append(
                ParseError(
                    row_number=file_line,
                    property_id=prop_id,
                    reason=(
                        f"availability must be one of {sorted(VALID_AVAILABILITIES)}, "
                        f"got '{avail_raw}'"
                    ),
                    raw_row=row_dict,
                )
            )
            continue

        # listing_updated_at (valid date)
        date_raw = row_dict.get("listing_updated_at", "").strip()
        try:
            listing_updated_at = datetime.date.fromisoformat(date_raw)
        except ValueError:
            errors.append(
                ParseError(
                    row_number=file_line,
                    property_id=prop_id,
                    reason=(
                        "listing_updated_at must be a valid ISO date (YYYY-MM-DD), "
                        f"got '{date_raw}'"
                    ),
                    raw_row=row_dict,
                )
            )
            continue

        # Optional and nullable: price_lakh (> 0)
        price_lakh_raw = row_dict.get("price_lakh", "").strip()
        price_lakh: Decimal | None = None
        if price_lakh_raw:
            try:
                price_lakh = Decimal(price_lakh_raw)
                if price_lakh <= 0:
                    raise ValueError("price_lakh must be > 0")
            except (InvalidOperation, ValueError):
                errors.append(
                    ParseError(
                        row_number=file_line,
                        property_id=prop_id,
                        reason=f"price_lakh must be a number > 0, got '{price_lakh_raw}'",
                        raw_row=row_dict,
                    )
                )
                continue

        # Optional and nullable: floor (>= 0)
        floor_raw = row_dict.get("floor", "").strip()
        floor: int | None = None
        if floor_raw:
            try:
                floor = int(floor_raw)
                if floor < 0:
                    raise ValueError("floor must be >= 0")
            except ValueError:
                errors.append(
                    ParseError(
                        row_number=file_line,
                        property_id=prop_id,
                        reason=f"floor must be an integer >= 0, got '{floor_raw}'",
                        raw_row=row_dict,
                    )
                )
                continue

        # Optional and nullable: area_sqft (> 0)
        area_raw = row_dict.get("area_sqft", "").strip()
        area_sqft: int | None = None
        if area_raw:
            try:
                area_sqft = int(area_raw)
                if area_sqft <= 0:
                    raise ValueError("area_sqft must be > 0")
            except ValueError:
                errors.append(
                    ParseError(
                        row_number=file_line,
                        property_id=prop_id,
                        reason=f"area_sqft must be an integer > 0, got '{area_raw}'",
                        raw_row=row_dict,
                    )
                )
                continue

        # price_per_sqft (= price_lakh*100000/area_sqft rounded, null if either missing)
        price_per_sqft: Decimal | None = None
        if price_lakh is not None and area_sqft is not None:
            raw_pps = (price_lakh * Decimal(100000)) / Decimal(area_sqft)
            price_per_sqft = round(raw_pps, 2)

        row_hash = compute_row_hash(
            property_id=prop_id,
            project_name=project_name,
            location_raw=location_raw,
            city=city,
            sector=sector,
            configuration_bhk=bhk,
            price_lakh=price_lakh,
            availability=avail_raw,
            floor=floor,
            area_sqft=area_sqft,
            listing_updated_at=listing_updated_at,
        )

        seen_property_ids.add(prop_id)
        properties.append(
            ParsedProperty(
                property_id=prop_id,
                project_name=project_name,
                location_raw=location_raw,
                city=city,
                sector=sector,
                configuration_bhk=bhk,
                price_lakh=price_lakh,
                price_per_sqft=price_per_sqft,
                availability=avail_raw,
                floor=floor,
                area_sqft=area_sqft,
                listing_updated_at=listing_updated_at,
                row_hash=row_hash,
            )
        )

    return ParseResult(
        properties=properties,
        errors=errors,
        total_rows_read=total_rows_read,
        file_sha256=file_sha256,
    )


def parse_csv_file(file_path: str | Path) -> ParseResult:
    """Read a CSV file from disk and parse it."""
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    content_bytes = path.read_bytes()
    return parse_csv_content(content_bytes)
