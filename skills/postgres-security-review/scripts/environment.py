"""Authentication, deployment and release evidence checks."""
import ipaddress
import re
from datetime import date, datetime, timezone
from urllib.parse import urlsplit


def observed_date(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).date()


def in_scope(review, evidence):
    return (evidence.get("target") == review.scope["target"]
            and evidence.get("database") == review.scope["database"]
            and evidence.get("server_version") == review.snapshot["identity"]["version"]
            and bool(evidence.get("observed_at"))
            and 0 <= (review.today - observed_date(evidence["observed_at"])).days <= 7)


def match_hba(row, route):
    conn = route["transport"]
    kind = row["type"]
    if kind == "local":
        if conn != "local":
            return False
    elif conn == "local":
        return False
    elif kind == "hostssl" and conn != "tls" or kind == "hostnossl" and conn == "tls":
        return False
    elif kind == "hostgssenc" and conn != "gss" or kind == "hostnogssenc" and conn == "gss":
        return False
    elif kind not in {"host", "hostssl", "hostnossl", "hostgssenc", "hostnogssenc"}:
        return None
    if conn != "local" and row["address"] != "all":
        try:
            client = ipaddress.ip_address(route["address"])
            address = ipaddress.ip_address(row["address"])
            mask = ipaddress.ip_address(row["netmask"])
            if client.version != address.version or (int(client) & int(mask)) != (int(address) & int(mask)):
                return False
        except (ValueError, TypeError, KeyError):
            return None  # DNS, samehost/samenet and absent network context
    db = "replication" if route.get("physical_replication") else route["database"]
    for value, patterns, is_db in ((db, row["databases"], True), (route["role"], row["users"], False)):
        if value in patterns:
            continue
        if "all" in patterns and not (is_db and route.get("physical_replication")):
            continue
        if any(p.startswith(("/", "+", "@")) or p in {"sameuser", "samerole", "samegroup"} for p in patterns):
            return None
        return False
    return True


def file_rule(rows, route):
    for row in rows:
        if row["invalid"]:
            continue
        matches = match_hba(row, route)
        if matches is None:
            return None, "An earlier file rule needs DNS, mapping, regex or membership evidence."
        if matches:
            return row, "First candidate in file order. Confirm active rules and quoted keywords."
    return None, "No matching rule found in the file metadata."


def auth(review, access):
    hba = review.rows("hba")
    routes = review.contract.get("routes", [])
    if not routes or not review.ready("hba"):
        review.missing("HBA", "connection routes", "Ordered HBA metadata and representative connection routes")
    if any(r["invalid"] for r in hba):
        auth_finding(review, "HBA", "file syntax", "finding",
                     "A current HBA file entry does not parse. Loaded rules can differ.", "Valid ordered authentication rules.")
    for route in routes:
        label = route["id"]
        if review.ready("hba") and not any(r["invalid"] for r in hba):
            match, note = file_rule(hba, route)
            evidence = {"note": note, "method": match["method"], "line": match["line"]} if match else note
            risk = bool(match and (match["method"] == "trust" and not route.get("allow_trust")
                                   or match["method"] == "password" and route["transport"] == "plain"
                                   or not route.get("allowed", True) and match["method"] != "reject"))
            auth_finding(review, "HBA", label, "finding" if risk else "unknown", evidence,
                         "Active authentication matches the route contract.", confidence="candidate")
        observed = route.get("verified") if in_scope(review, route.get("verification", {})) else None
        if observed and type(observed.get("connection_accepted")) is bool:
            accepted = observed["connection_accepted"]
            auth_finding(review, "ACCESS_ROUTE", label, "pass" if accepted == route.get("allowed", True) else "finding",
                         {"connection_accepted": accepted}, "Observed connection agrees with its route contract.")
        if route.get("require_tls") or route.get("require_identity_verification"):
            if observed is None:
                review.missing("TLS", label, "Fresh tests of TLS, plaintext refusal and client CA/hostname checks",
                               "Server TLS setting and a single observed connection cannot prove route enforcement.")
            else:
                expected = {}
                if route.get("require_tls"):
                    expected.update(tls=True, plaintext_accepted=False)
                if route.get("require_identity_verification"):
                    expected.update(valid_identity_accepted=True, invalid_identity_rejected=True)
                evidence = {k: observed[k] for k in expected if type(observed.get(k)) is bool}
                if any(value != expected[k] for k, value in evidence.items()):
                    status = "finding"
                elif evidence == expected:
                    status = "pass"
                else:
                    status = "unknown"
                auth_finding(review, "TLS", label, status, evidence,
                             "Route meets its transport and server identity contract.",
                             confidence="unverified" if status == "unknown" else "observed")
    password_contract = review.contract.get("password_roles", {})
    if not password_contract:
        review.missing("PASSWORD", "password roles", "Roles, authentication method and credential lifecycle evidence")
    settings = {s["name"]: s["setting"] for s in review.rows("settings")}
    if settings.get("password_encryption") == "md5":
        auth_finding(review, "PASSWORD", "new verifier setting", "finding",
                     "Current session creates MD5 verifiers. Existing verifiers may differ.",
                     "Create modern verifiers for password-based roles.")
    formats = {r["oid"]: r["format"] for r in review.rows("passwords")}
    for name, spec in password_contract.items():
        role = access.roles.get(access.names.get(name))
        if role is None:
            review.missing("PASSWORD", name, "Declared login role is visible")
            continue
        if not role["login"] or spec.get("method") == "external":
            auth_finding(review, "PASSWORD", name, "not_applicable",
                         "NOLOGIN or external authentication according to the scoped contract.",
                         "Check the external provider's lifecycle where applicable.")
            continue
        fmt = formats.get(role["oid"], "unknown")
        expired = expiration(role.get("valid_until"), review.as_of)
        if fmt in {"md5", "none"} or expired:
            auth_finding(review, "PASSWORD", name, "finding",
                         {"format": fmt, "expired": expired},
                         "Password logins use a modern verifier and valid credentials.")
        elif fmt == "scram" and expired is None:
            auth_finding(review, "PASSWORD", name, "unknown",
                         {"format": fmt, "expired": None},
                         "SCRAM format is observed; credential validity needs a timezone-aware evaluation instant.",
                         confidence="unverified")
        elif fmt == "scram":
            auth_finding(review, "PASSWORD", name, "pass", {"format": fmt, "expired": expired},
                         "Stored verifier uses SCRAM. Strength and lifecycle are assessed separately.")
        else:
            review.missing("PASSWORD", name, "Verifier format classification from a privileged read")
        checks = ("generated", "unique", "default_removed", "revocation_available")
        life = spec.get("lifecycle", {})
        if not in_scope(review, spec.get("verification", {})):
            life = {}
        evidence = {k: life[k] for k in checks if isinstance(life.get(k), bool)}
        if False in evidence.values():
            auth_finding(review, "CREDENTIAL_LIFECYCLE", name, "finding", evidence,
                         "Use unique, generated secrets with an established revocation path.")
        elif len(evidence) == len(checks):
            auth_finding(review, "CREDENTIAL_LIFECYCLE", name, "pass", evidence,
                         "Credential generation and revocation controls evidenced by the operator.")
        else:
            review.missing("CREDENTIAL_LIFECYCLE", name, "Credential generation, sharing, default and revocation controls",
                           "Verifier format cannot establish password strength.")


def expiration(value, as_of):
    if value in (None, "infinity"):
        return False
    if value == "-infinity":
        return True
    try:
        until = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if until.tzinfo is None or until.utcoffset() is None:
        return None
    until = until.astimezone(timezone.utc)
    if isinstance(as_of, datetime):
        return until < as_of
    # A date denotes a whole UTC day, not midnight. Same-day validity is unknown.
    if until.date() == as_of:
        return None
    return until.date() < as_of


def auth_finding(review, rule, subject, status, evidence, expected, confidence="observed"):
    review.add(rule, subject, status, "high" if status == "finding" else "info" if status == "pass" else "unrated",
               evidence, expected,
               "Review client and pooler compatibility. Stage scoped HBA/TLS changes or issue fresh credentials.",
               "Retain an admin path. Test accepted and rejected new connections over each required route.",
               "Restore reviewed routing/configuration or reissue credentials. Do not revive a compromised secret.",
               confidence=confidence)


def runtime(review):
    item = review.contract.get("runtime", {})
    if not in_scope(review, item):
        review.missing("RUNTIME", "deployment metadata", "Fresh scoped OS/container or provider evidence")
        return
    if item.get("deployment") == "managed":
        review.missing("RUNTIME", "provider-managed host", "Provider responsibility and available controls",
                       "Host UID, capabilities and file permissions require provider evidence.")
        return
    process_uid = item.get("process_uid")
    if type(process_uid) is not int or process_uid < 0:
        review.missing("RUNTIME", "postgres process", "Observed effective UID of the postgres process")
    else:
        runtime_finding(review, "RUNTIME", "postgres process", process_uid == 0,
                        {k: item[k] for k in ("process_uid", "entrypoint_uid", "rootless", "user_namespace")
                         if type(item.get(k)) in (int, bool)},
                        "PostgreSQL server process uses its dedicated non-root identity.")
    for control in ("privileged", "docker_socket", "unneeded_capabilities", "unneeded_host_mounts"):
        if type(item.get(control)) is not bool:
            review.missing("RUNTIME", control, "Observed deployment control and required operational scope")
        else:
            runtime_finding(review, "RUNTIME", control, item[control], item[control],
                            "Host access is limited to the service's required operations.")
    for file in item.get("files", []):
        kind = file["kind"]
        label = file["id"]
        if file.get("unauthorized_access") is True:
            runtime_finding(review, "FILE_ACCESS", label, True, {"kind": kind},
                            "Only approved principals can access the asset.")
            continue
        if kind == "tls_key":
            tls_key(review, file, process_uid)
            continue
        if file.get("platform") == "unix" and re.fullmatch(r"[0-7]{3,4}", file.get("mode", "")):
            mode = int(file["mode"], 8)
            if kind == "pgpass":
                correct = mode & 0o077 == 0 and file.get("owner_uid") == file.get("service_uid")
                if file.get("owner_uid") is None or file.get("service_uid") is None:
                    review.missing("FILE_ACCESS", label, "File owner and client OS identity")
                    continue
            elif mode & 0o007 or mode & 0o020:
                correct = False
            else:
                correct = file.get("unauthorized_access")
                if type(correct) is bool:
                    correct = not correct
            if type(correct) is bool:
                runtime_finding(review, "FILE_ACCESS", label, not correct, {"kind": kind, "mode": file["mode"]},
                                "Only approved service and backup principals can access the asset.")
                continue
        if type(file.get("unauthorized_access")) is bool:
            runtime_finding(review, "FILE_ACCESS", label, file["unauthorized_access"], {"kind": kind},
                            "Only approved principals have access according to the effective ACL or storage policy.")
        else:
            review.missing("FILE_ACCESS", label, "Effective Unix mode, Windows ACL or provider storage policy")
    for item in item.get("secret_locations", []):
        if item["kind"] in {"argv", "environment", "dsn", "ci_log", "config", "fdw", "subscription"}:
            runtime_finding(review, "SECRET_LOCATION", item["kind"], True, "Operator reported an exposed secret location.",
                            "Credentials are handled by an approved secret distribution path.")


def tls_key(review, file, process_uid):
    """Mode/owner suitability does not prove effective readability or reader scope."""
    evidence = {k: file[k] for k in ("kind", "platform", "mode", "owner_uid", "owner_gid",
                                    "service_readable", "unauthorized_access") if k in file}
    expected = "PostgreSQL can read its key and only approved principals can access it."
    if file.get("service_readable") is False:
        runtime_finding(review, "FILE_ACCESS", file["id"], True, evidence, expected)
        return
    if file.get("platform") not in {"unix", "windows"}:
        review.missing("FILE_ACCESS", file["id"], "Supported key platform: unix or windows")
        return
    if file.get("platform") == "unix":
        if (not re.fullmatch(r"[0-7]{3,4}", file.get("mode", ""))
                or type(file.get("owner_uid")) is not int or type(process_uid) is not int):
            review.missing("FILE_ACCESS", file["id"], "Key mode, owner and PostgreSQL service identity")
            return
        mode = int(file["mode"], 8)
        suitable = (file["owner_uid"] == 0 and mode in {0o600, 0o640}
                    or file["owner_uid"] == process_uid and mode == 0o600)
        if not suitable:
            runtime_finding(review, "FILE_ACCESS", file["id"], True, evidence, expected)
            return
    if file.get("service_readable") is True and file.get("unauthorized_access") is False:
        runtime_finding(review, "FILE_ACCESS", file["id"], False, evidence, expected)
    else:
        review.add("FILE_ACCESS", file["id"], "unknown", "unrated", evidence, expected,
                   "Check effective service readability and approved readers, including groups, ACLs and parent directories.",
                   "Use scoped access checks without reading key contents; test TLS startup in isolation.",
                   "Record owners, groups and ACLs before changing access.", confidence="unverified")


def runtime_finding(review, rule, subject, risk, evidence, expected):
    review.add(rule, subject, "finding" if risk else "pass", "high" if risk else "info", evidence, expected,
               "Narrow the service identity, capabilities, mount or effective ACL. Migrate exposed credentials to approved storage.",
               "In isolation verify startup, required writes, client connections, backups and access denial.",
               "Capture specific owners, ACLs and deployment settings before changing them. Reissue any exposed credentials.")


def safe_url(value):
    parts = urlsplit(value)
    return parts.scheme == "https" and bool(parts.hostname) and not (
        parts.username or parts.password or parts.query or parts.fragment)


def versions(review):
    identity = review.snapshot["identity"]
    version_num = identity["version_num"]
    major = str(version_num // 10000) if version_num >= 100000 else f"{version_num // 10000}.{version_num // 100 % 100}"
    version = identity["version"]
    if re.match(r"^\d+(?:\.\d+)?(?:beta|rc|devel)", version, re.I):
        release_finding(review, "RELEASE", "server", "finding", {"major": major, "stage": "preview"},
                        "Production targets use a supported final release.")
        return
    source = review.advisories
    if not source or source.get("source") != "postgresql.org" or not 0 <= (
        review.today - observed_date(source["retrieved_at"])).days <= 7:
        review.missing("RELEASE", "server", "Fresh official release and security evidence")
        return
    release = source.get("releases", {}).get(major)
    if release is None:
        review.missing("RELEASE", "server", "Recognized upstream release series")
        return
    deployment = review.contract.get("distribution", {})
    bound = in_scope(review, deployment)
    upstream = bound and deployment.get("type") == "upstream"
    end = date.fromisoformat(release["end"])
    current = version_num % (10000 if version_num >= 100000 else 100)
    release_finding(review, "LIFECYCLE", "community", "finding" if (end - review.today).days <= 90 else "pass",
                    {"major": major, "community_end": end.isoformat(), "days_remaining": (end - review.today).days},
                    "Use a supported series and plan migration before community end of life.")
    if bound and deployment.get("type") == "managed":
        support = deployment.get("extended_support", {})
        if support.get("active") is True and safe_url(support.get("source", "")):
            release_finding(review, "LIFECYCLE", "provider", "pass",
                            {"source": support["source"], "active": True},
                            "Provider coverage is distinct from community support.")
        else:
            review.missing("LIFECYCLE", "provider", "Evidence of the provider's applied support plan")
    release_finding(review, "RELEASE", "server", "finding" if current < release["minor"] else "pass",
                    {"major": major, "running_minor": current, "upstream_minor": release["minor"]},
                    "Review current minor release and vendor patch status.")
    build = deployment.get("build", "")
    build_known = bound and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+@:\\-]{0,199}", build))
    vendor = deployment.get("cves", {}) if build_known else {}
    for advisory in source.get("advisories", []):
        cve = advisory["cve"]
        if not re.fullmatch(r"CVE-\d{4}-\d{4,}", cve) or advisory["component"] != "server":
            continue
        fixed = advisory["fixed"].get(major)
        if fixed is None:
            continue
        claim = vendor.get(cve, {})
        proven = claim.get("state") in {"fixed", "affected", "not_affected"} and safe_url(claim.get("source", ""))
        if proven:
            status = "finding" if claim["state"] == "affected" else "pass"
            evidence = {"vendor_state": claim["state"], "vendor_source": claim["source"], "build": build}
        elif current < fixed:
            status, evidence = "unknown", {"upstream_fixed_minor": fixed, "running_minor": current}
        else:
            continue
        evidence["advisory"] = "https://www.postgresql.org/support/security/" + cve + "/"
        release_finding(review, "CVE", cve, status, evidence,
                        "Check component, exposure conditions and vendor package/build before concluding impact.")
    if not upstream and not vendor:
        review.missing("CVE", "server build", "Vendor release and advisory evidence for the running build")


def components(review):
    # Database extensions and client binaries have separate provenance and release feeds.
    components = {x["name"]: x["version"] for x in review.rows("extensions")}
    for component in review.contract.get("components", []):
        name = component["name"]
        if component["type"] == "extension" and components.get(name) != component["version"]:
            review.missing("COMPONENT", name, "Version of the installed extension matches the reviewed artifact")
            continue
        if not in_scope(review, component) or not safe_url(component.get("source", "")):
            review.missing("COMPONENT", name, "Fresh maintainer or vendor review of the running component")
            continue
        state = component.get("state")
        if state not in {"fixed", "current", "affected", "unsupported"}:
            review.missing("COMPONENT", name, "Maintainer's patch/support determination")
            continue
        release_finding(review, "COMPONENT", name, "finding" if state in {"affected", "unsupported"} else "pass",
                        {"state": state, "version": component["version"], "source": component["source"]},
                        "Patch or replace affected and unsupported components.")
    reviewed = {c["name"] for c in review.contract.get("components", []) if c["type"] == "extension"}
    for name in sorted(components.keys() - reviewed):
        review.missing("COMPONENT", name, "Maintainer's release/security information for this installed extension")
    if not any(c["type"] != "extension" for c in review.contract.get("components", [])):
        review.missing("COMPONENT", "clients and poolers", "Runtime client, driver, tooling, TLS library and pooler versions")


def release_finding(review, rule, subject, status, evidence, expected):
    review.add(rule, subject, status, "high" if status == "finding" else "info" if status == "pass" else "unrated",
               evidence, expected,
               "Verify upstream and vendor advisories. Plan a supported update with extension, driver and HA compatibility checks.",
               "Restore a backup in isolation. Verify connections, data, required access and rejection cases after upgrade.",
               "Record RPO/RTO, the point of no return and how writes on the new primary will be preserved.",
               confidence="candidate" if status == "unknown" else "observed")
