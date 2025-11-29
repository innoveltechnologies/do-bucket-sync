import argparse
import os

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

load_dotenv()

session = boto3.session.Session()
client = session.client('s3',
                      region_name=os.getenv('REGION'),
                      endpoint_url=f"https://{os.getenv('REGION')}.digitaloceanspaces.com",
                      aws_access_key_id=os.getenv('SPACES_ACCESS_KEY'),
                      aws_secret_access_key=os.getenv('SPACES_SECRET_KEY'))


def get_acl(bucket, key) -> str:
    acl = client.get_object_acl(Bucket=bucket, Key=key)

    for g in acl.get("Grants", []):
        grantee = g.get("Grantee", {})
        if grantee.get("Type") == "Group" and grantee.get("URI", "").endswith("/AllUsers"):
            perm = g.get("Permission")
            if perm == "READ":
                return "public-read"
            elif perm in ("WRITE", "FULL_CONTROL"):
                return "public-read-write"

    return "private"

def copy_object(src_bucket, dest_bucket, src_object, acl, dest_object = None):
    src_last_modified = src_object["LastModified"]
    dest_last_modified = dest_object["LastModified"] if dest_object else None

    if dest_last_modified and src_last_modified <= dest_last_modified:
        print(f"Skip: {src_object['Key']} is up to date")
        return

    client.copy_object(
        Bucket=dest_bucket,
        Key=src_object["Key"],
        CopySource={"Bucket": src_bucket, "Key": src_object['Key']},
        ACL=acl,
    )
    print(f"{src_object['Key']} copied to {dest_bucket}")

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("-s", "--start", type=str, required=True, help="Bucket you want to sync from")
    parser.add_argument("-f", "--finish", type=str, required=True, help="Bucket you want to sync to")
    args = parser.parse_args()

    start_bucket = args.start
    finish_bucket = args.finish

    try:
        start_objects = client.list_objects(Bucket=start_bucket)["Contents"]

        for object in start_objects:
            acl = get_acl(start_bucket, object["Key"])
            try:
                finish_bucket_object = client.get_object(Bucket=finish_bucket, Key=object["Key"])
                copy_object(src_bucket=start_bucket, dest_bucket=finish_bucket, src_object=object, acl=acl, dest_object=finish_bucket_object)
            except ClientError as e:
                if e.response["Error"]["Code"] == "NoSuchKey":
                    copy_object(src_bucket=start_bucket, dest_bucket=finish_bucket, src_object=object, acl=acl)
                else:
                    raise

    except ClientError as e:
        print(e.response['Error']['Message'])

if __name__ == "__main__":
    main()