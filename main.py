"""One-way sync between two DigitalOcean Spaces buckets."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import ClientError
from dotenv import load_dotenv

log = logging.getLogger("do-spaces-sync")

# Files above this size need a multipart copy. boto3's managed `copy` handles
# that automatically when the object exceeds the threshold.
MULTIPART_THRESHOLD = 5 * 1024 * 1024 * 1024
TRANSFER_CONFIG = TransferConfig(multipart_threshold=MULTIPART_THRESHOLD)


@dataclass
class Summary:
    copied: int = 0
    skipped: int = 0
    deleted: int = 0
    failed: list[str] = field(default_factory=list)


def build_client(region: str, access_key: str, secret_key: str, workers: int):
    session = boto3.session.Session()
    return session.client(
        "s3",
        region_name=region,
        endpoint_url=f"https://{region}.digitaloceanspaces.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(max_pool_connections=max(workers, 10)),
    )


def list_objects(client, bucket: str, prefix: str) -> dict[str, dict]:
    """Return every object in the bucket as a dict keyed by object key."""
    objects: dict[str, dict] = {}
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            objects[obj["Key"]] = obj
    return objects


def get_acl(client, bucket: str, key: str) -> str:
    acl = client.get_object_acl(Bucket=bucket, Key=key)
    for grant in acl.get("Grants", []):
        grantee = grant.get("Grantee", {})
        if grantee.get("Type") == "Group" and grantee.get("URI", "").endswith("/AllUsers"):
            perm = grant.get("Permission")
            if perm == "READ":
                return "public-read"
            if perm in ("WRITE", "FULL_CONTROL"):
                return "public-read-write"
    return "private"


def needs_copy(src: dict, dest: dict | None) -> bool:
    if dest is None:
        return True
    # Same ETag and size means the content is identical, whatever the timestamps say.
    if src.get("ETag") == dest.get("ETag") and src.get("Size") == dest.get("Size"):
        return False
    return src["LastModified"] > dest["LastModified"]


def copy_object(client, src_bucket: str, dest_bucket: str, key: str, dry_run: bool) -> None:
    acl = get_acl(client, src_bucket, key)
    if dry_run:
        log.info("Would copy %s (%s)", key, acl)
        return
    client.copy(
        CopySource={"Bucket": src_bucket, "Key": key},
        Bucket=dest_bucket,
        Key=key,
        ExtraArgs={"ACL": acl},
        Config=TRANSFER_CONFIG,
    )
    log.info("Copied %s (%s)", key, acl)


def delete_object(client, bucket: str, key: str, dry_run: bool) -> None:
    if dry_run:
        log.info("Would delete %s", key)
        return
    client.delete_object(Bucket=bucket, Key=key)
    log.info("Deleted %s", key)


def run_parallel(tasks: dict[str, callable], workers: int, summary: Summary, counter: str) -> None:
    """Run `tasks` (key -> zero-arg callable) in a thread pool and tally results."""
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fn): key for key, fn in tasks.items()}
        for future in as_completed(futures):
            key = futures[future]
            try:
                future.result()
                setattr(summary, counter, getattr(summary, counter) + 1)
            except ClientError as e:
                log.error("Failed %s: %s", key, e.response["Error"]["Message"])
                summary.failed.append(key)


def sync(client, src_bucket: str, dest_bucket: str, prefix: str, delete: bool, dry_run: bool, workers: int) -> Summary:
    summary = Summary()

    log.info("Listing %s", src_bucket)
    src_objects = list_objects(client, src_bucket, prefix)
    log.info("Listing %s", dest_bucket)
    dest_objects = list_objects(client, dest_bucket, prefix)
    log.info("%d objects in source, %d in destination", len(src_objects), len(dest_objects))

    to_copy = {}
    for key, src in src_objects.items():
        if needs_copy(src, dest_objects.get(key)):
            to_copy[key] = lambda k=key: copy_object(client, src_bucket, dest_bucket, k, dry_run)
        else:
            summary.skipped += 1
            log.debug("Skip %s: up to date", key)

    run_parallel(to_copy, workers, summary, "copied")

    if delete:
        to_delete = {
            key: (lambda k=key: delete_object(client, dest_bucket, k, dry_run))
            for key in dest_objects
            if key not in src_objects
        }
        run_parallel(to_delete, workers, summary, "deleted")

    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-s", "--start", required=True, help="Bucket you want to sync from")
    parser.add_argument("-f", "--finish", required=True, help="Bucket you want to sync to")
    parser.add_argument("-p", "--prefix", default="", help="Only sync keys under this prefix")
    parser.add_argument("--delete", action="store_true", help="Remove destination objects that are missing from the source")
    parser.add_argument("--dry-run", action="store_true", help="Show what would happen without changing anything")
    parser.add_argument("-w", "--workers", type=int, default=10, help="Number of parallel copies (default: 10)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Log skipped objects too")
    return parser.parse_args(argv)


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        log.error("Missing required environment variable %s", name)
        sys.exit(2)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    load_dotenv()
    region = require_env("REGION")
    access_key = require_env("SPACES_ACCESS_KEY")
    secret_key = require_env("SPACES_SECRET_KEY")

    client = build_client(region, access_key, secret_key, args.workers)

    if args.dry_run:
        log.info("Dry run: nothing will be changed")

    try:
        summary = sync(
            client,
            src_bucket=args.start,
            dest_bucket=args.finish,
            prefix=args.prefix,
            delete=args.delete,
            dry_run=args.dry_run,
            workers=args.workers,
        )
    except ClientError as e:
        log.error(e.response["Error"]["Message"])
        return 1

    log.info(
        "Done: %d copied, %d skipped, %d deleted, %d failed",
        summary.copied, summary.skipped, summary.deleted, len(summary.failed),
    )
    return 1 if summary.failed else 0


if __name__ == "__main__":
    sys.exit(main())
