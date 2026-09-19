# do-spaces-sync

One-way sync between two DigitalOcean Spaces buckets in the same region. Objects are copied server-side, so nothing is downloaded to the machine running the tool. Each object keeps its public/private ACL.

## Configuration

Set these environment variables, or put them in a `.env` file (see `.env.example`):

| Variable            | Description                              |
|---------------------|------------------------------------------|
| `SPACES_ACCESS_KEY` | Spaces access key                        |
| `SPACES_SECRET_KEY` | Spaces secret key                        |
| `REGION`            | Region of both buckets, e.g. `ams3`      |

## Usage

```
python main.py -s SOURCE_BUCKET -f DEST_BUCKET [options]
```

| Option           | Description                                                   |
|------------------|---------------------------------------------------------------|
| `-p, --prefix`   | Only sync keys under this prefix                              |
| `--delete`       | Remove objects from the destination that are not in the source |
| `--dry-run`      | Print what would happen without changing anything             |
| `-w, --workers`  | Number of parallel copies (default 10)                        |
| `-v, --verbose`  | Also log objects that were skipped                            |

An object is skipped when the destination already has the same ETag and size, or when the destination copy is newer. The exit code is non-zero if any object failed.

## Local setup

```
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in the values
python main.py -s source-bucket -f dest-bucket --dry-run
```

## Docker

Every push to `master` builds an image and pushes it to GitHub Container Registry as `ghcr.io/innoveltechnologies/do-bucket-sync:latest`, plus a tag for the commit SHA.

```
docker run --rm --env-file .env ghcr.io/innoveltechnologies/do-bucket-sync:latest \
  -s source-bucket -f dest-bucket
```

## Running on a schedule with Docker Compose

The image runs the sync once and exits, so to run it every 15 minutes wrap it in a loop. This `docker-compose.yml` keeps a single container alive that syncs, sleeps for 15 minutes, and repeats. Put your `.env` file next to it.

```yaml
services:
  spaces-sync:
    image: ghcr.io/innoveltechnologies/do-bucket-sync:latest
    env_file: .env
    restart: unless-stopped
    entrypoint: ["sh", "-c"]
    command:
      - |
        while true; do
          python main.py -s source-bucket -f dest-bucket
          sleep 900
        done
```

Replace `source-bucket` and `dest-bucket` with your bucket names, and add any options you need such as `--delete` or `-p some/prefix`. Then:

```
docker compose up -d          # start
docker compose logs -f        # watch the sync output
docker compose pull && docker compose up -d   # pick up a new image
```

The 15 minutes is measured from the end of one run to the start of the next, so a long sync will not overlap with the following one. With `restart: unless-stopped` the loop survives reboots and crashes.

### Alternative: host cron

If you would rather not keep a container running, define the service without the loop and let cron start it:

```yaml
services:
  spaces-sync:
    image: ghcr.io/innoveltechnologies/do-bucket-sync:latest
    env_file: .env
    command: ["-s", "source-bucket", "-f", "dest-bucket"]
```

```
*/15 * * * * cd /path/to/compose/dir && docker compose run --rm spaces-sync >> /var/log/spaces-sync.log 2>&1
```
