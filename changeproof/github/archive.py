import io
from pathlib import Path, PurePosixPath
import tarfile


def extract_snapshot(data, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    count, total, root = 0, 0, None
    with tarfile.open(fileobj=io.BytesIO(data), mode="r|gz") as archive:
        for member in archive:
            parts = PurePosixPath(member.name).parts
            if not parts or member.name.startswith("/") or ".." in parts or "\\" in member.name:
                raise ValueError("Unsafe archive path")
            root = root or parts[0]
            if parts[0] != root or not (member.isfile() or member.isdir()):
                raise ValueError("Archive links and special files are unsupported")
            count += 1
            total += member.size
            if count > 2000 or total > 100_000_000 or member.size > 5_000_000:
                raise ValueError("Repository snapshot exceeds extraction bounds")
            if len(parts) == 1:
                if not member.isdir():
                    raise ValueError("Archive must contain a single root directory")
                continue
            target = destination.joinpath(*parts[1:])
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, target.open("xb") as output:
                    output.write(source.read())
                target.chmod(0o644)
    if count == 0:
        raise ValueError("Empty repository archive")
    return destination
