"""Configured, auditable publisher identities; search scope is a separate constraint."""

import ipaddress
import re
from hashlib import sha256
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator


def normalize_domain(value):
    if not isinstance(value, str) or value != value.strip() or any(c in value for c in "/:@?#*"):
        raise ValueError("A source domain must be a bare hostname")
    host = value.rstrip(".").encode("idna").decode("ascii").lower()
    if "." not in host or not re.fullmatch(r"[a-z0-9]+(?:[a-z0-9.-]*[a-z0-9])?", host):
        raise ValueError("Invalid public source hostname")
    if any(not p or len(p) > 63 or p.startswith("-") or p.endswith("-") for p in host.split(".")):
        raise ValueError("Invalid hostname label")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return host
    raise ValueError("Source policy requires a hostname, not an IP address")


def query_domains(query):
    pattern = r"(?<![A-Za-z0-9_.-])(?:[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?\.)+[A-Za-z0-9-]+(?![A-Za-z0-9_.-])"
    domains = set()
    for candidate in re.findall(pattern, query):
        try:
            domains.add(normalize_domain(candidate))
        except ValueError:
            continue
    return domains


def domain_matches(host, domain, *, subdomains=True):
    return host == domain or subdomains and host.endswith("." + domain)


class PublisherIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    publisher: str = Field(min_length=1)
    host: str
    include_subdomains: bool = False
    path_prefix: str = "/"
    tier: str = Field(pattern="^(official|authorized|marketplace_self|third_party|media)$")
    verification: str = Field(min_length=1, description="Operator-confirmed identity evidence, not model assertion")

    @field_validator("host")
    @classmethod
    def valid_host(cls, value):
        return normalize_domain(value)

    @field_validator("path_prefix")
    @classmethod
    def valid_prefix(cls, value):
        if not value.startswith("/") or any(c in value for c in ("?", "#", "\\", "%")) or ".." in value.split("/"):
            raise ValueError("Identity path must be an absolute plain URL path prefix")
        return value.rstrip("/") or "/"


class SourcePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    identities: list[PublisherIdentity] = Field(default_factory=list)


def policy():
    from agent.runtime.context import get_run_context
    from agent.runtime.policies import load_runtime_policy

    context = get_run_context()
    return (context.policy if context and context.policy else load_runtime_policy()).sources


def page_tier(row):
    try:
        parsed = urlsplit(row.get("url", ""))
        port = parsed.port
    except ValueError:
        parsed = urlsplit("")
        port = None
    host = (parsed.hostname or "").lower().rstrip(".")
    safe_path = not any(p in (".", "..") for p in parsed.path.split("/")) and not any(
        ch in parsed.path for ch in ("%", chr(92))
    )
    body = str(row.get("raw_content") or row.get("content") or "")
    matches = [
        identity
        for identity in policy().identities
        if parsed.scheme in ("https", "http")
        and not parsed.username
        and not parsed.password
        and port in (None, 80, 443)
        and safe_path
        and domain_matches(host, identity.host, subdomains=identity.include_subdomains)
        and (
            identity.path_prefix == "/"
            or parsed.path == identity.path_prefix
            or parsed.path.startswith(identity.path_prefix + "/")
        )
    ]
    # Prefer the most specific confirmed mapping, never a model-supplied page label.
    matches.sort(key=lambda item: (len(item.host), len(item.path_prefix)), reverse=True)
    identity = matches[0] if matches else None
    ambiguous = identity is not None and any(
        (len(other.host), len(other.path_prefix)) == (len(identity.host), len(identity.path_prefix))
        and (other.publisher, other.tier) != (identity.publisher, identity.tier)
        for other in matches[1:]
    )
    if ambiguous:
        identity = None
    tier = identity.tier if identity else "unknown"
    return dict(
        publisher=identity.publisher if identity else host,
        channel_type=tier,
        identity_basis=identity.verification if identity else "",
        applicability=identity.path_prefix if identity else "",
        tier=tier,
        identity_status="confirmed" if identity else "unknown",
        content_version=sha256(body.encode()).hexdigest(),
    )


def requested_tiers(query):
    from agent.runtime.review_contract import _required_quote

    words = {
        "official": ("官方", "official"),
        "authorized": ("授权", "authorized"),
        "marketplace_self": ("自营",),
        "third_party": ("第三方", "third-party"),
        "media": ("媒体", "media"),
        "unknown": ("未知", "unknown"),
    }
    return tuple(
        tier
        for tier, terms in words.items()
        if any(
            _required_quote(term, query)
            and not re.search(
                r"(?:不要|禁止|不采用|不使用)\s*(?:引用|参考|使用|采用)?\s*" + re.escape(term), query, re.I
            )
            for term in terms
        )
    )


def task_domains(context, task_id):
    scope = context.task_scopes.get(task_id, {})
    explicit = scope.get("search_domains", [])
    if explicit:
        return explicit
    required = [r for r in context.requirements.values() if r["task_id"] == task_id]
    if any("official" in r["required_tier"] for r in required):
        name = context.objects.get(scope.get("object_id"), {}).get("user_name", "")
        return sorted(
            {
                i.host
                for i in context.policy.sources.identities
                if i.publisher.casefold() == name.casefold() and i.tier == "official"
            }
        )
    return []


def within_task_domains(context, task_id, url):
    domains = task_domains(context, task_id)
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if (
            parsed.scheme not in ("http", "https")
            or not host
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 80, 443)
        ):
            return False
    except ValueError:
        return False
    return not domains or any(domain_matches(host, domain) for domain in domains)


def source_assessment(requirement, row, context):
    identity = page_tier(row)
    if not within_task_domains(context, requirement["task_id"], row.get("url", "")):
        status = "outside_scope"
    elif not requirement["required_tier"] or identity["tier"] in requirement["required_tier"]:
        status = "eligible"
    elif identity["identity_status"] == "unknown":
        status = "unknown_identity"
    else:
        status = "disallowed_tier"
    return {**identity, "constraint_status": status}


def source_satisfies(requirement, fact, evidence):
    from agent.runtime.context import get_run_context
    from agent.runtime.quotes import verified_sources
    from agent.runtime.requirements import DIMENSIONS

    dimensions = fact.get("dimensions", {})
    if requirement["object_id"] is not None and fact.get("object_id") != requirement["object_id"]:
        return False
    if requirement["model"] is not None and requirement["object_id"] is None:
        return False
    if any(
        requirement.get(key) is not None and dimensions.get(key) != requirement[key]
        for key in DIMENSIONS
        if key != "model"
    ):
        return False
    context = get_run_context()
    required = requirement.get("required_tier", [])
    return any(
        (not required or page_tier(row)["tier"] in required)
        and (
            row.get("kind") == "sql"
            or context is None
            or within_task_domains(context, requirement["task_id"], row.get("url", ""))
        )
        for row in verified_sources(fact, evidence)
    )
