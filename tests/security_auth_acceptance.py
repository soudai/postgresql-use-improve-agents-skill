#!/usr/bin/env python3
"""Acceptance for #26-#28 in disposable PostgreSQL 18/19 containers.

The legacy-client check is an MD5-only protocol fixture, not an old libpq build.
Held/reopened psql sessions model pool credential refresh, not a specific pooler.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import select
import subprocess
import tempfile
import time

from security_scenarios import Cluster, expect, make_report, render, run, snapshot

OLD = "synthetic_auth_acceptance_old_75a2"
NEW = "synthetic_auth_acceptance_new_75a2"


def private_password(cluster, value):
    # Synthetic secret travels in stdin and a private test file, never the process argv.
    cluster.shell("umask 077\nprintf '*:*:*:accept_login:%s\\n' '" + value + "' > /tmp/auth-accept.pgpass")


def client_command(cluster, address="127.0.0.1", *options):
    return ["docker", "exec", "-i", "-e", "PGPASSFILE=/tmp/auth-accept.pgpass", cluster.name,
            "psql", "-XqAtw", "-v", "ON_ERROR_STOP=1", "-h", address, "-U", "accept_login", *options]


def fresh(cluster, address="127.0.0.1"):
    return run(client_command(cluster, address, "-c", "SELECT 1"), check=False)


def check_clean(text):
    assert OLD not in text and NEW not in text, "synthetic password escaped into an output"


def legacy(cluster):
    # Protocol v3, MD5 authentication only. AuthenticationSASL is deliberately unsupported.
    # https://www.postgresql.org/docs/18/protocol-message-formats.html
    script = r"""
use strict; use warnings; use Socket; use Digest::MD5 qw(md5_hex);
socket(my $s, PF_INET, SOCK_STREAM, getprotobyname('tcp')) or die "socket";
connect($s, sockaddr_in(5432, inet_aton('127.0.0.1'))) or die "connect";
sub exact {
    my ($n) = @_; my $buffer = '';
    while (length($buffer) < $n) {
        my $count = sysread($s, my $part, $n-length($buffer));
        die "unexpected end" unless $count; $buffer .= $part;
    }
    return $buffer;
}
sub send_all {
    my ($data) = @_; my $sent = 0;
    while ($sent < length($data)) {
        my $n = syswrite($s, $data, length($data)-$sent, $sent);
        die "send" unless $n; $sent += $n;
    }
}
my $params = pack('N',196608)."user\0accept_login\0database\0scen\0\0";
send_all(pack('N',length($params)+4).$params);
while (1) {
    my $kind=exact(1); my $len=unpack('N',exact(4)); my $body=exact($len-4);
    if ($kind eq 'R') {
        my $code=unpack('N',substr($body,0,4));
        if ($code==5) {
            my $secret='__PASSWORD__';
            my $hash='md5'.md5_hex(md5_hex($secret.'accept_login').substr($body,4,4))."\0";
            send_all('p'.pack('N',length($hash)+4).$hash);
        } elsif ($code==10) { print "unsupported SASL\n"; exit 42; }
        elsif ($code!=0) { die "unsupported authentication"; }
    } elsif ($kind eq 'E') { die "authentication rejected"; }
    elsif ($kind eq 'Z') { print "authenticated\n"; last; }
}
""".replace("__PASSWORD__", OLD)
    result = run(["docker", "exec", "-i", cluster.name, "perl", "-"], sql=script, check=False)
    check_clean(result.stdout + result.stderr)
    return result


def credentials(cluster):
    cluster.psql("CREATE ROLE accept_login LOGIN; SET password_encryption='md5'; "
                 "ALTER ROLE accept_login PASSWORD '" + OLD + "';")
    cluster.shell("printf 'local all all trust\\nhost scen accept_login 127.0.0.1/32 md5\\n"
                  "host scen accept_login ::1/128 md5\\nhost all all all reject\\n' > \"$PGDATA/pg_hba.conf\"")
    cluster.value("SELECT pg_reload_conf()")
    private_password(cluster, OLD)
    deadline = time.monotonic() + 10
    while legacy(cluster).stdout.strip() != "authenticated":
        assert time.monotonic() < deadline, "MD5 rule did not become active"
        time.sleep(.1)
    assert fresh(cluster, "::1").stdout.strip() == "1", "IPv6 MD5 connection failed"

    # Merely changing the setting does not change the stored MD5 verifier.
    cluster.psql("ALTER SYSTEM SET password_encryption='scram-sha-256'; SELECT pg_reload_conf();")
    s = snapshot(cluster, "postgres", True)
    check_clean(json.dumps(s))
    c = {"target": s["target"], "database": "scen", "password_roles": {"accept_login": {"method": "password"}}}
    expect(make_report(s, c, datetime.now(timezone.utc)), "PASSWORD", "finding", "accept_login")
    cluster.psql("SET password_encryption='scram-sha-256'; ALTER ROLE accept_login PASSWORD '" + OLD + "'")
    unsupported = legacy(cluster)
    assert unsupported.returncode == 42 and unsupported.stdout.strip() == "unsupported SASL"
    assert fresh(cluster).stdout.strip() == "1"

    # A pool-like retained backend survives password rotation; new connections must use the new secret.
    held = subprocess.Popen(client_command(cluster), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, bufsize=1)
    def query(sql):
        held.stdin.write(sql + "\n")
        held.stdin.flush()
        assert select.select([held.stdout], [], [], 10)[0], "held connection did not respond"
        return held.stdout.readline().strip()
    try:
        pid = query("SELECT pg_backend_pid();")
        assert pid.isdecimal()
        cluster.psql("ALTER ROLE accept_login PASSWORD '" + NEW + "'")
        assert query("SELECT pg_backend_pid();") == pid
        rejected = fresh(cluster)
        assert rejected.returncode and "password authentication failed" in rejected.stderr
        check_clean(rejected.stdout + rejected.stderr)
    finally:
        held.stdin.close()
        held.wait(timeout=10)
        check_clean(held.stdout.read() + held.stderr.read())
    private_password(cluster, NEW)
    assert fresh(cluster).stdout.strip() == "1"
    assert fresh(cluster, "::1").stdout.strip() == "1"
    expect(make_report(snapshot(cluster, "postgres", True), c, datetime.now(timezone.utc)),
           "PASSWORD", "pass", "accept_login")
    print("MD5-only protocol boundary, SCRAM reissue, held-session drain and IPv4/IPv6 reconnect passed", flush=True)


def tls(cluster):
    cluster.shell(r"""
set -eu
umask 077
cd "$PGDATA"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=acceptance-ca \
    -keyout ca.key -out ca.crt >/dev/null 2>&1
openssl req -newkey rsa:2048 -nodes -subj /CN=localhost \
    -keyout server.key -out server.csr >/dev/null 2>&1
printf 'subjectAltName=DNS:localhost,IP:127.0.0.1,IP:::1\n' > extensions.cnf
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 1 \
    -extfile extensions.cnf -out server.crt >/dev/null 2>&1
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=untrusted-ca \
    -keyout wrong-ca.key -out wrong-ca.crt >/dev/null 2>&1
printf '\nssl=on\n' >> postgresql.conf
printf 'local all all trust\nhostssl scen accept_login 127.0.0.1/32 scram-sha-256\nhostssl scen accept_login ::1/128 scram-sha-256\nhost all all all reject\n' > pg_hba.conf
""")
    cluster.value("SELECT pg_reload_conf()")
    deadline = time.monotonic() + 10
    while cluster.value("SHOW ssl") != "on":
        assert time.monotonic() < deadline
        time.sleep(.1)
    def connection(ca, host, address, mode="verify-full"):
        return cluster.shell("PGPASSFILE=/tmp/auth-accept.pgpass psql -XqAtw "
                             + '"host=' + host + " hostaddr=" + address + " dbname=scen user=accept_login "
                             + "sslmode=" + mode + " sslrootcert=$PGDATA/" + ca + '" -c \'SELECT 1\'', check=False)
    for address in ("127.0.0.1", "::1"):
        accepted = connection("ca.crt", "localhost", address)
        assert accepted.stdout.strip() == "1", accepted.stderr
        for ca, host, mode in (("wrong-ca.crt", "localhost", "verify-full"),
                               ("ca.crt", "wrong.invalid", "verify-full"), ("ca.crt", "localhost", "disable")):
            rejected = connection(ca, host, address, mode)
            assert rejected.returncode, (address, ca, host, mode)
            check_clean(rejected.stdout + rejected.stderr)
    assert cluster.value("SELECT 1") == "1", "local admin recovery path lost"
    print("Signed CA, wrong-CA/hostname refusal, TLS-only IPv4/IPv6 and admin access passed", flush=True)


def startup_and_backup(cluster):
    # A second isolated server permits a real stop/start without restarting Docker's PID 1.
    cluster.shell(r"""
set -eu
initdb --no-locale -A trust -D /tmp/auth-startup >/dev/null
pg_ctl -D /tmp/auth-startup -o '-p 5433 -k /tmp' -l /tmp/auth-startup.log -w start >/dev/null
psql -h /tmp -p 5433 -d postgres -XqAt -v ON_ERROR_STOP=1 -c \
    "CREATE TABLE auth_data(id int); INSERT INTO auth_data VALUES(1)"
pg_dump -h /tmp -p 5433 -d postgres > /tmp/auth-backup.sql
chmod 644 /tmp/auth-backup.sql
""")
    outsider = ["docker", "exec", "-u", "1234:1234", cluster.name, "test", "-r", "/tmp/auth-backup.sql"]
    assert run(outsider, check=False).returncode == 0
    s = snapshot(cluster, "postgres", True)
    check_clean(json.dumps(s))
    c = {"target": s["target"], "database": "scen"}
    info = {**c, "server_version": s["identity"]["version"], "observed_at": datetime.now(timezone.utc).isoformat()}
    uid = int(cluster.shell("id -u").stdout)
    file = {"id": "backup", "kind": "backup", "platform": "unix", "mode": "0644",
            "owner_uid": uid, "unauthorized_access": True}
    c["runtime"] = {**info, "process_uid": uid, "entrypoint_uid": 0, "files": [file]}
    expect(make_report(s, c, datetime.now(timezone.utc)), "FILE_ACCESS", "finding", "backup")
    cluster.shell(r"""
set -eu
chmod 600 /tmp/auth-backup.sql
chmod 700 /tmp/auth-startup
pg_ctl -D /tmp/auth-startup -m fast -w stop >/dev/null
pg_ctl -D /tmp/auth-startup -o '-p 5433 -k /tmp' -l /tmp/auth-startup.log -w start >/dev/null
psql -h /tmp -p 5433 -d postgres -XqAt -v ON_ERROR_STOP=1 -c "INSERT INTO auth_data VALUES(2)"
umask 077
pg_dump -h /tmp -p 5433 -d postgres > /tmp/auth-backup.sql
createdb -h /tmp -p 5433 auth_restored
psql -h /tmp -p 5433 -d auth_restored -XqAt -v ON_ERROR_STOP=1 -f /tmp/auth-backup.sql >/dev/null
test "$(psql -h /tmp -p 5433 -d auth_restored -XqAt -c 'SELECT sum(id) FROM auth_data')" = 3
""")
    assert run(outsider, check=False).returncode != 0
    file.update(mode="0600", unauthorized_access=False)
    report = make_report(s, c, datetime.now(timezone.utc))
    expect(report, "FILE_ACCESS", "pass", "backup")
    for format_name in ("json", "markdown", "issue"):
        check_clean(render(report, format_name))
    cluster.shell("pg_ctl -D /tmp/auth-startup -m fast -w stop >/dev/null")
    print("Restricted backup access preserves service startup, writes, dump and restore", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True, choices=("postgres:18-bookworm", "postgres:19rc1-bookworm"))
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="pgskills-auth-acceptance-") as directory:
        cluster = Cluster(args.image, Path(directory), docker_options=(
            "--sysctl", "net.ipv6.conf.all.disable_ipv6=0", "--sysctl", "net.ipv6.conf.default.disable_ipv6=0"))
        try:
            cluster.start()
            print(cluster.value("SELECT version()"), flush=True)
            credentials(cluster)
            tls(cluster)
            startup_and_backup(cluster)
            check_clean(cluster.shell('cat "$PGDATA"/log/*.json').stdout)
        finally:
            cluster.stop()


if __name__ == "__main__":
    main()
