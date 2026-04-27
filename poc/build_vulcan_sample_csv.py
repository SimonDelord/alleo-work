"""Write sample_vulcan_fleet_telemetry.csv (same rows as the OpenShift s3 uploader, static timestamps)."""
from __future__ import annotations

import os

from fleet_telemetry_data import build_fleet_csv_bytes


def main() -> None:
    out = os.path.join(os.path.dirname(__file__), "sample_vulcan_fleet_telemetry.csv")
    body = build_fleet_csv_bytes(bump_timestamps=False, upload_cycle=0)
    with open(out, "wb") as f:
        f.write(body)
    lines = body.count(b"\n")
    print("wrote", out, "bytes", len(body), "newlines~", lines)


if __name__ == "__main__":
    main()
