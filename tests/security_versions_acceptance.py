#!/usr/bin/env python3
"""Issue #31: restored access boundaries and post-cutover writes on isolated databases."""
import argparse
from pathlib import Path
import re
import tempfile

from security_scenarios import SETUP, FIX, access_checks, reject_tcp
from scenarios import Cluster, run


def migration_writes(source_image, destination_image, directory):
    source, target = Cluster(source_image, directory), Cluster(destination_image, directory)
    try:
        source.start()
        source.psql(SETUP + FIX)
        reject_tcp(source, configure=True)
        old_version = int(source.value("SHOW server_version_num"))
        roles = run(["docker", "exec", source.name, "pg_dumpall", "--roles-only", "--no-role-passwords"]).stdout
        dump = run(["docker", "exec", source.name, "pg_dump", "scen"]).stdout
        target.start()
        new_version = int(target.value("SHOW server_version_num"))
        assert old_version // 10000 < new_version // 10000
        target.psql(roles.replace("CREATE ROLE postgres;\n", "", 1))
        target.psql(dump)
        access_checks(target)
        reject_tcp(target, configure=True)

        # Authorized application work after cutover is absent from the old database.
        target.psql("SET app.tenant='a'; INSERT INTO app.orders VALUES ('a',3);", extra=("-U", "web"))
        assert target.psql("SET app.tenant='a'; SELECT id FROM app.orders ORDER BY id;",
                           extra=("-U", "web")).strip() == "1\n3"
        assert target.psql("SET app.tenant='b'; SELECT id FROM app.orders ORDER BY id;",
                           extra=("-U", "web")).strip() == "2"
        assert source.value("SELECT count(*) FROM app.orders WHERE id=3") == "0"
        assert target.value("SELECT count(*) FROM app.orders WHERE id=3") == "1"
        forbidden = run(["docker", "exec", target.name, "psql", "-XqAtw", "-v", "ON_ERROR_STOP=1",
                         "-U", "web", "-c", "SET app.tenant='a'; INSERT INTO app.orders VALUES ('b',4);"],
                        check=False)
        assert forbidden.returncode != 0
        assert target.value("SELECT count(*) FROM app.orders WHERE id=4") == "0"
        print(f"Logical restore {old_version} -> {new_version}: tenant boundaries retained; "
              "post-cutover write exists only on new primary; direct rollback would lose it.", flush=True)
    finally:
        target.stop()
        source.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="postgres:17-bookworm")
    parser.add_argument("--image", default="postgres:18-bookworm")
    args = parser.parse_args()
    if not all(re.fullmatch(r"postgres:(?:17|18|19)[A-Za-z0-9_.-]*", image)
               for image in (args.source, args.image)):
        parser.error("use local official postgres:17/18/19 images")
    with tempfile.TemporaryDirectory(prefix="pgskills-migration-writes-") as temporary:
        migration_writes(args.source, args.image, Path(temporary))


if __name__ == "__main__":
    main()

